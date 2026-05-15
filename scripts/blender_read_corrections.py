"""Runs INSIDE Blender. Reads the user's manually-corrected
Alps_assembled_for_tm.blend and dumps each named chunk's location +
rotation matrix to stdout as JSON.

The probe set every chunk's location via obj.location and left rotation
at identity. So whatever rotation/location is in the user's saved .blend
for these chunks IS the correction they applied.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy  # type: ignore

NAMES = [
    "Alps_Tile_p006_p003_00",
    "Alps_Tile_p002_p000_00",
    "Alps_Tile_p002_n001_00",
    "Alps_Tile_p004_p001_00",
]


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:]
    blend = args[0]
    bpy.ops.wm.open_mainfile(filepath=blend)

    out = []
    all_objs = sorted(o.name for o in bpy.data.objects if o.type == "MESH")
    print(f"# total mesh objects in file: {len(all_objs)}", file=sys.stderr)
    for name in NAMES:
        obj = bpy.data.objects.get(name)
        if obj is None:
            # Blender appends .001 etc on collision; try a name match
            cand = [o for o in bpy.data.objects if o.name.startswith(name)]
            obj = cand[0] if cand else None
        if obj is None:
            out.append({"name": name, "found": False})
            continue
        loc = list(obj.location)
        # rotation_euler in radians (XYZ)
        rot = list(obj.rotation_euler)
        # 4x4 world matrix — captures any combo of rot + loc
        mat = [list(row) for row in obj.matrix_world]
        out.append({
            "name": name,
            "found": True,
            "obj_name": obj.name,
            "location": loc,
            "rotation_euler_xyz_rad": rot,
            "matrix_world": mat,
        })

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
