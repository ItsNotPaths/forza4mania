"""Download NadeoImporter and Blendermania_Dotnet to the user's tools dir.

NadeoImporter comes from Nadeo's official CDN (free, no auth, stable URL).
Blendermania_Dotnet comes from the blendermania-assets GitHub release
(the addon team distributes it on their own; we piggyback because we don't
build the .NET helper ourselves).
"""
from __future__ import annotations

import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Latest TM2020-compatible NadeoImporter version (per
# vendor/blendermania-addon/properties/Functions.py:78)
NADEO_IMPORTER_VERSION = "2022_07_12"

# Per vendor/blendermania-addon/utils/Constants.py:206
BLENDERMANIA_DOTNET_VERSION = "v1.0.0"

# Official Nadeo CDN — bypasses any third-party mirror
NADEO_IMPORTER_URL = (
    f"https://nadeo-download.cdn.ubi.com/trackmania/NadeoImporter_{NADEO_IMPORTER_VERSION}.zip"
)

# blendermania-assets mirror — the addon team's own release of their .NET helper
_GITHUB_ASSETS = "https://github.com/skyslide22/blendermania-assets/releases/download"
BLENDERMANIA_DOTNET_URL = (
    f"{_GITHUB_ASSETS}/Blendermania_Dotnet_{BLENDERMANIA_DOTNET_VERSION}/"
    f"Blendermania_Dotnet_{BLENDERMANIA_DOTNET_VERSION}.zip"
)


@dataclass
class DownloadResult:
    name: str
    url: str
    dst_dir: Path
    extracted_files: list[Path]


def _download_to(url: str, dst: Path, progress=None) -> None:
    """Download `url` to `dst`. `progress(bytes_so_far, total_bytes)` callback optional.

    Validates the response is a zip (or at least not HTML) before writing —
    avoids silently saving a 404/error page as a corrupt "zip" the user only
    notices when extraction fails.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "forzamania/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" in ctype or "text" in ctype:
            raise RuntimeError(
                f"download URL returned {ctype} (expected zip): {url} — "
                f"the server probably gave a 404 or error page instead of the asset"
            )

        total = int(resp.headers.get("Content-Length", "0") or 0)
        dst.parent.mkdir(parents=True, exist_ok=True)
        bytes_done = 0
        with open(dst, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                bytes_done += len(chunk)
                if progress is not None:
                    progress(bytes_done, total)

    # Magic-byte check: PKZIP files start with "PK\x03\x04" (or PK\x05\x06 for
    # an empty zip). Catches the case where Content-Type lied.
    with open(dst, "rb") as f:
        head = f.read(4)
    if not head.startswith(b"PK"):
        raise RuntimeError(
            f"downloaded file from {url} is not a zip "
            f"(first bytes: {head!r}) — server probably returned an error page"
        )


def _unzip(zip_path: Path, dst_dir: Path) -> list[Path]:
    """Extract `zip_path` into `dst_dir`. Standard PKZIP — no Forza method-21
    weirdness here. Returns the list of files written."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            out = dst_dir / info.filename
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())
            written.append(out)
    return written


def download_nadeo_importer(
    tools_dir: Path,
    progress=None,
) -> DownloadResult:
    """Download NadeoImporter zip and extract into ``tools_dir``.

    `tools_dir` should typically be ``<TM install>/forzamania-tools/``.
    The blendermania-addon also accepts ``<TM install>/`` directly (canonical
    NadeoImporter location); we use a subfolder so we don't pollute the
    install with our copy.
    """
    tools_dir = Path(tools_dir)
    zip_path = tools_dir / f"NadeoImporter_{NADEO_IMPORTER_VERSION}.zip"
    _download_to(NADEO_IMPORTER_URL, zip_path, progress=progress)
    written = _unzip(zip_path, tools_dir)
    try:
        zip_path.unlink()
    except OSError:
        pass
    return DownloadResult(
        name="NadeoImporter",
        url=NADEO_IMPORTER_URL,
        dst_dir=tools_dir,
        extracted_files=written,
    )


def download_blendermania_dotnet(
    tools_dir: Path,
    progress=None,
) -> DownloadResult:
    """Download Blendermania_Dotnet zip and extract into ``tools_dir``."""
    tools_dir = Path(tools_dir)
    zip_path = tools_dir / f"Blendermania_Dotnet_{BLENDERMANIA_DOTNET_VERSION}.zip"
    _download_to(BLENDERMANIA_DOTNET_URL, zip_path, progress=progress)
    written = _unzip(zip_path, tools_dir)
    try:
        zip_path.unlink()
    except OSError:
        pass
    return DownloadResult(
        name="Blendermania_Dotnet",
        url=BLENDERMANIA_DOTNET_URL,
        dst_dir=tools_dir,
        extracted_files=written,
    )
