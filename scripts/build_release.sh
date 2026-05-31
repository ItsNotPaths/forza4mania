#!/usr/bin/env bash
# Shared Nuitka build for forzamania — produces a self-contained release dir
# for the HOST platform (Linux ELF bundle or Windows .exe bundle). The same
# script drives both `release.sh --local` and the GitHub release.yml matrix
# (ubuntu + windows runners), so the two never drift.
#
# Run ./download-deps.sh first. Usage:
#     scripts/build_release.sh [OUT_DIR]
# OUT_DIR defaults to ../<project>-release/. Honors $PYTHON (else python3/python)
# and $CC (else cc/gcc).
#
# Produces in OUT_DIR:
#   forzamania[.exe]            — the Nuitka --standalone app bundle (+ its .so/.dll, data)
#   tools/x360io/x360io[.exe]   — the FM4 reader CLI (separate Nuitka --standalone dist)
#   tools/nadeo-freeporter[.exe]— freeporter (importer + map composer), per-platform release asset
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
OUT_DIR="${1:-$(cd "$PROJECT_DIR/.." && pwd)/${PROJECT_NAME}-release}"
BUILD_DIR="$PROJECT_DIR/build"

# --- python / compiler ----------------------------------------------------
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi
CC_BIN="${CC:-${cc:-gcc}}"

# --- platform detection ---------------------------------------------------
case "$(uname -s)" in
    Linux*)                       PLATFORM=linux ;;
    MINGW*|MSYS*|CYGWIN*|Windows*) PLATFORM=windows ;;
    *) echo "error: unsupported build platform: $(uname -s)" >&2; exit 1 ;;
esac
echo "==> forzamania Nuitka build ($PLATFORM) -> $OUT_DIR"

# --- preflight ------------------------------------------------------------
MSPACK="$PROJECT_DIR/vendor/libmspack/libmspack/mspack"
[ -d "$MSPACK" ] || { echo "error: $MSPACK missing — run ./download-deps.sh first" >&2; exit 1; }
[ -d "$PROJECT_DIR/vendor/Forza-X360-IO/src/forza_blender" ] || {
    echo "error: vendor/Forza-X360-IO missing — run ./download-deps.sh first" >&2; exit 1; }
"$PY" -m nuitka --version >/dev/null 2>&1 || {
    echo "error: Nuitka not installed for '$PY'. Install: $PY -m pip install nuitka" >&2; exit 1; }
if [ "$PLATFORM" = linux ] && ! command -v patchelf >/dev/null 2>&1; then
    echo "error: patchelf not found — Nuitka --standalone needs it on Linux." >&2
    echo "    sudo apt install patchelf   # or your distro's equivalent" >&2
    exit 1
fi

MATERIALS_JSON="vendor/blendermania-addon/assets/materials/materials-map-trackmania2020_18122023.json"

# --- 1. lzxd_helper (per-platform native C) -------------------------------
# Windows: fully static-link the mingw runtime so the helper has zero DLL deps
# (libgcc_s/libwinpthread are absent under Wine and the daemon would die before
# its first read). Linux: a plain ELF.
if [ "$PLATFORM" = windows ]; then
    HELPER_REL="src/lzxd_helper.exe"
    HELPER_CFLAGS="-O2 -Wall -static -static-libgcc"
else
    HELPER_REL="src/lzxd_helper"
    HELPER_CFLAGS="-O2 -Wall"
fi
HELPER_NAME="$(basename "$HELPER_REL")"
echo "[build] lzxd_helper ($HELPER_NAME)"
$CC_BIN $HELPER_CFLAGS -I"$MSPACK" \
    -o "$PROJECT_DIR/$HELPER_REL" \
    "$PROJECT_DIR/src/lzxd_helper.c" \
    "$MSPACK/lzxd.c" \
    "$MSPACK/system.c"

