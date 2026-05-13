"""Intermediate representation for a parsed FM4 track.

`read_track` produces a `TrackIR` — a Blender-free dataclass tree the rest of
the pipeline (chunker, Blender bridge, XML writer) consumes. Numpy arrays
hold geometry; lists hold metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Material:
    """One submesh-level material from a rmb.bin MaterialSet.

    `shader_name` references one of the .fxobj filenames in pvs.shaders.
    `texture_sampler_indices` are indexes into the parent track's texture
    table (PVS-level), used to resolve which .bix/.bin/.dds gets bound.
    """
    shader_name: str
    texture_sampler_indices: list[int] = field(default_factory=list)
    pixel_shader_constants: list[float] = field(default_factory=list)


@dataclass
class MeshData:
    """Geometry for one unique mesh (one rmb.bin track section).

    `vertices` is the raw position float32 array (N, 3) in Forza space.
    `uvs` is (N, 2) float32 or None.
    `normals` is (N, 3) float32 or None.
    `faces` is (F, 3) uint32 — already detriangulated (TriStrip → TriList).
    `material_per_face` is (F,) uint32 — index into the MeshData's
    `materials` list.
    """
    name: str
    vertices: np.ndarray
    uvs: np.ndarray | None
    normals: np.ndarray | None
    faces: np.ndarray
    material_per_face: np.ndarray
    materials: list[Material]


@dataclass
class MeshInstance:
    """A single placement of a unique mesh into world space.

    `transform` is a 4x4 row-major float matrix in Forza coordinate space
    (Y-up). The Blender bridge applies the swap (1,0,0,0)/(0,0,1,0)/
    (0,1,0,0)/(0,0,0,1) to put it in Z-up before FBX export.
    """
    model_index: int
    transform: np.ndarray
    texture_index: int
    flags: int = 0


@dataclass
class TextureRef:
    """One entry in the PVS-level texture table.

    `file_index` matches the hex part of `_0xXXXXXXXX.bix` filenames in the
    extracted bin/ dir. `is_stx` means the texture lives inside the shared
    `<prefix>.stx.bin` CAFF container instead of a standalone file.
    """
    file_index: int
    is_stx: bool
    u_scale: float = 1.0
    v_scale: float = 1.0
    u_translate: float = 0.0
    v_translate: float = 0.0


@dataclass
class TrackIR:
    """Top-level result of read_track().

    `track_name` is e.g. "CrownJewel".
    `prefix` is the rmb.bin filename prefix (e.g. "CrownJewelout").
    `bin_dir` is the extracted working/extracted/<track>/bin/ path; the
    chunker / texture extractor still need it to resolve .bix files.
    `meshes` is keyed by model_index; missing entries failed to parse and
    should be skipped (with a warning) by downstream stages.
    """
    track_name: str
    prefix: str
    bin_dir: Any  # pathlib.Path; left as Any to avoid the import here
    meshes: dict[int, MeshData]
    instances: list[MeshInstance]
    textures: list[TextureRef]
    shader_names: list[str]
