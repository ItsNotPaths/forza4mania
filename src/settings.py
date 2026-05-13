"""Persistent app settings (paths, modes) backed by a JSON file.

Auto-detects Steam Blender + TM2020 install on first run; user can override
any field via the Settings tab in the Tk UI. Settings file lives at
~/.config/forzamania/settings.json (Linux) or %APPDATA%/forzamania/settings.json
(Windows).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _user_config_dir() -> Path:
    """Standard per-user config dir for the current OS."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "forzamania"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "forzamania"
    return Path.home() / ".config" / "forzamania"


SETTINGS_PATH = _user_config_dir() / "settings.json"


@dataclass
class Settings:
    """All app-wide settings. Default values are auto-detect heuristics; the
    UI fills them in on first launch and lets the user override.
    """
    # Source data
    fm4_install_dir: str = ""           # e.g. /path/to/xenia/.../4D530910/00007000/33E7B39F/Media
    blender_path: str = ""              # absolute path to `blender` binary

    # Output targets
    tm_install_dir: str = ""            # where Trackmania.exe + NadeoImporter live
    tm_user_dir: str = ""               # Documents/Trackmania (where Items/ + Maps/ go)

    # External tool paths (resolved by the runners; nullable)
    nadeo_importer_path: str = ""       # NadeoImporter.exe override
    blendermania_dotnet_path: str = ""  # Blendermania_Dotnet.exe override

    # Modes
    linux_mode: bool = False            # rewrite paths to Z:\ for Wine
    wine_command: list[str] = field(default_factory=list)
        # extra command parts before NadeoImporter when running on Linux/Wine

    # Conversion knobs
    tile_size_m: float = 64.0
    tri_budget: int = 50_000
    default_surface_link: str = "PlatformTech"
    default_physics_id: str = "Asphalt"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or SETTINGS_PATH
        if not path.is_file():
            s = cls()
            s.autodetect()
            return s
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()
        # Tolerate missing/extra keys
        known = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    def save(self, path: Path | None = None) -> Path:
        path = path or SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    def autodetect(self) -> None:
        """Best-effort probe for Blender + TM install. Only fills BLANK fields
        — never overwrites a value the user has already chosen."""
        if not self.blender_path:
            self.blender_path = _detect_blender() or ""
        if not self.tm_install_dir:
            self.tm_install_dir = _detect_tm_install() or ""
        if not self.tm_user_dir and self.tm_install_dir:
            self.tm_user_dir = _detect_tm_user_dir(Path(self.tm_install_dir)) or ""
        if not self.fm4_install_dir:
            self.fm4_install_dir = _detect_fm4() or ""
        # Linux mode is OS-driven, not auto-detect to True if a Windows binary
        # would also need wine — just propose, don't decide.
        if sys.platform != "win32" and not self.wine_command:
            wine = shutil.which("wine") or shutil.which("wine64")
            if wine:
                self.wine_command = [wine]


# ---- detection helpers -------------------------------------------------

_TM_GUESSES = [
    "/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Trackmania",
    "~/.steam/steam/steamapps/common/Trackmania",
    "~/.local/share/Steam/steamapps/common/Trackmania",
    "C:/Program Files (x86)/Ubisoft/Ubisoft Game Launcher/games/Trackmania",
    "C:/Program Files (x86)/Steam/steamapps/common/Trackmania",
]

_BLENDER_GUESSES = [
    "/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Blender/blender",
    "~/.steam/steam/steamapps/common/Blender/blender",
    "/usr/bin/blender",
    "/opt/blender/blender",
    "C:/Program Files/Blender Foundation/Blender 5.1/blender.exe",
    "C:/Program Files/Blender Foundation/Blender 5.0/blender.exe",
    "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe",
]

_FM4_GUESSES = [
    "/run/media/paths/SSS-Games/xenia_canary_windows/content/0000000000000000/4D530910/00007000/33E7B39F/Media",
]


def _first_existing(candidates: list[str]) -> str | None:
    for c in candidates:
        p = Path(os.path.expanduser(c))
        if p.exists():
            return str(p)
    return None


def _detect_blender() -> str | None:
    found = _first_existing(_BLENDER_GUESSES)
    if found:
        return found
    on_path = shutil.which("blender")
    return on_path if on_path else None


def _detect_tm_install() -> str | None:
    return _first_existing(_TM_GUESSES)


def _detect_tm_user_dir(tm_install: Path) -> str | None:
    """Find ``Documents/Trackmania/`` — on Linux/Proton this is inside the
    Steam compatdata prefix, not the user's $HOME."""
    # Steam Linux: Trackmania app id is 2225540
    candidates: list[Path] = []
    candidates.append(Path.home() / "Documents" / "Trackmania")  # Windows-style
    # Steam Proton prefix for TM2020
    tm_root_idx = str(tm_install).find("steamapps")
    if tm_root_idx > 0:
        steam_root = Path(str(tm_install)[:tm_root_idx])
        candidates.append(
            steam_root / "steamapps" / "compatdata" / "2225540" / "pfx"
            / "drive_c" / "users" / "steamuser" / "Documents" / "Trackmania"
        )
    for c in candidates:
        if c.is_dir():
            return str(c)
    return None


def _detect_fm4() -> str | None:
    return _first_existing(_FM4_GUESSES)
