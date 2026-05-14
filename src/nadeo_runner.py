"""Invoke NadeoImporter.exe to convert FBX → .Item.Gbx.

NadeoImporter ships separately from TM2020 (free download from Nadeo or via
the blendermania-assets GitHub release). Two-step pipeline per item:

    NadeoImporter.exe Mesh <fbx>          # FBX + .MeshParams.xml → .Mesh.Gbx + .Shape.Gbx
    NadeoImporter.exe Item <item.xml>     # Item.xml + .Mesh.Gbx → .Item.Gbx

The importer is Windows-only. On Linux, set ``linux_mode=True`` to translate
absolute file paths to ``Z:\\...`` form so a Wine-wrapped invocation can
resolve them. We don't run wine ourselves — that's the user's setup. We
just produce arguments wine can swallow.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def _exe_dir() -> Path:
    """Where forzamania.exe lives — Settings UI puts downloaded tools here."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@dataclass
class NadeoImporterResult:
    kind: str  # "Mesh" or "Item"
    returncode: int
    stdout: str
    stderr: str
    output_files: list[Path]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def find_nadeo_importer(
    override: Path | None = None,
    tm_install_dir: Path | None = None,
) -> Path:
    """Locate NadeoImporter.exe.

    Search order:
      1. Explicit override (Settings UI feeds this)
      2. <forzamania.exe dir>/tools/NadeoImporter.exe (where the Download
         button puts it — see ui/settings_tab.py:_tools_dir)
      3. <TM install>/NadeoImporter.exe (canonical: blendermania-addon
         expects this location too, so users with an existing install win)
    """
    if override is not None:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(f"NadeoImporter override does not exist: {p}")
        return p

    cand = _exe_dir() / "tools" / "NadeoImporter.exe"
    if cand.is_file():
        return cand

    if tm_install_dir is not None:
        cand = Path(tm_install_dir) / "NadeoImporter.exe"
        if cand.is_file():
            return cand

    raise FileNotFoundError(
        "NadeoImporter.exe not found. Use the Download button in Settings, "
        "or point at an existing install."
    )


def to_wine_path(p: Path | str) -> str:
    """Translate an absolute POSIX path into ``Z:\\<path>`` for Wine.

    Wine maps ``/`` onto drive ``Z:`` by default. NadeoImporter rejects
    POSIX path strings inside MeshParams.xml or as CLI args, so when we
    invoke under Wine we have to rewrite. Trailing components keep their
    case; only separator + drive prefix change.
    """
    p = str(p)
    if not p.startswith("/"):
        return p
    return "Z:" + p.replace("/", "\\")


def _run(cmd: list[str], cwd: Path | None) -> tuple[int, str, str]:
    # stdin=DEVNULL: under PyInstaller --windowed the parent's stdin is
    # an invalid handle and subprocess inheriting it raises WinError 6.
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_mesh(
    nadeo_importer: Path,
    fbx_path: Path,
    linux_mode: bool = False,
    wine_cmd: list[str] | None = None,
) -> NadeoImporterResult:
    """Run ``NadeoImporter.exe Mesh <fbx>`` and report the result.

    Expects ``<fbx>.MeshParams.xml`` to exist next to the FBX (write it with
    src/xml_writers.write_mesh_params first). Output: ``<fbx_stem>.Mesh.Gbx``
    and ``<fbx_stem>.Shape.Gbx`` next to the input.

    On Linux, pass ``wine_cmd=["wine"]`` (or a Proton run command) and
    ``linux_mode=True``.
    """
    fbx = Path(fbx_path)
    arg = to_wine_path(fbx) if linux_mode else str(fbx)
    cmd = list(wine_cmd or []) + [str(nadeo_importer), "Mesh", arg]
    rc, out, err = _run(cmd, cwd=fbx.parent)

    produced = []
    for ext in (".Mesh.Gbx", ".Shape.Gbx", ".Trigger.Shape.Gbx"):
        p = fbx.with_suffix(ext)
        if p.is_file():
            produced.append(p)
        # NadeoImporter sometimes lowercases extensions on case-sensitive FS
        p_lc = fbx.with_suffix(ext.lower())
        if p_lc.is_file() and p_lc not in produced:
            os.rename(p_lc, p)
            produced.append(p)

    return NadeoImporterResult(kind="Mesh", returncode=rc, stdout=out, stderr=err, output_files=produced)


def run_item(
    nadeo_importer: Path,
    item_xml_path: Path,
    linux_mode: bool = False,
    wine_cmd: list[str] | None = None,
) -> NadeoImporterResult:
    """Run ``NadeoImporter.exe Item <item.xml>`` and report the result.

    Expects ``<stem>.Mesh.Gbx`` (from a prior run_mesh call) to exist.
    Output: ``<stem>.Item.Gbx`` next to the input.
    """
    xml = Path(item_xml_path)
    arg = to_wine_path(xml) if linux_mode else str(xml)
    cmd = list(wine_cmd or []) + [str(nadeo_importer), "Item", arg]
    rc, out, err = _run(cmd, cwd=xml.parent)

    produced = []
    stem = xml.name.replace(".Item.xml", "")
    item_gbx = xml.parent / f"{stem}.Item.Gbx"
    if item_gbx.is_file():
        produced.append(item_gbx)
    item_gbx_lc = xml.parent / f"{stem}.item.gbx"
    if item_gbx_lc.is_file() and item_gbx not in produced:
        os.rename(item_gbx_lc, item_gbx)
        produced.append(item_gbx)

    return NadeoImporterResult(kind="Item", returncode=rc, stdout=out, stderr=err, output_files=produced)


def convert_chunk(
    nadeo_importer: Path,
    fbx_path: Path,
    linux_mode: bool = False,
    wine_cmd: list[str] | None = None,
) -> tuple[NadeoImporterResult, NadeoImporterResult]:
    """End-to-end convenience: Mesh step then Item step.

    Returns both results so callers can surface whichever step failed.
    Stops at the Item step if Mesh failed.
    """
    mesh_result = run_mesh(nadeo_importer, fbx_path, linux_mode, wine_cmd)
    if not mesh_result.ok:
        return mesh_result, NadeoImporterResult(
            kind="Item", returncode=-1, stdout="", stderr="(skipped: mesh step failed)",
            output_files=[],
        )

    item_xml = Path(fbx_path).with_suffix(".Item.xml")
    item_result = run_item(nadeo_importer, item_xml, linux_mode, wine_cmd)
    return mesh_result, item_result
