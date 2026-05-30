#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
RELEASE_DIR="$(cd "$PROJECT_DIR/.." && pwd)/${PROJECT_NAME}-release"

usage() {
    cat <<EOF
usage: $(basename "$0") [--local] [--public --version vX.Y.Z [--notes "text"] [--prerelease]]

  --local               build locally — produces ./src/lzxd_helper (Linux dev binary)
                        + a simple staging copy in ../<project>-release/ for inspection
  --public              trigger release.yml on GitHub via gh CLI (Windows .exe build)
  --version <tag>       required when --public is used (e.g. v0.1.0)
  --notes <text>        optional release notes (passed to GitHub release body)
  --prerelease          mark the release as a pre-release
EOF
}

DO_LOCAL=0
DO_PUBLIC=0
VERSION=""
NOTES=""
PRERELEASE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --local)      DO_LOCAL=1; shift ;;
        --public)     DO_PUBLIC=1; shift ;;
        --version)    VERSION="${2:-}"; shift 2 ;;
        --notes)      NOTES="${2:-}"; shift 2 ;;
        --prerelease) PRERELEASE=true; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "unknown flag: $1" >&2; usage; exit 1 ;;
    esac
done

if [ $DO_LOCAL -eq 0 ] && [ $DO_PUBLIC -eq 0 ]; then
    usage
    exit 1
fi

