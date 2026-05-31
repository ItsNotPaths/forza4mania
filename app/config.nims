# Build config for the forzamania Nim orchestrator+GUI.
#
# Vendored deps live at <repo>/vendor (gitignored, populated by
# ./download-deps.sh). app/ sits one level under the repo root, so we resolve
# both vendor paths relative to this file rather than hardcoding an absolute.
#
# Backend is picked at compile time:
#   (default)     X11      — links libX11 + freetype
#   -d:wayland    Wayland  — links wayland-client/cursor/xkbcommon + freetype
#   -d:mingw      Windows  — cross-compiles via x86_64-w64-mingw32-gcc, GDI fonts
import std/[os, strutils]

# Forward-slash host paths (see rawk_luigi.nim): cross-compiling to Windows
# (-d:mingw) otherwise turns the `/` operator into '\', which the Linux build
# host can't resolve. gcc/mingw + Nim's --path accept '/' everywhere.
proc hp(p: string): string = p.replace('\\', '/')

const repoRoot = hp(currentSourcePath()).parentDir.parentDir

switch("path", hp(repoRoot / "vendor" / "rawk-luigi" / "src"))
switch("define", "rawkLuigiVendor:" & hp(repoRoot / "vendor" / "wayluigi"))
