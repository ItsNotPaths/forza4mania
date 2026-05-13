"""Make Forza-X360-IO's parser modules importable outside Blender.

Forza-X360-IO is a Blender addon: its package __init__ imports `bpy` and
declares `bpy.types.Panel` subclasses at module load. Several deeper parser
modules also `import bpy` (and `mathutils`) at the top, even though they only
use those modules inside Blender-specific functions we don't call.

We bypass the addon __init__ entirely by pre-registering an empty
`forza_blender` namespace package whose __path__ points at the real package
dir. Submodule imports (`forza_blender.forza.pvs.pvs_util` etc.) then resolve
without triggering the addon's __init__.py. Minimal `bpy` and `mathutils`
stubs satisfy the top-level imports.

Call ``ensure_loaded()`` exactly once before importing any forza_blender.*
parsers. It is idempotent.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


def _bundle_root() -> Path:
    """Return the dir that holds vendor/, scripts/, etc.

    PyInstaller --onefile extracts data files to a temp dir exposed as
    ``sys._MEIPASS``; everything we --add-data'd lives under there. In a
    normal source checkout, the repo root is three levels up from this file
    (src/fm4/_vendor_setup.py → src/fm4 → src → repo).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent.parent


REPO = _bundle_root()
VENDOR = REPO / "vendor" / "Forza-X360-IO" / "src"

_loaded = False


def ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return

    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))

    pkg_dir = VENDOR / "forza_blender"
    if "forza_blender" not in sys.modules:
        stub = types.ModuleType("forza_blender")
        stub.__path__ = [str(pkg_dir)]
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

    _loaded = True
