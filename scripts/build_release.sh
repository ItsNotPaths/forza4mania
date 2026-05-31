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
#   tools/nadeo-freeporter[.exe]  — freeporter (importer + map composer), release asset.
#                                   Bundled ALWAYS (cheap cached download), incl. the
#                                   FM_GUI_ONLY fast path, so even `release.sh --local`
#                                   ships a runnable importer like the public bundle.
# and, unless FM_GUI_ONLY (the slow Nuitka step is what GUI_ONLY actually skips):
#   tools/lzxd_helper[.exe]       — LZX decompressor for bin.zip (native C)
#   tools/x360io/x360io[.exe]     — FM4 reader CLI (Nuitka --standalone dist)
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

# --- freeporter bundling (shared by the GUI-only + full paths) ------------
# The freeporter release asset matches the BUILD HOST's platform (it's spawned
# in the same OS-ABI env), so we only bundle it when the target backend matches
# the host — a windows .exe can't be fetched on Linux. Cached under build/ so
# repeat (esp. GUI-only UI-iteration) builds don't re-download, and non-fatal if
# offline / rate-limited (users can still Download it from Settings at runtime).
bundle_freeporter() {
    local out_tools="$1"
    local target=linux; [ "$BACKEND" = windows ] && target=windows
    local fp_bin="nadeo-freeporter"; [ "$target" = windows ] && fp_bin="nadeo-freeporter.exe"
    if [ "$target" != "$PLATFORM" ]; then
        echo "[build] skipping freeporter bundle (target=$target, host=$PLATFORM — asset can't cross-fetch)"
        return 0
    fi
    local cache="$BUILD_DIR/freeporter-cache"
    if [ ! -f "$cache/$fp_bin" ]; then
        echo "[build] downloading nadeo-freeporter ($PLATFORM release asset)"
        mkdir -p "$cache"
        if ! PYTHONPATH="$PROJECT_DIR/scripts" "$PY" - "$cache" <<'PYEOF'
import sys
from pathlib import Path
from external_downloader import download_freeporter
res = download_freeporter(Path(sys.argv[1]))
print("[build] freeporter:", ", ".join(p.name for p in res.extracted_files))
PYEOF
        then
            echo "[build] WARNING: freeporter download failed (rate limit/offline?) — skipping; users can Download it in Settings." >&2
            return 0
        fi
    else
        echo "[build] nadeo-freeporter (cached: $cache/$fp_bin)"
    fi
    if [ -f "$cache/$fp_bin" ]; then
        mkdir -p "$out_tools"
        cp "$cache/$fp_bin" "$out_tools/$fp_bin"
        [ "$PLATFORM" = linux ] && chmod +x "$out_tools/$fp_bin" || true
        echo "[build] bundled $fp_bin → $out_tools"
    fi
}

# --- out dir --------------------------------------------------------------
# A full (--with-tools) build gets a fresh dir. The GUI-only fast path only
# OVERWRITES the app binary (and refreshes freeporter) — it must NOT nuke a
# previously bundled tools/ (x360io, lzxd_helper), so you can iterate on the UI
# without rebuilding the slow Nuitka CLIs.
if [ "$GUI_ONLY" = 1 ]; then
    mkdir -p "$OUT_DIR"
else
    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR"
fi

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

# freeporter is a cheap cached download — bundle it in EVERY path (incl. the
# GUI-only fast build) so `release.sh --local` ships a runnable importer, just
# like the public release packaging.
bundle_freeporter "$OUT_DIR/tools"

# blender_export.py runs INSIDE Blender during the FBX step; the orchestrator
# (pipeline.nim findBlenderExportScript) looks for it at <appdir>/scripts/. It's
# a tiny source file, so stage it in EVERY path (incl. GUI-only) — without it the
# pipeline fails at [3/5] with "blender_export.py not found".
mkdir -p "$OUT_DIR/scripts"
cp "$PROJECT_DIR/scripts/blender_export.py" "$OUT_DIR/scripts/blender_export.py"

