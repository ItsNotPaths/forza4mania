"""Subprocess wrappers that work reliably under PyInstaller + Wine on Windows.

Why we don't just use ``subprocess.run(capture_output=True)``:

    Wine's ``CreateProcess`` rejects the ``STARTF_USESTDHANDLES`` setup
    that ``subprocess`` builds when ``stdout=PIPE`` / ``stderr=PIPE`` are
    used. Plain anonymous pipe handles passed via STARTUPINFO trip
    ``[WinError 6] Invalid handle`` deep inside ``_execute_child``. This
    happens regardless of:

      * ``stdin=DEVNULL`` (we set it),
      * ``CREATE_NO_WINDOW`` / ``STARTF_USESHOWWINDOW`` (we tried both),
      * ``--console`` vs ``--windowed`` PyInstaller mode.

    The reliable workaround is to redirect stdout/stderr to **temp files**
    instead of pipes. Wine handles regular file handle inheritance
    cleanly, so this dodges the broken pipe-handle path entirely. After
    the child exits we read the files back into a CompletedProcess so the
    caller's API doesn't change.

The daemon helper (``popen_pipes``) still uses pipes — long-running
bidirectional I/O can't be temp-filed — but that path runs ONLY for the
lzxd_helper which historically works fine under Wine (different code
path: parent doesn't try to read until after the child has its pipes
configured client-side).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def run_captured(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a child process and capture its stdout/stderr to temp files.

    Returns the same CompletedProcess shape as
    ``subprocess.run(capture_output=True)``: ``returncode``, ``stdout``,
    ``stderr``. The temp files are deleted before return.

    Manual Popen + Pipe-stdin: we don't use ``subprocess.run(..., stdin=
    subprocess.DEVNULL)`` because Wine's CreateProcess rejects the NUL-
    device handle Python uses to back ``DEVNULL`` (fails with WinError 6).
    Instead we follow the lzxd_helper daemon pattern that works on Wine:
    ``stdin=subprocess.PIPE`` then immediately close the parent end.
    """
    out_tmp = tempfile.NamedTemporaryFile(prefix="forzam_out_", delete=False)
    err_tmp = tempfile.NamedTemporaryFile(prefix="forzam_err_", delete=False)
    out_path = Path(out_tmp.name)
    err_path = Path(err_tmp.name)
    out_tmp.close()
    err_tmp.close()

    try:
        with open(out_path, "wb") as out_w, open(err_path, "wb") as err_w:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.PIPE,
                stdout=out_w,
                stderr=err_w,
            )
            # Don't keep stdin open — child sees EOF immediately. Avoids
            # the DEVNULL handle that Wine rejects.
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise

        out_bytes = out_path.read_bytes()
        err_bytes = err_path.read_bytes()
        if text:
            stdout = out_bytes.decode("utf-8", errors="replace")
            stderr = err_bytes.decode("utf-8", errors="replace")
        else:
            stdout = out_bytes
            stderr = err_bytes
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    finally:
        for p in (out_path, err_path):
            try:
                p.unlink()
            except OSError:
                pass


def popen_pipes(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    bufsize: int = 0,
    capture_stderr: bool = True,
) -> subprocess.Popen:
    """Long-running child with bidirectional pipes — used only by lzxd_helper.

    This path needs real pipes (we feed compressed bytes in and read
    decompressed bytes out for thousands of requests per session — temp
    files would be insane). The lzxd_helper.exe was already verified to
    work under Wine via run-via-proton.sh, so it's not subject to the
    CreateProcess pipe-handle rejection that bites run_captured callers.
    """
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else None,
        bufsize=bufsize,
    )
