"""Extract a Forza method-21 bin.zip into working/extracted/<name>/bin/.

Idempotent: if the sentinel file exists, returns the existing dir.
Uses our libmspack-backed lzxd_helper via lzx.py + binzip.py.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _working_dir() -> Path:
    """Where extracted FM4 bin/ trees live.

    PyInstaller --onefile mode: next to the .exe so files persist across
    runs (otherwise we'd re-extract a couple hundred MB to a temp dir
    every launch, since _MEIPASS gets wiped on exit).
    Dev mode: <repo>/working/.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "working"
    return Path(__file__).resolve().parent.parent.parent / "working"


# In dev mode this sets up sys.path so binzip / lzx import; under PyInstaller
# the bundled paths handle that already.
_DEV_REPO = Path(__file__).resolve().parent.parent.parent
_DEV_SRC = _DEV_REPO / "src"
if not getattr(sys, "frozen", False) and str(_DEV_SRC) not in sys.path:
    sys.path.insert(0, str(_DEV_SRC))

from binzip import list_entries, read_entry  # noqa: E402

WORKING = _working_dir()
SENTINEL_NAME = ".extracted"


def extract_bin_zip(track_dir: Path, dst_root: Path | None = None) -> Path:
    """Extract <track_dir>/bin.zip to a working directory; return that dir.

    If `dst_root` is None, defaults to working/extracted/<track_name>/bin/.
    Failures on individual entries are tolerated and printed to stderr;
    only a wholesale extract failure (no entries succeeded) raises.
    """
    src_zip = track_dir / "bin.zip"
    if not src_zip.is_file():
        raise FileNotFoundError(f"missing {src_zip}")

    if dst_root is None:
        dst_root = WORKING / "extracted" / track_dir.name / "bin"

    sentinel = dst_root / SENTINEL_NAME
    if sentinel.is_file():
        return dst_root

    dst_root.mkdir(parents=True, exist_ok=True)
    entries = list_entries(src_zip)

    # No per-entry try/except: any decompression error here is almost
    # always a config issue (lzxd_helper missing, libmspack symbol
    # mismatch) that affects every entry the same way. Letting the first
    # failure surface gives a clear root-cause message instead of a half-
    # extracted bin/ that quietly produces "0 meshes" downstream.
    for entry in entries:
        out_path = dst_root / entry.filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        data = read_entry(src_zip, entry)
        out_path.write_bytes(data)

    sentinel.touch()
    return dst_root