if [ $DO_LOCAL -eq 1 ]; then
    echo "==> Local native Linux build: $PROJECT_NAME -> $RELEASE_DIR"

    MSPACK="$PROJECT_DIR/vendor/libmspack/libmspack/mspack"
    if [ ! -d "$MSPACK" ]; then
        echo "error: $MSPACK missing — run ./download-deps.sh first" >&2
        exit 1
    fi
    if [ ! -d "$PROJECT_DIR/vendor/Forza-X360-IO/src/forza_blender" ]; then
        echo "error: vendor/Forza-X360-IO missing — run ./download-deps.sh first" >&2
        exit 1
    fi

    PY="${PYTHON:-python3}"

    # Nuitka (replaces PyInstaller for the native build) + patchelf (Nuitka's
    # --standalone needs it to rewrite RPATHs on Linux).
    if ! "$PY" -m nuitka --version >/dev/null 2>&1; then
        echo "error: Nuitka not installed. Install with:" >&2
        echo "    $PY -m pip install nuitka" >&2
        exit 1
    fi
    if ! command -v patchelf >/dev/null 2>&1; then
        echo "error: patchelf not found — Nuitka --standalone needs it on Linux. Install with:" >&2
        echo "    sudo apt install patchelf      # or your distro's equivalent" >&2
        exit 1
    fi

    # .NET SDK builds the map composer (blendermania-dotnet). Accept it on PATH
    # or at ~/.dotnet (where dotnet-install.sh puts it without sudo).
    DOTNET="${DOTNET:-$(command -v dotnet 2>/dev/null || echo "$HOME/.dotnet/dotnet")}"
    if [ ! -x "$DOTNET" ]; then
        echo "error: .NET SDK not found (needed to build blendermania-dotnet). Install with:" >&2
        echo "    curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 8.0" >&2
        exit 1
    fi
    if [ ! -d "$PROJECT_DIR/vendor/blendermania-dotnet" ]; then
        echo "error: vendor/blendermania-dotnet missing — run ./download-deps.sh first" >&2
        exit 1
    fi

    cc="${CC:-cc}"
    cflags="${CFLAGS:--O2 -Wall}"

    echo "[release] compiling lzxd_helper (native ELF)"
    $cc $cflags -I"$MSPACK" \
        -o "$PROJECT_DIR/src/lzxd_helper" \
        "$PROJECT_DIR/src/lzxd_helper.c" \
        "$MSPACK/lzxd.c" \
        "$MSPACK/system.c"

    BUILD_DIR="$PROJECT_DIR/build"
    MATERIALS_JSON="vendor/blendermania-addon/assets/materials/materials-map-trackmania2020_18122023.json"

    # Data-file mapping mirrors the PyInstaller --add-data/--add-binary set in
    # .github/workflows/release.yml. Nuitka places --include-data-* payloads
    # next to the binary; resources.bundle_root() resolves to that dir under
    # Nuitka, so the runtime lookups (lzx.helper_path, fm4/_vendor_setup, the
    # seed-map finder) find them at the same relative paths as in dev.
    echo "[release] Nuitka standalone build (first run is slow — C compile of numpy/tk)"
    rm -rf "$BUILD_DIR/main.dist"
    ( cd "$PROJECT_DIR" && "$PY" -m nuitka \
        --standalone \
        --assume-yes-for-downloads \
        --enable-plugin=tk-inter \
        --include-data-dir=vendor/Forza-X360-IO/src/forza_blender=vendor/Forza-X360-IO/src/forza_blender \
        --include-data-dir=assets=assets \
        --include-data-files=scripts/blender_export.py=scripts/blender_export.py \
        --include-data-files="$MATERIALS_JSON=$MATERIALS_JSON" \
        --include-data-files=src/lzxd_helper=lzxd_helper \
        --output-filename=forzamania \
        --output-dir="$BUILD_DIR" \
        src/main.py )

    # blendermania-dotnet: self-contained linux-x64 single file (bundles its own
    # .NET runtime, so the end user needs nothing installed). InvariantGlobalization
    # avoids a libicu dependency. There's no published Linux release binary for
    # it upstream, so we build it ourselves and bundle it.
    echo "[release] dotnet publish blendermania-dotnet (linux-x64, self-contained)"
    DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 "$DOTNET" publish \
        "$PROJECT_DIR/vendor/blendermania-dotnet/blendermania-dotnet/blendermania-dotnet.csproj" \
        -r linux-x64 -c Release -p:PublishSingleFile=true --self-contained true \
        -p:InvariantGlobalization=true \
        -o "$BUILD_DIR/dotnet-linux"

    # Nuitka names the standalone folder after the entry script (main.dist).
    rm -rf "$RELEASE_DIR"
    mkdir -p "$RELEASE_DIR"
    cp -a "$BUILD_DIR/main.dist/." "$RELEASE_DIR/"
    # tools/ is where the app's find_*() helpers look first.
    mkdir -p "$RELEASE_DIR/tools"
    cp "$BUILD_DIR/dotnet-linux/blendermania-dotnet" "$RELEASE_DIR/tools/blendermania-dotnet"
    chmod +x "$RELEASE_DIR/tools/blendermania-dotnet"
    echo "Map composer (blendermania-dotnet) is bundled here. Drop 'nadeo-freeporter' here too, or use Settings → Download freeporter." > "$RELEASE_DIR/tools/README.txt"
    [ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$RELEASE_DIR/" || true
    [ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$RELEASE_DIR/" || true
    echo "==> Local done: run $RELEASE_DIR/forzamania"
fi

if [ $DO_PUBLIC -eq 1 ]; then
    if [ -z "$VERSION" ]; then
        echo "error: --public requires --version <tag>" >&2
        exit 1
    fi
    if ! command -v gh >/dev/null 2>&1; then
        echo "error: gh CLI not found; install it and run 'gh auth login'" >&2
        exit 1
    fi
    REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || true)
    if [ -z "$REPO" ]; then
        echo "error: not in a github repo (or gh not authenticated)" >&2
        exit 1
    fi
    WORKFLOW="release.yml"
    echo "==> Triggering $WORKFLOW on $REPO ($VERSION, prerelease=$PRERELEASE)"
    OLD_ID=$(gh run list --workflow="$WORKFLOW" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
    gh workflow run "$WORKFLOW" \
        --field version="$VERSION" \
        --field notes="$NOTES" \
        --field prerelease="$PRERELEASE"
    echo "==> Waiting for run to register..."
    NEW_ID=""
    for i in $(seq 1 30); do
        sleep 2
        CUR_ID=$(gh run list --workflow="$WORKFLOW" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
        if [ -n "$CUR_ID" ] && [ "$CUR_ID" != "$OLD_ID" ]; then
            NEW_ID="$CUR_ID"
            break
        fi
    done
    if [ -z "$NEW_ID" ]; then
        echo "error: failed to detect new workflow run" >&2
        exit 1
    fi
    echo "==> Watching run $NEW_ID"
    gh run watch "$NEW_ID" --exit-status
fi
