## The export orchestrator — Nim port of `_pipeline` (+ the cross-prefix user-dir
## resolve and output copy) from `src/ui/convert_tab.py`. This is the glue that
## drives modules 4–9 end to end for one track: read FM4 → chunk → FBX (Blender)
## → freeporter mesh/item → compose .Map.Gbx → copy into TM2020's Proton prefix.
##
## Native-only simplifications vs the Python original (no Wine on this build):
##   * NO texture step — textures.py is dropped; mapMaterial needs only the
##     shader name, and stock TM Links resolve their own textures.
##   * NO Z:\ path translation / win32 branches — everything is a native path.
##   * The cross-prefix copy STAYS: TM2020 still runs under Proton, so its
##     Documents/Trackmania lives in a different compatdata prefix than our
##     working dir; we resolve it and copy Items/+Maps/ across at the end.
##
## Runs on the export worker thread; all output goes through the `log` callback
## (which marshals to the UI via the channel). blender_export.py is spawned and
## stays untouched (it runs inside Blender + writes the .center.json sidecar).

import std/[os, json, tables, sets, algorithm, strutils, times]
import settings, tracks
import x360io, chunker, materials, xml_writers, blender_bridge, nadeo_runner, map_composer

type LogProc = proc(s: string) {.gcsafe.}

proc samePath*(a, b: string): bool =
  ## True if a and b refer to the SAME directory/file — robust to trailing
  ## slashes, `.`/`..` and symlinks (device+inode), not just string equality.
  ## Critical for the cross-prefix copy guard: if outRoot == the resolved TM
  ## user dir (they differ only by a trailing slash in practice), a string `!=`
  ## wrongly triggers the copy, which then copyFile()s every output ONTO ITSELF
  ## and truncates it to 0 bytes.
  if a.len == 0 or b.len == 0: return false
  if a == b: return true
  try:
    return sameFile(a, b)               # device+inode — the authoritative check
  except OSError:
    return normalizedPath(absolutePath(a)) == normalizedPath(absolutePath(b))

# ---- blender_export.py discovery -----------------------------------------

proc findBlenderExportScript(): string =
  ## <appdir>/scripts/blender_export.py (release bundle) → walk up for the repo
  ## scripts/ dir (dev). Raises if neither is present.
  let bundled = getAppDir() / "scripts" / "blender_export.py"
  if fileExists(bundled): return bundled
  var dir = getCurrentDir()
  while true:
    let c = dir / "scripts" / "blender_export.py"
    if fileExists(c): return c
    let parent = parentDir(dir)
    if parent == dir or parent.len == 0: break
    dir = parent
  raise newException(IOError,
    "blender_export.py not found (expected <appdir>/scripts/ or a repo checkout)")

# ---- cross-prefix TM2020 user dir resolution -----------------------------

proc resolveTmUserDir*(cfg: Settings): string =
  ## Resolve TM2020's Documents/Trackmania. TM2020 runs under Proton even when
  ## forzamania is native, so its user dir lives inside a Steam compatdata
  ## prefix, NOT in $HOME. Walk tm_install up to steamapps/, scan
  ## compatdata/*/pfx/drive_c/users/steamuser/Documents/Trackmania for one with
  ## a real Maps/ dir (most-recently-modified wins). Fall back to Nadeo.ini's
  ## {userdocs}. Returns "" if nothing resolves. (Native: no Z:\ translation.)
  let tmInstall = cfg.tmInstallDir.replace("\\", "/")
  if tmInstall.len == 0: return ""

  # Find the steamapps ancestor.
  var dir = tmInstall
  var steamapps = ""
  while dir.len > 1:
    if extractFilename(dir) == "steamapps": steamapps = dir; break
    let parent = parentDir(dir)
    if parent == dir or parent.len == 0: break
    dir = parent

  if steamapps.len > 0:
    let compatdata = steamapps / "compatdata"
    if dirExists(compatdata):
      var best = ""
      var bestMtime: int64 = -1
      for kind, entry in walkDir(compatdata):
        if kind != pcDir: continue
        let tmUser = entry / "pfx" / "drive_c" / "users" / "steamuser" /
                     "Documents" / "Trackmania"
        if dirExists(tmUser / "Maps"):
          let mt = getLastModificationTime(tmUser).toUnix
          # most recent wins; ties broken by higher path (matches Python's
          # reverse sort on (mtime, path)).
          if mt > bestMtime or (mt == bestMtime and tmUser > best):
            bestMtime = mt; best = tmUser
      if best.len > 0: return best

  # Nadeo.ini fallback (single-prefix / dev).
  let iniPath = tmInstall / "Nadeo.ini"
  if fileExists(iniPath):
    for line in lines(iniPath):
      let l = line.strip()
      if not l.toLowerAscii.startsWith("userdir"): continue
      let idx = l.find('=')
      if idx < 0: continue
      var raw = l[idx+1 .. ^1].strip()
      raw = raw.replace("{userdocs}", getHomeDir() / "Documents")
      return raw.replace("\\", "/")
  return ""

