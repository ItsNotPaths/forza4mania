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
    echo "==> Local build: $PROJECT_NAME -> $RELEASE_DIR"

    MSPACK="$PROJECT_DIR/vendor/libmspack/libmspack/mspack"
    if [ ! -d "$MSPACK" ]; then
        echo "error: $MSPACK missing — run ./download-deps.sh first" >&2
        exit 1
    fi

    cc="${CC:-cc}"
    cflags="${CFLAGS:--O2 -Wall}"

    echo "[release] compiling lzxd_helper"
    $cc $cflags -I"$MSPACK" \
        -o "$PROJECT_DIR/src/lzxd_helper" \
        "$PROJECT_DIR/src/lzxd_helper.c" \
        "$MSPACK/lzxd.c" \
        "$MSPACK/system.c"

    rm -rf "$RELEASE_DIR"
    mkdir -p "$RELEASE_DIR"
    cp -r "$PROJECT_DIR/src" "$RELEASE_DIR/src"
    [ -f "$PROJECT_DIR/pyproject.toml" ] && cp "$PROJECT_DIR/pyproject.toml" "$RELEASE_DIR/" || true
    [ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$RELEASE_DIR/" || true
    [ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$RELEASE_DIR/" || true
    echo "==> Local done: $RELEASE_DIR"
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
