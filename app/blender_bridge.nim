## Drive headless Blender to convert a MeshChunk → .fbx.
##
## Nim port of `src/blender_bridge.py`. We never import bpy: marshal the chunk's
## geometry to JSON, spawn `blender --background --factory-startup --python
## scripts/blender_export.py -- <chunk.json>`, and let Blender's stock FBX
## exporter do the work (blender_export.py runs INSIDE Blender, untouched). That
## keeps the orchestrator independent of Blender's Python.
##
## The Wine `.exe`-vs-ELF guard from the Python version is dropped — the native
## build never runs under Wine. Blender location reuses settings.detectBlender
## (the cross-platform Steam-library detector), with a Settings override on top.

import std/[os, json, tables, strutils, sets]
import ir, chunker, materials, subproc, settings

proc findBlender*(override = ""): string =
  ## Locate the Blender binary: override (Settings) → settings.detectBlender
  ## (Steam libs / Program Files / Unix dirs / PATH). Raises if none found.
  if override.len > 0:
    if not fileExists(override):
      raise newException(IOError, "blender override path does not exist: " & override)
    return override
  let found = detectBlender()
  if found.len == 0:
    raise newException(IOError,
      "Blender not found. Set the override path in Settings or install Blender.")
  return found

proc dumpChunk*(chunk: MeshChunk; track: TrackIR;
                materials: Table[(int, int), TM2020Material];
                outFbx, outJson: string): string =
  ## Serialize the chunk's geometry + material names + instance transforms to
  ## the JSON that blender_export.py consumes; return the JSON path. Mirrors
  ## dump_chunk in src/blender_bridge.py byte-semantically (floats round-trip).
  var chunkKeySet = initHashSet[int]()
  for k in chunk.meshKeys: chunkKeySet.incl(k)

  var meshesPayload = newJObject()
  for mk in chunk.meshKeys:
    if not track.meshes.hasKey(mk): continue
    let m = track.meshes[mk]

    var verts = newJArray()
    for v in m.vertices:
      verts.add(%*[v[0].float, v[1].float, v[2].float])
    var faces = newJArray()
    for fc in m.faces:
      faces.add(%*[fc[0].int, fc[1].int, fc[2].int])
    var uvs: JsonNode
    if m.hasUvs:
      uvs = newJArray()
      for u in m.uvs: uvs.add(%*[u[0].float, u[1].float])
    else:
      uvs = newJNull()
    var mpf = newJArray()
    for x in m.materialPerFace: mpf.add(%(x.int))

    var names = newJArray()
    for matIdx in 0 ..< m.materials.len:
      if materials.hasKey((mk, matIdx)):
        names.add(%materials[(mk, matIdx)].name)
      else:
        names.add(% ("mat_" & toHex(mk, 8).toLowerAscii & "_" & intToStr(matIdx, 3)))

    meshesPayload[$mk] = %*{
      "verts": verts,
      "faces": faces,
      "uvs": uvs,
      "material_per_face": mpf,
      "material_names": names,
    }

  var instancesPayload = newJArray()
  for inst in chunk.instances:
    let base = inst.modelIndex shl 8
    var keys: seq[int]
    for s in 0 ..< 256:
      let k = base or s
      if track.meshes.hasKey(k) and k in chunkKeySet: keys.add(k)
    if keys.len == 0: continue
    # Emit as a NESTED 4x4 (row-major), matching numpy's (4,4).tolist() — this
    # is what blender_export.py's `Matrix(inst["transform"])` expects. Mat4 is
    # the row-major flat 16, so row r is elements [r*4 .. r*4+3].
    var tf = newJArray()
    for r in 0 ..< 4:
      var row = newJArray()
      for c in 0 ..< 4: row.add(%(inst.transform[r*4 + c].float))
      tf.add(row)
    var mkeys = newJArray()
    for k in keys: mkeys.add(% $k)
    instancesPayload.add(%*{"transform": tf, "mesh_keys": mkeys})

  let payload = %*{
    "out_fbx": outFbx,
    "meshes": meshesPayload,
    "instances": instancesPayload,
  }
  createDir(parentDir(outJson))
  writeFile(outJson, $payload)
  return outJson

proc exportChunkToFbx*(chunkJson, blenderPath, blenderExportScript: string) =
  ## Spawn headless Blender to consume chunkJson and write the FBX. Raises on a
  ## non-zero exit; warns (no raise) if Blender exits 0 without the OK sentinel.
  let args = @["--background", "--factory-startup",
               "--python", blenderExportScript, "--", chunkJson]
  let res = runCaptured(blenderPath, args)
  if res.rc != 0:
    raise newException(IOError,
      "blender export failed (rc=" & $res.rc & "):\n" & res.output)
  if "OK:" notin res.output:
    stderr.writeLine("warning: blender exited 0 but no OK sentinel found")
    stderr.writeLine(res.output)
