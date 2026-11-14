"""Download and unpack prebuilt runtime archives from GitHub releases."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""


class RuntimeMissing(RuntimeError):
    """No usable runtime binary. Carries the message the panel shows."""


def system() -> str:
    """``darwin``, ``windows`` or ``linux``."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def arch() -> str:
    """``x64`` or ``arm64``; any other machine has no prebuilt binary."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise RuntimeMissing(f"No prebuilt binary for {machine}.")


def executable(path: Path) -> bool:
    """Whether *path* names a program that can be run; on Windows, which has no execute bit, by its extension."""
    if not path.is_file():
        return False
    if not IS_WINDOWS:
        return os.access(path, os.X_OK)
    suffixes = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    return path.suffix.lower() in {s.strip().lower() for s in suffixes if s.strip()}


def github_api(url: str, user_agent: str):
    """One GitHub API document."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 -- fixed https host
        return json.load(response)


def download_asset(release: dict, wanted: str, into: Path, user_agent: str) -> Path:
    """One release asset onto disk, or ``RuntimeMissing`` naming what the tag does publish."""
    asset = next((a for a in release.get("assets", []) if a.get("name") == wanted), None)
    if asset is None:
        published = ", ".join(sorted(a["name"] for a in release.get("assets", []))) or "nothing"
        raise RuntimeMissing(f"{release['tag_name']} does not publish {wanted}. It publishes: {published}")
    logger.info("Fetching %s (%.0f MB)", wanted, asset.get("size", 0) / 1e6)
    archive = into / wanted
    request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=120) as response, open(archive, "wb") as fh:  # nosec B310 -- github release URL
        shutil.copyfileobj(response, fh)
    return archive


def unpack(archive: Path, into: Path) -> None:
    """Unpack a zip or tar archive, refusing entries that leave *into*."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(into)  # nosec B202 -- official release archive; zipfile strips absolute and .. paths
        return
    with tarfile.open(archive) as tf:
        # The `filter` keyword arrived in 3.11.4; the three 3.11 releases before it raise TypeError on it.
        if hasattr(tarfile, "data_filter"):
            tf.extractall(into, filter="data")  # nosec B202 -- data filter refuses escapes and links
            return
        base = into.resolve()
        for member in tf.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeMissing(f"Illegal tar archive entry: {member.name}")
            target = (base / member_path).resolve()
            if os.path.commonpath([str(base), str(target)]) != str(base):
                raise RuntimeMissing(f"Illegal tar archive entry: {member.name}")
            tf.extract(member, into)  # nosec B202 -- validated member path


def dir_bytes(root: str) -> int:
    """Total size of the files under *root*."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                total += os.path.getsize(path)
    return total
