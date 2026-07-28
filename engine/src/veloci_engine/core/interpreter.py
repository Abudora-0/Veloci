"""How to invoke this interpreter and yt-dlp -- shared by downloader.py and
probe.py, which both need to spawn yt-dlp as a subprocess.

Two very different runtimes need to work here:

- Dev (running from source via `uv run`): sys.executable is the venv's
  launcher stub, which internally re-execs a second, separate python.exe
  (see engine.rs's base_interpreter()) -- invisible to CREATE_NO_WINDOW, so
  spawning "sys.executable -m yt_dlp" would flash a fresh console on every
  single video probed/downloaded. sys._base_executable is the real
  interpreter binary; __PYVENV_LAUNCHER__ is the same env var CPython's own
  stub sets internally, and passing it through keeps sys.prefix/site-packages
  resolving to this venv even when invoking the base interpreter directly.

- Frozen (a PyInstaller build, bundled as a Tauri sidecar so the app doesn't
  need Python/uv installed on the target machine at all): there is no
  separate python.exe to hand "-m yt_dlp" to -- this executable *is* the
  interpreter and the app bundled together. Since yt_dlp is already one of
  this package's own dependencies, the frozen exe re-invokes *itself* with a
  sentinel flag (see cli.py's __main__ block) rather than requiring a
  second, separately downloaded/bundled yt-dlp.exe to be kept in sync.
"""

from __future__ import annotations

import os
import sys

YTDLP_PASSTHROUGH_FLAG = "--ytdlp-passthrough"

_PYTHON_EXECUTABLE = getattr(sys, "_base_executable", sys.executable)


def yt_dlp_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, YTDLP_PASSTHROUGH_FLAG]
    return [_PYTHON_EXECUTABLE, "-m", "yt_dlp"]


def subprocess_env() -> dict:
    """Env for any subprocess we spawn that talks to yt-dlp. On Windows, a
    piped (non-console) stdout falls back to the OEM codepage instead of
    UTF-8, silently mangling non-ASCII output (e.g. a title's "»" becomes a
    space) -- forcing UTF-8 mode keeps parsed output matching the real bytes
    on disk. __PYVENV_LAUNCHER__ is dev-only: a frozen exe re-invoking itself
    isn't going through any venv stub, and setting it there risks confusing
    PyInstaller's own bootloader for no benefit.
    """
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if not getattr(sys, "frozen", False):
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return env
