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


def _instance_world_aabb(
    track: TrackIR, instance: MeshInstance, keys: list[int]
) -> tuple[np.ndarray, np.ndarray]:
    """Real world-space AABB of a placed instance.

    We CAN'T use the transform's translation column as the position —
    FM4 bakes large track geometry's world position into the vertex data
    with an identity transform, so that column is (0,0,0) for the road,
    bridges, guardrails, etc. Instead we transform the mesh's local vertex
    AABB corners by the instance matrix and take the bounds of the result.
    That works for both world-baked geometry (identity transform, vertices
    already placed) and reused props (real transform, local vertices).
    """
    m = instance.transform  # 4x4 row-major
    lo = np.array([np.inf, np.inf, np.inf], dtype=np.float64)
    hi = np.array([-np.inf, -np.inf, -np.inf], dtype=np.float64)
    for k in keys:
        mesh = track.meshes.get(k)
        if mesh is None or mesh.vertices.shape[0] == 0:
            continue
        v = mesh.vertices  # (N,3)
        mlo = v.min(axis=0)
        mhi = v.max(axis=0)
        # 8 corners of the local AABB
        corners = np.array([
            [mlo[0], mlo[1], mlo[2]], [mhi[0], mlo[1], mlo[2]],
            [mlo[0], mhi[1], mlo[2]], [mhi[0], mhi[1], mlo[2]],
            [mlo[0], mlo[1], mhi[2]], [mhi[0], mlo[1], mhi[2]],
            [mlo[0], mhi[1], mhi[2]], [mhi[0], mhi[1], mhi[2]],
        ], dtype=np.float64)
        # apply transform: world = R·local + t
        world = corners @ m[:3, :3].T + m[:3, 3]
        lo = np.minimum(lo, world.min(axis=0))
        hi = np.maximum(hi, world.max(axis=0))
    if not np.all(np.isfinite(lo)):
        # No usable mesh — degenerate; collapse to origin so it still buckets.
        return np.zeros(3), np.zeros(3)
    return lo, hi


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


def _skybox_mesh_keys(track: TrackIR) -> set[int]:
    """Mesh keys whose every material is a sky-family FM4 shader.

    FM4 builds the world's skydome as a single huge inverted sphere with one
    material whose shader name contains ``sky_`` (Alps: ``sky_diff_1``).
    Geometrically it spans kilometers and lands at tile (0,-1) under the
    default 64 m bucket — a single oversized item slot for nothing the
    player ever interacts with. We exclude it here so it never makes it
    into a chunk; TM2020 supplies its own skybox via the environment.
    """
    sky_keys: set[int] = set()
    for key, mesh in track.meshes.items():
        if not mesh.materials:
            continue
        if all("sky_" in m.shader_name.lower() for m in mesh.materials):
            sky_keys.add(key)
    return sky_keys


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
    # Each keepable entry carries the instance, its tri count, its mesh
    # keys, AND its real world-space AABB (lo, hi) — see _instance_world_aabb
    # for why the transform translation alone is not a usable position.
    sky_keys = _skybox_mesh_keys(track)
    keepable: list[tuple[MeshInstance, int, list[int], np.ndarray, np.ndarray]] = []
    for inst in track.instances:
        keys = _mesh_keys_for_instance(track, inst)
        if not keys:
            continue
        # Skip instances whose every referenced mesh is sky. Mixed-material
        # instances (sky + real geometry — never seen in practice but
        # theoretically possible) flow through; the sky faces will render
        # as PlatformTech default and the player's eye will ignore them.
        if all(k in sky_keys for k in keys):
            continue
        tris = _tri_count_for_instance(track, inst)
        lo, hi = _instance_world_aabb(track, inst, keys)
        keepable.append((inst, tris, keys, lo, hi))

    buckets: dict[tuple[int, int], list[tuple[MeshInstance, int, list[int], np.ndarray, np.ndarray]]] = defaultdict(list)
    for entry in keepable:
        _, _, _, lo, hi = entry
        # Bucket by the AABB CENTER on the XZ ground plane (Forza is Y-up).
        cx = (lo[0] + hi[0]) * 0.5
        cz = (lo[2] + hi[2]) * 0.5
        bx = int(np.floor(cx / tile_size_m))
        bz = int(np.floor(cz / tile_size_m))
        buckets[(bx, bz)].append(entry)

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
            nonlocal current_lo, current_hi
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
                bbox_min=current_lo,
                bbox_max=current_hi,
            ))
            current_instances = []
            current_keys = set()
            current_tris = 0
            current_lo = None
            current_hi = None
            sub += 1

        current_lo: np.ndarray | None = None
        current_hi: np.ndarray | None = None

        for inst, tris, keys, lo, hi in bucket:
            if current_tris + tris > tri_budget and current_instances:
                flush()
            current_instances.append(inst)
            current_keys.update(keys)
            current_tris += tris
            if current_lo is None:
                current_lo, current_hi = lo.copy(), hi.copy()
            else:
                current_lo = np.minimum(current_lo, lo)
                current_hi = np.maximum(current_hi, hi)

        flush()

    return chunks


def _finalize_chunk(
    track: TrackIR,
    name: str,
    instances: list[MeshInstance],
    keys: Iterable[int],
    tri_count: int,
    bbox_min: np.ndarray | None = None,
    bbox_max: np.ndarray | None = None,
) -> MeshChunk:
    chunk = MeshChunk(
        name=name,
        instances=instances,
        mesh_keys=sorted(keys),
        bbox_min=bbox_min,
        bbox_max=bbox_max,
    )
    chunk.tri_count = tri_count
    return chunk
