"""Runs INSIDE Blender. Compares every chunk's location+rotation in the
user's saved .blend against the original placements from the chunkset
JSON. Reports any chunk whose placement changed (the user's manual
corrections).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy  # type: ignore


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:]
    blend_path = args[0]
    chunkset_json = args[1]

    bpy.ops.wm.open_mainfile(filepath=blend_path)

    # Reconstruct the ORIGINAL placement from the chunkset (same logic as
    # blender_assemble_tm: location = (tx, -tz, ty); rotation = (0, 0, -90)).
    set_data = json.loads(Path(chunkset_json).read_text())

    # We'd need the world_center per chunk to know its original location,
    # but the chunkset JSON doesn't include it — it's derived inside
    # blender_export. Rebuild the consolidated mesh for each chunk and
    # capture its world_center; that's the original location.
    _here = Path(__file__).resolve().parent
    sys.path.insert(0, str(_here))
    import blender_export as BE  # type: ignore

    # We're in a Blender session that already has the user's modified file
    # loaded. Don't wipe it — just compute centers from the chunkset and
    # compare to what's in the loaded scene.
    PER_CHUNK_YAW_RAD = math.radians(-90.0)
    moved = []
    for chunk in set_data["chunks"]:
        # Compute the original world_center the same way blender_export
        # does, but in a throwaway scratch scene so we don't pollute the
        # loaded one. _build_consolidated_mesh creates objects in the
        # current scene; create a hidden temp collection.
        # Simpler: just recompute the bbox math directly without building
        # geometry in the user's scene.
        from mathutils import Matrix  # type: ignore
        all_verts = []
        for inst in chunk["instances"]:
            xform = BE.FORZA_TO_TRACKMANIA @ Matrix(inst["transform"])
            for mk in inst["mesh_keys"]:
                m = chunk["meshes"].get(mk)
                if m is None:
                    continue
                for v in m["verts"]:
                    x, y, z = v
                    nx = xform[0][0] * x + xform[0][1] * y + xform[0][2] * z + xform[0][3]
                    ny = xform[1][0] * x + xform[1][1] * y + xform[1][2] * z + xform[1][3]
                    nz = xform[2][0] * x + xform[2][1] * y + xform[2][2] * z + xform[2][3]
                    all_verts.append((nx, ny, nz))
        if not all_verts:
            continue
        xs = [v[0] for v in all_verts]; ys = [v[1] for v in all_verts]; zs = [v[2] for v in all_verts]
        cx = (min(xs) + max(xs)) * 0.5
        cy = (min(ys) + max(ys)) * 0.5
        cz = (min(zs) + max(zs)) * 0.5
        # Sidecar formula: world_center = (-cx, cz, -cy)
        wc = (-cx, cz, -cy)
        # Original Blender location used by blender_assemble_tm:
        orig_loc = (wc[0], -wc[2], wc[1])
        orig_rot = (0.0, 0.0, PER_CHUNK_YAW_RAD)

        obj = bpy.data.objects.get(chunk["name"])
        if obj is None:
            cand = [o for o in bpy.data.objects if o.name.startswith(chunk["name"])]
            obj = cand[0] if cand else None
        if obj is None:
            continue
        cur_loc = tuple(obj.location)
        cur_rot = tuple(obj.rotation_euler)
        dx = cur_loc[0] - orig_loc[0]
        dy = cur_loc[1] - orig_loc[1]
        dz = cur_loc[2] - orig_loc[2]
        drx = cur_rot[0] - orig_rot[0]
        dry = cur_rot[1] - orig_rot[1]
        drz = cur_rot[2] - orig_rot[2]
        moved_dist = (dx * dx + dy * dy + dz * dz) ** 0.5
        if moved_dist < 0.5 and abs(drx) < 0.01 and abs(dry) < 0.01 and abs(drz) < 0.01:
            continue
        moved.append({
            "name": chunk["name"],
            "moved_dist": round(moved_dist, 2),
            "delta_loc": [round(dx, 2), round(dy, 2), round(dz, 2)],
            "delta_rot_deg": [round(math.degrees(drx), 2),
                              round(math.degrees(dry), 2),
                              round(math.degrees(drz), 2)],
            "orig_loc": [round(v, 2) for v in orig_loc],
            "new_loc": [round(v, 2) for v in cur_loc],
        })

    print(json.dumps(moved, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
