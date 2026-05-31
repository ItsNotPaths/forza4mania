## Invoke nadeo-freeporter to convert FBX → .Item.Gbx (native, no Wine).
##
## Nim port of `src/nadeo_runner.py`. Two-step pipeline per item:
##   nadeo-freeporter mesh <fbx>   # FBX + .MeshParams.xml → .Mesh.Gbx + .Shape.Gbx
##   nadeo-freeporter item <fbx>   # FBX + .Item.xml + .MeshParams.xml → .Item.Gbx
## freeporter takes the FBX path for BOTH steps (resolves the sibling XMLs
## itself) and writes outputs next to the input FBX (PascalCase). We run it with
## cwd=<fbx dir> + basename so the same call works native and (legacy) under Wine
## with no path translation. The map step lives in map_composer (same binary).

import std/os
import subproc

when defined(windows):
  const freeporterName = "nadeo-freeporter.exe"
else:
  const freeporterName = "nadeo-freeporter"

type
  ImporterResult* = object
    kind*:        string   ## "mesh" or "item"
    returncode*:  int
    stdout*:      string   ## merged stdout+stderr (runCaptured merges them)
    stderr*:      string
    outputFiles*: seq[string]

proc ok*(r: ImporterResult): bool = r.returncode == 0

proc findFreeporter*(override = ""): string =
  ## Locate the nadeo-freeporter binary: override (Settings) → <appdir>/tools/
  ## <platform binary>. Raises if not found.
  if override.len > 0:
    if not fileExists(override):
      raise newException(IOError, "freeporter override does not exist: " & override)
    return override
  let cand = getAppDir() / "tools" / freeporterName
  if fileExists(cand): return cand
  raise newException(IOError,
    "nadeo-freeporter not found. Use the Download button in Settings, " &
    "or point at an existing binary.")

proc gbxSibling(fbx, suffix: string): string =
  ## fbx "<dir>/<stem>.fbx" → "<dir>/<stem><suffix>" (mirrors Path.with_suffix).
  let (dir, name, _) = splitFile(fbx)
  dir / (name & suffix)

proc runFreeporter(binary, mode, fbx: string): (int, string) =
  ## Run `<binary> <mode> <fbx basename>` with cwd = the FBX's dir; passing the
  ## basename (not an absolute path) keeps it path-translation-free.
  let res = runCaptured(binary, @[mode, extractFilename(fbx)], workingDir = parentDir(fbx))
  (res.rc, res.output)

proc runMesh*(freeporter, fbx: string): ImporterResult =
  ## `freeporter mesh <fbx>` — expects <stem>.MeshParams.xml next to the FBX;
  ## produces <stem>.Mesh.Gbx + <stem>.Shape.Gbx.
  let (rc, outp) = runFreeporter(freeporter, "mesh", fbx)
  var produced: seq[string]
  for p in [gbxSibling(fbx, ".Mesh.Gbx"), gbxSibling(fbx, ".Shape.Gbx")]:
    if fileExists(p): produced.add(p)
  ImporterResult(kind: "mesh", returncode: rc, stdout: outp, stderr: "",
                 outputFiles: produced)

proc runItem*(freeporter, fbx: string): ImporterResult =
  ## `freeporter item <fbx>` — expects <stem>.Mesh.Gbx (from runMesh) +
  ## <stem>.Item.xml next to the FBX; produces <stem>.Item.Gbx.
  let (rc, outp) = runFreeporter(freeporter, "item", fbx)
  let itemGbx = gbxSibling(fbx, ".Item.Gbx")
  var produced: seq[string]
  if fileExists(itemGbx): produced.add(itemGbx)
  ImporterResult(kind: "item", returncode: rc, stdout: outp, stderr: "",
                 outputFiles: produced)

proc convertChunk*(freeporter, fbx: string): (ImporterResult, ImporterResult) =
  ## End-to-end: mesh step then item step. Stops at item if mesh failed.
  let meshResult = runMesh(freeporter, fbx)
  if not meshResult.ok:
    return (meshResult, ImporterResult(kind: "item", returncode: -1,
            stdout: "", stderr: "(skipped: mesh step failed)", outputFiles: @[]))
  let itemResult = runItem(freeporter, fbx)
  (meshResult, itemResult)
