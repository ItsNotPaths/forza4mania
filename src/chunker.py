"""Spatial chunker — split a TrackIR into TM2020-item-sized pieces.

TM2020 items soft-cap around 50–60k tris before the editor stutters; over
~100k they risk silent rejection by NadeoImporter. The chunker buckets
placed instances into world-space tiles, then merges tiles that exceed the
budget into smaller units (or splits a single oversized instance into a
chunk of its own).

A `MeshChunk` is a self-contained bundle: which instances belong to it,
plus the (de-duplicated) `MeshData`s those instances need. Downstream
stages (Blender bridge, XML writers) consume one chunk at a time.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from fm4.ir import MeshData, MeshInstance, TrackIR

DEFAULT_TILE_M = 64.0
DEFAULT_TRI_BUDGET = 50_000


@dataclass
class MeshChunk:
    """One TM2020-item-worth of geometry + placements.

    `name` is a stable per-track id like "Alps_Tile_03_07" — used to derive
    the .fbx / .Item.Gbx / .MeshParams.xml filenames downstream.
    `mesh_keys` are the TrackIR.meshes keys this chunk references.
    """
    name: str
    instances: list[MeshInstance] = field(default_factory=list)
    mesh_keys: list[int] = field(default_factory=list)
    bbox_min: np.ndarray | None = None
    bbox_max: np.ndarray | None = None

    @property
    def tri_count(self) -> int:
        return self._tri_count

    @tri_count.setter
    def tri_count(self, v: int) -> None:
        self._tri_count = v


def _instance_world_position(instance: MeshInstance) -> np.ndarray:
    """Extract world-space translation from instance's 4x4."""
    m = instance.transform
    # Forza transforms have translation in the last column rows 0..2
    return np.array([m[0, 3], m[1, 3], m[2, 3]], dtype=np.float32)


def _tri_count_for_instance(track: TrackIR, instance: MeshInstance) -> int:
    """Sum tris across every section parsed for the referenced model."""
    base = instance.model_index << 8
    total = 0
    for s_idx in range(256):
        m = track.meshes.get(base | s_idx)
        if m is None:
            continue
        total += int(m.faces.shape[0])
    return total


def _mesh_keys_for_instance(track: TrackIR, instance: MeshInstance) -> list[int]:
    base = instance.model_index << 8
    return [
        base | s_idx
        for s_idx in range(256)
        if (base | s_idx) in track.meshes
    ]


def chunk_track(
    track: TrackIR,
    tile_size_m: float = DEFAULT_TILE_M,
    tri_budget: int = DEFAULT_TRI_BUDGET,
) -> list[MeshChunk]:
    """Bucket a TrackIR's instances into chunks ≤ tri_budget tris each.

    Algorithm:
      1. Drop instances whose model has no parsed mesh (silent — they're
         already accounted for in the reader's skip list).
      2. Bucket each surviving instance by floor(pos.xz / tile_size_m).
         A 64 m tile is roughly Trackmania's stadium-block scale.
      3. For each tile, greedily pack instances until tri_budget would be
         exceeded; flush as a chunk and start the next.
      4. Single-instance chunks may exceed budget if the underlying mesh is
         itself oversized — flagged so downstream stages can warn or
         decimate. (v1: warn only.)
    """
    keepable: list[tuple[MeshInstance, int, list[int]]] = []
    for inst in track.instances:
        keys = _mesh_keys_for_instance(track, inst)
        if not keys:
            continue
        tris = _tri_count_for_instance(track, inst)
        keepable.append((inst, tris, keys))

    buckets: dict[tuple[int, int], list[tuple[MeshInstance, int, list[int]]]] = defaultdict(list)
    for inst, tris, keys in keepable:
        pos = _instance_world_position(inst)
        # Forza is Y-up; tile by XZ plane (the ground plane)
        bx = int(np.floor(pos[0] / tile_size_m))
        bz = int(np.floor(pos[2] / tile_size_m))
        buckets[(bx, bz)].append((inst, tris, keys))

    chunks: list[MeshChunk] = []
    for (bx, bz) in sorted(buckets.keys()):
        bucket = buckets[(bx, bz)]
        # Sort by tri count descending: pack heavies first, fits more cleanly
        bucket.sort(key=lambda t: -t[1])

        current_instances: list[MeshInstance] = []
        current_keys: set[int] = set()
        current_tris = 0
        sub = 0

        def flush() -> None:
            nonlocal current_instances, current_keys, current_tris, sub
            if not current_instances:
                return
            # Encode signed coords with a letter prefix instead of -/+:
            # n=negative, p=positive. TM2020 treats `-` and `+` as
            # identifier delimiters and will report items with those chars
            # as missing (the '-' before a digit gets eaten by the lookup).
            tile_x = f"{'n' if bx < 0 else 'p'}{abs(bx):03d}"
            tile_z = f"{'n' if bz < 0 else 'p'}{abs(bz):03d}"
            chunks.append(_finalize_chunk(
                track,
                f"{track.track_name}_Tile_{tile_x}_{tile_z}_{sub:02d}",
                current_instances,
                current_keys,
                current_tris,
            ))
            current_instances = []
            current_keys = set()
            current_tris = 0
            sub += 1

        for inst, tris, keys in bucket:
            if current_tris + tris > tri_budget and current_instances:
                flush()
            current_instances.append(inst)
            current_keys.update(keys)
            current_tris += tris

        flush()

    return chunks


def _finalize_chunk(
    track: TrackIR,
    name: str,
    instances: list[MeshInstance],
    keys: Iterable[int],
    tri_count: int,
) -> MeshChunk:
    bbox_min = None
    bbox_max = None
    for inst in instances:
        pos = _instance_world_position(inst)
        if bbox_min is None:
            bbox_min = pos.copy()
            bbox_max = pos.copy()
        else:
            bbox_min = np.minimum(bbox_min, pos)
            bbox_max = np.maximum(bbox_max, pos)

    chunk = MeshChunk(
        name=name,
        instances=instances,
        mesh_keys=sorted(keys),
        bbox_min=bbox_min,
        bbox_max=bbox_max,
    )
    chunk.tri_count = tri_count
    return chunk
