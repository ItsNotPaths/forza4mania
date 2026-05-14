"""Drive headless Blender to convert a MeshChunk → .fbx.

We don't import bpy in this process. Instead we marshal the chunk's geometry
to JSON, spawn ``blender --background --python scripts/blender_export.py --
<chunk.json>``, and let Blender do the FBX export with its stock exporter.
That keeps the main app independent of Blender's Python and lets us upgrade
Blender freely.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from chunker import MeshChunk
from fm4.ir import TrackIR
from materials import TM2020Material
from subproc import run_captured


def find_blender(override: Path | None = None) -> Path:
    """Locate the Blender binary. Tries override → Steam-Linux → PATH.

    Steam Blender on Linux installs at .../SteamLibrary/steamapps/common/Blender/blender;
    other paths can be supplied via ``override`` (Settings UI feeds this).

    Wine quirk: if we're running on Windows-Python (likely under Wine) and
    the configured/detected blender path lacks a .exe suffix, the binary is
    almost certainly a Linux ELF and CreateProcess will reject it with
    WinError 6. Refuse loudly with an actionable error instead of letting
    the user chase a misleading subprocess failure.
    """
    if override is not None:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(f"blender override path does not exist: {p}")
        _check_blender_is_windows_compatible(p)
        return p

    candidates = [
        # Windows installs first — under Wine these are the only ones that
        # can actually run.
        Path("C:/Program Files/Blender Foundation/Blender 5.1/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 5.0/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"),
        # Linux installs — fine when our Tk app is running on real Linux,
        # not when running under Wine.
        Path("/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender"),
        Path.home() / ".steam/steam/steamapps/common/Blender/blender",
        Path("/usr/bin/blender"),
        Path("/opt/blender/blender"),
    ]
    for c in candidates:
        if c.is_file():
            _check_blender_is_windows_compatible(c)
            return c

    import shutil
    found = shutil.which("blender") or shutil.which("blender.exe")
    if found:
        p = Path(found)
        _check_blender_is_windows_compatible(p)
        return p

    raise FileNotFoundError(
        "Blender not found. Set the override path in Settings or install Blender."
    )


def _check_blender_is_windows_compatible(p: Path) -> None:
    """Refuse a Linux ELF Blender when we're running on Windows-Python.

    Under Wine, calling CreateProcess on an ELF binary fails with WinError
    6. Catching it here gives a clear error pointing at Settings.
    """
    if sys.platform == "win32" and p.suffix.lower() != ".exe":
        raise RuntimeError(
            f"blender path {p} is not a Windows .exe. "
            "Under Wine/Proton you need a Blender for Windows install — "
            "download from blender.org, extract, and point Settings → "
            "Blender executable at blender.exe."
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
    timeout: float = 180.0,
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
    # subproc.run_captured = subprocess.run with the full PyInstaller-windowed
    # hardening (stdin=DEVNULL + STARTF_USESHOWWINDOW + CREATE_NO_WINDOW).
    # Plain subprocess.run fails with WinError 6 under --windowed because the
    # parent's stdio handles are NULL.
    result = run_captured(cmd, timeout=timeout)
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
