"""Map FM4 materials → TM2020 MeshParams.xml material entries.

Classification is a heuristic on the FM4 shader filename stem. The FM4 art
pipeline uses shader names like ``road_blnd_trilin_2`` for the main asphalt
surface, ``barr_shad_diff_spec_1`` for barriers, ``grass_diff_opac_2_2sd``
for terrain, etc. We bucket those stems onto stock TM2020 Stadium Links so
the geometry renders with appropriate stock textures and the right
PhysicsId for collision (Asphalt grippy road, Metal hard wall, Grass
slow surface, NotCollidable for trees/signs/flags).

We use STOCK Stadium Links exclusively — no Custom* / BaseTexture path.
Custom* Links require a per-user asset-folder install and cap at 14 slots
total; stock Links resolve to game-shipped textures on every install and
let us set PhysicsId freely. The visual fidelity loss (no FM4-derived
diffuse textures) is the trade-off; map shape and surface feel are
preserved.

The classifier table below is intentionally short. Every FM4 shader in
Alps (277 unique) classifies into one of 6 buckets — the residual
~deco bucket covers buildings and generic props, which get default
Concrete physics so the car actually collides with them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fm4.ir import Material as FM4Material


# Default for unrecognized FM4 shaders — generic decoration / buildings.
# Concrete (not NotCollidable) because most FM4 unknown geometry IS solid
# (buildings, bridge piers, tunnels) and players should crash into it.
DEFAULT_LINK = "PlatformTech"
DEFAULT_PHYSICS_ID = "Concrete"


# Order matters — first match wins. Patterns are tested against the
# lowercased shader stem (everything after the last slash, dot stripped).
# Each entry: (substring-test, TM2020 Link, PhysicsId).
#
# Roads: TM2020 has RoadTech (main asphalt), RoadDirt, RoadBump, RoadIce —
# we map every road-family FM4 stem onto RoadTech because FM4 doesn't
# distinguish surface types in shader names (it varies by .fxobj
# combination, which we don't decode). The PhysicsId differentiates feel.
#
# Barriers: TrackWall is the Stadium-yellow wall texture; visually it's
# the wrong colour but the right surface type. Metal physics so hits feel
# right.
#
# Trees/signs/flags: NotCollidable so the car drives through them. They
# stay visually (rendered as flat opaque quads with stock grass/asphalt
# textures — see scope note about substituting TM2020 stock tree items
# in a later pass to replace the placeholder quads with real 3D trees).
_CLASSIFIER: list[tuple[tuple[str, ...], str, str]] = [
    (("road_", "rdline_", "rdedg_", "rddet_", "shldr_"), "RoadTech", "Asphalt"),
    (("barr_",),                                          "TrackWall", "Metal"),
    (("grass_", "lake_"),                                 "Grass", "Grass"),
    # Trees: DecoHill is a real standalone Stadium Link (Grass surface,
    # DECO category) — visually a grass-toned deco material. PlatformGrass
    # would seem like the obvious pick from the texture-list doc but
    # NadeoImporter rejects it with "Material not found in library" —
    # PlatformGrass only exists as a modifier *prefix* in compound link
    # names like PlatformGrass_PlatformTech, not on its own.
    (("tree_", "treebend"),                               "DecoHill", "NotCollidable"),
    (("sign_", "anim_flag", "anim_diff"),                 "PlatformTech", "NotCollidable"),
    # Residual alpha-cutout catcher: FM4's "2sd" suffix tags double-sided
    # alpha-tested materials (foliage cards, fence wire, banner cloth).
    # Without this rule they'd flow to the Concrete default and the car
    # would crash into invisible-but-solid flat quads where FM4 expected
    # see-through cutouts. Must come AFTER the road/grass/etc. families
    # because those legit shaders sometimes carry `_opac_` (e.g.
    # rdline_blnd_spec_opac_3 — opaque road paint, NOT a cutout).
    (("_2sd", "_opac_"),                                  "PlatformTech", "NotCollidable"),
]


def _classify_shader(shader_name: str) -> tuple[str, str]:
    """Return (link, physics_id) for an FM4 shader filename.

    Falls back to DEFAULT_LINK / DEFAULT_PHYSICS_ID when no pattern matches.
    """
    s = shader_name.lower()
    for needles, link, phys in _CLASSIFIER:
        if any(n in s for n in needles):
            return link, phys
    return DEFAULT_LINK, DEFAULT_PHYSICS_ID


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

    `texture_paths` and `chunk_dir` are retained for caller-compatibility
    but unused — TM2020 stock Links resolve to game-shipped textures, so
    we never bind a custom diffuse here. (Earlier versions emitted
    BaseTexture for FM4-derived .dds files; NadeoImporter silently aborts
    on Stadium when BaseTexture is set, so it was removed in commit
    3c33bac and the texture-extractor output is now unused by the XML
    pipeline.)
    """
    link, physics_id = _classify_shader(fm4_mat.shader_name)
    return TM2020Material(
        name=f"{chunk_name}_{mat_index:03d}_{_safe_name(fm4_mat.shader_name)}",
        link=link,
        physics_id=physics_id,
        base_texture=None,
    )