if [ "$GUI_ONLY" = 1 ]; then
    [ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$OUT_DIR/" || true
    [ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$OUT_DIR/" || true
    echo "==> done (gui-only + freeporter, $BACKEND): $OUT_DIR/$MAIN_BIN"
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
# zstandard lets Nuitka compress the x360io onefile (~25MB vs ~84MB uncompressed).
# Non-fatal — the build still works, the binary's just bigger.
if ! "$PY" -c "import zstandard" >/dev/null 2>&1; then
    echo "[build] NOTE: 'zstandard' not installed for '$PY' — x360io onefile will be" >&2
    echo "       UNCOMPRESSED (~84MB vs ~25MB). For a smaller binary:" >&2
    echo "         $PY -m pip install zstandard      # or 'Nuitka[onefile]' (use a venv if PEP 668)" >&2
fi
if [ "$PLATFORM" = linux ] && ! command -v patchelf >/dev/null 2>&1; then
    echo "error: patchelf not found — Nuitka --standalone needs it on Linux." >&2
    echo "    sudo apt install patchelf   # or your distro's equivalent" >&2
    exit 1
fi

# --- 2. lzxd_helper (per-platform native C) -------------------------------
# Windows: fully static-link the mingw runtime so the helper has zero DLL deps.
# Linux: a plain ELF.
if [ "$PLATFORM" = windows ]; then
    HELPER_REL="scripts/lzxd_helper.exe"
    HELPER_CFLAGS="-O2 -Wall -static -static-libgcc"
else
    HELPER_REL="scripts/lzxd_helper"
    HELPER_CFLAGS="-O2 -Wall"
fi
HELPER_NAME="$(basename "$HELPER_REL")"
echo "[build] lzxd_helper ($HELPER_NAME)"
$CC_BIN $HELPER_CFLAGS -I"$MSPACK" \
    -o "$PROJECT_DIR/$HELPER_REL" \
    "$PROJECT_DIR/scripts/lzxd_helper.c" \
    "$MSPACK/lzxd.c" \
    "$MSPACK/system.c"
cp "$PROJECT_DIR/$HELPER_REL" "$OUT_DIR/tools/$HELPER_NAME"

# --- 3. Nuitka: x360io FM4 reader CLI (ONEFILE) ---------------------------
# Copy patches/x360io_cli.py into the cloned Forza-X360-IO src so it sits beside
# forza_blender/ — vendor/ is gitignored and never hand-edited.
#
# --onefile (was --standalone) ships ONE executable instead of a dist dir full
# of numpy/OpenBLAS .so spam. forza_blender is embedded via --include-data-dir
# and x360io_cli.py self-locates it (its __file__/_MEIPASS candidates cover the
# onefile extraction dir). --onefile-tempdir-spec pins a STABLE cache path so the
# payload extracts ONCE and is reused — no per-invocation re-extraction (x360io
# is spawned once per track read, so a volatile temp would re-unpack ~28MB each
# time). The runner (x360io.nim) already prefers tools/<bin> over tools/x360io/.
echo "[build] Nuitka onefile x360io reader CLI"
X360_SRC="$PROJECT_DIR/vendor/Forza-X360-IO/src"
cp "$PROJECT_DIR/patches/x360io_cli.py" "$X360_SRC/x360io_cli.py"
X360_OUT="x360io"; [ "$PLATFORM" = windows ] && X360_OUT="x360io.exe"
rm -f "$BUILD_DIR/$X360_OUT"
( cd "$X360_SRC" && "$PY" -m nuitka \
    --onefile \
    --assume-yes-for-downloads \
    --include-data-dir="$X360_SRC/forza_blender=forza_blender" \
    --onefile-tempdir-spec="{CACHE_DIR}/forzamania/x360io" \
    --output-filename="$X360_OUT" \
    --output-dir="$BUILD_DIR" \
    x360io_cli.py )
cp "$BUILD_DIR/$X360_OUT" "$OUT_DIR/tools/$X360_OUT"
[ "$PLATFORM" = linux ] && chmod +x "$OUT_DIR/tools/$X360_OUT" || true

# --- 4. freeporter already bundled above (bundle_freeporter, runs in every
#        path including GUI-only) -----------------------------------------

# --- 5. docs --------------------------------------------------------------
echo "Bundled: lzxd_helper (bin.zip LZX) + x360io/ (FM4 reader) + nadeo-freeporter (importer + map composer). Update freeporter via Settings → Download freeporter." > "$OUT_DIR/tools/README.txt"
[ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$OUT_DIR/" || true
[ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$OUT_DIR/" || true

echo "==> done ($BACKEND, full bundle): $OUT_DIR"
