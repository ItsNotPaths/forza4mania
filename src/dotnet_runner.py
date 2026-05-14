"""Invoke Blendermania_Dotnet.exe — the GBX.NET-backed helper that writes
.Map.Gbx files (and a few other GBX-side operations the addon uses).

Same shape as nadeo_runner: subprocess invocation with optional Linux path
translation. The dotnet helper accepts a JSON config file as its second
argument (the first is a command name like ``place-objects-on-map``).

Source: https://github.com/skyslide22/blendermania-dotnet
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from nadeo_runner import to_wine_path


def _exe_dir() -> Path:
    """Where forzamania.exe lives — Settings UI puts downloaded tools here."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Mirrors vendor/blendermania-addon/utils/Constants.py:9-11
CMD_PLACE_OBJECTS_ON_MAP = "place-objects-on-map"
CMD_GET_MEDIATRACKER_CLIPS = "get-mediatracker-clips"
CMD_PLACE_MEDIATRACKER_CLIPS_ON_MAP = "place-mediatracker-clips-on-map"
CMD_CONVERT_ITEM_TO_OBJ = "convert-item-to-obj"

RC_SUCCESS = 0
RC_UNKNOWN_ERROR = 1
RC_GBX_ERROR = 2
RC_INVALID_PAYLOAD = 3
_RC_NAMES = {
    RC_SUCCESS: "ok",
    RC_UNKNOWN_ERROR: "unknown error",
    RC_GBX_ERROR: "GBX error (file possibly malformed)",
    RC_INVALID_PAYLOAD: "invalid payload (JSON config wrong shape)",
}


@dataclass
class DotnetResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == RC_SUCCESS

    @property
    def explanation(self) -> str:
        return _RC_NAMES.get(self.returncode, f"rc={self.returncode}")


def find_blendermania_dotnet(
    override: Path | None = None,
    tm_install_dir: Path | None = None,
) -> Path:
    """Locate Blendermania_Dotnet executable.

    Search order:
      1. Explicit override (Settings UI)
      2. <forzamania.exe dir>/tools/Blendermania_Dotnet.exe (where the
         Download button puts it)
      3. <forzamania.exe dir>/tools/Blendermania_Dotnet/Blendermania_Dotnet.exe
         (in case the zip layout puts it in a subdir)
    """
    if override is not None:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(f"Blendermania_Dotnet override does not exist: {p}")
        return p

    tools = _exe_dir() / "tools"
    candidates = [
        tools / "Blendermania_Dotnet.exe",
        tools / "Blendermania_Dotnet" / "Blendermania_Dotnet.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand

    raise FileNotFoundError(
        "Blendermania_Dotnet.exe not found. Use the Download button in Settings, "
        "or point at an existing install."
    )


def run_dotnet_command(
    dotnet_exe: Path,
    command: str,
    config_path: Path,
    linux_mode: bool = False,
    wine_cmd: list[str] | None = None,
) -> DotnetResult:
    """Run ``Blendermania_Dotnet.exe <command> <config.json>``.

    The dotnet helper does its own JSON parsing; we just pass the path.
    Under Linux/Wine, translate the config path to Z:\\ form.
    """
    arg = to_wine_path(config_path) if linux_mode else str(config_path)
    cmd = list(wine_cmd or []) + [str(dotnet_exe), command, arg]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return DotnetResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def write_json_config(config_path: Path, payload: dict) -> Path:
    """Write a JSON config the dotnet helper consumes. Returns the path."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return config_path
