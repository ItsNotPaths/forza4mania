"""Runs INSIDE Blender. Re-assembles a whole track exactly the way TM2020
will see it: each chunk built with the real `_build_consolidated_mesh`
(centered + sidecar center), then PLACED at that center AND yawed by
the per-item correction the working map composer uses.

The map composer (probe_recompose_yawed -> Alps_yaw_neg) places every
item with Rotation.X = -90 deg, which TM2020 interprets as a yaw about
its up axis. Blender's up axis is Z, so the equivalent Blender-side
correction is rotation_euler.Z = -pi/2.

This .blend should now spatially match Alps_yaw_neg.Map.Gbx, and is the
right surface for noting per-chunk POSITION corrections.

Reads a chunkset JSON: {"out_blend": "...", "chunks": [chunk_json, ...]}
where each chunk_json has the same shape blender_export consumes.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy  # type: ignore

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
import blender_export as BE  # type: ignore

# Mirror the dotnet helper's per-item Rotation.X = -90deg (which TM applies
# as yaw about the up axis). In Blender that's a Z-axis rotation.
PER_CHUNK_YAW_RAD = math.radians(-90.0)


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:]
    set_path = Path(args[0])
    set_data = json.loads(set_path.read_text())
    out_blend = Path(set_data["out_blend"])

    BE._wipe_scene()

    placed = 0
    for chunk in set_data["chunks"]:
        obj, world_center = BE._build_consolidated_mesh(chunk["name"], chunk)
        # world_center is the chunk's bbox center in raw Blender world
        # coords (post FORZA_TO_TRACKMANIA, pre-centering). Place the
        # centered mesh back at that position to reconstruct the layout.
        obj.location = (world_center[0], world_center[1], world_center[2])
        # Visually mirror the runtime -90 deg item rotation TM applies
        # (per the addon's Position/Rotation convention). Without this
        # the .blend doesn't match what TM shows.
        obj.rotation_euler = (0.0, 0.0, PER_CHUNK_YAW_RAD)
        placed += 1

    out_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_blend))

    nv = sum(len(o.data.vertices) for o in bpy.data.objects if o.type == "MESH")
    np_ = sum(len(o.data.polygons) for o in bpy.data.objects if o.type == "MESH")
    print(f"OK: assembled {placed} chunks (each yawed {math.degrees(PER_CHUNK_YAW_RAD):+.0f}deg), "
          f"{nv} verts, {np_} polys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
