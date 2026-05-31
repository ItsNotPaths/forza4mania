#!/usr/bin/env python3
"""x360io — headless FM4 track reader as a standalone CLI.

This is forzamania's "patch" over the vendored Forza-X360-IO addon: it wraps
that addon's pure-Python binary parsers (rmb.bin / PVS / FXL shaders) into a
no-Blender CLI that reads one FM4 track + ribbon and serialises the parsed
geometry to a single ``.npz`` file. forzamania then spawns this binary instead
of importing the parsers in-process, so the heavy numpy/parser code lives in a
freeporter-style native binary and the orchestrator can be anything.

This file is kept in ``patches/`` (tracked) and copied into the cloned
``vendor/Forza-X360-IO/src/`` at build time — vendor/ is never hand-edited. At
build time it sits next to ``forza_blender/`` and is compiled with Nuitka /
PyInstaller; the addon package ships alongside as loose data and is loaded at
runtime via the same namespace-stub bypass forzamania has always used (the
addon's __init__ imports bpy, which we never want to run).

Usage:
    x360io read --bin-dir <extracted bin/ dir> --pvs <ribbon.pvs> \
                --track-name <name> --out <out.npz> [--vendor-src <dir>]

``--bin-dir`` is an already-extracted FM4 ``bin/`` tree (forzamania does the
method-21 bin.zip extraction with its libmspack helper before calling us).
``--vendor-src`` is the dir CONTAINING ``forza_blender/``; needed only when run
interpreted in dev (the compiled binary finds it next to itself).

Output ``.npz`` schema (consumed by src/fm4/reader.py):
    __manifest__ : 0-d array holding a JSON string (see _MANIFEST_VERSION).
    mesh_<key>_vertices        (N,3) f32   — required, per mesh
    mesh_<key>_faces           (F,3) u32   — required
    mesh_<key>_material_per_face (F,) u32  — required
    mesh_<key>_uvs             (N,2) f32   — optional (manifest.has_uvs)
    mesh_<key>_normals         (N,3) f32   — optional (manifest.has_normals)
    instance_transforms        (I,4,4) f32 — one per manifest.instances entry
"""
from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

import numpy as np

_MANIFEST_VERSION = 1


# --------------------------------------------------------------------------
# forza_blender bypass — mirrors forzamania's src/fm4/_vendor_setup.py.
# The addon package __init__ imports bpy and declares Panel subclasses at load;
# deeper parser modules `import bpy`/`mathutils` at the top even though they
# only touch them inside Blender-only functions we never call. We register an
# empty `forza_blender` namespace package (so __init__ never runs) plus minimal
# bpy/mathutils stubs, then import the parser submodules directly.
# --------------------------------------------------------------------------
def _candidate_vendor_srcs(explicit: str | None) -> list[Path]:
    """Dirs that might contain ``forza_blender/``, most-specific first.

    Compiled (Nuitka --include-data-dir / PyInstaller --add-data) ships the
    package next to the binary, so the executable/__file__ dir wins. Dev passes
    ``--vendor-src`` explicitly.
    """
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    # PyInstaller --onefile extracts --add-data to this temp dir.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass))
    here = Path(__file__).resolve().parent
    cands.append(here)
    try:
        cands.append(Path(sys.argv[0]).resolve().parent)
    except (OSError, ValueError):
        pass
    cands.append(Path(sys.executable).resolve().parent)
    cands.append(Path.cwd())
    return cands


def ensure_forza_blender(vendor_src: str | None = None) -> Path:
    """Make ``forza_blender.forza.*`` importable without Blender. Idempotent.

    Returns the resolved vendor-src dir (the one containing forza_blender/).
    Raises FileNotFoundError if no candidate holds the package.
    """
    chosen: Path | None = None
    for cand in _candidate_vendor_srcs(vendor_src):
        if (cand / "forza_blender").is_dir():
            chosen = cand
            break
    if chosen is None:
        raise FileNotFoundError(
            "could not locate forza_blender/ next to the x360io binary; "
            "pass --vendor-src <dir containing forza_blender>"
        )

    if str(chosen) not in sys.path:
        sys.path.insert(0, str(chosen))

    if "forza_blender" not in sys.modules:
        stub = types.ModuleType("forza_blender")
        stub.__path__ = [str(chosen / "forza_blender")]
        sys.modules["forza_blender"] = stub

    if "mathutils" not in sys.modules:
        mu = types.ModuleType("mathutils")

        class _V(list):
            def __init__(self, seq=()):
                super().__init__(seq)

        mu.Vector = _V
        mu.Matrix = _V
        mu.Quaternion = _V
        sys.modules["mathutils"] = mu

    if "bpy" not in sys.modules:
        bpy = types.ModuleType("bpy")
        bpy.data = types.SimpleNamespace(
            meshes=types.SimpleNamespace(new=lambda **k: None)
        )
        bpy.types = types.SimpleNamespace()
        bpy.props = types.SimpleNamespace()
        bpy.utils = types.SimpleNamespace()
        sys.modules["bpy"] = bpy

    return chosen


