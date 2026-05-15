"""Runs INSIDE Blender. Assembles a track for visual inspection using the
SAME coordinate convention as the x360-io Blender addon
(vendor/Forza-X360-IO/.../blender/ops.py:294), which is the reference
orientation the project uses anyway.

x360-io's Forza→Blender conversion is a pure Y/Z swap with no negations:

    (x, y, z)_forza  ->  (x, z, y)_blender

and instances are placed by their raw Forza transform composed with that
swap — no per-item yaw, no position offset, no chirality correction. This
matches what a user gets if they import the track natively via x360-io's
Blender addon, which is the orientation everyone on the project is
already familiar with.

NOTE: the production FBX pipeline uses a DIFFERENT matrix
(scripts/blender_export.py:FORZA_TO_TRACKMANIA — has X negation + Y/Z
swap + 180° yaw baked in) because TM2020's item placement + Rotation.X
convention cancels those terms out to produce a correct render in-game.
Those cancellations are right for TM but make a diagnostic blend
confusing. Using the x360-io convention here keeps inspection simple.

Reads a chunkset JSON: ``{"out_blend": "...", "chunks": [chunk_json, ...]}``
with the same chunk shape produced by scripts/export_track_blend.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy  # type: ignore
from mathutils import Matrix  # type: ignore

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
import blender_export as BE  # type: ignore


# x360-io's Forza→Blender matrix (vendor/Forza-X360-IO/.../blender/ops.py:294).
# (x, y, z)_forza  ->  (x, z, y)_blender — pure Y/Z swap, no negations.
X360IO_FORZA_TO_BLENDER = Matrix((
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
))


def _build_chunk_object_x360(name: str, chunk: dict) -> bpy.types.Object:
    """Consolidate a chunk's instances using x360-io's coordinate convention.

    Mirrors blender_export._build_consolidated_mesh's shape (one consolidated
    mesh per chunk, baked-instances geometry) but uses x360-io's pure-swap
    matrix instead of our FORZA_TO_TRACKMANIA, and KEEPS each vertex in
    world coords (no re-centering — we want the diagnostic blend to show
    the whole track laid out, not a pile of items at the origin).
    """
    all_verts: list[tuple[float, float, float]] = []
    all_faces: list[tuple[int, int, int]] = []
    all_mat_per_face: list[int] = []
    material_name_to_index: dict[str, int] = {}

    for inst in chunk["instances"]:
        forza_xform = Matrix(inst["transform"])
        xform = X360IO_FORZA_TO_BLENDER @ forza_xform
        for mk_str in inst["mesh_keys"]:
            m = chunk["meshes"].get(mk_str)
            if m is None:
                continue
            base_v = len(all_verts)
            xv = BE._transform_verts(m["verts"], xform)
            all_verts.extend(xv)

            local_to_global_mat: list[int] = []
            for mname in m["material_names"]:
                if mname not in material_name_to_index:
                    material_name_to_index[mname] = len(material_name_to_index)
                local_to_global_mat.append(material_name_to_index[mname])

            for f_i, face in enumerate(m["faces"]):
                all_faces.append((face[0] + base_v, face[1] + base_v, face[2] + base_v))
                local_mat = m["material_per_face"][f_i] if f_i < len(m["material_per_face"]) else 0
                all_mat_per_face.append(
                    local_to_global_mat[local_mat] if local_mat < len(local_to_global_mat) else 0
                )

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(all_verts, [], all_faces)
    mesh.update()

    # Slot materials so the FM4 shader stems appear in the outliner — the
    # whole point of the diagnostic blend is to identify mesh classes
    # visually.
    for mname in material_name_to_index:
        if mname not in bpy.data.materials:
            bpy.data.materials.new(mname)
        mesh.materials.append(bpy.data.materials[mname])
    for poly_i, poly in enumerate(mesh.polygons):
        if poly_i < len(all_mat_per_face):
            poly.material_index = all_mat_per_face[poly_i]

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:]
    set_path = Path(args[0])
    set_data = json.loads(set_path.read_text())
    out_blend = Path(set_data["out_blend"])

    BE._wipe_scene()

    placed = 0
    for chunk in set_data["chunks"]:
        _build_chunk_object_x360(chunk["name"], chunk)
        placed += 1

    out_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_blend))

    nv = sum(len(o.data.vertices) for o in bpy.data.objects if o.type == "MESH")
    np_ = sum(len(o.data.polygons) for o in bpy.data.objects if o.type == "MESH")
    print(f"OK: assembled {placed} chunks (x360-io convention, no yaw), "
          f"{nv} verts, {np_} polys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