# ---- cross-prefix output copy --------------------------------------------

const CANON_GBX = [(".item.gbx", ".Item.Gbx"),
                   (".mesh.gbx", ".Mesh.Gbx"),
                   (".shape.gbx", ".Shape.Gbx")]

proc copyOutputsToTmPrefix(itemsRoot, outMap, tmUserDir, trackName: string;
                           log: LogProc) =
  ## Mirror Items/Forzamania/<track>/ + Maps/Forzamania/<track>.Map.Gbx from our
  ## working dir into TM2020's prefix, normalizing every .Gbx suffix to canonical
  ## PascalCase (Linux ext4 lookup is case-sensitive — any drift = "missing item").
  log("[copy] copying outputs to TM2020 prefix...")
  let dstItems = tmUserDir / "Items" / "Forzamania" / trackName
  let dstMaps = tmUserDir / "Maps" / "Forzamania"
  try:
    createDir(dstItems); createDir(dstMaps)
  except OSError as e:
    log("[!] mkdir on TM prefix failed: " & e.msg); return

  var copied = 0
  for kind, src in walkDir(itemsRoot):
    if kind != pcFile: continue
    let nm = extractFilename(src)
    let low = nm.toLowerAscii
    var normalized = ""
    for (suf, canon) in CANON_GBX:
      if low.endsWith(suf):
        normalized = nm[0 ..< nm.len - suf.len] & canon; break
    if normalized.len == 0: continue
    let dst = dstItems / normalized
    if samePath(src, dst): continue   # already in place — never copyFile onto self (would 0-truncate)
    try:
      copyFile(src, dst); inc copied
    except OSError as e:
      log("[!] copy " & nm & " failed: " & e.msg)

  let dstMap = dstMaps / extractFilename(outMap)
  if samePath(outMap, dstMap):
    log("      map already in place (" & dstMap & ")")
    return
  try:
    copyFile(outMap, dstMap)
    log("      copied " & $copied & " item files + map → " & tmUserDir)
    log("      open in TM2020: My Local Maps → Forzamania → " & trackName)
  except OSError as e:
    log("[!] copy " & extractFilename(outMap) & " failed: " & e.msg)

# ---- helpers -------------------------------------------------------------

proc sibling(fbx, suffix: string): string =
  let (dir, name, _) = splitFile(fbx)
  dir / (name & suffix)

proc readCenter(path: string): Vec3f =
  let j = parseJson(readFile(path))
  let c = j["center"]
  (c[0].getFloat, c[1].getFloat, c[2].getFloat)

# ---- the pipeline --------------------------------------------------------

