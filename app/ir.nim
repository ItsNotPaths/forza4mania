## Intermediate representation for a parsed FM4 track + its reader.
##
## Nim port of `src/fm4/ir.py` (the dataclasses) and `_build_track_ir` in
## `src/fm4/reader.py` (the JSON+bin manifest consumer). The x360io CLI
## (`patches/x360io_cli.py`) parses an FM4 track and emits a glTF-style
## manifest: a `.json` describing each array as `{dtype, shape, offset,
## length}` plus a sibling `.bin` holding the concatenated canonical
## little-endian, C-order arrays. This module reads that pair back into a
## `TrackIR` the rest of the orchestrator consumes.
##
## No arraymancer — geometry is plain `seq`s of small fixed arrays. All build
## targets (Linux x86/x86_64, Windows x86_64) are little-endian, so the bin
## blob maps to host memory directly via `copyMem`; if a big-endian target is
## ever added this is the one place that would need byte-swapping.

import std/[json, tables, os]

# Must match patches/x360io_cli.py's _MANIFEST_VERSION (and src/fm4/reader.py).
const MANIFEST_VERSION* = 2

type
  Vec3* = array[3, float32]
  Vec2* = array[2, float32]
  ## Row-major 4x4, matching numpy's C-order flatten of an (4,4) array.
  Mat4* = array[16, float32]
  Tri*  = array[3, uint32]

  Material* = object
    shaderName*:            string
    textureSamplerIndices*: seq[int]
    pixelShaderConstants*:  seq[float32]

  MeshData* = object
    name*:            string
    vertices*:        seq[Vec3]
    uvs*:             seq[Vec2]   ## empty + hasUvs=false when the mesh had none
    hasUvs*:          bool
    normals*:         seq[Vec3]   ## empty + hasNormals=false when absent
    hasNormals*:      bool
    faces*:           seq[Tri]
    materialPerFace*: seq[uint32]
    materials*:       seq[Material]

  MeshInstance* = object
    modelIndex*:   int
    transform*:    Mat4
    textureIndex*: int
    flags*:        int

  TextureRef* = object
    fileIndex*:   int
    isStx*:       bool
    uScale*:      float32
    vScale*:      float32
    uTranslate*:  float32
    vTranslate*:  float32

  TrackIR* = object
    trackName*:    string
    prefix*:       string
    binDir*:       string   ## extracted working/extracted/<track>/bin/ path
    meshes*:       Table[int, MeshData]
    instances*:    seq[MeshInstance]
    textures*:     seq[TextureRef]
    shaderNames*:  seq[string]

# ---- array loaders -------------------------------------------------------
# Each reads one `{dtype, shape, offset, length}` descriptor out of the blob.
# `length` is in bytes; the element size is fixed by the destination type, so
# the count falls out of length div sizeof(T). We assert the dtype string so a
# schema drift (e.g. f4→f8) fails loudly rather than silently mis-striding.

proc descRange(desc: JsonNode): (int, int) =
  (desc["offset"].getInt, desc["length"].getInt)

proc expectDtype(desc: JsonNode; want: string) =
  let got = desc["dtype"].getStr
  if got != want:
    raise newException(ValueError,
      "x360io manifest dtype mismatch: got '" & got & "' expected '" & want & "'")

proc loadArr[T](blob: string; desc: JsonNode; dtype: string): seq[T] =
  expectDtype(desc, dtype)
  let (off, length) = descRange(desc)
  doAssert length mod sizeof(T) == 0,
    "x360io array length " & $length & " not a multiple of " & $sizeof(T)
  result = newSeq[T](length div sizeof(T))
  if length > 0:
    doAssert off + length <= blob.len, "x360io array runs past end of .bin blob"
    copyMem(addr result[0], unsafeAddr blob[off], length)

proc loadVec3(blob: string; desc: JsonNode): seq[Vec3] = loadArr[Vec3](blob, desc, "<f4")
proc loadVec2(blob: string; desc: JsonNode): seq[Vec2] = loadArr[Vec2](blob, desc, "<f4")
proc loadTri(blob: string;  desc: JsonNode): seq[Tri]  = loadArr[Tri](blob, desc, "<u4")
proc loadU32(blob: string;  desc: JsonNode): seq[uint32] = loadArr[uint32](blob, desc, "<u4")
proc loadMat4(blob: string; desc: JsonNode): seq[Mat4] = loadArr[Mat4](blob, desc, "<f4")

# ---- manifest reader -----------------------------------------------------

proc readManifest*(jsonPath: string; trackName, binDir: string): TrackIR =
  ## Reconstruct a TrackIR from the `.json` manifest + sibling `.bin` blob.
  ## Mirrors `_build_track_ir` in src/fm4/reader.py.
  let manifest = parseJson(readFile(jsonPath))
  let ver = manifest["version"].getInt
  if ver != MANIFEST_VERSION:
    raise newException(IOError,
      "x360io manifest version " & $ver & " != expected " & $MANIFEST_VERSION &
      " — rebuild the x360io binary (stale tools/x360io)")

  let blob = readFile(jsonPath.parentDir / manifest["bin"].getStr)

  result.trackName = trackName
  result.prefix = manifest["prefix"].getStr
  result.binDir = binDir
  result.meshes = initTable[int, MeshData]()

  for me in manifest["meshes"]:
    var mesh = MeshData(
      name: me["name"].getStr,
      vertices: loadVec3(blob, me["vertices"]),
      faces: loadTri(blob, me["faces"]),
      materialPerFace: loadU32(blob, me["material_per_face"]),
    )
    if me.hasKey("uvs"):
      mesh.uvs = loadVec2(blob, me["uvs"]); mesh.hasUvs = true
    if me.hasKey("normals"):
      mesh.normals = loadVec3(blob, me["normals"]); mesh.hasNormals = true
    for m in me["materials"]:
      var mat = Material(shaderName: m["shader_name"].getStr)
      for i in m["texture_sampler_indices"]: mat.textureSamplerIndices.add(i.getInt)
      for c in m["pixel_shader_constants"]: mat.pixelShaderConstants.add(c.getFloat.float32)
      mesh.materials.add(mat)
    result.meshes[me["key"].getInt] = mesh

  let transforms = loadMat4(blob, manifest["instance_transforms"])
  for i, inst in manifest["instances"].elems:
    result.instances.add(MeshInstance(
      modelIndex: inst["model_index"].getInt,
      transform: transforms[i],
      textureIndex: inst["texture_index"].getInt,
      flags: inst["flags"].getInt,
    ))

  for t in manifest["textures"]:
    result.textures.add(TextureRef(
      fileIndex: t["file_index"].getInt,
      isStx: t["is_stx"].getBool,
      uScale: t["u_scale"].getFloat.float32,
      vScale: t["v_scale"].getFloat.float32,
      uTranslate: t["u_translate"].getFloat.float32,
      vTranslate: t["v_translate"].getFloat.float32,
    ))

  for s in manifest["shader_names"]: result.shaderNames.add(s.getStr)
