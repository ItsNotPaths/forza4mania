## Compose a TM2020 .Map.Gbx from converted .Item.Gbx chunks.
##
## Nim port of `src/map_composer.py`. Drives nadeo-freeporter's `map` subcommand:
## write a JSON config (the `place-objects-on-map` payload) whose `MapPath` is
## the output map; freeporter generates the blank void base from its own embedded
## seed when that path doesn't exist, so we ship no seed and copy nothing.
##
## The valuable, hard-won part is the Blender→TM coordinate convention below —
## EMPIRICALLY VERIFIED, do not "fix" it. The negations + the +90° rotation
## cancel the X-mirror and 180° yaw that FORZA_TO_TRACKMANIA bakes into our
## content; a hand-modeled Blender item would use the addon's raw formula.

import std/[os, math, json]
import subproc

const
  RC_SUCCESS* = 0
  RC_CONFIG_ERROR* = 3

  # Blender → TM Position/Rotation. EMPIRICALLY VERIFIED via the user-confirmed
  # Alps_yaw_pos.Map.Gbx:  TM Position = (-Blender_Y, Blender_Z + 8, -Blender_X),
  # TM Rotation.X = +90° (unconditional). See module doc for why the negations
  # are required for OUR content (they cancel FORZA_TO_TRACKMANIA's X-mirror +
  # 180° yaw) — the addon's raw formula gave distance-dependent position errors.
  ITEM_POSITION_Y_LIFT* = 8.0
  DEFAULT_ITEM_ROTATION_X_RAD* = degToRad(90.0)

  BLOCK_GRID_M* = 32.0
  BLOCK_GRID_Y_OFFSET* = 9   # cells, per addon

  # TM2020 maps are positive-only, anchored at the (0,0,0) corner — anything at
  # negative X/Z is off-map and reported "missing". FM4 world coords span both
  # signs, so compose_map shifts the whole track positive before composing.
  POSITIVE_MARGIN_M* = 64.0
  GROUND_CLEARANCE_M* = 16.0

type
  Vec3f* = tuple[x, y, z: float64]

  ComposeResult* = object
    returncode*: int
    stdout*:     string
    stderr*:     string

  PlacedItem* = object
    ## One item placement. `name` MUST be the item path relative to
    ## <userdir>/Items/ with the .Item.Gbx extension (TM's library lookup key);
    ## `itemGbxPath` is the absolute path on disk.
    name*:        string
    itemGbxPath*: string
    position*:    Vec3f
    rotation*:    Vec3f

  GroundBlock* = object
    name*:     string
    dir*:      int
    position*: tuple[x, y, z: int]

proc ok*(r: ComposeResult): bool = r.returncode == RC_SUCCESS
proc explanation*(r: ComposeResult): string =
  case r.returncode
  of RC_SUCCESS: "ok"
  of RC_CONFIG_ERROR: "config not found or invalid JSON payload"
  else: "rc=" & $r.returncode

proc chunkToPlacedItem*(itemGbxPath, itemsRelPath: string;
                        blenderCenter: Vec3f): PlacedItem =
  ## Lift one converted chunk into a PlacedItem. `blenderCenter` is the chunk's
  ## bbox centre in RAW Blender world coords (post FORZA_TO_TRACKMANIA, pre
  ## centering — the value blender_export emits to the .center.json sidecar).
  let (bx, by, bz) = blenderCenter
  # TM Position = (-Blender_Y, Blender_Z + 8, -Blender_X)
  PlacedItem(
    name: itemsRelPath,
    itemGbxPath: itemGbxPath,
    position: (-by, bz + ITEM_POSITION_Y_LIFT, -bx),
    rotation: (DEFAULT_ITEM_ROTATION_X_RAD, 0.0, 0.0),
  )

