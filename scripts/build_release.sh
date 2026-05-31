#!/usr/bin/env bash
# Shared build for forzamania — produces a release dir for the chosen backend.
# The main app is the Nim/wayluigi binary (app/forzamania.nim); the runtime
# helpers (lzxd_helper, x360io, freeporter) stay as subprocess CLIs. The same
# script drives both `release.sh --local` and the GitHub release.yml matrix so
# the two never drift.
#
# Run ./download-deps.sh first. Usage:
#     scripts/build_release.sh [OUT_DIR] [BACKEND]
#   OUT_DIR  defaults to ../<project>-release/
#   BACKEND  x11 (default) | wayland | windows
#
# Env:
#   FM_GUI_ONLY=1   build ONLY the Nim app (skip the CLI bundling + its slow
#                   Nuitka/patchelf preflights). Fast path for UI iteration.
#   NIM            nim binary (default: nim)
#   PYTHON / CC    python / C compiler for the --with-tools steps
#
# Produces in OUT_DIR:
#   forzamania[.exe]              — the Nim/wayluigi app (built-in font, no freetype dep)
# and, unless FM_GUI_ONLY:
#   tools/lzxd_helper[.exe]       — LZX decompressor for bin.zip (native C)
#   tools/x360io/x360io[.exe]     — FM4 reader CLI (Nuitka --standalone dist)
#   tools/nadeo-freeporter[.exe]  — freeporter (importer + map composer), release asset
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
OUT_DIR="${1:-$(cd "$PROJECT_DIR/.." && pwd)/${PROJECT_NAME}-release}"
BACKEND="${2:-x11}"
BUILD_DIR="$PROJECT_DIR/build"
GUI_ONLY="${FM_GUI_ONLY:-0}"

NIM_BIN="${NIM:-nim}"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi
CC_BIN="${CC:-${cc:-gcc}}"

# --- host platform (for the native CLI steps) -----------------------------
case "$(uname -s)" in
    Linux*)                       PLATFORM=linux ;;
    MINGW*|MSYS*|CYGWIN*|Windows*) PLATFORM=windows ;;
    *) echo "error: unsupported build platform: $(uname -s)" >&2; exit 1 ;;
esac

# --- backend → nim flags + output name ------------------------------------
NIM_BACKEND_FLAGS=""
MAIN_BIN="forzamania"
case "$BACKEND" in
    x11)     NIM_BACKEND_FLAGS="" ;;
    wayland) NIM_BACKEND_FLAGS="-d:wayland" ;;
    windows) NIM_BACKEND_FLAGS="-d:mingw"; MAIN_BIN="forzamania.exe" ;;
    *) echo "error: unknown backend '$BACKEND' (want x11|wayland|windows)" >&2; exit 1 ;;
esac

echo "==> forzamania build (backend=$BACKEND, gui_only=$GUI_ONLY) -> $OUT_DIR"

# --- preflight ------------------------------------------------------------
command -v "$NIM_BIN" >/dev/null 2>&1 || {
    echo "error: nim not found. Install Nim (https://nim-lang.org) or set \$NIM." >&2; exit 1; }
[ -d "$PROJECT_DIR/vendor/wayluigi" ] && [ -d "$PROJECT_DIR/vendor/rawk-luigi" ] || {
    echo "error: vendor/wayluigi or vendor/rawk-luigi missing — run ./download-deps.sh first" >&2; exit 1; }

# A full Windows bundle needs a Windows host: Nuitka (x360io) and the
# freeporter asset picker both key off the host platform, so they can't be
# cross-produced from Linux. The Nim app itself cross-compiles fine, so the
# GUI-only Windows build is allowed from Linux.
if [ "$BACKEND" = windows ] && [ "$GUI_ONLY" != 1 ] && [ "$PLATFORM" != windows ]; then
    echo "error: --windows --with-tools must run on a Windows host (x360io/freeporter" >&2
    echo "       can't cross-compile from Linux). Use --windows alone for a GUI-only .exe," >&2
    echo "       or build the full Windows bundle in CI / on Windows." >&2
    exit 1
fi

# --- fresh out dir --------------------------------------------------------
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# --- 1. Nim app (the main binary) -----------------------------------------
# Built-in luigi bitmap font (-d:luigiNoFreetype) — no freetype dep. --app:gui
# gives the Windows build the GUI subsystem (no console window); harmless on
# Linux. -d:ssl links OpenSSL for the Settings → Download freeporter button
# (std/httpclient over HTTPS). config.nims wires the vendored rawk-luigi +
# wayluigi paths.
echo "[build] Nim app ($MAIN_BIN, $BACKEND)"
( cd "$PROJECT_DIR/app" && "$NIM_BIN" c --hints:off \
    -d:release -d:luigiNoFreetype -d:ssl --app:gui \
    $NIM_BACKEND_FLAGS \
    -o:"$OUT_DIR/$MAIN_BIN" \
    forzamania.nim )

if [ "$GUI_ONLY" = 1 ]; then
    [ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$OUT_DIR/" || true
    [ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$OUT_DIR/" || true
    echo "==> done (gui-only, $BACKEND): $OUT_DIR/$MAIN_BIN"
    exit 0
fi

# ====================== full bundle (--with-tools) ========================
mkdir -p "$OUT_DIR/tools"

# --- tool preflight -------------------------------------------------------
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

# --- 2. lzxd_helper (per-platform native C) -------------------------------
# Windows: fully static-link the mingw runtime so the helper has zero DLL deps.
# Linux: a plain ELF.
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
cp "$PROJECT_DIR/$HELPER_REL" "$OUT_DIR/tools/$HELPER_NAME"

# --- 3. Nuitka: x360io FM4 reader CLI -------------------------------------
# Copy patches/x360io_cli.py into the cloned Forza-X360-IO src so it sits beside
# forza_blender/ — vendor/ is gitignored and never hand-edited.
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
mkdir -p "$OUT_DIR/tools/x360io"
cp -a "$BUILD_DIR/x360io_cli.dist/." "$OUT_DIR/tools/x360io/"
[ "$PLATFORM" = linux ] && chmod +x "$OUT_DIR/tools/x360io/x360io" || true

# --- 4. bundle freeporter (per-platform release asset) --------------------
# Reuse the app's own verified downloader — picks the linux/windows asset by the
# build host's sys.platform, unzips into tools/, restores the ELF exec bit.
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

# --- 5. docs --------------------------------------------------------------
echo "Bundled: lzxd_helper (bin.zip LZX) + x360io/ (FM4 reader) + nadeo-freeporter (importer + map composer). Update freeporter via Settings → Download freeporter." > "$OUT_DIR/tools/README.txt"
[ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$OUT_DIR/" || true
[ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$OUT_DIR/" || true

echo "==> done ($BACKEND, full bundle): $OUT_DIR"