proc runPipeline*(track: TrackInfo; cfg: Settings; stopAtFbx: bool; log: LogProc) =
  ## Convert one FM4 track to a TM2020 .Map.Gbx. Raises only on unexpected
  ## errors; expected failures (no blender, no importer, per-chunk export
  ## failures) are logged and the pipeline returns/continues gracefully.
  let trackDir = track.path
  let ribbonDir = trackDir / track.ribbon

  # Single native user dir: work straight in the TM2020 user dir if known, else
  # a local out dir. (No separate Wine prefix for forzamania anymore.)
  let outRoot = if cfg.tmUserDir.len > 0: cfg.tmUserDir
                else: getHomeDir() / "forzamania-out"
  let workRoot = outRoot / "Work"
  let workItemsRoot = workRoot / "Items" / "Forzamania" / track.name
  let itemsRoot = outRoot / "Items" / "Forzamania" / track.name
  createDir(workItemsRoot)
  createDir(itemsRoot)

  let tmUserDir = resolveTmUserDir(cfg)
  log("      working dir: " & outRoot)
  if tmUserDir.len > 0 and not samePath(tmUserDir, outRoot):
    log("      will copy outputs to TM2020 prefix: " & tmUserDir)

  # [1/5] read FM4 ---------------------------------------------------------
  log("[1/5] reading track: " & trackDir & "/" & track.ribbon)
  let ir = readTrack(trackDir, ribbonDir, workRoot, cfg.x360ioPath)
  log("      meshes=" & $ir.meshes.len & " instances=" & $ir.instances.len &
      " textures=" & $ir.textures.len)

  # [2/5] chunk ------------------------------------------------------------
  log("[2/5] chunking...")
  var chunks: seq[MeshChunk]
  var maxTri = 0
  for c in chunkTrack(ir, cfg.tileSizeM, cfg.triBudget):
    if c.triCount > 0:
      chunks.add(c)
      if c.triCount > maxTri: maxTri = c.triCount
  log("      " & $chunks.len & " chunks (max tri=" & $maxTri & ")")

  # [3/5] FBX + XML --------------------------------------------------------
  log("[3/5] FBX + XML for " & $chunks.len & " chunks → " & workItemsRoot)
  var blender: string
  try:
    blender = findBlender(cfg.blenderPath)
  except IOError as e:
    log("[!] " & e.msg); return
  log("      blender: " & blender)
  let exportScript = findBlenderExportScript()

  var fbxChunks: seq[tuple[chunk: MeshChunk, fbx: string]]
  var firstExportFailLogged = false
  for i, chunk in chunks:
    log("      [" & $(i+1) & "/" & $chunks.len & "] " & chunk.name &
        "  tri=" & $chunk.triCount)
    var mats = initTable[(int, int), TM2020Material]()
    for mk in chunk.meshKeys:
      let mesh = ir.meshes[mk]
      for j in 0 ..< mesh.materials.len:
        mats[(mk, j)] = mapMaterial(mesh.materials[j].shaderName, chunk.name, j)

    let fbxPath = workItemsRoot / (chunk.name & ".fbx")
    let jsonPath = workItemsRoot / (chunk.name & ".chunk.json")
    discard dumpChunk(chunk, ir, mats, fbxPath, jsonPath)
    try:
      exportChunkToFbx(jsonPath, blender, exportScript)
    except CatchableError as e:
      log("      [!] export failed: " & e.msg)
      if not firstExportFailLogged: firstExportFailLogged = true
      continue
    fbxChunks.add((chunk, fbxPath))

    # Dedup materials by name (identical shaders collapse), sort, write XML.
    var seen = initHashSet[string]()
    var tmMats: seq[TM2020Material]
    for v in mats.values:
      if v.name in seen: continue
      seen.incl(v.name); tmMats.add(v)
    tmMats.sort(proc(a, b: TM2020Material): int = cmp(a.name, b.name))
    discard writeMeshParams(fbxPath, tmMats)
    discard writeItemXml(fbxPath)

  if stopAtFbx:
    log("[done] stopped at FBX. " & $fbxChunks.len & " chunks ready in " & itemsRoot)
    return

  # [4/5] freeporter mesh/item --------------------------------------------
  log("[4/5] running nadeo-freeporter on each chunk...")
  var importer: string
  try:
    importer = findFreeporter(cfg.nadeoImporterPath)
  except IOError as e:
    log("[!] " & e.msg & "  — pipeline stops at FBX. Use Settings → Download freeporter.")
    return
  log("      importer: " & importer)

  var convertedItems: seq[tuple[chunk: MeshChunk, itemGbx: string]]
  var firstFailLogged = false
  for fc in fbxChunks:
    let (meshRes, itemRes) = convertChunk(importer, fc.fbx)
    if not meshRes.ok:
      log("      [!] " & extractFilename(fc.fbx) & " mesh step failed (rc=" &
          $meshRes.returncode & "): " & meshRes.stdout.strip()[0 ..< min(200, meshRes.stdout.strip().len)])
      if not firstFailLogged:
        for ln in meshRes.stdout.splitLines()[0 ..< min(40, meshRes.stdout.splitLines().len)]:
          log("      " & ln)
        firstFailLogged = true
      continue
    if not itemRes.ok:
      log("      [!] " & extractFilename(fc.fbx) & " item step failed (rc=" &
          $itemRes.returncode & "): " & itemRes.stdout.strip()[0 ..< min(200, itemRes.stdout.strip().len)])
      continue

    let itemSrc = sibling(fc.fbx, ".Item.Gbx")
    if not fileExists(itemSrc):
      log("      [!] " & extractFilename(fc.fbx) & ": rc=0 from both steps but no .Item.Gbx")
      continue

    # Move Item/Mesh/Shape.Gbx from Work/ into items_root.
    var movedOk = true
    var itemDst = ""
    for ext in [".Item.Gbx", ".Mesh.Gbx", ".Shape.Gbx"]:
      let src = sibling(fc.fbx, ext)
      if not fileExists(src): continue
      let dst = itemsRoot / extractFilename(src)
      if src == dst: continue
      try:
        if fileExists(dst): removeFile(dst)
        moveFile(src, dst)
        if ext == ".Item.Gbx": itemDst = dst
      except OSError as e:
        log("      [!] move " & extractFilename(src) & " failed: " & e.msg)
        if ext == ".Item.Gbx": movedOk = false
    if not movedOk or itemDst.len == 0: continue
    convertedItems.add((fc.chunk, itemDst))

  log("      " & $convertedItems.len & " of " & $fbxChunks.len & " items converted")
  if convertedItems.len == 0:
    log("[!] no items converted; skipping map composition"); return

  # [5/5] compose map ------------------------------------------------------
  log("[5/5] composing .Map.Gbx...")
  let mapsRoot = outRoot / "Maps" / "Forzamania"
  createDir(mapsRoot)
  let outMap = mapsRoot / (track.name & ".Map.Gbx")

  var placed: seq[PlacedItem]
  for ci in convertedItems:
    let itemsRel = "Forzamania/" & track.name & "/" & extractFilename(ci.itemGbx)
    let centerPath = workItemsRoot / (ci.chunk.name & ".center.json")
    var center: Vec3f = (0.0, 0.0, 0.0)
    try:
      center = readCenter(centerPath)
    except CatchableError:
      log("      [!] " & ci.chunk.name & ": no center sidecar, placing at origin")
    placed.add(chunkToPlacedItem(ci.itemGbx, itemsRel, center))

  let res = composeMap(importer, outMap, placed)
  if res.ok:
    log("[done] map written: " & outMap)
  else:
    log("[!] map compose failed (" & res.explanation & ", rc=" & $res.returncode & ")")
    for ln in res.stdout.splitLines()[0 ..< min(40, res.stdout.splitLines().len)]:
      log("      " & ln)
    return

  # Cross-prefix copy ------------------------------------------------------
  if tmUserDir.len > 0 and not samePath(tmUserDir, outRoot):
    copyOutputsToTmPrefix(itemsRoot, outMap, tmUserDir, track.name, log)
