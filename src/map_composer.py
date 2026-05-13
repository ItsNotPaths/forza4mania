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


@dataclass
class PlacedItem:
    """One item placement in the composed map.

    Position is in TM world units (meters). Path is the absolute Item.Gbx
    location in the user's TM Documents/Items/ folder; the dotnet helper
    embeds a path-only reference into the map (no embed-by-content in v1).
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


def chunk_to_placed_item(chunk: MeshChunk, item_gbx_path: Path) -> PlacedItem:
    """Lift one chunk + its NadeoImporter output into a PlacedItem.

    Position uses the chunk's bbox center for stability — items in a
    composed map sit at their named anchor, and centering avoids surprise
    offsets when the same chunk gets re-exported with slightly different
    bounds.

    Coordinate mapping mirrors the addon (MapObjects.py:218):
      Forza X → TM Z
      Forza Y → TM Y (+ITEM_POSITION_Y_OFFSET)
      Forza Z → TM X
    The Y/Z swap matches the FORZA_TO_TRACKMANIA basis flip we apply at
    Blender export time, keeping Forza Y-up content vertical in TM.
    """
    if chunk.bbox_min is None or chunk.bbox_max is None:
        center = np.zeros(3, dtype=np.float32)
    else:
        center = (chunk.bbox_min + chunk.bbox_max) * 0.5

    fx, fy, fz = float(center[0]), float(center[1]), float(center[2])
    tm_x = fz
    tm_y = fy + ITEM_POSITION_Y_OFFSET
    tm_z = fx

    return PlacedItem(
        name=chunk.name,
        item_gbx_path=Path(item_gbx_path),
        position_xyz=(tm_x, tm_y, tm_z),
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

    items_payload = []
    for it in placed_items:
        items_payload.append({
            "Name": it.name,
            "Path": str(it.item_gbx_path),
            "Position": _to_dotnet_vector3(it.position_xyz),
            "Rotation": _to_dotnet_vector3(it.rotation_xyz),
            "Pivot": _to_dotnet_vector3((0.0, 0.0, 0.0)),
            "AnimPhaseOffset": "",
            "DifficultyColor": "",
            "LightmapQuality": "",
        })

    blocks_payload = compute_ground_block_grid(placed_items, block_name=block_name)

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
