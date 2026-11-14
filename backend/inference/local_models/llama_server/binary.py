"""Find and download the llama-server binary."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import release_archive
from ..release_archive import EXE, IS_WINDOWS, RuntimeMissing

logger = logging.getLogger(__name__)

#: The repo root. Four parents up from
#: ``backend/inference/local_models/llama_server/`` -- a wrong count does not
#: raise, it silently points ``bin_dir()`` at a directory nothing ever put a
#: binary in.
_ROOT = Path(__file__).resolve().parents[4]

BINARY_NAME = f"llama-server{EXE}"

#: Verified build. Bump deliberately, never automatically.
DEFAULT_BUILD = "b12032"
REPO_SLUG = "ggml-org/llama.cpp"
#: Outbound only, on the GitHub releases API. Named for the binary rather than
#: for the one feature that happens to use it today.
USER_AGENT = "Orb/llama-server"

#: Build tags carry binaries; the semver tags are nightlies with no assets, so
#: "latest release" is the wrong thing to ask the API for.
_BUILD_TAG = re.compile(r"^b\d+$")


class LlamaServerMissing(RuntimeMissing):
    """No usable llama-server binary. Carries the message the panel shows."""


#: The two builds kept side by side. A fetch installs BOTH, because the GPU
#: setting is a runtime switch between them: one download, then flipping the
#: toggle relaunches against the other directory with nothing to wait for.
FLAVOURS = ("cpu", "gpu")


def bin_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "llama-bin")
    os.makedirs(d, exist_ok=True)
    return d


#: The build tag ``fetch`` installed, beside the two flavour directories.
_BUILD_FILE = "BUILD"


def wanted_build() -> str:
    """The tag the managed runtime must be: the pin, or an ``ORB_LLAMA_CPP_BUILD`` override (``latest`` takes any)."""
    return os.environ.get("ORB_LLAMA_CPP_BUILD") or DEFAULT_BUILD


def installed_build() -> str:
    """The tag ``fetch`` recorded, or ``""`` for an install from before the record (or none)."""
    try:
        return Path(bin_dir(), _BUILD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _managed_current() -> bool:
    installed = installed_build()
    return bool(installed) and wanted_build() in ("latest", installed)


def flavour_dir(gpu: bool) -> str:
    """Where one build lives: ``llama-bin/gpu/`` or ``llama-bin/cpu/``.

    Kept apart rather than swapped in place because swapping is what made the GPU setting a lie -- the panel wrote
    ``--n-gpu-layers 999`` onto whichever single binary had last been unpacked, and a CPU build accepts that flag and ignores
    it, silently and with a zero exit status.
    """
    return os.path.join(bin_dir(), "gpu" if gpu else "cpu")


def _named(path: Path) -> tuple[Path, ...]:
    r"""A path as given, plus the .exe a Windows user meant by it.

    ``ORB_LLAMA_SERVER=C:\llama\llama-server`` is what someone transcribes from
    a Linux README, and it is one suffix away from correct rather than wrong.
    """
    if IS_WINDOWS and not path.suffix:
        return (path.with_suffix(".exe"), path)
    return (path,)


def find_binary(gpu: bool = True) -> Path:
    """Resolve llama-server from env override, PATH, then managed CPU/GPU builds.

    Custom binaries serve both GPU settings; only gpu_layers changes.
    An invalid explicit ORB_LLAMA_SERVER is an error, never a fallback.
    """
    explicit = os.environ.get("ORB_LLAMA_SERVER")
    if explicit:
        path = Path(explicit).expanduser()
        for candidate in _named(path):
            if release_archive.executable(candidate):
                return candidate
        raise LlamaServerMissing(f"ORB_LLAMA_SERVER points at {path}, which is not an executable file.")
    found = shutil.which("llama-server")
    if found:
        return Path(found)
    local = Path(flavour_dir(gpu)) / BINARY_NAME
    if release_archive.executable(local) and _managed_current():
        return local
    if release_archive.executable(local):
        raise LlamaServerMissing(f"The llama.cpp runtime is out of date; download {wanted_build()} again.")
    raise LlamaServerMissing(
        "No llama-server binary. Downloading a model that needs it fetches one, "
        "or point ORB_LLAMA_SERVER at one you already have."
    )


def runtime_ok() -> bool:
    """Whether the runtime is installed. The panel's runtime gate.

    BOTH flavours have to resolve, because the GPU toggle switches between them with no download in the way: half a pair is a
    toggle that works in one direction and silently does nothing in the other. An install from before the split has a flat
    ``llama-bin/`` and reads as missing here, which puts the Download button back on screen -- one press installs the pair, and
    that is the whole migration. A managed pair from another build than the pin reads the same way.
    """
    try:
        for gpu in (False, True):
            find_binary(gpu=gpu)
    except LlamaServerMissing:
        return False
    return True


_HELP_CACHE: dict[str, str] = {}


def _help_text(binary: Path) -> str:
    """``--help`` once per binary, cached, so optional flags can be probed."""
    key = str(binary)
    if key not in _HELP_CACHE:
        try:
            done = subprocess.run(  # noqa: S603 -- binary resolved by find_binary
                [key, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
            _HELP_CACHE[key] = (done.stdout or "") + (done.stderr or "")
        except Exception:  # a binary that will not even print help fails properly at boot
            _HELP_CACHE[key] = ""
    return _HELP_CACHE[key]


def supports_flag(binary: Path, flag: str) -> bool:
    """Whether this build accepts *flag*.

    People bring their own llama-server -- a distro package, a release tarball, a build from last year -- and a flag the binary
    has never heard of is not a warning, it is an immediate exit with a usage message.
    """
    return flag in _help_text(binary)


#: Devices per binary path. ``None`` is "this build could not be asked", which
#: is not the same answer as "this build found nothing".
_DEVICE_CACHE: dict[str, tuple[str, ...] | None] = {}

_DEVICES_HEADER = "available devices:"


def _forget_probes() -> None:
    """Drop every cached probe. Called after a fetch.

    A re-fetch writes the SAME path, so a cache keyed by path would keep answering for the build that was just replaced -- the
    CPU one, in the case somebody swapping to Vulkan is trying to get out of.
    """
    _HELP_CACHE.clear()
    _DEVICE_CACHE.clear()


def _parse_devices(text: str) -> tuple[str, ...] | None:
    """Read non-CPU device names under Available devices.

    Ignore preceding chatter; (none) means no devices. Missing header returns None for older builds whose capability is unknown.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() != _DEVICES_HEADER:
            continue
        names = []
        for entry in lines[index + 1 :]:
            if not entry.strip() or not entry.startswith((" ", "\t")):
                break
            if entry.strip() != "(none)":
                names.append(entry.strip())
        return tuple(names)
    return None