# --- 2. Nuitka: main app --------------------------------------------------
# --standalone (not --onefile: onefile re-extracts the numpy bundle every
# launch). Data mappings place payloads next to the binary, where
# resources.bundle_root() resolves them under Nuitka. forza_blender is still
# needed by textures.py (the in-process Bix/deswizzle path).
echo "[build] Nuitka standalone app (first run is slow — C compile of numpy/tk)"
rm -rf "$BUILD_DIR/main.dist"
( cd "$PROJECT_DIR" && "$PY" -m nuitka \
    --standalone \
    --assume-yes-for-downloads \
    --enable-plugin=tk-inter \
    --include-data-dir=vendor/Forza-X360-IO/src/forza_blender=vendor/Forza-X360-IO/src/forza_blender \
    --include-data-dir=assets=assets \
    --include-data-files=scripts/blender_export.py=scripts/blender_export.py \
    --include-data-files="$MATERIALS_JSON=$MATERIALS_JSON" \
    --include-data-files="$HELPER_REL=$HELPER_NAME" \
    --output-filename=forzamania \
    --output-dir="$BUILD_DIR" \
    src/main.py )

# --- 3. Nuitka: x360io FM4 reader CLI -------------------------------------
# Copy patches/x360io_cli.py into the cloned Forza-X360-IO src so it sits beside
# forza_blender/ — vendor/ is gitignored and never hand-edited. Ships
# forza_blender as a data dir; the wrapper loads those parsers at runtime via
# the bpy-bypass stub (the addon __init__ imports bpy, which we never run).
echo "[build] Nuitka standalone x360io reader CLI"
X360_SRC="$PROJECT_DIR/vendor/Forza-X360-IO/src"
cp "$PROJECT_DIR/patches/x360io_cli.py" "$X360_SRC/x360io_cli.py"
rm -rf "$BUILD_DIR/x360io_cli.dist"
( cd "$X360_SRC" && "$PY" -m nuitka \
    --standalone \
    --assume-yes-for-downloads \
    --include-data-dir="$X360_SRC/forza_blender=forza_blender" \
    --output-filename=x360io \
    --output-dir="$BUILD_DIR" \
    x360io_cli.py )

# --- 4. assemble release dir ----------------------------------------------
echo "[build] assembling $OUT_DIR"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -a "$BUILD_DIR/main.dist/." "$OUT_DIR/"
mkdir -p "$OUT_DIR/tools/x360io"
cp -a "$BUILD_DIR/x360io_cli.dist/." "$OUT_DIR/tools/x360io/"
[ "$PLATFORM" = linux ] && chmod +x "$OUT_DIR/tools/x360io/x360io" || true

# --- 5. bundle freeporter (per-platform release asset) --------------------
# Reuse the app's own verified downloader — it picks the linux/windows asset by
# the build host's sys.platform (host == target for each matrix job), unzips
# into tools/, and restores the ELF exec bit. Non-fatal: if GitHub rate-limits
# the API in CI, the app can still fetch it at runtime (Settings → Download).
echo "[build] bundling nadeo-freeporter ($PLATFORM release asset)"
if PYTHONPATH="$PROJECT_DIR/src" "$PY" - "$OUT_DIR/tools" <<'PYEOF'
import sys
from pathlib import Path
from external_downloader import download_freeporter
res = download_freeporter(Path(sys.argv[1]))
print("[build] freeporter:", ", ".join(p.name for p in res.extracted_files))
PYEOF
then :; else
    echo "[build] WARNING: freeporter bundling failed (rate limit?); users can Download it in Settings." >&2
fi

# --- 6. docs --------------------------------------------------------------
echo "Bundled: x360io/ (FM4 reader) + nadeo-freeporter (importer + map composer). Update freeporter via Settings → Download freeporter." > "$OUT_DIR/tools/README.txt"
[ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$OUT_DIR/" || true
[ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$OUT_DIR/" || true

echo "==> done ($PLATFORM): $OUT_DIR"
