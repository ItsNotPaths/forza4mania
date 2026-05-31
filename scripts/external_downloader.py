"""Download nadeo-freeporter to the user's tools dir.

nadeo-freeporter is the native, single-binary replacement for BOTH
NadeoImporter.exe (FBX → .Mesh/.Shape/.Item.Gbx) AND blendermania's .NET map
tool (place items into a .Map.Gbx, via its ``map`` subcommand). We grab the
latest GitHub release and pick the asset matching the platform forzamania
*runs as*: the Windows build runs under Wine/Proton (sys.platform == 'win32')
and spawns the .exe; the native Linux build spawns the ELF. Either way
freeporter is a child process in the same OS-ABI environment as the app, so
no path translation is needed.
"""
from __future__ import annotations

import json
import stat
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

# nadeo-freeporter ships per-platform zips on its GitHub releases; we always
# take the latest. Asset names contain "linux"/"windows"; each zip holds the
# binary plus LICENSE/README.
FREEPORTER_REPO = "ItsNotPaths/tm2020-freeporter"
FREEPORTER_LATEST_API = f"https://api.github.com/repos/{FREEPORTER_REPO}/releases/latest"


def freeporter_asset_for_platform() -> tuple[str, str]:
    """Return (asset_keyword, binary_filename) for the running platform.

    We match the OS forzamania itself runs as — nadeo-freeporter is spawned as
    a child in the same environment, so the Wine/Windows build needs the .exe
    and the native Linux build needs the ELF.
    """
    if sys.platform == "win32":
        return "windows", "nadeo-freeporter.exe"
    return "linux", "nadeo-freeporter"


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


def _fetch_latest_freeporter_asset(keyword: str) -> tuple[str, str]:
    """Query GitHub for the latest tm2020-freeporter release.

    Returns ``(download_url, release_tag)`` for the .zip asset whose name
    contains ``keyword`` (``"linux"`` or ``"windows"``). Raises if no such
    asset exists in the latest release.
    """
    req = urllib.request.Request(
        FREEPORTER_LATEST_API,
        headers={
            "User-Agent": "forzamania/0.1",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    tag = data.get("tag_name", "?")
    assets = data.get("assets", [])
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if keyword in name and name.endswith(".zip"):
            return asset["browser_download_url"], tag
    raise RuntimeError(
        f"no '{keyword}' .zip asset in {FREEPORTER_REPO} release {tag} "
        f"(assets present: {[a.get('name') for a in assets]})"
    )


def find_freeporter_binary(extracted_files: list[Path]) -> Path | None:
    """Pick the nadeo-freeporter binary out of a freeporter zip's contents."""
    _, binary_name = freeporter_asset_for_platform()
    for p in extracted_files:
        if p.name == binary_name:
            return p
    return None


def download_freeporter(
    tools_dir: Path,
    progress=None,
) -> DownloadResult:
    """Download the latest nadeo-freeporter for this platform into ``tools_dir``.

    Picks the linux/windows asset to match the OS forzamania runs as (see
    ``freeporter_asset_for_platform``), extracts it, and restores the
    executable bit on the ELF (zip extraction drops it) so the runner can
    spawn it directly.
    """
    tools_dir = Path(tools_dir)
    keyword, binary_name = freeporter_asset_for_platform()
    url, tag = _fetch_latest_freeporter_asset(keyword)

    zip_path = tools_dir / f"nadeo-freeporter-{keyword}-{tag}.zip"
    _download_to(url, zip_path, progress=progress)
    written = _unzip(zip_path, tools_dir)
    try:
        zip_path.unlink()
    except OSError:
        pass

    # zipfile.extract loses the Unix exec bit; the Linux ELF must be +x for
    # the runner to exec it.
    if sys.platform != "win32":
        binary = find_freeporter_binary(written)
        if binary is not None:
            binary.chmod(
                binary.stat().st_mode
                | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

    return DownloadResult(
        name="nadeo-freeporter",
        url=url,
        dst_dir=tools_dir,
        extracted_files=written,
    )
