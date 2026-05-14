"""Runs INSIDE Blender (--background --python). Builds a chunk's geometry
into a fresh scene, applies the Forza→Trackmania coordinate flip, exports FBX.

Invoked by src/blender_bridge.py. Reads chunk JSON via:
    blender --background --python scripts/blender_export.py -- <chunk.json>

The JSON shape is defined in blender_bridge.dump_chunk(). All numpy arrays
are flattened to lists for JSON safety; this script reshapes them back.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy  # type: ignore
from mathutils import Matrix  # type: ignore


# Forza is Y-up; Trackmania (and Blender's default FBX flow) is Z-up.
# This mirrors the convention from vendor/Forza-X360-IO/.../blender/ops.py:294.
FORZA_TO_TRACKMANIA = Matrix((
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
))


def _wipe_scene() -> None:
    """Strip Blender's startup scene so we author into a clean room."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _enable_fbx() -> None:
    """`--factory-startup` disables every addon including io_scene_fbx, which
    is what `bpy.ops.export_scene.fbx` lives in. Enable it explicitly so the
    export op exists. (It's a built-in addon, no install needed.)
    """
    try:
        import addon_utils  # type: ignore
        addon_utils.enable("io_scene_fbx", default_set=True, persistent=False)
    except Exception as e:
        print(f"warning: failed to enable io_scene_fbx: {e}", file=sys.stderr)


def _transform_verts(verts: list[list[float]], xform: Matrix) -> list[tuple[float, float, float]]:
    """Apply a 4x4 to every vertex. Pre-baked instancing — see why below."""
    out: list[tuple[float, float, float]] = []
    for v in verts:
        x, y, z = v[0], v[1], v[2]
        nx = xform[0][0] * x + xform[0][1] * y + xform[0][2] * z + xform[0][3]
        ny = xform[1][0] * x + xform[1][1] * y + xform[1][2] * z + xform[1][3]
        nz = xform[2][0] * x + xform[2][1] * y + xform[2][2] * z + xform[2][3]
        out.append((nx, ny, nz))
    return out


def _build_consolidated_mesh(name: str, chunk: dict) -> bpy.types.Object:
    """Build ONE mesh per chunk by baking every instance's transformed
    geometry into a single vertex/face buffer.

    Why not share mesh datablocks across objects? Blender 5.1's FBX exporter
    emits "Cannot register a valid material index ... different objects
    using the same mesh, but different material slots layouts" warnings
    even when slots are identical, and produces an FBX that Blender's own
    importer crashes on. Consolidating sidesteps the entire issue at the
    cost of larger per-chunk vertex counts (the chunker already capped these
    to NadeoImporter-friendly sizes).
    """
    all_verts: list[tuple[float, float, float]] = []
    all_faces: list[tuple[int, int, int]] = []
    all_uvs: list[tuple[float, float]] = []
    all_mat_per_face: list[int] = []
    material_name_to_index: dict[str, int] = {}

    for inst in chunk["instances"]:
        forza_xform = Matrix(inst["transform"])
        xform = FORZA_TO_TRACKMANIA @ forza_xform
        for mk_str in inst["mesh_keys"]:
            m = chunk["meshes"].get(mk_str)
            if m is None:
                continue
            base_v = len(all_verts)
            xv = _transform_verts(m["verts"], xform)
            all_verts.extend(xv)

            uvs_src = m["uvs"]
            for vi in range(len(xv)):
                if uvs_src is not None and vi < len(uvs_src):
                    all_uvs.append(tuple(uvs_src[vi]))
                else:
                    all_uvs.append((0.0, 0.0))

            local_to_global_mat: list[int] = []
            for mname in m["material_names"]:
                if mname not in material_name_to_index:
                    material_name_to_index[mname] = len(material_name_to_index)
                local_to_global_mat.append(material_name_to_index[mname])

            for f_i, face in enumerate(m["faces"]):
                all_faces.append((face[0] + base_v, face[1] + base_v, face[2] + base_v))
                local_mat = m["material_per_face"][f_i] if f_i < len(m["material_per_face"]) else 0
                all_mat_per_face.append(local_to_global_mat[local_mat] if local_mat < len(local_to_global_mat) else 0)

    mesh = bpy.data.meshes.new(name=name)
    mesh.from_pydata(all_verts, [], all_faces)
    mesh.update()

    if all_uvs:
        uv_layer = mesh.uv_layers.new(name="BaseMaterial")
        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                uv_layer.data[li].uv = all_uvs[vi]

    # Materials in stable order so MeshParams.xml indices line up.
    ordered_names = sorted(material_name_to_index, key=material_name_to_index.get)
    for mname in ordered_names:
        mat = bpy.data.materials.get(mname) or bpy.data.materials.new(name=mname)
        mesh.materials.append(mat)
    for poly_i, poly in enumerate(mesh.polygons):
        if poly_i < len(all_mat_per_face):
            poly.material_index = all_mat_per_face[poly_i]

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)

    _add_lightmap_uv(obj)
    return obj


