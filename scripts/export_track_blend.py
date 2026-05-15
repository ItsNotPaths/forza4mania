"""Build a whole-track .blend for visual inspection — runs OUTSIDE Blender.

Reads the FM4 IR for one track (all ribbons), runs it through the chunker
(so skybox filtering + spatial bucketing match the real pipeline), then
spawns headless Blender with scripts/blender_assemble_tm.py to consolidate
every chunk into one inspectable .blend.

Use:
    python3 scripts/export_track_blend.py <track_name>
e.g.
    python3 scripts/export_track_blend.py LeMans

Output: working/full_blend/<track_name>.blend

This is a debugging tool, not part of the normal convert pipeline. Useful
for spotting mesh classes the heuristic classifier should filter or remap.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Add src/ to path so we can use the same readers + chunker the app uses
_THIS = Path(__file__).resolve()
_PROJ = _THIS.parent.parent
sys.path.insert(0, str(_PROJ / "src"))

from chunker import chunk_track  # noqa: E402
from fm4.reader import read_track  # noqa: E402

FM4_TRACKS_ROOT = Path(
    "/run/media/paths/SSS-Games/xenia_canary_windows/content/"
    "0000000000000000/4D530910/00007000/33E7B39F/Media/tracks"
)
BLENDER = Path("/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender")
OUT_DIR = _PROJ / "working" / "full_blend"


def _chunk_payload(chunk, track) -> dict:
    """Build the JSON shape blender_assemble_tm expects for one chunk.

    Mirrors src/blender_bridge.dump_chunk but without TM2020Material name
    lookups — we don't need real material names for the diagnostic blend.
    """
    meshes_payload: dict[str, dict] = {}
    for mk in chunk.mesh_keys:
        m = track.meshes.get(mk)
        if m is None:
            continue
        meshes_payload[str(mk)] = {
            "verts": m.vertices.tolist(),
            "faces": m.faces.tolist(),
            "uvs": m.uvs.tolist() if m.uvs is not None else None,
            "material_per_face": m.material_per_face.tolist(),
            "material_names": [
                # Embed the FM4 shader stem in the Blender material name so
                # the user can identify mesh classes visually in the
                # .blend's outliner. The stem after the last slash, .fx
                # stripped, is enough to classify.
                _stem(mat.shader_name) or f"mat_{mk:08x}_{i:03d}"
                for i, mat in enumerate(m.materials)
            ],
        }

    instances_payload: list[dict] = []
    for inst in chunk.instances:
        base = inst.model_index << 8
        keys = [base | s for s in range(256)
                if (base | s) in track.meshes and (base | s) in chunk.mesh_keys]
        if not keys:
            continue
        instances_payload.append({
            # `.tolist()` recurses into the ndarray and emits native Python
            # floats; iterating with `list()` would leave numpy.float32 in
            # the rows, which json.dumps rejects.
            "transform": inst.transform.tolist(),
            "mesh_keys": [str(k) for k in keys],
        })

    return {
        "name": chunk.name,
        "meshes": meshes_payload,
        "instances": instances_payload,
    }


def _stem(shader_name: str) -> str:
    s = shader_name.replace("\\", "/").rsplit("/", 1)[-1]
    return s[:-3] if s.lower().endswith(".fx") else s


def main(track_name: str) -> int:
    track_dir = FM4_TRACKS_ROOT / track_name
    if not track_dir.is_dir():
        print(f"error: {track_dir} not found", file=sys.stderr)
        return 2
    if not BLENDER.is_file():
        print(f"error: blender not found at {BLENDER}", file=sys.stderr)
        return 2

    ribbons = sorted(p for p in track_dir.iterdir() if p.is_dir() and p.name.startswith("Ribbon_"))
    if not ribbons:
        print(f"error: no Ribbon_* dirs in {track_dir}", file=sys.stderr)
        return 2

    # First ribbon only by default — multi-ribbon merging is a separate
    # concern (some tracks have variants that aren't compatible to merge).
    ribbon = ribbons[0]
    print(f"reading {track_name}/{ribbon.name} (first of {len(ribbons)} ribbons)")
    ir = read_track(track_dir, ribbon)
    print(f"  meshes={len(ir.meshes)} instances={len(ir.instances)}")

    print("chunking...")
    chunks = chunk_track(ir, tile_size_m=64.0, tri_budget=50_000)
    chunks = [c for c in chunks if c.tri_count > 0]
    print(f"  {len(chunks)} chunks (after skybox + zero-tri filter)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    set_json = OUT_DIR / f"{track_name}.full.json"
    out_blend = OUT_DIR / f"{track_name}.blend"
    payload = {
        "out_blend": str(out_blend),
        "chunks": [_chunk_payload(c, ir) for c in chunks],
    }
    set_json.write_text(json.dumps(payload))
    size_mb = set_json.stat().st_size / (1024 * 1024)
    print(f"wrote set JSON: {set_json} ({size_mb:.1f} MB)")

    script = _PROJ / "scripts" / "blender_assemble_tm.py"
    cmd = [str(BLENDER), "--background", "--factory-startup",
           "--python", str(script), "--", str(set_json)]
    print(f"invoking blender:\n  {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"blender failed: rc={rc}", file=sys.stderr)
        return rc
    print(f"OK: {out_blend}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.splitlines()[0], file=sys.stderr)
        print("usage: export_track_blend.py <track_name>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
