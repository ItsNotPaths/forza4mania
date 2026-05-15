"""Runs INSIDE Blender. Assembles a track THREE ways with different
correction hypotheses, saves three .blend files. Compare to find which
correction makes the chunk-distance-dependent position error vanish.

Hypotheses:
  baseline : current pipeline, no global correction (per-chunk yaw only)
  scale    : same as baseline + uniform scale of placement positions
             (positions multiplied by SCALE_FACTOR around world origin)
  rotate   : same as baseline + small rotation of placement positions
             about world origin (about Blender Z, the up axis)
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

PER_CHUNK_YAW_RAD = math.radians(-90.0)
SCALE_FACTOR = 1.232    # 48/207 + 1, derived from one data point
ROTATE_DEG = 13.4       # arcsin(48/207), derived from same data point


def _assemble(set_data, scale: float, rotate_rad: float, out_path: Path):
    BE._wipe_scene()
    cs = math.cos(rotate_rad)
    sn = math.sin(rotate_rad)
    for chunk in set_data["chunks"]:
        obj, world_center = BE._build_consolidated_mesh(chunk["name"], chunk)
        tx, ty, tz = world_center
        # Blender placement (TM (X,Y,Z) -> Blender (X, -Z, Y))
        bx, by, bz = tx, -tz, ty
        # apply scale + rotation about Blender origin
        bx *= scale
        by *= scale
        bz *= scale
        # rotate about Blender Z (planar XY)
        nx = cs * bx - sn * by
        ny = sn * bx + cs * by
        obj.location = (nx, ny, bz)
        obj.rotation_euler = (0.0, 0.0, PER_CHUNK_YAW_RAD)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_path))


def main() -> int:
    args = sys.argv[sys.argv.index("--") + 1:]
    set_path = Path(args[0])
    out_dir = Path(args[1])
    set_data = json.loads(set_path.read_text())

    print(f"baseline  -> {out_dir / 'baseline.blend'}")
    _assemble(set_data, scale=1.0, rotate_rad=0.0, out_path=out_dir / "baseline.blend")
    print(f"scale {SCALE_FACTOR:.3f} -> {out_dir / 'scaled.blend'}")
    _assemble(set_data, scale=SCALE_FACTOR, rotate_rad=0.0, out_path=out_dir / "scaled.blend")
    print(f"rotate {ROTATE_DEG:.1f}deg -> {out_dir / 'rotated.blend'}")
    _assemble(set_data, scale=1.0, rotate_rad=math.radians(ROTATE_DEG), out_path=out_dir / "rotated.blend")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
