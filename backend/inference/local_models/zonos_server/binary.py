"""Find and download the ZONOS3 server binary."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import release_archive
from ..release_archive import EXE, IS_WINDOWS, RuntimeMissing

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[4]

BINARY_NAME = f"zonos3-server{EXE}"

#: Verified build of the fork that adds reference codes, the end-of-audio hold and ffmpeg-free WAV uploads.
DEFAULT_BUILD = "ggml-v0.1.0-orb3"
REPO_SLUG = "OrbFrontend/ZONOS3-Mini"
USER_AGENT = "Orb/zonos3-server"

#: Largest archive per system, for the panel's download size.
ARCHIVE_MB = {"linux": 107, "windows": 111, "darwin": 35}

_BUILD_FILE = "BUILD"
_LIBRARY_SUFFIXES = (".dll", ".so", ".dylib")
_KEPT_NAMES = ("LICENSE", "NOTICE", "third_party")


class ZonosServerMissing(RuntimeMissing):
    """No usable zonos3-server binary."""


def bin_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "zonos-bin")
    os.makedirs(d, exist_ok=True)
    return d


def wanted_build() -> str:
    """The tag the managed runtime must be: the pin, or an ``ORB_ZONOS3_BUILD`` override."""
    return os.environ.get("ORB_ZONOS3_BUILD") or DEFAULT_BUILD


def _record() -> list[str]:
    try:
        return Path(bin_dir(), _BUILD_FILE).read_text(encoding="utf-8").split()
    except OSError:
        return []


def installed_build() -> str:
    """The tag ``fetch`` recorded, or ``""``."""
    return (_record() or [""])[0]


def gpu_capable() -> bool:
    """Whether the installed build can run on a GPU; a custom binary is taken at its word."""
    if os.environ.get("ORB_ZONOS3_SERVER"):
        return True
    return not any("-cpu." in part for part in _record()[1:])


def asset_names(*, system: str, arch: str) -> tuple[str, ...]:
    """The release assets for this platform, best first. Linux falls back to the CPU build where the Vulkan one will not run."""
    if (system, arch) == ("darwin", "arm64"):
        return ("zonos3-macos-arm64-metal.tar.gz",)
    if (system, arch) == ("windows", "x64"):
        return ("zonos3-windows-x64-vulkan.zip",)
    if (system, arch) == ("linux", "x64"):
        return ("zonos3-linux-x64-vulkan.tar.gz", "zonos3-linux-x64-cpu.tar.gz")
    raise ZonosServerMissing(f"No prebuilt ZONOS3 server for {system} {arch}; build one and set ORB_ZONOS3_SERVER.")


def find_binary() -> Path:
    """Resolve zonos3-server from ``ORB_ZONOS3_SERVER``, then the managed build."""
    explicit = os.environ.get("ORB_ZONOS3_SERVER")
    if explicit:
        path = Path(explicit).expanduser()
        if release_archive.executable(path):
            return path
        raise ZonosServerMissing(f"ORB_ZONOS3_SERVER points at {path}, which is not an executable file.")
    local = Path(bin_dir()) / BINARY_NAME
    if not release_archive.executable(local):
        raise ZonosServerMissing("The ZONOS3 runtime is not downloaded.")
    if installed_build() != wanted_build():
        raise ZonosServerMissing(f"The ZONOS3 runtime is out of date; download {wanted_build()} again.")
    return local


def runtime_ok() -> bool:
    try:
        find_binary()
    except ZonosServerMissing:
        return False
    return True


def _prove(binary: Path) -> str:
    """Why *binary* will not run, or ``""``. Started with no arguments, a working build prints its usage."""
    try:
        proof = subprocess.run(  # noqa: S603 -- path we just wrote, fixed argv
            [str(binary)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        )
    except OSError as exc:
        return str(exc)
    output = (proof.stdout or "") + (proof.stderr or "")
    return "" if "usage:" in output else output.strip()[-400:] or f"exit status {proof.returncode}"


def _install(unpacked: Path, dest: Path) -> Path:
    """Move the server, its libraries and its licences into an emptied *dest*; the archive's other tools stay behind."""
    matches = sorted(unpacked.rglob(BINARY_NAME))
    if not matches:
        raise ZonosServerMissing(f"The downloaded archive contains no {BINARY_NAME}.")
    source = matches[0].parent
    for entry in dest.iterdir():
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for entry in source.iterdir():
        if entry.name == BINARY_NAME or entry.name in _KEPT_NAMES or entry.suffix.lower() in _LIBRARY_SUFFIXES:
            shutil.move(str(entry), str(dest / entry.name))
    binary = dest / BINARY_NAME
    if not IS_WINDOWS:
        binary.chmod(binary.stat().st_mode | 0o755)
    return binary


def fetch() -> str:
    """Download the managed build for this platform and check that it runs. Blocking. Returns the binary's path."""
    system = release_archive.system()
    try:
        names = asset_names(system=system, arch=release_archive.arch())
    except ZonosServerMissing:
        raise
    except RuntimeMissing as exc:
        raise ZonosServerMissing(f"{exc} Build the ZONOS3 server and set ORB_ZONOS3_SERVER.") from None
    tag = wanted_build()
    release = release_archive.github_api(f"https://api.github.com/repos/{REPO_SLUG}/releases/tags/{tag}", USER_AGENT)
    dest = Path(bin_dir())
    failure = ""
    with tempfile.TemporaryDirectory(prefix="orb-zonos-") as tmp:
        for name in names:
            archive = release_archive.download_asset(release, name, Path(tmp), USER_AGENT)
            unpacked = Path(tmp) / f"unpacked-{name}"
            release_archive.unpack(archive, unpacked)
            binary = _install(unpacked, dest)
            failure = _prove(binary)
            if not failure:
                Path(dest, _BUILD_FILE).write_text(f"{tag} {name}", encoding="utf-8")
                logger.info("zonos3-server %s ready at %s (%s)", tag, binary, name)
                return str(binary)
            logger.warning("%s was unpacked but will not run: %s", name, failure)
    raise ZonosServerMissing(f"The ZONOS3 runtime was downloaded but will not run:\n{failure}")


def bin_bytes() -> int:
    """Total size of ``data/zonos-bin/``, for the storage figure on /api/stats."""
    return release_archive.dir_bytes(bin_dir())
