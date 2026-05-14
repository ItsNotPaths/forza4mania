"""Defensive subprocess.run wrapper for PyInstaller --windowed on Windows.

When PyInstaller is built with ``--windowed`` (no console), the parent
process's standard handles are NULL on Windows. Plain ``subprocess.run``
then fails with ``[WinError 6] Invalid handle`` even when the user
provides explicit ``stdin=DEVNULL`` and ``capture_output=True`` — Python's
subprocess machinery still touches the parent's invalid handles in
various corner-case code paths.

The full known-good recipe:

    1. ``stdin=DEVNULL`` (don't inherit invalid stdin)
    2. ``stdout=PIPE, stderr=PIPE`` (don't inherit invalid stdout/stderr)
    3. ``startupinfo`` with ``STARTF_USESHOWWINDOW + SW_HIDE``
       (suppress child console window)
    4. ``creationflags=CREATE_NO_WINDOW``
       (CRITICAL: tells Windows not to attach the child to the parent's
       non-existent console)

(1) + (2) we already do; (3) + (4) are what makes the difference under
PyInstaller --windowed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    """Build a STARTUPINFO that hides the child's window. Windows-only."""
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def run_captured(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """subprocess.run with the full PyInstaller-windowed safe defaults.

    Same return shape as subprocess.run(capture_output=True). Use this
    everywhere we shell out from inside the Tk app instead of calling
    subprocess.run directly.
    """
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        timeout=timeout,
        startupinfo=_hidden_startupinfo(),
        creationflags=_NO_WINDOW,
    )


def popen_pipes(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    bufsize: int = 0,
    capture_stderr: bool = True,
) -> subprocess.Popen:
    """subprocess.Popen for our long-running daemon (lzxd_helper).

    Same hardening as run_captured but returns a live Popen that the
    caller drives via stdin/stdout pipes.
    """
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else None,
        bufsize=bufsize,
        startupinfo=_hidden_startupinfo(),
        creationflags=_NO_WINDOW,
    )
