"""Extract FM4 .bix / .stx.bin / .bin textures to standalone .dds files.

Wraps Forza-X360-IO's ``Bix`` decoder (bypassing its bpy-bound save path) so
we get raw DDS bytes we can write into a chunk's Textures/ folder. NadeoImporter
later embeds these into the .Mesh.Gbx via the MeshParams.xml `BaseTexture`
attribute.

Only handles the .bix flavour for v1. CAFF (`.stx.bin` / `.bin`) follows the
same pattern but routes through ``read_bin.CAFF.get_image_from_bin`` —
deferred until tracks need it.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from fm4 import _vendor_setup

_vendor_setup.ensure_loaded()

from forza_blender.forza.textures.read_bix import Bix  # noqa: E402
from forza_blender.forza.utils.deswizzle import Deswizzler  # noqa: E402


# Format dispatch mirrors Bix.get_image_from_bix (vendor/.../forza/textures/read_bix.py).
# Returning DDS bytes instead of round-tripping through bpy.data.images.
def _bix_to_dds(file_a: Path, file_b: Path) -> bytes | None:
    with open(file_a, "rb") as f:
        magic = int.from_bytes(f.read(4), "big")
        if magic not in (1112102960, 1112102961):
            return None
        width = int.from_bytes(f.read(4), "big")
        height = int.from_bytes(f.read(4), "big")
        levels = int.from_bytes(f.read(4), "big")
        fmt = int.from_bytes(f.read(4), "big")

    body = np.fromfile(file_b, np.uint8)
    endian = (fmt & 0xC0) >> 6
    if endian == 1:
        body = Bix.flip_byte_order_16bit(body)
    elif endian == 2:
        body = Bix.flip_byte_order_32bit(body)
    elif endian != 0:
        return None

    blocks = Deswizzler.XGUntileSurfaceToLinearTexture(body, width, height, fmt, levels).tobytes()

    if fmt in (438305106, 438337362):
        return Bix.wrap_as_dds_dx10_bc(71, blocks, width, height)  # BC1
    if fmt == 438337363:
        return Bix.wrap_as_dds_dx10_bc(74, blocks, width, height)  # BC2
    if fmt in (438305108, 438337364):
        return Bix.wrap_as_dds_dx5_bc3_linear(blocks, width, height)  # BC3 (DXT5 FourCC)
    if fmt in (438305147, 438337403):
        return Bix.wrap_as_dds_dx10_bc(80, blocks, width, height)  # BC4
    if fmt == 438305137:
        return Bix.wrap_as_dds_dx10_bc(83, blocks, width, height)  # BC5
    if fmt == 673710470:
        return Bix.wrap_as_dds_dx10_bc(88, blocks, width, height)  # B8G8R8X8
    if fmt == 671088898:
        return Bix.wrap_as_dds_dx10_bc(61, blocks, width, height)  # R8
    return None


def extract_bix_texture(file_index: int, bin_dir: Path, dst_dir: Path) -> Path | None:
    """Decode one PVS-referenced .bix texture to .dds in ``dst_dir``.

    Returns the written path, or None if the source files were missing or
    the format wasn't supported. Bix textures are stored as a pair:
    ``_0xXXXXXXXX.bix`` (header) + ``_0xXXXXXXXX_B.bix`` (pixels).
    """
    name = f"_0x{file_index:08X}"
    file_a = bin_dir / f"{name}.bix"
    file_b = bin_dir / f"{name}_B.bix"
    if not (file_a.is_file() and file_b.is_file()):
        return None

    dds = _bix_to_dds(file_a, file_b)
    if dds is None:
        return None

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{name}.dds"
    dst.write_bytes(dds)
    return dst


def extract_track_textures(track, dst_dir: Path) -> dict[int, Path]:
    """Extract every .bix texture referenced by a TrackIR.

    Returns a mapping of pvs_texture_index → output .dds path. Failures are
    silently dropped (CAFF/stx textures, unsupported formats, missing
    files); call sites should warn on missing keys when binding materials.
    """
    out: dict[int, Path] = {}
    for i, tex in enumerate(track.textures):
        path = extract_bix_texture(tex.file_index, track.bin_dir, dst_dir)
        if path is not None:
            out[i] = path
    return out