proc computeGroundBlockGrid*(placedItems: seq[PlacedItem];
                             blockName = "StadiumPlatform";
                             marginCells = 2): seq[GroundBlock] =
  ## Flat 32 m grid of Stadium ground blocks covering the placements' bbox +
  ## margin. NOTE: dead in compose_map (blocks_payload is []), kept for API
  ## parity — `StadiumPlatform` isn't a valid TM2020 block name.
  if placedItems.len == 0: return @[]
  var xMin, zMin = Inf
  var xMax, zMax = NegInf
  for p in placedItems:
    xMin = min(xMin, p.position.x); xMax = max(xMax, p.position.x)
    zMin = min(zMin, p.position.z); zMax = max(zMax, p.position.z)
  let cxMin = int(floor(xMin / BLOCK_GRID_M)) - marginCells
  let cxMax = int(floor(xMax / BLOCK_GRID_M)) + marginCells
  let czMin = int(floor(zMin / BLOCK_GRID_M)) - marginCells
  let czMax = int(floor(zMax / BLOCK_GRID_M)) + marginCells
  for cx in cxMin .. cxMax:
    for cz in czMin .. czMax:
      result.add(GroundBlock(name: blockName, dir: 0,
                             position: (cx, BLOCK_GRID_Y_OFFSET, cz)))

proc vec3Json(v: Vec3f): JsonNode =
  ## Match the C# DotnetVector3 JSON shape.
  %*{"X": v.x, "Y": v.y, "Z": v.z}

proc buildComposeConfig*(outputMap: string; placedItems: seq[PlacedItem];
                         blockName = "StadiumPlatform";
                         configDir = ""): string =
  ## Shift the track into positive space, build the freeporter `map` payload,
  ## write the JSON config, and return its path. (Split out of composeMap so it
  ## can be exercised without spawning freeporter.)
  let cfgDir = if configDir.len > 0: configDir else: parentDir(outputMap)

  var offX, offY, offZ = 0.0
  if placedItems.len > 0:
    var minX, minY, minZ = Inf
    for it in placedItems:
      minX = min(minX, it.position.x)
      minY = min(minY, it.position.y)
      minZ = min(minZ, it.position.z)
    offX = POSITIVE_MARGIN_M - minX
    offY = GROUND_CLEARANCE_M - minY
    offZ = POSITIVE_MARGIN_M - minZ

  var itemsPayload = newJArray()
  for it in placedItems:
    let newPos: Vec3f = (it.position.x + offX, it.position.y + offY,
                         it.position.z + offZ)
    itemsPayload.add(%*{
      "Name": it.name,
      "Path": it.itemGbxPath,
      "Position": vec3Json(newPos),
      "Rotation": vec3Json(it.rotation),
      "Pivot": vec3Json((x: 0.0, y: 0.0, z: 0.0)),
      "AnimPhaseOffset": "None",
      "DifficultyColor": "Default",
      "LightmapQuality": "Normal",
    })

  # Block grid SKIPPED: StadiumPlatform isn't a valid TM2020 block name (the
  # helper rejects all of them). blockName kept for backward-compat callers.
  discard blockName
  let payload = %*{
    "MapPath": outputMap,
    "Blocks": newJArray(),
    "Items": itemsPayload,
    "ShouldOverwrite": true,
    "MapSuffix": "_modified",
    "CleanBlocks": true,
    "CleanItems": true,
    "Env": "Stadium2020",
  }

  let (_, stem, _) = splitFile(outputMap)
  let configPath = cfgDir / (stem & "_compose.json")
  createDir(parentDir(configPath))
  writeFile(configPath, pretty(payload, 2))
  return configPath

proc composeMap*(freeporter, outputMap: string; placedItems: seq[PlacedItem];
                 blockName = "StadiumPlatform"; configDir = ""): ComposeResult =
  ## Build a .Map.Gbx by stamping items into a fresh void base map. freeporter
  ## writes the composed map to the config's MapPath, generating the blank base
  ## from its embedded seed when absent — no seed file to ship or copy.
  let configPath = buildComposeConfig(outputMap, placedItems, blockName, configDir)
  createDir(parentDir(outputMap))
  let res = runCaptured(freeporter, @["map", configPath])
  ComposeResult(returncode: res.rc, stdout: res.output, stderr: "")
