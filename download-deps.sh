#!/usr/bin/env bash
# Fetches third-party deps into vendor/. Run once before building.
set -euo pipefail

VENDOR="$(cd "$(dirname "$0")" && pwd)/vendor"

fetch() {
    local name="$1"
    local url="$2"
    local dest="$3"
    local strip="${4:-1}"
    local filter="${5:-}"

    if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        echo "  already present: $(basename "$dest")"
        return
    fi

    echo "  downloading $name..."
    mkdir -p "$dest"
    if [ -n "$filter" ]; then
        curl -fsSL "$url" | tar xz --strip-components="$strip" -C "$dest" --wildcards "$filter"
    else
        curl -fsSL "$url" | tar xz --strip-components="$strip" -C "$dest"
    fi
    echo "  done."
}

echo "==> Forza-X360-IO"
if [ -d "$VENDOR/Forza-X360-IO" ] && [ -n "$(ls -A "$VENDOR/Forza-X360-IO" 2>/dev/null)" ]; then
    echo "  already present: Forza-X360-IO"
else
    echo "  cloning Forza-X360-IO..."
    git clone --depth=1 "https://github.com/austinbaccus/Forza-X360-IO.git" "$VENDOR/Forza-X360-IO"
    echo "  done."
fi

echo "==> libmspack"
if [ -d "$VENDOR/libmspack" ] && [ -n "$(ls -A "$VENDOR/libmspack" 2>/dev/null)" ]; then
    echo "  already present: libmspack"
else
    echo "  cloning libmspack..."
    git clone --depth=1 "https://github.com/kyz/libmspack.git" "$VENDOR/libmspack"
    echo "  done."
fi

echo "==> wayluigi"
if [ -d "$VENDOR/wayluigi" ] && [ -n "$(ls -A "$VENDOR/wayluigi" 2>/dev/null)" ]; then
    echo "  already present: wayluigi"
else
    echo "  cloning wayluigi..."
    git clone --depth=1 "https://github.com/ItsNotPaths/wayluigi.git" "$VENDOR/wayluigi"
    echo "  done."
fi

echo "==> rawk-luigi (nim bindings)"
if [ -d "$VENDOR/rawk-luigi" ] && [ -n "$(ls -A "$VENDOR/rawk-luigi" 2>/dev/null)" ]; then
    echo "  already present: rawk-luigi"
else
    echo "  cloning rawk-luigi..."
    git clone --depth=1 "https://github.com/ItsNotPaths/rawk-luigi.git" "$VENDOR/rawk-luigi"
    echo "  done."
fi

echo ""
echo "All deps ready."
