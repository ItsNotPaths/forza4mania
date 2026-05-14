"""Drive headless Blender to convert a MeshChunk → .fbx.

We don't import bpy in this process. Instead we marshal the chunk's geometry
to JSON, spawn ``blender --background --python scripts/blender_export.py --
<chunk.json>``, and let Blender do the FBX export with its stock exporter.
That keeps the main app independent of Blender's Python and lets us upgrade
Blender freely.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from chunker import MeshChunk
from fm4.ir import TrackIR
from materials import TM2020Material


def find_blender(override: Path | None = None) -> Path:
    """Locate the Blender binary. Tries override → Steam-Linux → PATH.

    Steam Blender on Linux installs at .../SteamLibrary/steamapps/common/Blender/blender;
    other paths can be supplied via ``override`` (Settings UI feeds this).
    """
    if override is not None:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(f"blender override path does not exist: {p}")
        return p

    candidates = [
        Path("/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender"),
        Path.home() / ".steam/steam/steamapps/common/Blender/blender",
        Path("/usr/bin/blender"),
        Path("/opt/blender/blender"),
    ]
    for c in candidates:
        if c.is_file():
            return c

    import shutil
    found = shutil.which("blender")
    if found:
        return Path(found)

    raise FileNotFoundError(
        "Blender not found. Set the override path in Settings or install Steam Blender."
    )


def dump_chunk(
    chunk: MeshChunk,
    track: TrackIR,
    materials_by_chunk_mat: dict[tuple[int, int], TM2020Material],
    out_fbx: Path,
    out_json: Path,
) -> Path:
    """Serialize the chunk's geometry + material names + transforms to JSON.

    `materials_by_chunk_mat` maps (mesh_key, fm4_mat_index) → TM2020Material;
    we only need the .name for FBX, but the caller has already built them so
    we accept the full dict to avoid recomputation.
    """
    meshes_payload: dict[str, dict] = {}
    for mk in chunk.mesh_keys:
        m = track.meshes.get(mk)
        if m is None:
            continue
        verts = m.vertices.tolist()
        faces = m.faces.tolist()
        uvs = m.uvs.tolist() if m.uvs is not None else None
        mat_per_face = m.material_per_face.tolist()

        names: list[str] = []
        for mat_idx in range(len(m.materials)):
            tm = materials_by_chunk_mat.get((mk, mat_idx))
            names.append(tm.name if tm else f"mat_{mk:08x}_{mat_idx:03d}")

        meshes_payload[str(mk)] = {
            "verts": verts,
            "faces": faces,
            "uvs": uvs,
            "material_per_face": mat_per_face,
            "material_names": names,
        }

    instances_payload: list[dict] = []
    for inst in chunk.instances:
        base = inst.model_index << 8
        keys = [base | s for s in range(256) if (base | s) in track.meshes and (base | s) in chunk.mesh_keys]
        if not keys:
            continue
        instances_payload.append({
            "transform": np.asarray(inst.transform, dtype=np.float32).tolist(),
            "mesh_keys": [str(k) for k in keys],
        })

    payload = {
        "out_fbx": str(out_fbx),
        "meshes": meshes_payload,
        "instances": instances_payload,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload))
    return out_json


def export_chunk_to_fbx(
    chunk_json: Path,
    blender_path: Path,
    blender_export_script: Path,
    timeout: float = 600.0,
) -> None:
    """Spawn headless Blender to consume chunk_json and write the FBX.

    Raises RuntimeError on non-zero exit; surfaces stderr verbatim.
    """
    cmd = [
        str(blender_path),
        "--background",
        "--factory-startup",
        "--python", str(blender_export_script),
        "--",
        str(chunk_json),
    ]
    # stdin=DEVNULL: under PyInstaller --windowed on Windows, the parent
    # has no console so its stdin handle is invalid; subprocess inheriting
    # it fails with WinError 6. Explicitly nulling stdin avoids that.
    result = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"blender export failed (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    if "OK:" not in result.stdout:
        # Blender may exit 0 even on partial failures; sentinel-check the script
        print("warning: blender exited 0 but no OK sentinel found", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
