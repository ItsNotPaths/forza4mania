"""Runs INSIDE Blender. Reconstruction test for the chunk→center→place
pipeline.

Reads a chunk-set JSON: a list of chunks, each with its instances/meshes
(same shape as a normal chunk JSON) PLUS the expected placement center.
For each chunk it builds the consolidated+centered mesh exactly like the
real export, then OFFSETS the object by the placement center.

If our centering + placement math is self-consistent, the result is
identical to baking everything in one mesh (the full-track .blend that's
already verified correct). Any drift = a bug in the center math.

Saves the reconstruction as a .blend for visual diff against the
full-track one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy  # type: ignore

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
# reuse the real builder + constants
import blender_export as BE  # type: ignore


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:]
    set_json = Path(args[0])
    out_blend = Path(args[1])

    data = json.loads(set_json.read_text())
    chunks = data["chunks"]  # list of {name, meshes, instances}

    BE._wipe_scene()

    for ch in chunks:
        # Build the consolidated+centered mesh + get its reported center,
        # exactly as the real pipeline does.
        obj, world_center = BE._build_consolidated_mesh(ch["name"], ch)
        # Place it: the reported center is in Y-up space. Blender is Z-up,
        # and the object's geometry is in Blender-Z-up (pre FBX export).
        # To reconstruct in Blender we must place using the BLENDER-space
        # center, i.e. swap the reported Y/Z back: (cx,cy,cz)_Yup ->
        # (cx,cz,cy)_Zup.
        cx, cy, cz = world_center
        obj.location = (cx, cz, cy)

    out_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_blend))

    # report combined bbox
    lo = [1e18, 1e18, 1e18]
    hi = [-1e18, -1e18, -1e18]
    nverts = 0
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        nverts += len(o.data.vertices)
        for v in o.data.vertices:
            w = o.matrix_world @ v.co
            for i in range(3):
                lo[i] = min(lo[i], w[i])
                hi[i] = max(hi[i], w[i])
    print(f"OK: {len(chunks)} chunks placed, {nverts} verts, "
          f"bbox=[{lo[0]:.0f},{lo[1]:.0f},{lo[2]:.0f}]→[{hi[0]:.0f},{hi[1]:.0f},{hi[2]:.0f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
