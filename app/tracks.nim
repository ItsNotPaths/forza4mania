## FM4 track enumeration — Nim port of the _enumerate_tracks / _pick_first_ribbon
## helpers in src/ui/convert_tab.py. Pure filesystem logic, no GUI.

import std/[os, algorithm, strutils]

type TrackInfo* = object
  name*:   string   ## track folder name
  path*:   string   ## absolute path to the track dir
  ribbon*: string   ## chosen ribbon subdir name ("" = none)

proc pickFirstRibbon*(trackDir: string): string =
  ## Lowest-numbered Ribbon_NN subdir name. Ribbon_00 is the canonical forward
  ## configuration on every FM4 track checked. Returns "" if none.
  var ribbons: seq[string]
  for kind, p in walkDir(trackDir):
    if kind == pcDir:
      let n = extractFilename(p)
      if n.toLowerAscii.startsWith("ribbon"):
        ribbons.add(n)
  if ribbons.len == 0: return ""
  ribbons.sort()
  ribbons[0]

proc enumerateTracks*(fm4Dir: string): seq[TrackInfo] =
  ## Every track under <fm4>/tracks/: a subdir holding a bin.zip AND at least
  ## one Ribbon_NN. Sorted by name; partial/non-track dirs filtered out.
  result = @[]
  if fm4Dir.len == 0: return
  let tracksRoot = fm4Dir / "tracks"
  if not dirExists(tracksRoot): return
  var dirs: seq[string]
  for kind, p in walkDir(tracksRoot):
    if kind == pcDir: dirs.add(p)
  dirs.sort()
  for d in dirs:
    if not fileExists(d / "bin.zip"): continue
    let rib = pickFirstRibbon(d)
    if rib.len == 0: continue
    result.add(TrackInfo(name: extractFilename(d), path: d, ribbon: rib))
