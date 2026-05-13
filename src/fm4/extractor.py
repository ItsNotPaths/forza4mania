"""Extract a Forza method-21 bin.zip into working/extracted/<name>/bin/.

Idempotent: if the sentinel file exists, returns the existing dir.
Uses our libmspack-backed lzxd_helper via lzx.py + binzip.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from binzip import list_entries, read_entry  # noqa: E402

WORKING = _REPO / "working"
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

    ok = 0
    failures: list[tuple[str, str]] = []
    for entry in entries:
        out_path = dst_root / entry.filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = read_entry(src_zip, entry)
        except Exception as e:
            failures.append((entry.filename, f"{type(e).__name__}: {e}"))
            continue
        out_path.write_bytes(data)
        ok += 1

    if ok == 0:
        raise RuntimeError(
            f"all {len(entries)} entries failed to extract from {src_zip}"
        )

    if failures:
        print(
            f"[extract] {len(failures)} of {len(entries)} entries failed",
            file=sys.stderr,
        )
        for name, why in failures[:5]:
            print(f"  {name}: {why}", file=sys.stderr)

    sentinel.touch()
    return dst_root
