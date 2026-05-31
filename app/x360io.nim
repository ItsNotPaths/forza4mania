## Spawn the x360io FM4 reader CLI and reconstruct a TrackIR.
##
## Nim port of `read_track` / `find_x360io` in `src/fm4/reader.py`. The heavy
## binary parsing lives in the x360io binary (Python/Nuitka, built from
## patches/x360io_cli.py); this module does the in-process bin.zip extraction
## (extractor.nim), spawns x360io to emit the .json manifest + .bin blob, then
## hands off to ir.nim's readManifest. No numpy/parser code on the Nim side.

import std/[os, strutils]
import ir, extractor, subproc

when defined(windows):
  const x360ioName = "x360io.exe"
else:
  const x360ioName = "x360io"

proc findX360io*(override = ""): seq[string] =
  ## Locate the x360io reader; return its argv prefix (mirrors find_x360io).
  ##   1. explicit override (Settings → x360io path)
  ##   2. <appdir>/tools/x360io[.exe]  or  <appdir>/tools/x360io/x360io[.exe]
  ##   3. dev fallback: [python3, <repo>/patches/x360io_cli.py]
  if override.len > 0:
    if not fileExists(override):
      raise newException(IOError, "x360io override does not exist: " & override)
    return @[override]

  let tools = getAppDir() / "tools"
  for cand in [tools / x360ioName, tools / "x360io" / x360ioName]:
    if fileExists(cand): return @[cand]

  # Dev fallback: run patches/x360io_cli.py interpreted. Locate the repo by
  # walking up from the cwd looking for patches/x360io_cli.py.
  var dir = getCurrentDir()
  while true:
    if fileExists(dir / "patches" / "x360io_cli.py"):
      let py = findExe("python3")
      if py.len == 0: raise newException(IOError, "python3 not found for x360io dev fallback")
      return @[py, dir / "patches" / "x360io_cli.py"]
    let parent = parentDir(dir)
    if parent == dir or parent.len == 0: break
    dir = parent

  raise newException(IOError,
    "x360io reader not found. Build it (release.sh --local stages tools/x360io), " &
    "set the path in Settings, or run from a source checkout with patches/x360io_cli.py.")

proc devVendorSrc*(): string =
  ## In a source checkout the dev fallback can't self-locate forza_blender, so
  ## we pass --vendor-src. Returns "" in a release bundle (binary self-locates).
  var dir = getCurrentDir()
  while true:
    let vsrc = dir / "vendor" / "Forza-X360-IO" / "src"
    if dirExists(vsrc / "forza_blender"): return vsrc
    let parent = parentDir(dir)
    if parent == dir or parent.len == 0: break
    dir = parent
  return ""

proc firstPvs(ribbonDir: string): string =
  for kind, p in walkDir(ribbonDir):
    if kind == pcFile and p.toLowerAscii.endsWith(".pvs"): return p
  raise newException(IOError, "no .pvs in " & ribbonDir)

proc readTrack*(trackDir, ribbonDir, workingRoot: string;
                x360ioOverride = ""): TrackIR =
  ## Parse an FM4 track + ribbon into a TrackIR: extract bin.zip, spawn x360io,
  ## reconstruct the IR. Raises if x360io is missing or fails.
  let binDir = extractBinZip(trackDir, workingRoot)
  let pvsPath = firstPvs(ribbonDir)

  let argv = findX360io(x360ioOverride)
  let pvsStem = splitFile(pvsPath).name
  let outJson = parentDir(binDir) / ("ir_" & pvsStem & ".json")

  var args = argv[1 .. ^1]
  args.add(["read", "--bin-dir", binDir, "--pvs", pvsPath,
            "--track-name", lastPathPart(trackDir), "--out", outJson])
  let vendorSrc = devVendorSrc()
  if vendorSrc.len > 0:
    args.add(["--vendor-src", vendorSrc])

  let res = runCaptured(argv[0], args)
  if res.rc != 0:
    raise newException(IOError,
      "x360io read failed (rc=" & $res.rc & "):\n" & res.output.strip())
  if not fileExists(outJson):
    raise newException(IOError,
      "x360io reported success but " & outJson & " is missing; output:\n" & res.output)

  return readManifest(outJson, lastPathPart(trackDir), binDir)
