## Spatial chunker — split a TrackIR into TM2020-item-sized pieces.
##
## Nim port of `src/chunker.py`. TM2020 items soft-cap around 50–60k tris;
## the chunker buckets placed instances into world-space tiles, then greedily
## packs each tile into chunks ≤ a tri budget. A `MeshChunk` bundles the
## instances it owns + the (sorted, de-duplicated) mesh keys they reference;
## downstream stages (blender bridge, xml writers) consume one chunk at a time.
##
## Faithfulness notes vs the Python oracle:
##   * The instance world AABB transforms the 8 corners of the mesh's LOCAL
##     vertex AABB by the instance matrix and takes the bounds — we CANNOT use
##     the transform translation as the position (FM4 bakes big track geometry
##     into the vertices with an identity transform). See instanceWorldAabb.
##   * Math is done in float64 to mirror numpy promoting the float32 mesh/matrix
##     data to float64 in `corners @ R.T + t`. Products of two ≤24-bit-mantissa
##     float32 values are exact in float64; only the 3-term sum can differ from
##     a BLAS FMA path by ~1 float64 ULP — irrelevant to bucketing decisions.
##   * Bucket sort is by tri count descending and MUST be stable (Python's
##     list.sort is) — std/algorithm.sort is a stable merge sort.

import std/[tables, algorithm, math, strutils, sets]
import ir

const
  DEFAULT_TILE_M* = 64.0
  DEFAULT_TRI_BUDGET* = 50_000

  # FM4 cull volumes use one of these shaders, alpha-transparent or color-0 so
  # the player never sees them in FM4. In our TM2020 port they'd render as
  # opaque slabs (see chunker.py for the LeMans examples). The vert+extent
  # guards keep legit thin/long geometry (banners, liners) out of this net.
  CULL_VOLUME_SHADERS = ["diff_opac_2_nolm", "clr_0"]
  CULL_MAX_VERTS = 8
  CULL_MIN_EXTENT_M = 20.0

type
  Vec3d* = array[3, float64]  ## world-space AABB corners; float64 like numpy

  MeshChunk* = object
    ## One TM2020-item-worth of geometry + placements. `name` is a stable
    ## per-track id like "Alps_Tile_p03_p07_00" used to derive downstream
    ## filenames. `meshKeys` are the sorted TrackIR.meshes keys it references.
    name*:      string
    instances*: seq[MeshInstance]
    meshKeys*:  seq[int]
    triCount*:  int
    bboxMin*:   Vec3d
    bboxMax*:   Vec3d
    hasBbox*:   bool

# ---- per-instance helpers ------------------------------------------------

proc meshKeysForInstance(track: TrackIR; inst: MeshInstance): seq[int] =
  let base = inst.modelIndex shl 8
  for s in 0 ..< 256:
    let k = base or s
    if track.meshes.hasKey(k): result.add(k)

proc triCountForInstance(track: TrackIR; inst: MeshInstance): int =
  ## Sum tris across every section parsed for the referenced model.
  let base = inst.modelIndex shl 8
  for s in 0 ..< 256:
    let k = base or s
    if track.meshes.hasKey(k):
      result += track.meshes[k].faces.len

proc instanceWorldAabb(track: TrackIR; inst: MeshInstance;
                       keys: seq[int]): (Vec3d, Vec3d) =
  ## Real world-space AABB of a placed instance: bounds of the instance
  ## matrix applied to the 8 corners of each referenced mesh's local AABB.
  let m = inst.transform  # 4x4 row-major
  var lo: Vec3d = [Inf, Inf, Inf]
  var hi: Vec3d = [NegInf, NegInf, NegInf]
  var any = false
  for k in keys:
    if not track.meshes.hasKey(k): continue
    let mesh = track.meshes[k]
    if mesh.vertices.len == 0: continue
    any = true
    # local AABB (min/max select actual elements, so float32→float64 upcast
    # is exact and order-independent — identical to numpy's v.min/max).
    var mlo: Vec3d = [Inf, Inf, Inf]
    var mhi: Vec3d = [NegInf, NegInf, NegInf]
    for v in mesh.vertices:
      for j in 0 .. 2:
        let c = v[j].float64
        if c < mlo[j]: mlo[j] = c
        if c > mhi[j]: mhi[j] = c
    let cx = [mlo[0], mhi[0]]
    let cy = [mlo[1], mhi[1]]
    let cz = [mlo[2], mhi[2]]
    for ix in 0 .. 1:
      for iy in 0 .. 1:
        for iz in 0 .. 1:
          let c = [cx[ix], cy[iy], cz[iz]]
          for j in 0 .. 2:
            # world[j] = R[j]·c + t[j]  (R = m[:3,:3], t = m[:3,3])
            let w = m[j*4+0].float64 * c[0] +
                    m[j*4+1].float64 * c[1] +
                    m[j*4+2].float64 * c[2] +
                    m[j*4+3].float64
            if w < lo[j]: lo[j] = w
            if w > hi[j]: hi[j] = w
  if not any:
    # No usable mesh — degenerate; collapse to origin so it still buckets.
    return ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
  return (lo, hi)

# ---- drop sets (sky + cull volumes) --------------------------------------

proc skyboxMeshKeys(track: TrackIR): HashSet[int] =
  ## Mesh keys whose every material is a sky-family FM4 shader. FM4 builds the
  ## skydome as one huge inverted sphere (Alps: `sky_diff_1`); TM2020 supplies
  ## its own skybox, so we exclude it from every chunk.
  for key, mesh in track.meshes:
    if mesh.materials.len == 0: continue
    var allSky = true
    for mat in mesh.materials:
      if "sky_" notin mat.shaderName.toLowerAscii:
        allSky = false; break
    if allSky: result.incl(key)

