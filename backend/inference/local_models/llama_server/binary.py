"""Find and download the llama-server binary."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: The repo root. Four parents up from
#: ``backend/inference/local_models/llama_server/`` — a wrong count does not
#: raise, it silently points ``bin_dir()`` at a directory nothing ever put a
#: binary in. Pinned by ``tests/unit/test_local_models_paths.py``.
_ROOT = Path(__file__).resolve().parents[4]

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
BINARY_NAME = f"llama-server{EXE}"

#: Verified build. Bump deliberately, never automatically.
DEFAULT_BUILD = "b10549"
REPO_SLUG = "ggml-org/llama.cpp"
#: Outbound only, on the GitHub releases API. Named for the binary rather than
#: for the one feature that happens to use it today.
USER_AGENT = "Orb/llama-server"

#: Build tags carry binaries; the semver tags are nightlies with no assets, so
#: "latest release" is the wrong thing to ask the API for.
_BUILD_TAG = re.compile(r"^b\d+$")


class LlamaServerMissing(RuntimeError):
    """No usable llama-server binary. Carries the message the panel shows."""


#: The two builds kept side by side. A fetch installs BOTH, because the GPU
#: setting is a runtime switch between them: one download, then flipping the
#: toggle relaunches against the other directory with nothing to wait for.
FLAVOURS = ("cpu", "gpu")


def bin_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "llama-bin")
    os.makedirs(d, exist_ok=True)
    return d


def flavour_dir(gpu: bool) -> str:
    """Where one build lives: ``llama-bin/gpu/`` or ``llama-bin/cpu/``.

    Kept apart rather than swapped in place because swapping is what made the
    GPU setting a lie — the panel wrote ``--n-gpu-layers 999`` onto whichever
    single binary had last been unpacked, and a CPU build accepts that flag and
    ignores it, silently and with a zero exit status.
    """
    return os.path.join(bin_dir(), "gpu" if gpu else "cpu")


def _executable(path: Path) -> bool:
    """Whether this path names a program that can be run.

    ``os.access(..., X_OK)`` is the whole answer everywhere except Windows,
    which has no execute bit: there the call degrades to "does this file exist"
    and would cheerfully hand back a README. The extension is the only signal
    that survives, and PATHEXT is the machine's own list of which ones count.
    """
    if not path.is_file():
        return False
    if not IS_WINDOWS:
        return os.access(path, os.X_OK)
    suffixes = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    return path.suffix.lower() in {s.strip().lower() for s in suffixes if s.strip()}


def _named(path: Path) -> tuple[Path, ...]:
    r"""A path as given, plus the .exe a Windows user meant by it.

    ``ORB_LLAMA_SERVER=C:\llama\llama-server`` is what someone transcribes from
    a Linux README, and it is one suffix away from correct rather than wrong.
    """
    if IS_WINDOWS and not path.suffix:
        return (path.with_suffix(".exe"), path)
    return (path,)


def find_binary(gpu: bool = True) -> Path:
    """The llama-server to run: env override → PATH → ``data/llama-bin/<flavour>/``.

    *gpu* picks which of the two fetched builds to run, and it is the entire
    GPU switch — the caller passes ``profile.gpu_layers > 0`` and gets a binary
    that can honour it.

    An override and a PATH binary answer for both flavours: somebody who
    supplied their own llama-server gets that one either way, and their toggle
    then moves ``--n-gpu-layers`` alone, which is the right meaning for a build
    this code did not choose. An explicit ``ORB_LLAMA_SERVER`` that does not
    resolve stays a hard error rather than a fallthrough — someone who set it
    wants *that* binary, and quietly running a different one is how a Vulkan
    build gets swapped for a CPU one without anybody noticing.
    """
    explicit = os.environ.get("ORB_LLAMA_SERVER")
    if explicit:
        path = Path(explicit).expanduser()
        for candidate in _named(path):
            if _executable(candidate):
                return candidate
        raise LlamaServerMissing(f"ORB_LLAMA_SERVER points at {path}, which is not an executable file.")
    found = shutil.which("llama-server")
    if found:
        return Path(found)
    local = Path(flavour_dir(gpu)) / BINARY_NAME
    if _executable(local):
        return local
    # NAMES A FEATURE, DELIBERATELY, in shared code. This is panel text, the
    # prose rewriter is the only place in the UI that offers the fetch, and a
    # generic "no binary" message would send the user nowhere. The moment a
    # second llama-server feature exists this becomes wrong, and it is a
    # one-line fix then.
    raise LlamaServerMissing(
        "No llama-server binary. Fetch one from Settings → Local ML → Prose Rewriter, "
        "or point ORB_LLAMA_SERVER at one you already have."
    )


def runtime_ok() -> bool:
    """Whether the runtime is installed. The panel's runtime gate.

    BOTH flavours have to resolve, because the GPU toggle switches between them
    with no download in the way: half a pair is a toggle that works in one
    direction and silently does nothing in the other. An install from before
    the split has a flat ``llama-bin/`` and reads as missing here, which puts
    the Download button back on screen — one press installs the pair, and that
    is the whole migration.
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
            done = subprocess.run(  # noqa: S603 — binary resolved by find_binary
                [key, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
            _HELP_CACHE[key] = (done.stdout or "") + (done.stderr or "")
        except Exception:  # a binary that will not even print help fails properly at boot
            _HELP_CACHE[key] = ""
    return _HELP_CACHE[key]


def supports_flag(binary: Path, flag: str) -> bool:
    """Whether this build accepts *flag*.

    People bring their own llama-server — a distro package, a release tarball,
    a build from last year — and a flag the binary has never heard of is not a
    warning, it is an immediate exit with a usage message.
    """
    return flag in _help_text(binary)


#: Devices per binary path. ``None`` is "this build could not be asked", which
#: is not the same answer as "this build found nothing".
_DEVICE_CACHE: dict[str, tuple[str, ...] | None] = {}

_DEVICES_HEADER = "available devices:"


def _forget_probes() -> None:
    """Drop every cached probe. Called after a fetch.

    A re-fetch writes the SAME path, so a cache keyed by path would keep
    answering for the build that was just replaced — the CPU one, in the case
    somebody swapping to Vulkan is trying to get out of.
    """
    _HELP_CACHE.clear()
    _DEVICE_CACHE.clear()


def _parse_devices(text: str) -> tuple[str, ...] | None:
    """The device names under llama-server's ``Available devices:`` header.

    Everything above the header is backend chatter — a Vulkan build narrates
    its own enumeration before it answers — so the header is the anchor, and
    the indented lines under it are the answer. ``(none)`` is what a build with
    no non-CPU backend prints: a device list of length zero, not a device. The
    CPU never appears in this list, which is what makes "non-empty" mean "can
    offload".

    ``None`` when there is no header at all: a build too old to know the flag
    has not said it has no GPU, it has said nothing.
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
        done = subprocess.run(  # noqa: S603 — binary resolved by find_binary
            [str(binary), "--list-devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
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

    THE FLAG IS NOT THE CAPABILITY. Every build parses ``--n-gpu-layers`` and
    documents it in ``--help``; a CPU-only build then has nowhere to put the
    layers and offloads none of them, silently and with a zero exit status.
    Asking the binary what devices it found is the only honest answer, and it
    is what stops the panel offering a GPU switch that cannot do anything.
    """
    found = devices(binary)
    return None if found is None else bool(found)


def _arch() -> str:
    import platform  # noqa: PLC0415 — only needed on the fetch path

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise LlamaServerMissing(f"No prebuilt llama.cpp binary for {machine}; build one and set ORB_LLAMA_SERVER.")


def gpu_build_published(*, system: str, arch: str) -> bool:
    """Whether a GPU-capable archive exists for this platform at all.

    macOS carries Metal inside the one asset per arch, so the answer is yes and
    the choice never reaches the archive. Windows on arm64 publishes no Vulkan
    build — its GPU assets are OpenCL for Adreno and CUDA for Grace, both
    narrower than "any card" — so the answer is no.

    Split out of :func:`asset_name` because the panel has to ask it WITHOUT
    fetching anything: "ticking this box cannot help you here" is a different
    message from "the build you have cannot help you", and a platform that has
    no GPU build to offer must not be shown a button offering one.
    """
    if system == "windows":
        return arch == "x64"
    return True


def asset_name(tag: str, backend: str, *, system: str, arch: str) -> str:
    """The release asset for this platform, GPU flavour and architecture.

    A ``gpu`` request on a platform with no GPU build degrades to the CPU
    archive rather than 404ing on an asset the release does not carry.
    """
    gpu = backend == "gpu" and gpu_build_published(system=system, arch=arch)
    if system == "darwin":
        return f"llama-{tag}-bin-macos-{arch}.tar.gz"
    if system == "windows":
        return f"llama-{tag}-bin-win-vulkan-x64.zip" if gpu else f"llama-{tag}-bin-win-cpu-{arch}.zip"
    if gpu:
        return f"llama-{tag}-bin-ubuntu-vulkan-{arch}.tar.gz"
    return f"llama-{tag}-bin-ubuntu-{arch}.tar.gz"


def _system() -> str:
    import sys  # noqa: PLC0415 — fetch path only

    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _api(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — fixed https host
        return json.load(response)


def resolve_release(tag: str | None = None) -> dict:
    """The release to take binaries from: the pin, an explicit tag, or newest-with-assets."""
    tag = tag or os.environ.get("ORB_LLAMA_CPP_BUILD") or DEFAULT_BUILD
    if tag != "latest":
        return _api(f"https://api.github.com/repos/{REPO_SLUG}/releases/tags/{tag}")
    for release in _api(f"https://api.github.com/repos/{REPO_SLUG}/releases?per_page=30"):
        if _BUILD_TAG.fullmatch(release.get("tag_name") or "") and release.get("assets"):
            return release
    raise LlamaServerMissing("No llama.cpp build release with binaries was found.")


def _unpack(archive: Path, into: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(into)  # noqa: S202 — official release archive
    else:
        with tarfile.open(archive) as tf:
            # `filter="data"` refuses absolute paths, `..` escapes, links that
            # point out of the tree, and device nodes. Asked for explicitly
            # rather than left to the default: it only becomes the default in
            # 3.14, warns in between, and this is unpacking something fetched
            # over the network. Probed because the keyword arrived in 3.11.4 as
            # a backport and the three 3.11 patch releases before it raise
            # TypeError on it — the same reason `--no-webui` is probed on the
            # binary rather than simply sent.
            if hasattr(tarfile, "data_filter"):
                tf.extractall(into, filter="data")  # noqa: S202 — official release archive
            else:
                base = into.resolve()
                for member in tf.getmembers():
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise LlamaServerMissing(f"Illegal tar archive entry: {member.name}")
                    target = (base / member_path).resolve()
                    if os.path.commonpath([str(base), str(target)]) != str(base):
                        raise LlamaServerMissing(f"Illegal tar archive entry: {member.name}")
                    tf.extract(member, into)  # noqa: S202 — validated member path


def _flatten(unpacked: Path, dest: Path) -> Path:
    """Move the directory that actually contains llama-server into *dest*.

    The Windows zips are flat today and the Linux tarballs are not, and this
    project has to name one stable path either way — the same thing
    ``tar --strip-components=1`` does, but derived from where the binary
    landed rather than assumed.
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


def _download(release: dict, wanted: str, into: Path) -> Path:
    """One release asset onto disk, or the message naming what the tag does have."""
    asset = next((a for a in release.get("assets", []) if a.get("name") == wanted), None)
    if asset is None:
        published = ", ".join(sorted(a["name"] for a in release.get("assets", []))) or "nothing"
        raise LlamaServerMissing(f"{release['tag_name']} does not publish {wanted}. It publishes: {published}")
    logger.info("Fetching %s (%.0f MB)", wanted, asset.get("size", 0) / 1e6)
    archive = into / wanted
    request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, open(archive, "wb") as fh:  # noqa: S310 — github release URL
        shutil.copyfileobj(response, fh)
    return archive


def _prove(binary: Path) -> None:
    """Run ``--version`` before calling a binary installed.

    An archive for the wrong glibc, or a Vulkan build on a machine with no
    loader, fails here — which is a message — rather than at the first turn,
    which is a hang.
    """
    proof = subprocess.run(  # noqa: S603 — path we just wrote, fixed argv
        [str(binary), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )
    if proof.returncode != 0:
        detail = ((proof.stderr or "") + (proof.stdout or "")).strip()[-400:]
        raise LlamaServerMissing(f"{binary} was unpacked but will not run:\n{detail}")


def _clear_legacy_builds() -> None:
    """Remove a pre-split flat install from ``llama-bin/``.

    Before the CPU and GPU builds were kept apart, the binary and its ~40
    shared objects sat loose at this level. Nothing resolves them any more and
    they are a couple of hundred MB that ``bin_bytes`` would still report on
    the storage row.
    """
    root = Path(bin_dir())
    for entry in root.iterdir():
        if entry.is_dir() and entry.name in FLAVOURS:
            continue
        shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)


def fetch() -> str:
    """Download and unpack BOTH llama-server builds. Blocking.

    Both in one press, because the GPU setting is a switch between them: paying
    for a second download at the moment somebody ticks a checkbox is the reason
    that checkbox used to do nothing instead. Each is proved with ``--version``
    before it counts as installed. Returns the GPU build's path.

    Platforms that publish one archive for both — macOS carries Metal inside
    it — download once and unpack it into each directory, so every caller
    downstream can assume the pair exists.
    """
    release = resolve_release()
    tag = release["tag_name"]
    system, arch = _system(), _arch()
    _clear_legacy_builds()
    installed: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="orb-llama-") as tmp:
        archives: dict[str, Path] = {}
        for flavour in FLAVOURS:
            wanted = asset_name(tag, flavour, system=system, arch=arch)
            if wanted not in archives:
                archives[wanted] = _download(release, wanted, Path(tmp))
            # Unpacked per flavour even when the archive is shared: `_flatten`
            # MOVES what it finds, so a second pass over one unpack directory
            # would find it empty.
            unpacked = Path(tmp) / f"unpacked-{flavour}"
            _unpack(archives[wanted], unpacked)
            dest = Path(flavour_dir(flavour == "gpu"))
            binary = _flatten(unpacked, dest)
            if not IS_WINDOWS:
                # Windows has no execute bit; everywhere else the archive's mode
                # may not have survived, and a binary nobody may execute is not
                # a binary.
                for entry in dest.iterdir():
                    if entry.is_file():
                        entry.chmod(entry.stat().st_mode | 0o755)
            _prove(binary)
            installed[flavour] = binary
    # The paths did not change, so every cached answer about them is now about
    # a build that is gone. Dropped wholesale rather than per key: `_flatten`
    # replaced directories, not files inside them.
    _forget_probes()
    logger.info("llama-server %s ready: %s", tag, ", ".join(f"{k} at {v}" for k, v in installed.items()))
    return str(installed["gpu"])


def bin_bytes() -> int:
    """Total size of ``data/llama-bin/``, for the storage figure on /api/stats."""
    total = 0
    for dirpath, _dirs, files in os.walk(bin_dir()):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                total += os.path.getsize(path)
    return total
