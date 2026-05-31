## Map FM4 materials → TM2020 MeshParams.xml material entries.
##
## Nim port of `src/materials.py`. Classification is a heuristic on the FM4
## shader name: bucket each shader onto a stock TM2020 Stadium Link + a
## PhysicsId for collision feel. STOCK Links only — no Custom*/BaseTexture
## path (so `base_texture` is gone from the Nim port; it was always None and
## xml_writers never read it — the texture extractor is dropped entirely on
## the native path). The classifier table order matters: first match wins.
##
## NOTE vs the docstring in materials.py: the test is against the FULL
## lowercased shader name (`s = shader_name.lower()`), NOT just the stem —
## we match the code, not the comment.

import std/strutils

const
  # Default for unrecognized FM4 shaders — generic decoration / buildings.
  # Concrete (not NotCollidable) because most unknown FM4 geometry IS solid.
  DEFAULT_LINK* = "PlatformTech"
  DEFAULT_PHYSICS_ID* = "Concrete"

type
  ClassRule = tuple[needles: seq[string]; link, phys: string]

  TM2020Material* = object
    ## One row of the <Materials> block in MeshParams.xml. `name` is the
    ## per-chunk-unique material id written into the FBX; xml_writers binds
    ## Link + PhysicsId by it.
    name*:      string
    link*:      string
    physicsId*: string

# Order matters — first match wins; substring test on the lowercased shader
# name. Mirrors _CLASSIFIER in materials.py exactly (same order, same buckets).
const CLASSIFIER: seq[ClassRule] = @[
  (@["road_", "rdline_", "rddet_"],                   "PlatformTech", "Asphalt"),
  (@["rdedg_", "shldr_"],                             "RoadTech",     "Asphalt"),
  (@["barr_"],                                        "TrackWall",    "Metal"),
  (@["grass_", "lake_"],                              "Grass",        "Grass"),
  (@["sand_"],                                        "RoadDirt",     "Sand"),
  (@["tree_", "treebend", "bush_", "treecard_"],      "DecoHill",     "NotCollidable"),
  (@["sign_", "anim_flag", "anim_diff"],              "PlatformTech", "NotCollidable"),
  (@["_2sd", "_opac_"],                               "PlatformTech", "NotCollidable"),
]

proc classifyShader*(shaderName: string): (string, string) =
  ## Return (link, physicsId) for an FM4 shader name; falls back to the
  ## DEFAULT_LINK / DEFAULT_PHYSICS_ID when no pattern matches.
  let s = shaderName.toLowerAscii
  for rule in CLASSIFIER:
    for n in rule.needles:
      if n in s:
        return (rule.link, rule.phys)
  return (DEFAULT_LINK, DEFAULT_PHYSICS_ID)

proc safeName*(s: string): string =
  ## Sanitize an FM4 shader name for use as a material identifier. FM4 names
  ## look like "shaders\track\rdline_blnd_spec_opac_3.fx": strip directory +
  ## a trailing ".fx", replace space/dot with "_", truncate to 60.
  var base = s.replace("\\", "/")
  let idx = base.rfind("/")
  if idx >= 0: base = base[idx+1 .. ^1]
  if base.toLowerAscii.endsWith(".fx"): base = base[0 ..< base.len - 3]
  result = base.replace(" ", "_").replace(".", "_")
  if result.len > 60: result = result[0 ..< 60]

proc mapMaterial*(shaderName, chunkName: string; matIndex: int): TM2020Material =
  ## Build a TM2020Material for one FM4 material in one chunk. (The Python
  ## map_material took the whole FM4Material + unused texture_paths/chunk_dir;
  ## only the shader name is needed.)
  let (link, phys) = classifyShader(shaderName)
  TM2020Material(
    name: chunkName & "_" & intToStr(matIndex, 3) & "_" & safeName(shaderName),
    link: link,
    physicsId: phys,
  )