# --------------------------------------------------------------------------
# FM4 decode — lifted verbatim from forzamania's old in-process reader.py.
# These functions used to live in src/fm4/reader.py; they move here so the
# parser-heavy code ships in this binary and reader.py becomes a thin spawn +
# deserialize shim. Keep the algorithms identical (they encode hard-won FM4
# format heuristics).
# --------------------------------------------------------------------------
def _decode_indexed_triangles(indices: np.ndarray, reset_index: int) -> np.ndarray:
    """Decode an FM4 'TriStrip' (IndexType 6) index buffer to a face array.

    FM4 type-6 buffers are 0xFFFF-SEPARATED runs, not one continuous strip:
        [a,b,c, 0xFFFF, d,e,f, 0xFFFF, g,h,i,j,k, 0xFFFF, ...]
    Most runs are exactly 3 indices (a plain triangle); some are longer (a
    genuine strip). Splitting on the reset index and decoding each run by
    length produces the correct face count instead of the ~2.4x inflation the
    pure-strip decoder gives on separated data. Returns (F,3) uint32.
    """
    faces: list[np.ndarray] = []
    is_reset = indices == reset_index
    split_points = np.flatnonzero(is_reset)
    runs = np.split(indices, split_points)
    for run in runs:
        # np.split keeps the delimiter at the start of each run after the first.
        if run.size and run[0] == reset_index:
            run = run[1:]
        n = run.size
        if n < 3:
            continue
        if n == 3:
            faces.append(run.reshape(1, 3))
            continue
        a = run[:-2]
        b = run[1:-1]
        c = run[2:]
        tris = np.stack([a, b, c], axis=1).astype(np.int64)
        odd = np.arange(tris.shape[0]) % 2 == 1
        tris[odd, 0], tris[odd, 1] = tris[odd, 1], tris[odd, 0].copy()
        keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
        faces.append(tris[keep])
    if not faces:
        return np.zeros((0, 3), dtype=np.uint32)
    return np.concatenate(faces).astype(np.uint32)


def _patch_triangle_decoder() -> None:
    """Replace the vendored strip decoder with our run-aware one.

    forza_track_section imports generate_triangle_list by value at module load,
    so we patch it in that module's namespace (not just mesh_util).
    """
    import forza_blender.forza.models.forza_track_section as _fts

    def _shim(indices, reset_index):
        return _decode_indexed_triangles(np.asarray(indices), reset_index)

    _fts.generate_triangle_list = _shim


def _build_case_index(bin_dir: Path) -> dict[str, Path]:
    """Map every lowercased relative path under bin_dir to its real path.

    FM4 ships filenames in mixed/upper case (Xbox FAT) but PVS shader paths and
    rmb.bin shader_filenames reference them lowercase. Linux being
    case-sensitive, we resolve the ambiguity ourselves. Forward slashes.
    """
    index: dict[str, Path] = {}
    for p in bin_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(bin_dir).as_posix().lower()
            index[rel] = p
    return index


def _resolve(case_index: dict[str, Path], ref: str) -> Path | None:
    key = ref.replace("\\", "/").lower()
    return case_index.get(key)


def _build_transform(instance) -> np.ndarray:
    """PVS instance → 4x4 row-major transform matrix in Forza space."""
    return np.asarray(instance.transform, dtype=np.float32).reshape(4, 4)


_VTYPE_BYTES = {
    2761657: 12,   # D3DDECLTYPE_FLOAT3
    2891865: 4,    # D3DDECLTYPE_USHORT2N
    1712519: 4,    # D3DDECLTYPE_DEC4N
    1583238: 4,    # D3DDECLTYPE_D3DCOLOR
}


def _vdecl_stride(shader) -> int:
    """Sum element sizes from a shader's vertex declaration. 0 if any unknown."""
    total = 0
    for el in shader.vdecl.elements:
        size = _VTYPE_BYTES.get(el.type)
        if size is None:
            return 0
        total += size
    return total


