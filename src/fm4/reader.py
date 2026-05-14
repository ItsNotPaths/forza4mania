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


def _decode_indexed_triangles(indices: np.ndarray, reset_index: int) -> np.ndarray:
    """Decode an FM4 'TriStrip' (IndexType 6) index buffer to a face array.

    The vendored generate_triangle_list assumes a classic continuous
    triangle strip. FM4's actual format for type-6 buffers is a
    0xFFFF-SEPARATED set of runs:

        [a,b,c, 0xFFFF, d,e,f, 0xFFFF, g,h,i,j,k, 0xFFFF, ...]

    Most runs are exactly 3 indices (a plain triangle); some are longer
    (a genuine strip). Splitting on the reset index and decoding each run
    by length handles both — and produces the correct face count instead
    of the ~2.4x inflation the pure-strip decoder gives on separated data.

    Returns an (F, 3) uint32 array.
    """
    faces: list[np.ndarray] = []
    # Boundaries between runs are the reset markers.
    is_reset = indices == reset_index
    # Indices of reset markers; split the array on them.
    split_points = np.flatnonzero(is_reset)
    runs = np.split(indices, split_points)
    for run in runs:
        # The first element of every run after the first IS the reset
        # marker — strip it. (np.split keeps the delimiter at the start.)
        if run.size and run[0] == reset_index:
            run = run[1:]
        n = run.size
        if n < 3:
            continue
        if n == 3:
            faces.append(run.reshape(1, 3))
            continue
        # Genuine strip run: classic alternating-winding triangulation.
        a = run[:-2]
        b = run[1:-1]
        c = run[2:]
        tris = np.stack([a, b, c], axis=1).astype(np.int64)
        # odd triangles get their first two verts swapped
        odd = np.arange(tris.shape[0]) % 2 == 1
        tris[odd, 0], tris[odd, 1] = tris[odd, 1], tris[odd, 0].copy()
        # drop degenerate (collapsed) triangles from the strip stitching
        keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
        faces.append(tris[keep])
    if not faces:
        return np.zeros((0, 3), dtype=np.uint32)
    return np.concatenate(faces).astype(np.uint32)


def _patch_triangle_decoder() -> None:
    """Replace the vendored strip decoder with our run-aware one.

    forza_track_section imports generate_triangle_list by value at module
    load, so we patch it in that module's namespace (not just mesh_util).
    """
    import forza_blender.forza.models.forza_track_section as _fts

    def _shim(indices, reset_index):
        return _decode_indexed_triangles(np.asarray(indices), reset_index)

    _fts.generate_triangle_list = _shim


_patch_triangle_decoder()


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
    # Multi-section rmb files frequently carry FEWER material_sets than
    # sections — all trailing sections share the last matset. Clamp the
    # index instead of IndexError-ing (or, as the old code did, refusing
    # to decode those sections at all — which silently dropped ~60% of
    # all road surface geometry, including pit lanes).
    matset = rmb.material_sets[min(section_idx, len(rmb.material_sets) - 1)]

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
        # Decode EVERY section. A single rmb file's sections are distinct
        # PARTS of one model (e.g. road base + lane surface), not LODs —
        # LODs live in separate files. The old min(sections, matsets)
        # cutoff dropped any section past the matset count, losing the
        # majority of road surface geometry. _decode_track_section clamps
        # the matset lookup so trailing sections reuse the last matset.
        for s_idx in range(len(rmb.track_sections)):
            mesh = _decode_track_section(rmb, s_idx, shaders)
            if mesh is None:
                continue
            key = (model_idx << 8) | s_idx
            meshes[key] = mesh

    # Combine main + lone instance arrays (mirrors addon ops.py:218). FM4
    # tracks split visible placements across both arrays.
    #
    # We keep EVERY instance — no filtering. An earlier version dropped
    # instances with all-zero translation, assuming they were templates.
    # That was wrong: in FM4, large track geometry (road, bridges,
    # guardrails, tunnels, buildings) has its world position baked into
    # the vertex data, so its instance transform is legitimately identity
    # at the origin. Only small *reused* props (trees, lampposts, vehicles)
    # carry a non-zero transform. Filtering on zero-translation threw out
    # the entire track and kept only the garnish. The chunker handles the
    # mixed local/world-space geometry by bucketing on real vertex AABBs.
    instances: list[MeshInstance] = []
    raw_iter = list(pvs.models_instances) + list(getattr(pvs, "lone_models_instances", []))
    for inst in raw_iter:
        instances.append(MeshInstance(
            model_index=inst.model_index,
            transform=_build_transform(inst),
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
