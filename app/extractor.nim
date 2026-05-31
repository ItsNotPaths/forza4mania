## Extract a Forza method-21 bin.zip into <workingRoot>/extracted/<name>/bin/.
##
## Nim port of `src/fm4/extractor.py`. Idempotent: a `.extracted` sentinel short-
## circuits a re-extract. One lzxd daemon is spawned for the whole batch (amortised
## fork/exec over thousands of entries) and closed when the track is done.

import std/os
import binzip, lzx

const SENTINEL = ".extracted"

proc extractBinZip*(trackDir, workingRoot: string): string =
  ## Extract <trackDir>/bin.zip; return the bin/ dir holding the results.
  let srcZip = trackDir / "bin.zip"
  if not fileExists(srcZip):
    raise newException(IOError, "missing " & srcZip)

  let dstRoot = workingRoot / "extracted" / lastPathPart(trackDir) / "bin"
  if fileExists(dstRoot / SENTINEL):
    return dstRoot

  createDir(dstRoot)
  let entries = listEntries(srcZip)

  # No per-entry try/except: a decompression error here is almost always a
  # config issue (helper missing, libmspack mismatch) that hits every entry the
  # same way. Let the first failure surface a clear root cause rather than a
  # half-extracted bin/ that quietly yields "0 meshes" downstream.
  var daemon = startDaemon()
  try:
    for entry in entries:
      let outPath = dstRoot / entry.filename
      createDir(parentDir(outPath))
      writeFile(outPath, readEntry(srcZip, entry, daemon))
  finally:
    daemon.close()

  writeFile(dstRoot / SENTINEL, "")
  return dstRoot
