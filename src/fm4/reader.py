"""Read an FM4 track from disk into a TrackIR.

Adapts vendor/Forza-X360-IO/src/forza_blender's PVS + RmbBin parsers without
running them inside Blender (see _vendor_setup for the bypass mechanism).

Public entry point: ``read_track(track_dir, ribbon_dir) -> TrackIR``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import _vendor_setup
from .extractor import extract_bin_zip
from .ir import Material, MeshData, MeshInstance, TextureRef, TrackIR

_vendor_setup.ensure_loaded()

# These imports must come AFTER ensure_loaded() — they trigger the bypassed
# forza_blender package + need the bpy/mathutils stubs in place.
from forza_blender.forza.models.read_rmbbin import RmbBin  # noqa: E402
from forza_blender.forza.pvs.pvs_util import BinaryStream  # noqa: E402
from forza_blender.forza.pvs.read_pvs import PVS  # noqa: E402
from forza_blender.forza.shaders.read_shader import FXLShader  # noqa: E402


def _build_case_index(bin_dir: Path) -> dict[str, Path]:
    """Map every lowercased relative path under bin_dir to its real path.

    FM4 ships filenames in mixed/upper case (Xbox FAT) but PVS shader paths
    and rmb.bin shader_filenames reference them in lowercase. Linux being
    case-sensitive, we have to resolve the ambiguity ourselves.
    Forward slashes everywhere — Forza references use Windows backslashes.
    """
    index: dict[str, Path] = {}
    for p in bin_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(bin_dir).as_posix().lower()
            index[rel] = p
    return index


def _resolve(case_index: dict[str, Path], ref: str) -> Path | None:
    """Resolve a Forza-style backslash path against the case-insensitive index."""
    key = ref.replace("\\", "/").lower()
    return case_index.get(key)


def _build_transform(instance) -> np.ndarray:
    """PVS instance → 4x4 row-major transform matrix in Forza space.

    PVSModelInstance.transform is already the assembled 4x4 (list of lists).
    """
    return np.asarray(instance.transform, dtype=np.float32).reshape(4, 4)


_VTYPE_BYTES = {
    2761657: 12,   # D3DDECLTYPE_FLOAT3
    2891865: 4,    # D3DDECLTYPE_USHORT2N
    1712519: 4,    # D3DDECLTYPE_DEC4N
    1583238: 4,    # D3DDECLTYPE_D3DCOLOR
}


def _vdecl_stride(shader: FXLShader) -> int:
    """Sum element sizes from a shader's vertex declaration.

    Mirrors what ForzaVertex.from_buffer expects (see vendor/.../forza/models/
    forza_vertex.py). Returns 0 if any element type is unknown — meaning we
    can't trust the computed stride at all.
    """
    total = 0
    for el in shader.vdecl.elements:
        size = _VTYPE_BYTES.get(el.type)
        if size is None:
            return 0
        total += size
    return total


def _pick_shader_for_section(
    rmb: RmbBin,
    section: "object",
    matset: "object",
    shaders: dict[str, FXLShader],
) -> FXLShader | None:
    """Find a material whose shader's vdecl stride matches the vertex buffer.

    A section can hold multiple materials with *different* shaders, only one
    of which uses the same vertex layout as the buffer. Picking the wrong
    one (e.g., always materials[0]) produces inf/nan vertex positions when
    the parser strides through bytes at the wrong rate.

    Falls back to the first available shader if no stride match is found —
    that's better than dropping the mesh entirely.
    """
    target_stride = section.vertex_buffer.stride
    fallback: FXLShader | None = None
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


def _decode_track_section(rmb: RmbBin, section_idx: int, shaders: dict[str, FXLShader]) -> MeshData | None:
    """Build one MeshData from one RmbBin track section."""
    section = rmb.track_sections[section_idx]
    matset = rmb.material_sets[section_idx]

    if not matset.materials:
        return None

    shader = _pick_shader_for_section(rmb, section, matset, shaders)
    if shader is None:
        return None

    # generate_vertices returns (ForzaVertex, faces ndarray, mat_idx_per_face ndarray)
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
        materials.append(Material(
            shader_name=sname,
            texture_sampler_indices=list(mat.texture_sampler_indices),
            pixel_shader_constants=list(mat.pixel_shader_constants),
        ))

    return MeshData(
        name=section.name,
        vertices=positions,
        uvs=uvs,
        normals=normals,
        faces=np.asarray(faces, dtype=np.uint32),
        material_per_face=np.asarray(mat_idx_per_face, dtype=np.uint32),
        materials=materials,
    )


def read_track(track_dir: Path | str, ribbon_dir: Path | str) -> TrackIR:
    """Parse an FM4 track + ribbon into a TrackIR.

    `track_dir` must contain `bin.zip` (will be extracted to `working/` if not
    already). `ribbon_dir` must contain a `*.pvs` file.
    """
    track_dir = Path(track_dir)
    ribbon_dir = Path(ribbon_dir)

    bin_dir = extract_bin_zip(track_dir)

    pvs_files = list(ribbon_dir.glob("*.pvs"))
    if not pvs_files:
        raise FileNotFoundError(f"no .pvs in {ribbon_dir}")
    pvs_path = pvs_files[0]

    pvs = PVS.from_stream(BinaryStream.from_path(str(pvs_path), ">"))
    case_index = _build_case_index(bin_dir)

    shaders: dict[str, FXLShader] = {}
    for shader_rel_path in pvs.shaders:
        # PVS paths use backslashes and lowercase; on-disk filenames may be
        # mixed case. The dict is keyed by the .fx form because that's what
        # rmb.bin material entries reference (see Forza-X360-IO model_util.py:92).
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
    meshes: dict[int, MeshData] = {}
    for model_idx in referenced:
        rmb_path = _resolve(case_index, f"{pvs.prefix}.{model_idx:05d}.rmb.bin")
        if rmb_path is None:
            continue
        try:
            rmb = RmbBin.from_path(str(rmb_path))
        except Exception:
            continue
        section_count = min(len(rmb.track_sections), len(rmb.material_sets))
        for s_idx in range(section_count):
            mesh = _decode_track_section(rmb, s_idx, shaders)
            if mesh is None:
                continue
            key = (model_idx << 8) | s_idx
            meshes[key] = mesh

    # Combine main + lone instance arrays (mirrors addon ops.py:218). FM4
    # tracks split visible placements across both arrays. Within
    # models_instances, ~60% of entries on real FM4 data have all-zero
    # translation + identity rotation + flags=0x0fff0214 — they're some kind
    # of template/deferred marker that contributes no visible geometry. We
    # drop them so they don't all collapse at world origin and dominate the
    # bbox / chunker buckets.
    instances: list[MeshInstance] = []
    raw_iter = list(pvs.models_instances) + list(getattr(pvs, "lone_models_instances", []))
    for inst in raw_iter:
        m = _build_transform(inst)
        is_origin = (
            m[0, 3] == 0.0 and m[1, 3] == 0.0 and m[2, 3] == 0.0
        )
        if is_origin and (inst.flags & 0x0FFF0000) == 0x0FFF0000:
            continue
        instances.append(MeshInstance(
            model_index=inst.model_index,
            transform=m,
            texture_index=inst.texture,
            flags=inst.flags,
        ))

    textures: list[TextureRef] = []
    for t in pvs.textures:
        textures.append(TextureRef(
            file_index=t.texture_file_name,
            is_stx=False,
            u_scale=getattr(t, "u_scale", 1.0),
            v_scale=getattr(t, "v_scale", 1.0),
            u_translate=getattr(t, "u_translate", 0.0),
            v_translate=getattr(t, "v_translate", 0.0),
        ))

    return TrackIR(
        track_name=track_dir.name,
        prefix=pvs.prefix,
        bin_dir=bin_dir,
        meshes=meshes,
        instances=instances,
        textures=textures,
        shader_names=list(pvs.shaders),
    )
