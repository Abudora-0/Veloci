"""yt-dlp subprocess-based downloader with bounded concurrency.

Runs yt-dlp as a subprocess per video (process isolation + yt-dlp's own
concurrent-fragment downloading) rather than embedding it as a library, so
a crashed download can't take down the engine and yt-dlp can be upgraded
independently. Progress is read from a custom --progress-template so the
caller gets structured events without touching yt-dlp's internal API.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from veloci_engine.core.page_meta import fetch_page_meta

# sys.executable inside a venv is a tiny launcher stub (see engine.rs's
# base_interpreter()) that re-execs a second, separate python.exe under the
# hood -- invisible to the creationflags below, so spawning
# "sys.executable -m yt_dlp" would flash a fresh console on every single
# video probed/downloaded, not just once at app startup. sys._base_executable
# is the real interpreter binary; __PYVENV_LAUNCHER__ is the same env var
# CPython's own stub sets internally, and passing it through keeps
# sys.prefix/site-packages resolving to this venv even when invoking the
# base interpreter directly (verified live: "import yt_dlp" still succeeds).
_PYTHON_EXECUTABLE = getattr(sys, "_base_executable", sys.executable)

# On Windows, a piped (non-console) stdout falls back to the system's OEM
# codepage instead of UTF-8, so yt-dlp silently mangles non-ASCII characters
# in anything it prints -- e.g. a title's "»" turns into a space. Harmless
# for display, but it corrupts the exact bytes of the --print'd VELOCI_FILEPATH
# line below, which we depend on to open the real file afterwards (the
# downscale step). Forcing UTF-8 mode makes what we parse match the real
# filename on disk.
_UTF8_ENV = {
    **os.environ,
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "__PYVENV_LAUNCHER__": sys.executable,
}

# Same root cause as the Rust side's CREATE_NO_WINDOW on the engine process
# itself (see engine.rs): spawning a console-subsystem exe (yt-dlp/ffmpeg/
# ffprobe, all python.exe or *.exe underneath) with no flag lets Windows
# allocate and flash a brand new console window for it. subprocess.
# CREATE_NO_WINDOW passed as creationflags suppresses that -- harmless
# no-op on non-Windows since it's only ever added to the kwargs there.
_NO_WINDOW_KWARGS: dict = {"creationflags": 0x0800_0000} if sys.platform == "win32" else {}

# yt-dlp's %(progress._percent_str)s and friends are fixed-width, left-padded
# with spaces for terminal alignment (e.g. "  0.1%", not "0.1%") -- a
# single-space-separated regex silently fails to match as soon as a field's
# own padding adds extra whitespace, which drops every progress event with no
# error. "|" can't appear inside any of these values, so split on that
# instead of relying on whitespace as the field separator.
_PROGRESS_PREFIX = "VELOCI_PROGRESS|"
_FILEPATH_RE = re.compile(r"^VELOCI_FILEPATH (?P<path>.+)$")

# If a yt-dlp subprocess produces zero output for this long, treat it as
# stalled and kill it rather than let it block a worker slot forever. Some
# connections (rate-limited/half-open CDN sockets) never error out on their
# own -- yt-dlp just sits there with no bytes flowing and no progress line
# printed -- which previously meant a handful of stuck videos in a batch
# quietly ate the whole concurrency budget and the rest of the queue stopped
# making visible progress. Generous enough that a real, alive-but-slow
# transfer (which still prints a progress line periodically) never trips it.
_STALL_TIMEOUT_SECONDS = 90.0

# yt-dlp format selectors. "<=?" (vs plain "<=") also matches formats with an
# unknown height rather than excluding them -- most of these sites serve a
# single direct .mp4/.webm with no real quality ladder, so their one format
# usually has no reported height at all; without the "?" a quality filter
# would just reject every format and the download would fail outright. The
# "/best" fallback covers sites that genuinely have no format under the cap.
_QUALITY_FORMAT_SELECTORS: dict[str, str | None] = {
    "best": None,
    "1080p": "best[height<=?1080]/best",
    "720p": "best[height<=?720]/best",
    "480p": "best[height<=?480]/best",
    "360p": "best[height<=?360]/best",
    "worst": "worst",
}

# The format selector above is a no-op on sites that only ever expose one
# fixed-quality direct file with no reported height at all (confirmed via
# `yt-dlp -F` against fap-nation/futapo: exactly one "unknown resolution"
# format per video) -- there's nothing lower to select. For those, the only
# way "480p" etc. actually shrinks the file is re-encoding it locally after
# download. "worst" has no fixed target height (it just means "whatever
# yt-dlp picked as smallest," which may still be the only format), so it's
# left out here and never triggers a re-encode.
_QUALITY_TARGET_HEIGHTS: dict[str, int | None] = {
    "best": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "worst": None,
}

ProgressCallback = Callable[[str, dict], "Awaitable[None] | None"]

# Some sites' own "DOWNLOAD" buttons on the post page link directly to a
# real per-quality CDN file that yt-dlp's generic <video>/<source> scraper
# never discovers (see page_meta.py) -- confirmed on fap-nation.org, where
# yt-dlp instead finds an unrelated single-fixed-quality mirror. When the
# requested quality has a matching direct link, using it outright skips the
# multi-minute local ffmpeg re-encode entirely (exact file, no transcode).
def _pick_direct_quality_url(quality: str, quality_links: dict[int, str]) -> str | None:
    if not quality_links:
        return None
    if quality == "best":
        return quality_links[max(quality_links)]
    if quality == "worst":
        return quality_links[min(quality_links)]
    target = _QUALITY_TARGET_HEIGHTS.get(quality)
    if target is None:
        return None
    # Largest available at or below the requested tier (a real 480p file
    # beats re-encoding a "best" download down to 480p); if the page only
    # offers higher tiers than requested, fall back to its smallest -- still
    # no re-encode, just not quite as small as asked for.
    at_or_below = [h for h in quality_links if h <= target]
    return quality_links[max(at_or_below)] if at_or_below else quality_links[min(quality_links)]


# Windows reserves these in filenames; a direct-link download builds its own
# filename from the (already-known) title since there's no page for yt-dlp
# to derive one from -- see the direct-link branch in download_one().
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')

# Safety nets against a hung ffprobe/ffmpeg (e.g. a corrupt or partially
# written file that makes either one sit forever instead of erroring out) --
# same "never block a worker slot forever" reasoning as the yt-dlp stall
# watchdog above. ffprobe should return almost instantly; ffmpeg's cap is
# generous since a real re-encode can legitimately take minutes.
_FFPROBE_TIMEOUT_SECONDS = 20.0
_FFMPEG_TIMEOUT_SECONDS = 1200.0


async def _probe_height(filepath: Path) -> int | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=height", "-of", "csv=p=0", str(filepath),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **_NO_WINDOW_KWARGS,
        )
    except OSError:
        return None
    try:
        stdout_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=_FFPROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        return None
    if process.returncode != 0:
        return None
    try:
        return int(stdout_bytes.decode().strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


# Tried in order; the first that actually works on this machine's hardware
# wins. A GPU encoder finishes in roughly a tenth of the time libx264 takes
# at the same settings, but which (if any) is present varies per machine --
# confirmed live on this dev box: h264_nvenc and h264_amf both fail instantly
# (no compatible GPU), h264_qsv (Intel Quick Sync) actually works. A failed
# hardware attempt errors out near-instantly (no compatible device), so
# trying each one costs nothing on machines that don't have it -- last
# resort is libx264 software encoding, "veryfast" rather than the old "fast"
# for a real speed win on machines with no hardware encoder at all.
_ENCODER_ATTEMPTS: list[list[str]] = [
    ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"],
    ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "23"],
    ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", "23", "-qp_p", "23"],
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
]


async def _try_encode(filepath: Path, tmp_path: Path, target_height: int, encoder_args: list[str]) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(filepath),
            "-vf", f"scale=-2:{target_height}",
            *encoder_args,
            "-c:a", "aac", "-b:a", "128k",
            str(tmp_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **_NO_WINDOW_KWARGS,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=_FFMPEG_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            return False
    except OSError:
        return False
    return process.returncode == 0 and tmp_path.exists()


async def _downscale_if_needed(filepath: Path, target_height: int) -> Path:
    """Re-encode filepath down to target_height if the source is taller.

    No-op (returns filepath unchanged) if ffmpeg/ffprobe aren't installed,
    the probe fails, every encoder attempt fails, or the source is already
    at or below the target -- losing the already-completed download to a
    transcoding hiccup would be worse than just leaving it at its original
    quality.
    """
    actual_height = await _probe_height(filepath)
    if actual_height is None or actual_height <= target_height:
        return filepath

    tmp_path = filepath.with_name(f"{filepath.stem}.tmp{target_height}p.mp4")
    for encoder_args in _ENCODER_ATTEMPTS:
        if await _try_encode(filepath, tmp_path, target_height, encoder_args):
            break
        tmp_path.unlink(missing_ok=True)
    else:
        return filepath

    final_path = filepath.with_suffix(".mp4")
    filepath.unlink()
    tmp_path.rename(final_path)
    return final_path


@dataclass
class DownloadResult:
    url: str
    success: bool
    dest_path: str | None = None
    error: str | None = None


async def _run_yt_dlp(
    args: list[str],
    *,
    report_url: str,
    on_progress: ProgressCallback | None,
    on_process_started: Callable[[asyncio.subprocess.Process], None] | None,
) -> tuple[bool, str | None, str | None]:
    """Spawn yt-dlp with the given args, stream progress, and return
    (success, filepath, error). Shared by both the generic-extraction path
    and the direct-quality-link fast path below -- only the args differ."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_UTF8_ENV,
        **_NO_WINDOW_KWARGS,
    )
    if on_process_started is not None:
        on_process_started(process)

    filepath: str | None = None
    stalled = False
    assert process.stdout is not None
    while True:
        try:
            raw_line = await asyncio.wait_for(process.stdout.readline(), timeout=_STALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            stalled = True
            process.kill()
            break
        if not raw_line:
            break
        line = raw_line.decode(errors="replace").strip()

        if line.startswith(_PROGRESS_PREFIX):
            fields = line[len(_PROGRESS_PREFIX):].split("|")
            if len(fields) == 5 and on_progress is not None:
                percent, speed, eta, downloaded, total = (f.strip() for f in fields)
                maybe_awaitable = on_progress(
                    report_url,
                    {"percent": percent, "speed": speed, "eta": eta, "downloaded": downloaded, "total": total},
                )
                if maybe_awaitable is not None:
                    await maybe_awaitable
            continue

        filepath_match = _FILEPATH_RE.match(line)
        if filepath_match:
            filepath = filepath_match.group("path")

    stderr_bytes = await process.stderr.read() if process.stderr else b""
    returncode = await process.wait()

    if stalled:
        return False, None, f"download stalled: no response for {int(_STALL_TIMEOUT_SECONDS)}s, connection appears dead"
    if returncode != 0:
        return False, None, stderr_bytes.decode(errors="replace")[-2000:]
    return True, filepath, None


async def download_one(
    url: str,
    dest_dir: Path,
    *,
    concurrent_fragments: int = 4,
    quality: str = "best",
    rate_limit: str | None = None,
    title: str | None = None,
    on_progress: ProgressCallback | None = None,
    on_process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> DownloadResult:
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Cheap page fetch (page_meta.py) up front: if the post page has a direct
    # per-quality download link matching what was asked for, use it and skip
    # yt-dlp's generic extraction entirely -- no format selector, no ad/
    # duration filter (meaningless for a single known-good direct file), and
    # no ffmpeg re-encode afterward, since it's already the exact quality.
    loop = asyncio.get_running_loop()
    page_meta = await loop.run_in_executor(None, fetch_page_meta, url)
    direct_url = _pick_direct_quality_url(quality, page_meta.quality_links)

    common_args = [
        # Invoke yt-dlp as a module of the current interpreter rather than the
        # "yt-dlp" console-script shim on PATH: when spawned this deep (Tauri
        # -> uv run -> this process -> subprocess), the shim's venv-detection
        # got confused and re-exec'd itself recursively forever (observed: 3-4
        # nested yt-dlp.exe/python.exe layers, zero network connections, zero
        # bytes written). Calling the already-correct interpreter directly
        # sidesteps that shim entirely. _PYTHON_EXECUTABLE (not plain
        # sys.executable) -- see its definition above for why.
        _PYTHON_EXECUTABLE,
        "-m",
        "yt_dlp",
        # yt-dlp silently skips the whole progress-template ("download:") hook
        # when it doesn't think it's attached to an interactive terminal --
        # true for our piped subprocess stdout even with --newline (which
        # only controls *how* progress renders, not *whether* it does).
        # Without --progress here, zero progress events ever fire; the video
        # still downloads fine, so this is easy to miss.
        "--progress",
        "--newline",
        "--no-part",
        # Caps how long yt-dlp's own HTTP client will sit on a single dead/
        # half-open socket before giving up on that attempt and retrying --
        # without this, a connection that accepted the request but never
        # sends a byte back can hang far longer than our own stall watchdog
        # would like, needlessly eating into that budget on every retry.
        "--socket-timeout", "30",
        "--concurrent-fragments", str(concurrent_fragments),
        "--progress-template",
        "download:VELOCI_PROGRESS|%(progress._percent_str)s|%(progress._speed_str)s"
        "|%(progress._eta_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str)s",
        "--print", "after_move:VELOCI_FILEPATH %(filepath)s",
    ]
    if rate_limit:
        # yt-dlp's own bytes-per-second cap, e.g. "2M" or "500K". Applied per
        # download (so N parallel downloads can total N * limit) -- the UI
        # labels it accordingly.
        common_args += ["--limit-rate", rate_limit]

    if direct_url is not None:
        # A direct CDN file, not a page to extract from: no --no-playlist/
        # --match-filter (nothing to filter, there's exactly one file), no
        # -f selector (it's already the exact quality). Filename is built
        # from the already-known title since there's no page for yt-dlp to
        # derive %(title)s from -- an untitled ".../play_480p.mp4" url would
        # otherwise name the file after the CDN path instead of the video.
        safe_title = _UNSAFE_FILENAME_CHARS.sub("_", title).strip()[:200] if title else None
        name_template = f"{safe_title} [{quality}]" if safe_title else "%(title).200B [%(id)s]"
        args = [
            *common_args,
            "-o", str(dest_dir / f"{name_template}.%(ext)s"),
            direct_url,
        ]
        success, filepath, error = await _run_yt_dlp(
            args, report_url=url, on_progress=on_progress, on_process_started=on_process_started
        )
        if not success:
            return DownloadResult(url=url, success=False, error=error)
        return DownloadResult(url=url, success=True, dest_path=filepath)

    format_selector = _QUALITY_FORMAT_SELECTORS.get(quality)
    args = [
        *common_args,
        # Some pages resolve as a multi-entry playlist (e.g. a WordPress post
        # embedding a whole gallery), so cap each queued item at one video.
        "--no-playlist",
        # Some listing/video pages also embed a few seconds of muted preview
        # clips for other/related videos alongside the real one; yt-dlp's
        # generic extractor picks those up too, so drop anything under 20s
        # (confirmed against a real fap-nation.org page: main video was 153s,
        # incidental previews were 4-5.6s). ">?" lets unknown-duration
        # entries through instead of dropping them.
        #
        # Same generic extractor also picks up in-page ad banners as their
        # own playlist entries when a post embeds multiple <video> tags
        # (confirmed on fap-nation.org: every post page also serves 4 ad
        # clips this way alongside the real video). All four sampled ads
        # were either named with "banner" or were GIF-loop ads re-encoded
        # to ".gif.mp4" for autoplay -- a pattern real content never
        # matches -- so exclude both regardless of duration.
        "--match-filter", "duration >? 20 & url !~= '(?i)(banner|\\.gif\\.mp4)'",
        "-o", str(dest_dir / "%(title).200B [%(id)s].%(ext)s"),
    ]
    if format_selector is not None:
        args += ["-f", format_selector]
    args.append(url)

    success, filepath, error = await _run_yt_dlp(
        args, report_url=url, on_progress=on_progress, on_process_started=on_process_started
    )
    if not success:
        return DownloadResult(url=url, success=False, error=error)

    target_height = _QUALITY_TARGET_HEIGHTS.get(quality)
    if target_height is not None and filepath is not None:
        filepath = str(await _downscale_if_needed(Path(filepath), target_height))

    return DownloadResult(url=url, success=True, dest_path=filepath)


async def run_pool(
    urls: list[str],
    dest_dir: Path,
    *,
    concurrency: int = 4,
    on_progress: ProgressCallback | None = None,
) -> list[DownloadResult]:
    """Download urls with at most `concurrency` yt-dlp subprocesses running at once."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(url: str) -> DownloadResult:
        async with semaphore:
            return await download_one(url, dest_dir, on_progress=on_progress)

    return await asyncio.gather(*(_bounded(url) for url in urls))
