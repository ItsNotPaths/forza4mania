"""Compose a TM2020 .Map.Gbx from converted .Item.Gbx chunks.

Wraps the dotnet helper's ``place-objects-on-map`` command. The helper
needs an existing seed map (any valid .Map.Gbx works as a starting point);
we ship a tiny `assets/empty_stadium.Map.Gbx` for that purpose. It also
needs a 32 m grid of Stadium ground blocks under the items so the player
has somewhere to start before reaching our scenery.

JSON schema mirrors vendor/blendermania-addon/utils/Dotnet.py:150
``DotnetPlaceObjectsOnMap`` exactly so we can fall back to using the
addon's own helper builds if ours ever diverges.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from chunker import MeshChunk
from dotnet_runner import (
    CMD_PLACE_OBJECTS_ON_MAP,
    DotnetResult,
    run_dotnet_command,
    write_json_config,
)


# Per addon MapObjects.py:218,230 — Y is the up axis in TM, Forza is Y-up too,
# but the addon swaps Y/Z and offsets +8 to lift items off the ground (block
# heights are in 8 m vertical units; +8 = one cell above the floor).
ITEM_POSITION_Y_OFFSET = 8.0
BLOCK_GRID_M = 32.0
BLOCK_GRID_Y_OFFSET = 9  # cells, per addon

# TM2020 maps are a positive-only coordinate space anchored at the (0,0,0)
# corner — anything at negative X/Z is literally off the map and TM reports
# the item as "missing". FM4 world coords span both signs, so we shift the
# whole track into positive space before composing. Margins keep the track
# clear of the very edge.
POSITIVE_MARGIN_M = 64.0       # X/Z breathing room from the (0,0) corner
GROUND_CLEARANCE_M = 16.0      # lift so the lowest geometry sits above floor


@dataclass
class PlacedItem:
    """One item placement in the composed map.

    `name` MUST be the path of the item relative to ``<userdir>/Items/``,
    including subdirs and the ``.Item.Gbx`` extension. Example:
    ``"Forzamania/Alps/Alps_Tile_n010_p001_00.Item.Gbx"``. TM2020 uses
    this string as the lookup key against its item library — passing just
    the stem produces "missing item" errors at map load.

    See vendor/blendermania-addon/utils/MapObjects.py:202-211 for the
    canonical convention.

    `item_gbx_path` is the absolute path on disk (the dotnet helper uses
    it for the initial item ingestion).
    """
    name: str
    item_gbx_path: Path
    position_xyz: tuple[float, float, float]
    rotation_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)


def _to_dotnet_vector3(xyz: tuple[float, float, float]) -> dict:
    """Match the C# DotnetVector3 JSON shape."""
    return {"X": float(xyz[0]), "Y": float(xyz[1]), "Z": float(xyz[2])}


def _to_dotnet_int3(xyz: tuple[int, int, int]) -> dict:
    return {"X": int(xyz[0]), "Y": int(xyz[1]), "Z": int(xyz[2])}


def chunk_to_placed_item(
    item_gbx_path: Path,
    items_rel_path: str,
    center_xyz: tuple[float, float, float],
) -> PlacedItem:
    """Lift one converted chunk into a PlacedItem.

    `items_rel_path` is the item's path relative to ``<userdir>/Items/``
    with forward slashes and the ``.Item.Gbx`` extension — e.g.
    ``"Forzamania/Alps/Alps_Tile_n010_p001_00.Item.Gbx"``. This is the
    string TM2020 looks the item up by.

    `center_xyz` is the item's world position in TM-space — the bbox
    centre that blender_export re-centred the mesh on (read back from the
    ``.center.json`` sidecar). Since the item geometry is now LOCAL (verts
    around its own origin), placing it here puts it back at its true
    world location. No Y/Z swap or Y-offset here: blender_export already
    emitted the centre in TM-space (post FORZA_TO_TRACKMANIA), and the
    ground-clearance lift happens once in compose_map.
    """
    return PlacedItem(
        name=items_rel_path,
        item_gbx_path=Path(item_gbx_path),
        position_xyz=(float(center_xyz[0]), float(center_xyz[1]), float(center_xyz[2])),
    )


