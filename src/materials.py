"""Map FM4 materials → TM2020 MeshParams.xml material entries.

v1 is intentionally trivial: every FM4 material becomes a TM2020
``PlatformTech`` (Asphalt physics, no gameplay zone, custom diffuse texture).
Visually each material still gets its own FM4-derived .dds via the
BaseTexture binding — so the track looks like FM4, just with TM2020 lighting
and uniform asphalt physics.

Surface variety (asphalt vs grass vs dirt vs kerbs) and gameplay zones
(boost / reset / wallride) are deferred. Future versions can expand into a
real heuristic + per-material override UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fm4.ir import Material as FM4Material


DEFAULT_LINK = "PlatformTech"
DEFAULT_PHYSICS_ID = "Asphalt"


@dataclass
class TM2020Material:
    """One row of the <Materials> block in MeshParams.xml.

    `name` is the per-chunk-unique material name written into the FBX. The XML
    references it by name to bind PhysicsId + texture. NadeoImporter packs the
    BaseTexture .dds into the resulting .Mesh.Gbx.
    """
    name: str
    link: str
    physics_id: str
    base_texture: str | None  # relative path to the .dds, or None to use stock


def _safe_name(s: str) -> str:
    """Sanitize an FM4 shader name for use as a material identifier.

    FM4 shader names look like "shaders\\track\\rdline_blnd_spec_opac_3.fx".
    Strip directory and extension, replace separators that XML / FBX choke on.
    """
    base = s.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".fx"):
        base = base[:-3]
    return base.replace(" ", "_").replace(".", "_")[:60]


def map_material(
    fm4_mat: FM4Material,
    chunk_name: str,
    mat_index: int,
    texture_paths: dict[int, Path],
    chunk_dir: Path,
) -> TM2020Material:
    """Build a TM2020Material for one FM4 material in one chunk.

    `texture_paths` is the {pvs_texture_index: dds_path} returned by
    textures.extract_track_textures. We pick the first sampler index that
    has a successfully-extracted .dds; missing textures leave base_texture
    as None (NadeoImporter will fall back to the stock PlatformTech texture).

    `chunk_dir` is where the FBX + XML will live; BaseTexture must be either
    absolute, NadeoImporter-relative, or relative to the FBX. We emit
    a path relative to chunk_dir so the bundle is portable.
    """
    base_texture: str | None = None
    for sampler_idx in fm4_mat.texture_sampler_indices:
        if sampler_idx < 0:
            continue
        dds = texture_paths.get(sampler_idx)
        if dds is None:
            continue
        try:
            base_texture = str(dds.relative_to(chunk_dir))
        except ValueError:
            base_texture = str(dds.resolve())
        break

    return TM2020Material(
        name=f"{chunk_name}_{mat_index:03d}_{_safe_name(fm4_mat.shader_name)}",
        link=DEFAULT_LINK,
        physics_id=DEFAULT_PHYSICS_ID,
        base_texture=base_texture,
    )