proc cullVolumeMeshKeys(track: TrackIR): HashSet[int] =
  ## Mesh keys for FM4 invisible cull / occlusion volumes: a single low-poly
  ## quad/box (verts ≤ 8), huge AABB (≥ 20 m on some axis), using one of FM4's
  ## transparent-or-color-only shaders. The shader check is necessary — clr_0
  ## and diff_opac_2_nolm are also used for legit thin/long geometry, which the
  ## vert+extent guards exclude.
  for key, mesh in track.meshes:
    if mesh.materials.len == 0: continue
    let n = mesh.vertices.len
    if n == 0 or n > CULL_MAX_VERTS: continue
    var mlo: Vec3d = [Inf, Inf, Inf]
    var mhi: Vec3d = [NegInf, NegInf, NegInf]
    for v in mesh.vertices:
      for j in 0 .. 2:
        let c = v[j].float64
        if c < mlo[j]: mlo[j] = c
        if c > mhi[j]: mhi[j] = c
    var maxExtent = NegInf
    for j in 0 .. 2:
      let e = mhi[j] - mlo[j]
      if e > maxExtent: maxExtent = e
    if maxExtent < CULL_MIN_EXTENT_M: continue
    # Every material must be a cull-family shader: basename, lowercased, with
    # Python's `.rstrip(".fx")` (strips any trailing chars in {'.','f','x'}).
    var allCull = true
    for mat in mesh.materials:
      var s = mat.shaderName.replace("\\", "/")
      let idx = s.rfind("/")
      if idx >= 0: s = s[idx+1 .. ^1]
      s = s.toLowerAscii.strip(leading = false, trailing = true,
                               chars = {'.', 'f', 'x'})
      if s notin CULL_VOLUME_SHADERS:
        allCull = false; break
    if allCull: result.incl(key)

# ---- main entry ----------------------------------------------------------

type Entry = tuple
  inst: MeshInstance
  tris: int
  keys: seq[int]
  lo, hi: Vec3d

proc chunkTrack*(track: TrackIR;
                 tileSizeM = DEFAULT_TILE_M;
                 triBudget = DEFAULT_TRI_BUDGET): seq[MeshChunk] =
  ## Bucket a TrackIR's instances into chunks ≤ triBudget tris each. Mirrors
  ## chunk_track in src/chunker.py.
  let skyKeys = skyboxMeshKeys(track)
  let cullKeys = cullVolumeMeshKeys(track)
  var dropKeys = skyKeys
  dropKeys.incl(cullKeys)

  var keepable: seq[Entry]
  for inst in track.instances:
    let keys = meshKeysForInstance(track, inst)
    if keys.len == 0: continue
    # Drop instances whose every referenced mesh is sky OR a cull marker.
    var allDropped = true
    for k in keys:
      if k notin dropKeys: allDropped = false; break
    if allDropped: continue
    let tris = triCountForInstance(track, inst)
    let (lo, hi) = instanceWorldAabb(track, inst, keys)
    keepable.add((inst, tris, keys, lo, hi))

  var buckets = initTable[(int, int), seq[Entry]]()
  for e in keepable:
    # Bucket by the AABB CENTER on the XZ ground plane (Forza is Y-up).
    let cx = (e.lo[0] + e.hi[0]) * 0.5
    let cz = (e.lo[2] + e.hi[2]) * 0.5
    let bx = int(floor(cx / tileSizeM))
    let bz = int(floor(cz / tileSizeM))
    buckets.mgetOrPut((bx, bz), @[]).add(e)

  var sortedKeys: seq[(int, int)]
  for k in buckets.keys: sortedKeys.add(k)
  sortedKeys.sort()  # lexicographic (bx then bz), matches sorted(buckets)

  for (bx, bz) in sortedKeys:
    var bucket = buckets[(bx, bz)]
    # Pack heavies first; stable so equal-tri ties keep instance order.
    bucket.sort(proc(a, b: Entry): int = cmp(b.tris, a.tris))

    var curInst: seq[MeshInstance]
    var curKeys: HashSet[int]
    var curTris = 0
    var sub = 0
    var curLo, curHi: Vec3d
    var haveBox = false

    template doFlush() =
      if curInst.len > 0:
        let tileX = (if bx < 0: "n" else: "p") & intToStr(abs(bx), 3)
        let tileZ = (if bz < 0: "n" else: "p") & intToStr(abs(bz), 3)
        var ks: seq[int]
        for k in curKeys: ks.add(k)
        ks.sort()
        result.add(MeshChunk(
          name: track.trackName & "_Tile_" & tileX & "_" & tileZ & "_" &
                intToStr(sub, 2),
          instances: curInst,
          meshKeys: ks,
          triCount: curTris,
          bboxMin: curLo, bboxMax: curHi, hasBbox: haveBox,
        ))
        curInst = @[]
        curKeys = initHashSet[int]()
        curTris = 0
        haveBox = false
        inc sub

    for e in bucket:
      if curTris + e.tris > triBudget and curInst.len > 0:
        doFlush()
      curInst.add(e.inst)
      for k in e.keys: curKeys.incl(k)
      curTris += e.tris
      if not haveBox:
        curLo = e.lo; curHi = e.hi; haveBox = true
      else:
        for j in 0 .. 2:
          curLo[j] = min(curLo[j], e.lo[j])
          curHi[j] = max(curHi[j], e.hi[j])

    doFlush()
