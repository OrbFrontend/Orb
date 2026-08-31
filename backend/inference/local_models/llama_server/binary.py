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


def bin_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "llama-bin")
    os.makedirs(d, exist_ok=True)
    return d


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


def find_binary() -> Path:
    """The llama-server to run: env override → PATH → ``data/llama-bin/``.

    An explicit ``ORB_LLAMA_SERVER`` that does not resolve is a hard error, not
    a fallthrough — someone who set it wants *that* binary, and quietly running
    a different one is how a Vulkan build gets swapped for a CPU one without
    anybody noticing.
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
    local = Path(bin_dir()) / BINARY_NAME
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
    """Whether a llama-server binary resolves. The panel's runtime row."""
    try:
        find_binary()
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


def _arch() -> str:
    import platform  # noqa: PLC0415 — only needed on the fetch path

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise LlamaServerMissing(f"No prebuilt llama.cpp binary for {machine}; build one and set ORB_LLAMA_SERVER.")


def asset_name(tag: str, backend: str, *, system: str, arch: str) -> str:
    """The release asset for this platform, GPU flavour and architecture.

    macOS builds carry Metal already, so there is one asset per arch and the
    GPU/CPU choice does not reach the archive — only ``--n-gpu-layers``.
    Windows on arm64 publishes no Vulkan build (its GPU assets are OpenCL for
    Adreno and CUDA for Grace, both narrower than "any card"), so the honest
    default there is the CPU build rather than a 404.
    """
    gpu = backend == "gpu"
    if system == "darwin":
        return f"llama-{tag}-bin-macos-{arch}.tar.gz"
    if system == "windows":
        if gpu and arch == "x64":
            return f"llama-{tag}-bin-win-vulkan-x64.zip"
        return f"llama-{tag}-bin-win-cpu-{arch}.zip"
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


def fetch(backend: str = "gpu") -> str:
    """Download and unpack a llama-server into ``data/llama-bin/``. Blocking.

    Returns the binary's path, having proved it runs with ``--version``: an
    archive for the wrong glibc or a Vulkan build with no loader present fails
    there, which is a message, rather than at the first turn, which is a hang.
    """
    release = resolve_release()
    tag = release["tag_name"]
    system, arch = _system(), _arch()
    wanted = asset_name(tag, backend, system=system, arch=arch)
    asset = next((a for a in release.get("assets", []) if a.get("name") == wanted), None)
    if asset is None:
        published = ", ".join(sorted(a["name"] for a in release.get("assets", []))) or "nothing"
        raise LlamaServerMissing(f"{tag} does not publish {wanted}. It publishes: {published}")
    logger.info("Fetching %s (%.0f MB)", wanted, asset.get("size", 0) / 1e6)
    dest = Path(bin_dir())
    with tempfile.TemporaryDirectory(prefix="orb-llama-") as tmp:
        archive = Path(tmp) / wanted
        request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response, open(archive, "wb") as fh:  # noqa: S310 — github release URL
            shutil.copyfileobj(response, fh)
        unpacked = Path(tmp) / "unpacked"
        _unpack(archive, unpacked)
        binary = _flatten(unpacked, dest)
    if not IS_WINDOWS:
        # Windows has no execute bit; everywhere else the archive's mode may not
        # have survived, and a binary nobody may execute is not a binary.
        for entry in dest.iterdir():
            if entry.is_file():
                entry.chmod(entry.stat().st_mode | 0o755)
    proof = subprocess.run(  # noqa: S603 — path we just wrote, fixed argv
        [str(binary), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )
    if proof.returncode != 0:
        detail = ((proof.stderr or "") + (proof.stdout or "")).strip()[-400:]
        raise LlamaServerMissing(f"{binary} was unpacked but will not run:\n{detail}")
    logger.info("llama-server %s ready at %s", tag, binary)
    return str(binary)


def bin_bytes() -> int:
    """Total size of ``data/llama-bin/``, for the storage figure on /api/stats."""
    total = 0
    for dirpath, _dirs, files in os.walk(bin_dir()):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                total += os.path.getsize(path)
    return total