def _pick_shader_for_section(rmb, section, matset, shaders):
    """Find a material whose shader's vdecl stride matches the vertex buffer.

    Picking the wrong material's shader (e.g. always materials[0]) produces
    inf/nan positions when the parser strides bytes at the wrong rate. Falls
    back to the first available shader (better than dropping the mesh).
    """
    target_stride = section.vertex_buffer.stride
    fallback = None
    for mat in matset.materials:
        sname = rmb.shader_filenames[mat.fx_filename_index]
        sh = shaders.get(sname.replace("\\", "/").lower())
        if sh is None:
            continue
        if fallback is None:
            fallback = sh
        if _vdecl_stride(sh) == target_stride:
            return sh
    return fallback


def _decode_track_section(rmb, section_idx: int, shaders: dict) -> dict | None:
    """Build one mesh dict from one RmbBin track section.

    Returns a plain dict (not a dataclass) ready for serialisation:
        {name, vertices, uvs|None, normals|None, faces, material_per_face,
         materials: [{shader_name, texture_sampler_indices, pixel_shader_constants}]}
    """
    section = rmb.track_sections[section_idx]
    # Multi-section rmb files often carry FEWER material_sets than sections;
    # trailing sections share the last matset. Clamp instead of IndexError-ing
    # (or dropping those sections — that silently lost ~60% of road geometry).
    matset = rmb.material_sets[min(section_idx, len(rmb.material_sets) - 1)]
    if not matset.materials:
        return None

    shader = _pick_shader_for_section(rmb, section, matset, shaders)
    if shader is None:
        return None

    try:
        vertices, faces, mat_idx_per_face = section.generate_vertices(shader.vdecl.elements)
    except Exception:
        return None

    positions = np.asarray(vertices.position, dtype=np.float32) if vertices.position is not None else None
    if positions is None or len(positions) == 0:
        return None

    uvs = None
    if vertices.texcoords:
        uv0 = vertices.texcoords[0] if len(vertices.texcoords) > 0 else None
        if uv0 is not None:
            uvs = np.asarray(uv0, dtype=np.float32)

    normals = None
    if vertices.normal is not None:
        normals = np.asarray(vertices.normal, dtype=np.float32)

    materials = []
    for mat in matset.materials:
        sname = rmb.shader_filenames[mat.fx_filename_index]
        materials.append({
            "shader_name": sname,
            "texture_sampler_indices": [int(i) for i in mat.texture_sampler_indices],
            "pixel_shader_constants": [float(c) for c in mat.pixel_shader_constants],
        })

    return {
        "name": section.name,
        "vertices": positions,
        "uvs": uvs,
        "normals": normals,
        "faces": np.asarray(faces, dtype=np.uint32),
        "material_per_face": np.asarray(mat_idx_per_face, dtype=np.uint32),
        "materials": materials,
    }


