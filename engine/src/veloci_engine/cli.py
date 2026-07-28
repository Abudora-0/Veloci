"""Sidecar entrypoint: newline-delimited JSON over stdin/stdout.

This is what a GUI shell (or a human, for testing) drives.

Architecture note (the reason this isn't just "handle a command, maybe
create_task() some background work"): on this machine, a Windows
ProactorEventLoop, asyncio.create_subprocess_exec() can hang forever --
the subprocess actually launches (confirmed via Task Manager) but asyncio's
own "child exited" notification never fires, so `await proc.wait()` /
`communicate()` never returns. Two distinct triggers were found by
elimination, and both are avoided below:

1. Wrapping the subprocess-creating coroutine in a *freshly created* task
   (asyncio.create_task(), or an inner asyncio.gather() that implicitly
   wraps its arguments in new tasks) *after* the loop is already running.
   Coroutines that were already one of the arguments to the single
   *top-level* asyncio.gather() in main() don't trigger this.
2. A hand-rolled background thread blocked in a synchronous, blocking read
   of the process's own inherited stdin pipe (e.g. `for line in sys.stdin`),
   handing lines back via `loop.call_soon_threadsafe()`, running
   concurrently with subprocess creation -- reproduced with a minimal repro
   (thread genuinely blocked reading a real, unwritten pipe handle, exactly
   Tauri's Stdio::piped() setup) even with no task nesting at all; a thread
   that isn't touching real pipe I/O (e.g. plain time.sleep) does NOT
   trigger it. `loop.connect_read_pipe()` isn't a fix either -- it needs an
   overlapped-capable handle, and subprocess.PIPE/Stdio::piped() are plain
   anonymous pipes, so registering one fails outright ("the handle is
   invalid"). What does work: reading stdin via loop.run_in_executor() one
   readline() at a time, the same mechanism handle_add_listing already uses
   for its (also blocking) crawl -- see dispatch_loop below.

That's why downloads and metadata probing are both structured as *persistent
worker loops*, started once alongside the dispatch loop via a single
top-level asyncio.gather() in main(), and fed via asyncio.Queue -- rather
than spawning a fresh task per request. This also happens to fix a real
UX bug: the previous per-request-blocking design meant "pause"/"cancel"
commands couldn't be processed while a bulk download was running, since the
main loop was itself blocked awaiting that download's completion.

Commands in (stdin, one JSON object per line):
  {"cmd": "add_listing", "url": "...", "max_items": 50}
  {"cmd": "start_downloads", "dest_dir": "...", "concurrency": 4, "quality": "best", "rate_limit": "2M"}
  {"cmd": "download_single", "url": "...", "dest_dir": "...", "concurrent_fragments": 4,
      "quality": "best", "rate_limit": "2M"}
  {"cmd": "cancel", "url": "..."}
  {"cmd": "pause_video", "url": "..."}
  {"cmd": "remove_video", "url": "..."}
  {"cmd": "list_videos"}
  {"cmd": "status"}
  {"cmd": "pause"}

`quality` is one of "best" (default), "1080p", "720p", "480p", "360p",
"worst" -- see downloader.py's _QUALITY_FORMAT_SELECTORS. Most of these
sites serve one direct file per video with no real quality ladder, so this
is mainly useful for sites (like rule34video.com) that actually offer
multiple resolutions; elsewhere it's a no-op fallback to whatever's there.

Events out (stdout, one JSON object per line):
  {"event": "crawled_urls", "listing_url": "...", "found": N, "added": N, "urls": [...],
      "videos": [{"url": ..., "status": ..., "title": ..., "duration": ..., "filesize": ...,
      "thumbnail": ..., "dest_path": ...}]}  # status reflects prior sessions (dupe detection)
  {"event": "metadata", "url": "...", "title": ..., "duration": ..., "filesize": ..., "thumbnail": ...}
  {"event": "queue_finished", "done": N, "failed": N}  # last active download in a batch ended
  {"event": "video_list", "videos": [{"url": ..., "status": ..., "dest_path": ..., "error": ...,
      "title": ..., "duration": ..., "filesize": ...}]}
  {"event": "progress", "url": "...", "percent": "...", "speed": "...", "eta": "...",
      "downloaded": "...", "total": "..."}
  {"event": "download_done", "url": "...", "dest_path": "..."}
  {"event": "download_failed", "url": "...", "error": "..."}
  {"event": "cancelled", "url": "..."}
  {"event": "video_paused", "url": "..."}
  {"event": "removed", "url": "..."}
  {"event": "status", "counts": {...}}
  {"event": "paused"}
  {"event": "error", "message": "..."}

"cancel" kills the download and marks it cancelled; "pause_video" kills the
*same way* but marks it paused instead -- yt-dlp resumes from the partial
file either way (its own --continue default), the distinction is purely the
status bucket the video ends up in, matching an IDM-style pause/resume
rather than a hard stop. Re-issuing "download_single" on a paused video
resumes it. The blanket "pause" command (stop everything) now also marks
every download it kills as paused rather than cancelled, for the same
reason.

`filesize` is bytes (int) when known; `downloaded`/`total` in the `progress`
event are yt-dlp's human-readable strings (e.g. "10.52MiB"). Index numbers
aren't part of the protocol -- the frontend derives "video N of M" from
position in the `urls`/`videos` arrays, which are always in fetch order.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import msvcrt
import os
import sys
from ctypes import wintypes
from pathlib import Path

from veloci_engine.core import downloader
from veloci_engine.core.crawler import UnsupportedSiteError, crawl_listing
from veloci_engine.core.probe import ProbeResult, probe_many
from veloci_engine.core.queue_db import QueueDB
from veloci_engine.extractors.base import ExtractorError

DEFAULT_DB_PATH = Path.home() / ".veloci" / "queue.sqlite3"

# Fixed pool size; actual concurrency is gated dynamically via
# Engine._concurrency_limit rather than by changing how many workers exist
# (workers must all be started once at program start -- see module docstring).
MAX_DOWNLOAD_WORKERS = 16

# How long an idle worker waits before rechecking the queue/pause state/gate.
_POLL_INTERVAL_SECONDS = 0.2


class DownloadJob:
    __slots__ = ("url", "dest_dir", "concurrent_fragments", "quality", "rate_limit")

    def __init__(
        self,
        url: str,
        dest_dir: Path,
        concurrent_fragments: int,
        quality: str = "best",
        rate_limit: str | None = None,
    ) -> None:
        self.url = url
        self.dest_dir = dest_dir
        self.concurrent_fragments = concurrent_fragments
        self.quality = quality
        self.rate_limit = rate_limit


class Engine:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db = QueueDB(db_path)
        self._paused = False
        self._concurrency_limit = 4
        self._active_download_count = 0
        self._download_jobs: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._probe_jobs: asyncio.Queue[list[str]] = asyncio.Queue()
        # Tracks in-flight yt-dlp subprocesses by url so a specific one can be
        # killed on request (per-item cancel, or all-at-once on pause).
        self._active: dict[str, asyncio.subprocess.Process] = {}
        # Urls whose in-flight download we just killed ourselves -- lets the
        # download coroutine's failure path tell "cancelled" apart from a
        # genuine yt-dlp error. Checked before _cancel_requested so a paused
        # download is reported as paused, not cancelled, even though both
        # paths terminate() the same process.
        self._cancel_requested: set[str] = set()
        self._pause_requested: set[str] = set()
        # Per-batch completion tally so the UI can announce "queue finished:
        # X done, Y failed" exactly once when the last active download ends.
        self._batch_done = 0
        self._batch_failed = 0
        self._batch_ran = False

    def emit(self, event: dict) -> None:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    # ---- commands -----------------------------------------------------

    async def handle_add_listing(self, url: str, max_items: int | None) -> None:
        loop = asyncio.get_running_loop()

        def _crawl() -> list[str]:
            return list(crawl_listing(url, max_items=max_items))

        try:
            urls = await loop.run_in_executor(None, _crawl)
        except (UnsupportedSiteError, ExtractorError) as exc:
            # Network/site failures shouldn't take the whole sidecar down --
            # report and let the caller retry or move on.
            self.emit({"event": "error", "message": str(exc)})
            return

        added = self.db.enqueue(urls)

        # Enrich each url with what the queue DB already knows about it: a
        # video downloaded in an earlier session shows up as done (with its
        # metadata) instead of a fresh queued row -- these sites repeat the
        # same videos across listing pages, so this is what stops silent
        # duplicate downloads from cluttering the queue.
        records = self.db.get_records(urls)
        videos = []
        for u in urls:
            r = records.get(u)
            videos.append(
                {
                    "url": u,
                    "status": r.status if r else "queued",
                    "title": r.title if r else None,
                    "duration": r.duration if r else None,
                    "filesize": r.filesize if r else None,
                    "thumbnail": r.thumbnail if r else None,
                    "dest_path": r.dest_path if r else None,
                }
            )
        self.emit(
            {
                "event": "crawled_urls",
                "listing_url": url,
                "found": len(urls),
                "added": added,
                "urls": urls,
                "videos": videos,
            }
        )

        # Hand off to the persistent probe_worker rather than create_task()
        # (see module docstring for why that would hang). Only probe urls
        # that still lack metadata -- re-scanning a page full of already-known
        # videos shouldn't re-fetch every page again.
        unknown = [u for u in urls if records.get(u) is None or records[u].title is None]
        if unknown:
            await self._probe_jobs.put(unknown)

    async def handle_start_downloads(
        self, dest_dir: str, concurrency: int, quality: str, rate_limit: str | None
    ) -> None:
        self._paused = False
        self._concurrency_limit = max(1, concurrency)
        dest = Path(dest_dir)
        # Includes paused urls, not just queued ones -- a bulk start is also
        # how paused downloads get resumed in bulk (yt-dlp's own --continue
        # default picks up from the partial file already on disk).
        for url in self.db.list_startable_urls():
            await self._download_jobs.put(DownloadJob(url, dest, 4, quality, rate_limit))

    async def handle_download_single(
        self, url: str, dest_dir: str, concurrent_fragments: int, quality: str, rate_limit: str | None
    ) -> None:
        await self._download_jobs.put(
            DownloadJob(url, Path(dest_dir), concurrent_fragments, quality, rate_limit)
        )

    def handle_cancel(self, url: str) -> None:
        process = self._active.get(url)
        if process is None:
            self.emit({"event": "error", "message": f"no active download for {url}"})
            return
        self._cancel_requested.add(url)
        process.terminate()

    def handle_pause_video(self, url: str) -> None:
        process = self._active.get(url)
        if process is None:
            self.emit({"event": "error", "message": f"no active download for {url}"})
            return
        self._pause_requested.add(url)
        process.terminate()

    def handle_remove_video(self, url: str) -> None:
        if url in self._active:
            self.emit({"event": "error", "message": f"cancel the active download for {url} before removing it"})
            return
        self.db.remove(url)
        self.emit({"event": "removed", "url": url})

    def handle_status(self) -> None:
        self.emit({"event": "status", "counts": self.db.counts_by_status()})

    def handle_list_videos(self) -> None:
        videos = [
            {
                "url": r.url,
                "status": r.status,
                "dest_path": r.dest_path,
                "error": r.error,
                "title": r.title,
                "duration": r.duration,
                "filesize": r.filesize,
                "thumbnail": r.thumbnail,
            }
            for r in self.db.list_all()
        ]
        self.emit({"event": "video_list", "videos": videos})

    def handle_pause(self) -> None:
        self._paused = True
        # IDM-style: pause stops everything immediately, not just future
        # pickups -- and (like a per-video pause_video) leaves each one
        # resumable rather than cancelled.
        for url, process in list(self._active.items()):
            self._pause_requested.add(url)
            process.terminate()
        self.emit({"event": "paused"})

    async def dispatch(self, command: dict) -> None:
        cmd = command.get("cmd")
        if cmd == "add_listing":
            await self.handle_add_listing(command["url"], command.get("max_items"))
        elif cmd == "start_downloads":
            await self.handle_start_downloads(
                command.get("dest_dir", str(Path.cwd())),
                command.get("concurrency", 4),
                command.get("quality", "best"),
                command.get("rate_limit"),
            )
        elif cmd == "download_single":
            await self.handle_download_single(
                command["url"],
                command.get("dest_dir", str(Path.cwd())),
                command.get("concurrent_fragments", 4),
                command.get("quality", "best"),
                command.get("rate_limit"),
            )
        elif cmd == "cancel":
            self.handle_cancel(command["url"])
        elif cmd == "pause_video":
            self.handle_pause_video(command["url"])
        elif cmd == "remove_video":
            self.handle_remove_video(command["url"])
        elif cmd == "status":
            self.handle_status()
        elif cmd == "list_videos":
            self.handle_list_videos()
        elif cmd == "pause":
            self.handle_pause()
        else:
            self.emit({"event": "error", "message": f"unknown command: {cmd!r}"})

    # ---- persistent workers (started once in main(), see module docstring) -----

    async def probe_worker(self) -> None:
        while True:
            urls = await self._probe_jobs.get()

            def on_result(result: ProbeResult) -> None:
                if result.error is None:
                    self.db.set_metadata(
                        result.url, result.title, result.duration, result.filesize, result.thumbnail
                    )
                self.emit(
                    {
                        "event": "metadata",
                        "url": result.url,
                        "title": result.title,
                        "duration": result.duration,
                        "filesize": result.filesize,
                        "thumbnail": result.thumbnail,
                    }
                )

            try:
                await probe_many(urls, on_result=on_result)
            except Exception as exc:
                # This loop (like download_worker below) shares the single
                # top-level asyncio.gather() in main() with every other
                # worker and dispatch_loop itself -- an uncaught exception
                # here would silently kill probing (and, since gather()
                # propagates, everything else) for the rest of the process's
                # life instead of just failing this one probe batch.
                self.emit({"event": "error", "message": f"probe batch failed: {exc}"})

    async def download_worker(self) -> None:
        while True:
            if self._paused or self._active_download_count >= self._concurrency_limit:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue
            try:
                job = self._download_jobs.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue

            self._active_download_count += 1
            self._batch_ran = True
            try:
                await self._run_and_record(
                    job.url, job.dest_dir, job.concurrent_fragments, job.quality, job.rate_limit
                )
            except Exception as exc:
                # Same reasoning as probe_worker's guard above: a bug in one
                # download must not take out this worker, let alone (via the
                # shared gather()) every other in-flight download and the
                # command dispatcher along with it.
                self._active.pop(job.url, None)
                self._batch_failed += 1
                self.db.mark_failed(job.url, f"internal error: {exc}")
                self.emit({"event": "download_failed", "url": job.url, "error": f"internal error: {exc}"})
            finally:
                self._active_download_count -= 1
                # Last worker out announces the batch result. Pauses/cancels
                # aren't failures, so a fully-paused queue doesn't ping.
                if (
                    self._active_download_count == 0
                    and self._download_jobs.empty()
                    and self._batch_ran
                ):
                    self.emit(
                        {
                            "event": "queue_finished",
                            "done": self._batch_done,
                            "failed": self._batch_failed,
                        }
                    )
                    self._batch_done = 0
                    self._batch_failed = 0
                    self._batch_ran = False

    async def _run_and_record(
        self,
        url: str,
        dest_dir: Path,
        concurrent_fragments: int,
        quality: str = "best",
        rate_limit: str | None = None,
    ) -> None:
        self.db.mark_downloading(url)

        # Known title (if this video was already probed) lets the direct-
        # quality-link fast path in download_one() name the file properly --
        # there's no page for yt-dlp to derive %(title)s from when
        # downloading a raw CDN url directly.
        existing = self.db.get_records([url]).get(url)
        title = existing.title if existing else None

        def on_progress(u: str, fields: dict) -> None:
            self.emit({"event": "progress", "url": u, **fields})

        def on_process_started(process: asyncio.subprocess.Process) -> None:
            self._active[url] = process

        result = await downloader.download_one(
            url,
            dest_dir,
            concurrent_fragments=concurrent_fragments,
            quality=quality,
            rate_limit=rate_limit,
            title=title,
            on_progress=on_progress,
            on_process_started=on_process_started,
        )
        self._active.pop(url, None)

        if result.success:
            self._batch_done += 1
            self.db.mark_done(result.url, result.dest_path or "")
            self.emit({"event": "download_done", "url": result.url, "dest_path": result.dest_path})
        elif url in self._pause_requested:
            self._pause_requested.discard(url)
            self.db.mark_paused(url)
            self.emit({"event": "video_paused", "url": url})
        elif url in self._cancel_requested:
            self._cancel_requested.discard(url)
            self.db.mark_cancelled(url)
            self.emit({"event": "cancelled", "url": url})
        else:
            self._batch_failed += 1
            self.db.mark_failed(result.url, result.error or "unknown error")
            self.emit({"event": "download_failed", "url": result.url, "error": result.error})


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PeekNamedPipe = _kernel32.PeekNamedPipe
_PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
_PeekNamedPipe.restype = wintypes.BOOL


def _stdin_bytes_available() -> int | None:
    """Bytes currently sitting in stdin's pipe buffer, or None if the pipe is
    closed (EOF). A plain, fast, always-returns-immediately native call --
    see dispatch_loop for why that matters."""
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    available = wintypes.DWORD(0)
    ok = _PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
    if not ok:
        return None
    return available.value


async def dispatch_loop(engine: Engine) -> None:
    # Reading stdin via a blocking call -- whether a hand-rolled background
    # thread with `for line in sys.stdin` + `loop.call_soon_threadsafe(...)`,
    # or `loop.run_in_executor(None, sys.stdin.readline)` -- reliably made
    # asyncio.create_subprocess_exec() elsewhere on this loop hang forever on
    # Windows, confirmed by elimination against a minimal repro: whatever
    # thread ends up genuinely blocked inside the OS read on the real
    # inherited stdin pipe (exactly Tauri's Stdio::piped() setup), for as
    # long as that read is outstanding, a concurrent subprocess creation's
    # "child exited" notification gets lost. A thread not doing real pipe
    # I/O (e.g. plain time.sleep) doesn't trigger it, so it's the duration
    # of the outstanding blocking read that matters, not the mechanism.
    #
    # loop.connect_read_pipe() -- the "proper" asyncio-native fix -- doesn't
    # work here either: it needs an overlapped-capable handle, and
    # subprocess.PIPE/Stdio::piped() both create plain anonymous pipes, so
    # registering one with its IOCP fails outright ("the handle is
    # invalid").
    #
    # What actually works: never let a read sit blocked for long. Poll
    # PeekNamedPipe (a native call that always returns immediately, whether
    # or not data is waiting) to check for data, and only call the real,
    # blocking os.read() once PeekNamedPipe says bytes are already sitting
    # in the buffer -- so that read also returns immediately instead of
    # actually blocking.
    loop = asyncio.get_running_loop()
    buffer = b""
    while True:
        available = await loop.run_in_executor(None, _stdin_bytes_available)
        if available is None:
            break
        if available == 0:
            await asyncio.sleep(0.05)
            continue

        chunk = await loop.run_in_executor(None, os.read, sys.stdin.fileno(), available)
        if not chunk:
            break
        buffer += chunk

        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except json.JSONDecodeError as exc:
                engine.emit({"event": "error", "message": f"invalid JSON: {exc}"})
                continue
            try:
                await engine.dispatch(command)
            except Exception as exc:
                # dispatch_loop shares main()'s single top-level
                # asyncio.gather() with every download/probe worker -- a bug
                # in handling one command must not propagate out of this
                # loop, since that would silently end command processing
                # (and, via gather(), every worker) for the rest of the
                # process's life rather than just failing this one request.
                engine.emit({"event": "error", "message": f"command failed: {exc}"})


async def main() -> None:
    engine = Engine()
    download_workers = [engine.download_worker() for _ in range(MAX_DOWNLOAD_WORKERS)]
    await asyncio.gather(dispatch_loop(engine), engine.probe_worker(), *download_workers)


if __name__ == "__main__":
    asyncio.run(main())
