"""Emit MeshParams.xml + Item.xml that NadeoImporter accepts.

Shape mirrors vendor/blendermania-addon/utils/NadeoXML.py — specifically
``generate_mesh_XML`` (line 387) and the item-XML writer (line 295). We
diverge in one place: for TM2020 we emit ``BaseTexture="..."`` on each
``<Material>``, which the addon currently only does for Maniaplanet
(NadeoXML.py:479). NadeoImporter accepts it for both; this is what makes
custom textures travel into the .Item.Gbx.

No light / waypoint / pivot support in v1 — TM2020 items can be added
later when needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from materials import TM2020Material


def write_mesh_params(
    fbx_path: Path,
    materials: Iterable[TM2020Material],
    scale: float = 1.0,
) -> Path:
    """Write <fbx>.MeshParams.xml next to the FBX. Returns the XML path.

    NadeoImporter reads this when invoked as ``NadeoImporter.exe Mesh <fbx>``.
    Required attributes: Scale, MeshType, Collection, FbxFile, plus the
    <Materials> block. Lights are optional; we emit an empty <Lights/>.
    """
    fbx_path = Path(fbx_path)
    xml_path = fbx_path.with_suffix(".MeshParams.xml")

    rows: list[str] = []
    for m in materials:
        attrs = [f'Name="{_escape(m.name)}"', f'Link="{_escape(m.link)}"']
        if m.physics_id:
            attrs.append(f'PhysicsId="{_escape(m.physics_id)}"')
        if m.base_texture:
            attrs.append(f'BaseTexture="{_escape(m.base_texture)}"')
        rows.append("        <Material " + " ".join(attrs) + " />")

    materials_block = "\n".join(rows) if rows else ""

    # Collection is "Stadium" — NadeoImporter's library uses the short
    # environment name. "Stadium2020" is the in-game env identifier the
    # dotnet helper uses when composing maps; the importer's library
    # rejects it ("collection : 'Stadium2020' not found").
    xml = (
        '<?xml version="1.0" ?>\n'
        f'<MeshParams Scale="{scale}" MeshType="Static" Collection="Stadium" '
        f'FbxFile="{_escape(fbx_path.name)}">\n'
        '    <Materials>\n'
        f'{materials_block}\n'
        '    </Materials>\n'
        '    <Lights/>\n'
        '</MeshParams>\n'
    )
    xml_path.write_text(xml, encoding="utf-8")
    return xml_path


def write_item_xml(
    fbx_path: Path,
    author: str = "forzamania",
) -> Path:
    """Write <fbx>.Item.xml next to the FBX. Returns the XML path.

    NadeoImporter reads this when invoked as ``NadeoImporter.exe Item <xml>``.
    Pairs the .Mesh.Gbx (built from MeshParams.xml + the FBX) with the item
    metadata to produce the final .Item.Gbx. v1 items are bare
    StaticObjects with no waypoint, no pivots, no levitation snap.
    """
    fbx_path = Path(fbx_path)
    xml_path = fbx_path.with_suffix(".Item.xml")
    name = fbx_path.stem
    meshparams_filename = f"{name}.MeshParams.xml"

    xml = (
        '<?xml version="1.0" ?>\n'
        f'<Item AuthorName="{_escape(author)}" Collection="Stadium" Type="StaticObject">\n'
        '    <MeshParamsLink '
        f'File="{_escape(meshparams_filename)}" />\n'
        '    <Phy/>\n'
        '    <Vis/>\n'
        '    <GridSnap HStep="0" VStep="0" HOffset="0" VOffset="0" />\n'
        '    <Levitation HStep="0" VStep="0" HOffset="0" VOffset="0" GhostMode="false" />\n'
        '    <Options AutoRotation="false" ManualPivotSwitch="false" NotOnItem="false" OneAxisRotation="false" />\n'
        '    <PivotSnap Distance="0" />\n'
        '</Item>\n'
    )
    xml_path.write_text(xml, encoding="utf-8")
    return xml_path


def _escape(s: str) -> str:
    """Minimal XML attribute escaping (we control the inputs but be safe)."""
    return (
        s.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