def read_track(bin_dir: Path, pvs_path: Path, track_name: str) -> tuple[dict, dict]:
    """Parse an FM4 track into (manifest, arrays).

    `manifest` is the JSON-serialisable structure/metadata; `arrays` maps npz
    keys → ndarrays. Together they round-trip into forzamania's TrackIR.
    """
    # Imports must come after ensure_forza_blender() — they trigger the
    # bypassed package and need the bpy/mathutils stubs in place.
    from forza_blender.forza.models.read_rmbbin import RmbBin
    from forza_blender.forza.pvs.pvs_util import BinaryStream
    from forza_blender.forza.pvs.read_pvs import PVS
    from forza_blender.forza.shaders.read_shader import FXLShader

    _patch_triangle_decoder()

    pvs = PVS.from_stream(BinaryStream.from_path(str(pvs_path), ">"))
    case_index = _build_case_index(bin_dir)

    shaders: dict = {}
    for shader_rel_path in pvs.shaders:
        # rmb.bin material entries reference the .fx form (lowercased,
        # backslashes); on-disk filenames may be mixed case.
        fxobj = _resolve(case_index, shader_rel_path + ".fxobj")
        if fxobj is None:
            continue
        try:
            shaders[(shader_rel_path + ".fx").replace("\\", "/").lower()] = (
                FXLShader.from_stream(BinaryStream.from_path(str(fxobj), ">"))
            )
        except Exception:
            pass

    referenced = sorted({mi.model_index for mi in pvs.models_instances})
    arrays: dict = {}
    mesh_manifest: list[dict] = []
    for model_idx in referenced:
        rmb_path = _resolve(case_index, f"{pvs.prefix}.{model_idx:05d}.rmb.bin")
        if rmb_path is None:
            continue
        try:
            rmb = RmbBin.from_path(str(rmb_path))
        except Exception:
            continue
        # Decode EVERY section — sections are distinct PARTS of one model (road
        # base + lane surface), not LODs (those live in separate files).
        for s_idx in range(len(rmb.track_sections)):
            mesh = _decode_track_section(rmb, s_idx, shaders)
            if mesh is None:
                continue
            key = (model_idx << 8) | s_idx
            arrays[f"mesh_{key}_vertices"] = mesh["vertices"]
            arrays[f"mesh_{key}_faces"] = mesh["faces"]
            arrays[f"mesh_{key}_material_per_face"] = mesh["material_per_face"]
            entry = {
                "key": int(key),
                "name": mesh["name"],
                "has_uvs": mesh["uvs"] is not None,
                "has_normals": mesh["normals"] is not None,
                "materials": mesh["materials"],
            }
            if mesh["uvs"] is not None:
                arrays[f"mesh_{key}_uvs"] = mesh["uvs"]
            if mesh["normals"] is not None:
                arrays[f"mesh_{key}_normals"] = mesh["normals"]
            mesh_manifest.append(entry)

    # Combine main + lone instance arrays — FM4 splits placements across both.
    # Keep EVERY instance: large track geometry bakes its world position into
    # the vertices (identity transform at origin is legitimate); only reused
    # props carry non-zero transforms. The chunker buckets on real vertex AABBs.
    instance_manifest: list[dict] = []
    transforms: list[np.ndarray] = []
    raw_iter = list(pvs.models_instances) + list(getattr(pvs, "lone_models_instances", []))
    for inst in raw_iter:
        transforms.append(_build_transform(inst))
        instance_manifest.append({
            "model_index": int(inst.model_index),
            "texture_index": int(inst.texture),
            "flags": int(inst.flags),
        })
    if transforms:
        arrays["instance_transforms"] = np.stack(transforms).astype(np.float32)
    else:
        arrays["instance_transforms"] = np.zeros((0, 4, 4), dtype=np.float32)

    texture_manifest: list[dict] = []
    for t in pvs.textures:
        texture_manifest.append({
            "file_index": int(t.texture_file_name),
            "is_stx": False,
            "u_scale": float(getattr(t, "u_scale", 1.0)),
            "v_scale": float(getattr(t, "v_scale", 1.0)),
            "u_translate": float(getattr(t, "u_translate", 0.0)),
            "v_translate": float(getattr(t, "v_translate", 0.0)),
        })

    manifest = {
        "version": _MANIFEST_VERSION,
        "track_name": track_name,
        "prefix": pvs.prefix,
        "shader_names": list(pvs.shaders),
        "meshes": mesh_manifest,
        "instances": instance_manifest,
        "textures": texture_manifest,
    }
    return manifest, arrays


def cmd_read(args: argparse.Namespace) -> int:
    ensure_forza_blender(args.vendor_src)

    bin_dir = Path(args.bin_dir)
    pvs_path = Path(args.pvs)
    if not bin_dir.is_dir():
        print(f"error: --bin-dir not a directory: {bin_dir}", file=sys.stderr)
        return 2
    if not pvs_path.is_file():
        print(f"error: --pvs not found: {pvs_path}", file=sys.stderr)
        return 2

    manifest, arrays = read_track(bin_dir, pvs_path, args.track_name)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(arrays)
    payload["__manifest__"] = np.array(json.dumps(manifest), dtype=object)
    np.savez_compressed(out, **payload)
    # np.savez_compressed appends .npz if the name lacks it; normalise so the
    # caller finds the file at exactly --out.
    written = out if out.suffix == ".npz" else out.with_suffix(out.suffix + ".npz")
    if written != out and written.is_file():
        written.replace(out)

    print(
        f"SUCCESS: {args.track_name} -> {out} "
        f"({len(manifest['meshes'])} meshes, {len(manifest['instances'])} instances, "
        f"{len(manifest['textures'])} textures)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="x360io", description="Headless FM4 track reader (Forza-X360-IO).")
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("read", help="parse an FM4 track+ribbon into a .npz TrackIR")
    r.add_argument("--bin-dir", required=True, help="already-extracted FM4 bin/ tree")
    r.add_argument("--pvs", required=True, help="ribbon .pvs file")
    r.add_argument("--track-name", required=True, help="track name (stored in the manifest)")
    r.add_argument("--out", required=True, help="output .npz path")
    r.add_argument("--vendor-src", default=None,
                   help="dir containing forza_blender/ (dev only; auto-found next to the binary)")
    r.set_defaults(func=cmd_read)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