def _add_lightmap_uv(obj: bpy.types.Object) -> None:
    """Add a non-overlapping LightMap UV layer.

    NadeoImporter requires every material to have at least 2 UV layers
    (``BaseMaterial`` + ``LightMap``); without it, mesh import fails with
    "not enough UvLayers for material (1 < 2)". The LightMap UV needs to
    be non-overlapping per face so the in-game lightmap baker can give
    each face unique pixels.

    Strategy (in order of preference):
      1. ``uv.lightmap_pack`` — purpose-built op, fast on big meshes.
      2. ``uv.smart_project`` — slower but more universally available.
      3. Clone ``BaseMaterial`` UVs into ``LightMap``. Lightmap will
         have overlap artifacts but the FBX still passes the importer.
    """
    mesh = obj.data
    mesh.uv_layers.new(name="LightMap")

    # Make obj the active selection — bpy.ops.uv.* operators need this
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Mark LightMap as the active UV layer so the unwrap writes there
    for i, layer in enumerate(mesh.uv_layers):
        if layer.name == "LightMap":
            mesh.uv_layers.active_index = i
            break

    unwrap_ok = False
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        try:
            bpy.ops.uv.lightmap_pack(
                PREF_CONTEXT="ALL_FACES",
                PREF_PACK_IN_ONE=True,
                PREF_NEW_UVLAYER=False,
                PREF_APPLY_IMAGE=False,
                PREF_IMG_PX_SIZE=1024,
                PREF_BOX_DIV=12,
                PREF_MARGIN_DIV=0.1,
            )
            unwrap_ok = True
        except Exception:
            try:
                bpy.ops.uv.smart_project(
                    angle_limit=66.0,
                    island_margin=0.02,
                    area_weight=0.0,
                )
                unwrap_ok = True
            except Exception:
                pass
    finally:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

    if not unwrap_ok:
        # Fallback: copy BaseMaterial → LightMap so we at least pass the
        # importer's UV-layer-count check. Visual lighting will be wrong.
        base = mesh.uv_layers.get("BaseMaterial")
        light = mesh.uv_layers.get("LightMap")
        if base is not None and light is not None:
            for i in range(len(base.data)):
                light.data[i].uv = base.data[i].uv

    # Restore BaseMaterial as the active UV layer — Blender's FBX exporter
    # writes the active layer first, and NadeoImporter wants BaseMaterial
    # at index 0 so it can find diffuse textures correctly.
    for i, layer in enumerate(mesh.uv_layers):
        if layer.name == "BaseMaterial":
            mesh.uv_layers.active_index = i
            break


def main() -> int:
    if "--" not in sys.argv:
        print("error: missing -- separator and chunk.json arg", file=sys.stderr)
        return 2
    args = sys.argv[sys.argv.index("--") + 1:]
    if not args:
        print("error: chunk.json arg required after --", file=sys.stderr)
        return 2

    chunk_path = Path(args[0])
    if not chunk_path.is_file():
        print(f"error: chunk.json not found: {chunk_path}", file=sys.stderr)
        return 2

    chunk = json.loads(chunk_path.read_text())
    out_fbx = Path(chunk["out_fbx"])

    _wipe_scene()
    _enable_fbx()

    chunk_name = Path(chunk["out_fbx"]).stem
    obj = _build_consolidated_mesh(chunk_name, chunk)
    instance_count = sum(len(inst["mesh_keys"]) for inst in chunk["instances"])
    mesh_count = len(chunk["meshes"])

    # FBX export — stock Blender exporter, no addon required
    out_fbx.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.fbx(
        filepath=str(out_fbx),
        check_existing=False,
        use_selection=False,
        use_visible=False,
        use_active_collection=False,
        global_scale=1.0,
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_NONE",
        bake_space_transform=False,
        object_types={"MESH"},
        use_mesh_modifiers=False,
        mesh_smooth_type="OFF",
        use_subsurf=False,
        use_mesh_edges=False,
        use_tspace=False,
        use_custom_props=False,
        path_mode="AUTO",
        embed_textures=False,
        batch_mode="OFF",
        axis_forward="-Z",
        axis_up="Y",
    )

    print(
        f"OK: wrote {out_fbx} ({instance_count} placements of {mesh_count} unique "
        f"meshes baked into 1 consolidated mesh, "
        f"{len(obj.data.vertices)} verts, {len(obj.data.polygons)} polys)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
