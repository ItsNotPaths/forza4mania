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


def _build_consolidated_mesh(name: str, chunk: dict) -> tuple[bpy.types.Object, tuple[float, float, float]]:
    """Build ONE mesh per chunk by baking every instance's transformed
    geometry into a single vertex/face buffer, then RE-CENTER that buffer
    on its own bounding-box centre.

    Returns (object, world_center). The world_center is the bbox centre in
    TM-space *before* re-centering — the caller uses it as the item's
    placement position in the map, so the item renders back at its true
    world location.

    Why re-center? TM2020 items must be LOCAL-space geometry (verts around
    the origin), then placed at a world position. Baking absolute world
    coordinates into the item mesh produces verts hundreds of metres
    off-origin; TM2020 silently rejects those items as invalid ("missing
    item"). Centering makes every item a well-formed local object.

    Why consolidate at all (not share mesh datablocks)? Blender 5.1's FBX
    exporter emits "different objects using the same mesh, different
    material slot layouts" warnings and produces an FBX its own importer
    crashes on. Consolidating sidesteps that.
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

    # Re-center: compute the bbox centre, subtract it from every vertex.
    # all_verts are in Blender-Z-up space here (we applied FORZA_TO_TRACKMANIA,
    # which is really a Forza-Y-up → Blender-Z-up swap). Centering the mesh
    # in this space is correct — the mesh gets exported and the FBX exporter
    # converts Blender-Z-up back to FBX-Y-up, so the item lands Y-up.
    if all_verts:
        xs = [v[0] for v in all_verts]
        ys = [v[1] for v in all_verts]
        zs = [v[2] for v in all_verts]
        cx = (min(xs) + max(xs)) * 0.5
        cy = (min(ys) + max(ys)) * 0.5
        cz = (min(zs) + max(zs)) * 0.5
        all_verts = [(v[0] - cx, v[1] - cy, v[2] - cz) for v in all_verts]
    else:
        cx = cy = cz = 0.0
    # The CENTER we report must be in the item's FINAL coordinate space
    # (Y-up — what the FBX exporter produces and what TM2020 places in),
    # NOT Blender-Z-up. FORZA_TO_TRACKMANIA is a pure Y/Z swap, so un-swap
    # the centre: (cx, cy, cz)_blender → (cx, cz, cy)_Yup.
    world_center = (cx, cz, cy)

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
    return obj, world_center


def _add_lightmap_uv(obj: bpy.types.Object) -> None:
    """Add a non-overlapping LightMap UV layer via a synthetic grid layout.

    NadeoImporter requires every material to have at least 2 UV layers
    (``BaseMaterial`` + ``LightMap``); without it, mesh import fails with
    "not enough UvLayers for material (1 < 2)". The LightMap UV must be
    non-overlapping per face so TM's lightmap baker can give each face
    unique pixels.

    We DON'T use ``bpy.ops.uv.lightmap_pack`` or ``smart_project`` because
    both run an island-packing algorithm that scales poorly on FM4-derived
    geometry — chunks with thousands of disconnected face islands (one
    per baked instance, plus many degenerate triangles) make the packers
    spin for many minutes or hang outright.

    Instead, lay every face out in a regular ``G×G`` grid where
    ``G = ceil(sqrt(face_count))``. Each face gets its own cell with a
    small margin to avoid bleed. The lightmap will be approximately
    uniform per face (one cell baked to one solid colour), which is the
    right trade-off for FM4 scenery items: pure arithmetic, microseconds
    on any mesh size, no hang risk.
    """
    mesh = obj.data
    mesh.uv_layers.new(name="LightMap")
    light = mesh.uv_layers["LightMap"]

    n_faces = len(mesh.polygons)
    if n_faces == 0:
        print("  lightmap: empty mesh, skipping", flush=True)
        return

    grid = max(1, math.ceil(math.sqrt(n_faces)))
    cell = 1.0 / grid
    margin = cell * 0.05         # 5% gutter — keeps the baker from bleeding
    inner = cell - 2 * margin

    print(f"  lightmap: {n_faces} faces → {grid}×{grid} grid, cell={cell:.4f}", flush=True)

    for face_i, poly in enumerate(mesh.polygons):
        cx = (face_i % grid) * cell
        cy = (face_i // grid) * cell
        # 4 cell corners (CCW). Triangles use 3, quads use 4, n-gons cycle.
        # Each loop gets its own corner so the face has area in UV space —
        # required for the baker to produce useful samples.
        corners = (
            (cx + margin,         cy + margin),
            (cx + margin + inner, cy + margin),
            (cx + margin + inner, cy + margin + inner),
            (cx + margin,         cy + margin + inner),
        )
        for li_idx, li in enumerate(poly.loop_indices):
            light.data[li].uv = corners[li_idx % 4]

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

    # Two output modes: normal pipeline writes an FBX per chunk; the
    # diagnostic "full blend" mode (chunk JSON carries "out_blend" instead
    # of "out_fbx") saves a .blend of the whole consolidated mesh so the
    # geometry can be inspected directly, upstream of NadeoImporter.
    out_blend = chunk.get("out_blend")
    out_fbx = chunk.get("out_fbx")

    _wipe_scene()
    _enable_fbx()

    chunk_name = Path(out_blend or out_fbx).stem
    obj, world_center = _build_consolidated_mesh(chunk_name, chunk)
    instance_count = sum(len(inst["mesh_keys"]) for inst in chunk["instances"])
    mesh_count = len(chunk["meshes"])

    if out_blend:
        out_path = Path(out_blend)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(out_path))
        print(
            f"OK: wrote {out_path} ({instance_count} placements of {mesh_count} "
            f"unique meshes, {len(obj.data.vertices)} verts, "
            f"{len(obj.data.polygons)} polys)"
        )
        return 0

    out_fbx_path = Path(out_fbx)
    out_fbx_path.parent.mkdir(parents=True, exist_ok=True)

    # Sidecar: the bbox centre we re-centred the mesh on. convert_tab reads
    # this and uses it as the item's placement position in the composed
    # map, so the (now local-space) item renders back at its true world
    # location. JSON next to the FBX, same stem.
    center_path = out_fbx_path.with_suffix(".center.json")
    center_path.write_text(json.dumps({"center": list(world_center)}))

    # FBX export args MATCH blendermania-addon/utils/ItemsExport.py:250-256
    # EXACTLY. That addon's FBX→NadeoImporter path is proven-good; our
    # earlier custom arg set (explicit axis_forward/axis_up, apply_unit_scale
    # =True, FBX_SCALE_NONE, object_types filter, etc.) drifted from it and
    # produced items with subtly-wrong rotation in-game. Everything not
    # listed here intentionally uses Blender's export defaults — same as
    # the addon. `use_selection=True` needs the object selected first.
    for o in bpy.context.scene.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.fbx(
        filepath=str(out_fbx_path),
        use_selection=True,
        use_custom_props=True,
        apply_unit_scale=False,
    )

    print(
        f"OK: wrote {out_fbx_path} ({instance_count} placements of {mesh_count} unique "
        f"meshes baked into 1 consolidated mesh, "
        f"{len(obj.data.vertices)} verts, {len(obj.data.polygons)} polys)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