def compute_ground_block_grid(
    placed_items: list[PlacedItem],
    block_name: str = "StadiumPlatform",
    margin_cells: int = 2,
) -> list[dict]:
    """Lay a flat grid of Stadium ground blocks under the placed items.

    Returns DotnetBlock dicts (Name, Dir, Position) snapped to the 32 m
    grid. The grid covers the bbox of all placements + ``margin_cells``
    cells of padding on every side so the player has somewhere to drive
    before falling off the world.
    """
    if not placed_items:
        return []

    xs = [p.position_xyz[0] for p in placed_items]
    zs = [p.position_xyz[2] for p in placed_items]
    x_min, x_max = min(xs), max(xs)
    z_min, z_max = min(zs), max(zs)

    cx_min = math.floor(x_min / BLOCK_GRID_M) - margin_cells
    cx_max = math.floor(x_max / BLOCK_GRID_M) + margin_cells
    cz_min = math.floor(z_min / BLOCK_GRID_M) - margin_cells
    cz_max = math.floor(z_max / BLOCK_GRID_M) + margin_cells

    blocks = []
    for cx in range(cx_min, cx_max + 1):
        for cz in range(cz_min, cz_max + 1):
            blocks.append({
                "Name": block_name,
                "Dir": 0,
                "Position": _to_dotnet_int3((cx, BLOCK_GRID_Y_OFFSET, cz)),
            })
    return blocks


def compose_map(
    dotnet_exe: Path,
    seed_map: Path,
    output_map: Path,
    placed_items: list[PlacedItem],
    block_name: str = "StadiumPlatform",
    config_dir: Path | None = None,
    linux_mode: bool = False,
    wine_cmd: list[str] | None = None,
) -> DotnetResult:
    """Build a .Map.Gbx by stamping items + a ground-block grid into seed_map.

    The dotnet helper writes the composed map next to the seed at
    ``<seed>_<MapSuffix>.Map.Gbx`` by default, OR to ``<output_map>`` if we
    set ShouldOverwrite=True with MapPath=<output_map>. We use the explicit-
    output approach so the result lands in TM's Maps/Forzamania/ folder
    directly.
    """
    if config_dir is None:
        config_dir = output_map.parent

    # --- shift the whole track into TM2020's positive-only space --------
    # Find the min corner across all item positions, then translate every
    # item so that corner lands at (+POSITIVE_MARGIN_M, _, +POSITIVE_MARGIN_M)
    # and the lowest point is GROUND_CLEARANCE_M above the floor. Without
    # this, ~58% of a typical FM4 track sits at negative X/Z = off-map =
    # TM reports every one of those items as "missing".
    if placed_items:
        min_x = min(it.position_xyz[0] for it in placed_items)
        min_y = min(it.position_xyz[1] for it in placed_items)
        min_z = min(it.position_xyz[2] for it in placed_items)
        off_x = POSITIVE_MARGIN_M - min_x
        off_y = GROUND_CLEARANCE_M - min_y
        off_z = POSITIVE_MARGIN_M - min_z
    else:
        off_x = off_y = off_z = 0.0

    def _shifted(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
        return (xyz[0] + off_x, xyz[1] + off_y, xyz[2] + off_z)

    items_payload = []
    shifted_items: list[PlacedItem] = []
    for it in placed_items:
        new_pos = _shifted(it.position_xyz)
        shifted_items.append(PlacedItem(
            name=it.name,
            item_gbx_path=it.item_gbx_path,
            position_xyz=new_pos,
            rotation_xyz=it.rotation_xyz,
        ))
        # Empty strings for the enum fields make the dotnet helper bail
        # with "Unable to parse '' as a value of EPhaseOffset" — defaults
        # come from vendor/blendermania-addon/utils/Dotnet.py:29-66.
        items_payload.append({
            "Name": it.name,
            "Path": str(it.item_gbx_path),
            "Position": _to_dotnet_vector3(new_pos),
            "Rotation": _to_dotnet_vector3(it.rotation_xyz),
            "Pivot": _to_dotnet_vector3((0.0, 0.0, 0.0)),
            "AnimPhaseOffset": "None",
            "DifficultyColor": "Default",
            "LightmapQuality": "Normal",
        })

    # Block grid is derived from the (already-shifted) item positions, so it
    # lands in positive space too and stays aligned under the items.
    blocks_payload = compute_ground_block_grid(shifted_items, block_name=block_name)

    payload = {
        "MapPath": str(output_map),
        "Blocks": blocks_payload,
        "Items": items_payload,
        "ShouldOverwrite": True,
        "MapSuffix": "_modified",
        "CleanBlocks": True,
        "CleanItems": True,
        "Env": "Stadium2020",
    }

    config_path = config_dir / f"{output_map.stem}_compose.json"
    write_json_config(config_path, payload)

    # The helper doesn't create the seed; copy it into place before invoking
    # so the helper opens-and-modifies in situ.
    if seed_map.is_file() and not output_map.is_file():
        output_map.parent.mkdir(parents=True, exist_ok=True)
        output_map.write_bytes(seed_map.read_bytes())

    return run_dotnet_command(
        dotnet_exe,
        CMD_PLACE_OBJECTS_ON_MAP,
        config_path,
        linux_mode=linux_mode,
        wine_cmd=wine_cmd,
    )