def _probe_devices(binary: Path) -> tuple[str, ...] | None:
    if not supports_flag(binary, "--list-devices"):
        return None
    try:
        done = subprocess.run(  # noqa: S603 -- binary resolved by find_binary
            [str(binary), "--list-devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except Exception:  # a build that will not enumerate fails properly at boot
        return None
    return _parse_devices((done.stdout or "") + (done.stderr or ""))


def devices(binary: Path) -> tuple[str, ...] | None:
    """The non-CPU devices this build can offload to; ``None`` if unknowable.

    Cached per path: the Settings panel polls status every 1.5 s while a model
    loads, and enumerating Vulkan adapters is not free.
    """
    key = str(binary)
    if key not in _DEVICE_CACHE:
        _DEVICE_CACHE[key] = _probe_devices(binary)
    return _DEVICE_CACHE[key]


def gpu_capable(binary: Path) -> bool | None:
    """Whether ``--n-gpu-layers`` means anything to this build. Tri-state.

    THE FLAG IS NOT THE CAPABILITY. Every build parses ``--n-gpu-layers`` and documents it in ``--help``; a CPU-only build then
    has nowhere to put the layers and offloads none of them, silently and with a zero exit status. Asking the binary what
    devices it found is the only honest answer, and it is what stops the panel offering a GPU switch that cannot do anything.
    """
    found = devices(binary)
    return None if found is None else bool(found)


def asset_name(tag: str, backend: str, *, system: str, arch: str) -> str:
    """The release asset for this platform, GPU flavour and architecture. macOS has one build, with Metal."""
    gpu = backend == "gpu"
    if system == "darwin":
        return f"llama-{tag}-bin-macos-{arch}.tar.gz"
    if system == "windows":
        return f"llama-{tag}-bin-win-{'vulkan' if gpu else 'cpu'}-{arch}.zip"
    return f"llama-{tag}-bin-ubuntu-{'vulkan-' if gpu else ''}{arch}.tar.gz"


def _api(url: str):
    return release_archive.github_api(url, USER_AGENT)


def resolve_release(tag: str | None = None) -> dict:
    """The release to take binaries from: the pin, an explicit tag, or newest-with-assets."""
    tag = tag or wanted_build()
    if tag != "latest":
        return _api(f"https://api.github.com/repos/{REPO_SLUG}/releases/tags/{tag}")
    for release in _api(f"https://api.github.com/repos/{REPO_SLUG}/releases?per_page=30"):
        if _BUILD_TAG.fullmatch(release.get("tag_name") or "") and release.get("assets"):
            return release
    raise LlamaServerMissing("No llama.cpp build release with binaries was found.")


def _flatten(unpacked: Path, dest: Path) -> Path:
    """Move the directory that actually contains llama-server into *dest*.

    The Windows zips are flat today and the Linux tarballs are not, and this project has to name one stable path either way --
    the same thing ``tar --strip-components=1`` does, but derived from where the binary landed rather than assumed.
    """
    matches = sorted(unpacked.rglob(BINARY_NAME))
    if not matches:
        raise LlamaServerMissing(f"The downloaded archive contains no {BINARY_NAME}.")
    source = matches[0].parent
    dest.mkdir(parents=True, exist_ok=True)
    for entry in dest.iterdir():  # a re-fetch replaces the previous build wholesale
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for entry in source.iterdir():
        shutil.move(str(entry), str(dest / entry.name))
    return dest / BINARY_NAME


def _prove(binary: Path) -> None:
    """Run ``--version`` before calling a binary installed.

    An archive for the wrong glibc, or a Vulkan build on a machine with no loader, fails here -- which is a message -- rather than
    at the first turn, which is a hang.
    """
    proof = subprocess.run(  # noqa: S603 -- path we just wrote, fixed argv
        [str(binary), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )
    if proof.returncode != 0:
        detail = ((proof.stderr or "") + (proof.stdout or "")).strip()[-400:]
        raise LlamaServerMissing(f"{binary} was unpacked but will not run:\n{detail}")


def _clear_legacy_builds() -> None:
    """Remove a pre-split flat install from ``llama-bin/``.

    Before the CPU and GPU builds were kept apart, the binary and its ~40 shared objects sat loose at this level. Nothing
    resolves them any more and they are a couple of hundred MB that ``bin_bytes`` would still report on the storage row.
    """
    root = Path(bin_dir())
    for entry in root.iterdir():
        if entry.is_dir() and entry.name in FLAVOURS:
            continue
        shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)


def fetch() -> str:
    """Download both managed CPU/GPU builds and verify each with --version. Blocking.

    Shared archives such as macOS are fetched once and unpacked into both directories. Return the GPU build path.
    """
    release = resolve_release()
    tag = release["tag_name"]
    system = release_archive.system()
    try:
        arch = release_archive.arch()
    except RuntimeMissing as exc:
        raise LlamaServerMissing(f"{exc} Build llama.cpp and set ORB_LLAMA_SERVER.") from None
    _clear_legacy_builds()
    installed: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="orb-llama-") as tmp:
        archives: dict[str, Path] = {}
        for flavour in FLAVOURS:
            wanted = asset_name(tag, flavour, system=system, arch=arch)
            if wanted not in archives:
                archives[wanted] = release_archive.download_asset(release, wanted, Path(tmp), USER_AGENT)
            # Unpacked per flavour even when the archive is shared: `_flatten` MOVES what it finds, so a second pass over one
            # unpack directory would find it empty.
            unpacked = Path(tmp) / f"unpacked-{flavour}"
            release_archive.unpack(archives[wanted], unpacked)
            dest = Path(flavour_dir(flavour == "gpu"))
            binary = _flatten(unpacked, dest)
            if not IS_WINDOWS:
                # Windows has no execute bit; everywhere else the archive's mode may not have survived, and a binary nobody may
                # execute is not a binary.
                for entry in dest.iterdir():
                    if entry.is_file():
                        entry.chmod(entry.stat().st_mode | 0o755)
            _prove(binary)
            installed[flavour] = binary
    # The paths did not change, so every cached answer about them is now about a build that is gone. Dropped wholesale rather
    # than per key: `_flatten` replaced directories, not files inside them.
    _forget_probes()
    # Last, after both builds proved themselves: an interrupted fetch leaves no record, so it reads as missing and offers itself
    # again. `_clear_legacy_builds` removed the previous record when this fetch began.
    Path(bin_dir(), _BUILD_FILE).write_text(tag, encoding="utf-8")
    logger.info("llama-server %s ready: %s", tag, ", ".join(f"{k} at {v}" for k, v in installed.items()))
    return str(installed["gpu"])


def bin_bytes() -> int:
    """Total size of ``data/llama-bin/``, for the storage figure on /api/stats."""
    return release_archive.dir_bytes(bin_dir())
