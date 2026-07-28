"""Lightweight metadata probing: title + duration via yt-dlp --simulate.

Mirrors downloader.py's subprocess pattern (same interpreter-direct
invocation, same filters) but never writes any media -- just resolves
enough to report what would be downloaded.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

from veloci_engine.core.page_meta import fetch_page_meta

# See downloader.py's _PYTHON_EXECUTABLE/_UTF8_ENV: sys.executable is a venv
# launcher stub whose internal re-exec hop would flash a fresh console on
# every probe; __PYVENV_LAUNCHER__ keeps the real base interpreter resolving
# to this venv's site-packages when invoked directly.
_PYTHON_EXECUTABLE = getattr(sys, "_base_executable", sys.executable)

# See downloader.py's _UTF8_ENV: piped stdout on Windows falls back to the
# OEM codepage, mangling non-ASCII titles (e.g. "»" becomes a stray space).
_UTF8_ENV = {
    **os.environ,
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "__PYVENV_LAUNCHER__": sys.executable,
}

# See downloader.py's _NO_WINDOW_KWARGS: without this, every probe flashes a
# fresh console window (yt-dlp is a console-subsystem exe spawned from our
# GUI app with no console of its own).
_NO_WINDOW_KWARGS: dict = {"creationflags": 0x0800_0000} if sys.platform == "win32" else {}

# Same reasoning as downloader.py's _STALL_TIMEOUT_SECONDS: a probe against a
# dead/half-open connection should fail fast instead of hanging its slot in
# the shared probe semaphore forever. --simulate never downloads media, so
# this only has to cover page/API resolution, not a real transfer.
_PROBE_TIMEOUT_SECONDS = 45.0

_TITLE_RE = re.compile(r"^VELOCI_TITLE:(?P<title>.*)$")
_DURATION_RE = re.compile(r"^VELOCI_DURATION:(?P<duration>.*)$")
_FILESIZE_RE = re.compile(r"^VELOCI_FILESIZE:(?P<filesize>.*)$")
_THUMBNAIL_RE = re.compile(r"^VELOCI_THUMB:(?P<thumbnail>.*)$")

ResultCallback = Callable[["ProbeResult"], "Awaitable[None] | None"]


@dataclass
class ProbeResult:
    url: str
    title: str | None = None
    duration: float | None = None
    # Bytes. Exact when the server reports Content-Length, otherwise yt-dlp's
    # estimate from bitrate * duration (hence the filesize,filesize_approx
    # fallback below) -- either way, close enough for a UI size hint.
    filesize: int | None = None
    # Poster/preview image url (usually the page's og:image via the generic
    # extractor). Best-effort: some hosts 403 hotlinked images, the UI hides
    # the <img> on error.
    thumbnail: str | None = None
    error: str | None = None


async def probe_one(url: str) -> ProbeResult:
    args = [
        _PYTHON_EXECUTABLE,
        "-m",
        "yt_dlp",
        "--simulate",
        "--socket-timeout", "30",
        "--no-playlist",
        # Same filter as the real download so probed metadata matches what
        # would actually be fetched (see downloader.py for why: incidental
        # muted preview clips and in-page ad banners get excluded).
        "--match-filter", "duration >? 20 & url !~= '(?i)(banner|\\.gif\\.mp4)'",
        "--print", "VELOCI_TITLE:%(title)s",
        "--print", "VELOCI_DURATION:%(duration)s",
        "--print", "VELOCI_FILESIZE:%(filesize,filesize_approx)s",
        "--print", "VELOCI_THUMB:%(thumbnail)s",
        url,
    ]

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_UTF8_ENV,
        **_NO_WINDOW_KWARGS,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        return ProbeResult(url=url, error=f"probe stalled: no response for {int(_PROBE_TIMEOUT_SECONDS)}s")

    if process.returncode != 0:
        return ProbeResult(url=url, error=stderr_bytes.decode(errors="replace")[-500:])

    title: str | None = None
    duration: float | None = None
    filesize: int | None = None
    thumbnail: str | None = None
    for raw_line in stdout_bytes.decode(errors="replace").splitlines():
        line = raw_line.strip()
        if match := _TITLE_RE.match(line):
            title = match.group("title") or None
        elif match := _DURATION_RE.match(line):
            value = match.group("duration")
            try:
                duration = float(value)
            except ValueError:
                duration = None
        elif match := _FILESIZE_RE.match(line):
            value = match.group("filesize")
            try:
                filesize = int(float(value))
            except ValueError:
                filesize = None
        elif match := _THUMBNAIL_RE.match(line):
            value = match.group("thumbnail").strip()
            # yt-dlp prints the literal "NA" when a field is missing.
            thumbnail = value if value.startswith("http") else None

    if thumbnail is None:
        # fetch_page_meta's timeout (8s) keeps a slow/dead page from stalling
        # this probe slot for long -- see page_meta.py for why one shared
        # fetch covers both this and downloader.py's direct-quality lookup.
        loop = asyncio.get_running_loop()
        page_meta = await loop.run_in_executor(None, fetch_page_meta, url)
        thumbnail = page_meta.thumbnail

    return ProbeResult(url=url, title=title, duration=duration, filesize=filesize, thumbnail=thumbnail)


async def probe_many(urls: list[str], *, concurrency: int = 5, on_result: ResultCallback) -> None:
    """Probe urls with bounded concurrency, streaming each result as it finishes."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(url: str) -> None:
        async with semaphore:
            result = await probe_one(url)
        maybe_awaitable = on_result(result)
        if maybe_awaitable is not None:
            await maybe_awaitable

    await asyncio.gather(*(_bounded(url) for url in urls))
