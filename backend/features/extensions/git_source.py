"""Installing from a Git URL, without ever running Git.

Dulwich speaks the Git wire protocol in-process, which is the whole reason this
module can exist: there is no ``git`` executable to invoke, so there are no
hooks, no configured filters, no credential helpers, no checkout smudge
commands, and no package-selected subprocess. Nothing is ever checked out --
the commit's tree is walked as *objects*, and only the files the validated
manifest names are ever read by the compiler.

The network policy is the flow HTTP client's policy, reused rather than
reimplemented. A repository URL gets the same parsing, the same address
validation and pinning, the same refusal of environment proxies, and the same
redirect revalidation an ``http.request`` gets, because "installer fetches
receive equivalent URL/address validation" is an acceptance test and not a
nice-to-have: an SSRF through the installer reaches exactly the same internal
services an SSRF through a flow does.

What is deliberately absent, per section 12: SSH, the unauthenticated ``git://``
protocol, private-repository credentials, submodules, and LFS. Each would add a
credential path or a second fetch protocol, and none is needed to install a
package that is, by contract, a handful of JSON files.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import struct
import tempfile
import time
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, cast

from .errors import PackageLimitExceeded, PackageParseError, PackageValidationError
from .limits import (
    MAX_GIT_EXPANDED_BYTES,
    MAX_GIT_OBJECT_BYTES,
    MAX_HTTP_REDIRECTS,
    MAX_REFERENCED_BYTES_TOTAL,
    MAX_SOURCE_BYTES,
    MAX_TREE_ENTRIES,
)
from .network import (
    Destination,
    address_is_local,
    address_rejection,
    origin_allows_local_addresses,
    parse_url,
)
from .paths import assert_no_case_collisions, normalize_package_path
from .sources import MANIFEST_PATH, _BudgetedSource

logger = logging.getLogger(__name__)

MAX_REF_CHARS = 200
"""A branch, tag, or commit id the user typed. Not a place for a payload."""

GIT_FETCH_TIMEOUT_SECONDS = 60.0
"""Wall-clock budget for one repository fetch.

Longer than a flow's single request because a fetch is several round trips
(``/info/refs``, then the pack), and shorter than "until the socket dies"
because an installer that hangs holds the lifecycle lock."""

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")

_GIT_MODE_TREE = 0o040000
_GIT_MODE_LINK = 0o120000
_GIT_MODE_GITLINK = 0o160000
_GIT_MODE_REGULAR = frozenset({0o100644, 0o100755})


class GitUnavailable(PackageValidationError):
    """This build cannot install from Git because Dulwich is not present.

    A :class:`~.errors.PackageValidationError` so the route maps it like any
    other refusal, with a message that names the missing dependency rather than
    letting an ``ImportError`` become a 500. Local ``.orbext`` install is the
    documented fallback and stays available.
    """


def dulwich_available() -> bool:
    """Whether this deployment can install from a Git URL at all."""
    try:
        import dulwich.client  # noqa: F401
    except ImportError:
        return False
    return True


def _dulwich():
    """Import Dulwich lazily, or raise :class:`GitUnavailable`.

    Lazy because Git installation is one source among two: a deployment without
    the dependency must still boot, still reconcile installed packages, and
    still accept a local archive. An import at module scope would turn a missing
    optional dependency into a dead application.
    """
    try:
        from dulwich.client import HttpGitClient
        from dulwich.objects import Commit, Tag, Tree
        from dulwich.repo import Repo
    except ImportError:  # pragma: no cover - exercised only without the dependency
        raise GitUnavailable(
            "this Orb build cannot install from Git because the 'dulwich' package is not installed; "
            "install from a local .orbext archive instead"
        ) from None
    return HttpGitClient, Repo, Commit, Tag, Tree


# ── input policy ─────────────────────────────────────────────────────────────


def validate_ref(raw: Any) -> str:
    """Validate an optional branch/tag/commit selector, or return ``""``.

    Ref names reach the wire protocol, so the grammar is conservative: no
    spaces, no control characters, no leading dash that could read as a flag to
    something downstream, and no ``..`` sequence. An empty value means "the
    repository's default branch", resolved from the advertised HEAD rather than
    guessed as ``main``.
    """
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise PackageValidationError("the git ref must be a string")
    if len(raw) > MAX_REF_CHARS or _REF_RE.fullmatch(raw) is None or ".." in raw:
        raise PackageValidationError(f"{raw!r} is not a valid branch, tag, or commit id")
    return raw


def validate_repository_url(raw: Any) -> Destination:
    """Apply the installer URL policy: exactly the flow client's, plus one rule.

    Reuses :func:`~.network.parse_url`, so userinfo, wildcards, non-http(s)
    schemes, control characters, and malformed ports are rejected by the same
    code that rejects them for a flow -- an installer with its own parser is an
    installer with its own bugs. The extra rule is that a repository URL carries
    no query string or fragment: a Git endpoint's parameters belong to the
    protocol, and one supplied by the user would be appended to the service
    request Dulwich builds.
    """
    if not isinstance(raw, str):
        raise PackageValidationError("the repository URL must be a string")
    if "?" in raw or "#" in raw:
        raise PackageValidationError("a repository URL carries no query string or fragment")
    try:
        return parse_url(raw, what="the repository URL")
    except Exception as exc:
        raise PackageValidationError(str(exc)) from None


def resolve_pinned(destination: Destination, *, allow_local: bool) -> str:
    """Resolve the repository host and return the one address to connect to.

    Every returned address is checked, not only the pinned one -- a round-robin
    record mixing a public address with a link-local metadata endpoint would
    otherwise be a coin flip the attacker gets to keep flipping.

    ``allow_local`` is the user's explicit confirmation that this URL points at
    their own machine or LAN, the installer's counterpart to a weak origin
    grant. Without it a repository host resolving to ``169.254.169.254`` is
    refused exactly as a flow's would be.
    """
    local_http = destination.scheme == "http"
    if local_http and not origin_allows_local_addresses(destination):
        raise PackageValidationError(
            "public Git repositories require HTTPS; plain HTTP is allowed only for an explicitly confirmed local repository"
        )
    try:
        infos = socket.getaddrinfo(destination.host, destination.port, type=socket.SOCK_STREAM)
    except OSError:
        raise PackageValidationError(f"the repository host of {destination.origin} could not be resolved") from None
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos if info[4]))
    if not addresses:
        raise PackageValidationError(f"the repository host of {destination.origin} resolved to no addresses")
    for candidate in addresses:
        rejection = address_rejection(candidate, allow_local=allow_local)
        if rejection is not None:
            raise PackageValidationError(rejection)
        if local_http and not address_is_local(candidate):
            raise PackageValidationError("a plain-HTTP repository must resolve only to local addresses")
    return addresses[0]


# ── the pinned transport ─────────────────────────────────────────────────────


class PinnedPoolManager:
    """The ``pool_manager`` Dulwich's HTTP client talks through.

    It offers the two members Dulwich uses -- ``headers`` and ``request`` --
    and nothing else, which is what makes the installer's egress reviewable:
    there is no configuration path, no proxy lookup, no ``.netrc``, and no
    credential callback, because none of them is implemented here.

    Each hop resolves and validates its own address and connects to the pinned
    one while ``Host`` and TLS ``server_hostname`` keep the repository's real
    name, so virtual hosting and certificate verification both keep working
    while the socket goes exactly where the policy said it could. Redirects are
    followed by this class rather than by urllib3, because urllib3 would resolve
    the new host itself and that second resolution is the one an attacker wants.
    """

    def __init__(self, *, allow_local: bool, deadline: float) -> None:
        import urllib3

        self._urllib3 = urllib3
        self._allow_local = allow_local
        self._deadline = deadline
        self._pools: dict[tuple[str, str, int, str], Any] = {}
        self.headers: dict[str, str] = {"User-Agent": "orb-extension-installer"}

    def _pool(self, destination: Destination, address: str):
        import urllib3

        key = (destination.scheme, address, destination.port, destination.host)
        pool = self._pools.get(key)
        if pool is None:
            common = {
                "port": destination.port,
                "maxsize": 1,
                "retries": False,
                "timeout": urllib3.Timeout(connect=10.0, read=30.0),
            }
            if destination.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(
                    address,
                    assert_hostname=destination.host,
                    server_hostname=destination.host,
                    **common,
                )
            else:
                pool = urllib3.HTTPConnectionPool(address, **common)
            self._pools[key] = pool
        return pool

    def request(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        body: bytes | None = kwargs.pop("body", None)
        preload_content = kwargs.pop("preload_content", True)
        kwargs.pop("timeout", None)
        kwargs.pop("redirect", None)

        for _hop in range(MAX_HTTP_REDIRECTS + 1):
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise PackageLimitExceeded(f"the repository fetch exceeded its {GIT_FETCH_TIMEOUT_SECONDS:.0f} second budget")
            destination = validate_repository_url(url.split("?", 1)[0]) if "?" not in url else parse_url(url)
            address = resolve_pinned(destination, allow_local=self._allow_local)
            target = destination.url.split("://", 1)[1]
            path = "/" + target.split("/", 1)[1] if "/" in target else "/"
            response = self._pool(destination, address).urlopen(
                method,
                path,
                body=bytes(body) if body is not None else None,
                headers={**headers, "Host": destination.authority},
                redirect=False,
                assert_same_host=False,
                preload_content=preload_content,
                timeout=self._urllib3.Timeout(connect=min(10.0, remaining), read=min(30.0, remaining)),
                **kwargs,
            )
            if response.status not in (301, 302, 303, 307, 308):
                # Dulwich reads ``geturl()`` to notice that a repository moved,
                # so the *final* URL has to be what it sees -- not the first one
                # this loop was handed.
                response.url = destination.url
                return response
            location = response.headers.get("location")
            response.drain_conn()
            if not location:
                raise PackageValidationError(f"{destination.origin} sent a redirect with no location")
            if body is not None and not isinstance(body, (bytes, bytearray)):
                raise PackageValidationError("the repository redirected a streamed request, which cannot be replayed")
            url = (
                location
                if "://" in location
                else f"{destination.origin}{location if location.startswith('/') else '/' + location}"
            )
            if response.status == 303 and method != "GET":
                method, body = "GET", None
        raise PackageValidationError(f"the repository fetch followed more than {MAX_HTTP_REDIRECTS} redirects")


# ── the fetched tree ─────────────────────────────────────────────────────────


class GitTreeSource(_BudgetedSource):
    """A commit's tree, indexed as normalized paths, read by exact key.

    The same interface :class:`~.sources.ArchiveSource` offers and for the same
    reason: no enumeration, no glob, no walk. The compiler cannot tell a Git
    install from an archive install, so both compile identically and produce the
    same digest for the same content -- which is what lets an update inspected
    from a Git URL be diffed against a revision that was installed from a zip.
    """

    def __init__(self, blobs: Mapping[str, bytes]) -> None:
        super().__init__()
        self._blobs = dict(blobs)

    def has(self, path: str) -> bool:
        return path in self._blobs

    def read(self, path: str, *, max_bytes: int) -> bytes:
        data = self._blobs.get(path)
        if data is None:
            raise PackageValidationError(f"the repository does not contain {path!r}")
        if len(data) > max_bytes:
            raise PackageLimitExceeded(f"{path}: {len(data)} bytes, limit is {max_bytes}")
        self._charge(path, len(data))
        return data


@dataclass(frozen=True, slots=True)
class GitFetch:
    """One resolved commit and the blobs its tree reaches."""

    commit_id: str
    source: GitTreeSource


def walk_tree(store, tree, tree_type, prefix: str = "", budget: dict[str, int] | None = None) -> Iterator[tuple[str, bytes]]:
    """Yield ``(normalized path, blob bytes)`` for one tree, recursively.

    Every rejection is a rejection of the whole repository rather than of one
    entry. A symlink or a submodule is not a repository with one bad file; it is
    a repository doing something the package format does not permit, and
    skipping the entry would let the author believe their layout worked.

    Both budgets are charged as the walk proceeds, so a tree that is too large
    is abandoned partway through rather than after it has been fully
    materialized.
    """
    counters = budget if budget is not None else {"entries": 0, "bytes": 0}
    for entry in tree.items():
        path = f"{prefix}{entry.path.decode('utf-8', errors='replace')}"
        if entry.mode == _GIT_MODE_LINK:
            raise PackageValidationError(f"repository entry {path!r} is a symlink; packages contain regular files only")
        if entry.mode == _GIT_MODE_GITLINK:
            raise PackageValidationError(f"repository entry {path!r} is a submodule, which v1 does not fetch")
        counters["entries"] += 1
        if counters["entries"] > MAX_TREE_ENTRIES:
            raise PackageLimitExceeded(f"repository tree has more than {MAX_TREE_ENTRIES} entries")
        child = store[entry.sha]
        if entry.mode == _GIT_MODE_TREE:
            if not isinstance(child, tree_type):
                raise PackageValidationError(f"repository entry {path!r} is not a tree where one was expected")
            yield from walk_tree(store, child, tree_type, f"{path}/", counters)
            continue
        if entry.mode not in _GIT_MODE_REGULAR:
            raise PackageValidationError(f"repository entry {path!r} is not a regular file")
        data = child.as_raw_string()
        counters["bytes"] += len(data)
        if counters["bytes"] > MAX_REFERENCED_BYTES_TOTAL:
            raise PackageLimitExceeded(f"repository content exceeds the total limit of {MAX_REFERENCED_BYTES_TOTAL} bytes")
        yield normalize_package_path(path, what="repository entry"), data


def index_tree(store, tree, tree_type) -> GitTreeSource:
    """Validate a commit's whole tree into a path-keyed source."""
    blobs: dict[str, bytes] = {}
    for path, data in walk_tree(store, tree, tree_type):
        if path in blobs:
            raise PackageValidationError(f"repository contains duplicate normalized path {path!r}")
        blobs[path] = data
    assert_no_case_collisions(blobs)
    if MANIFEST_PATH not in blobs:
        raise PackageValidationError(f"the repository does not contain {MANIFEST_PATH} at its root")
    return GitTreeSource(blobs)


def select_ref(refs: Mapping[bytes, bytes], wanted: bytes) -> bytes:
    """Resolve the user's ref selector against the advertised refs.

    Deliberately explicit rather than clever: an exact ref name, then the two
    conventional namespaces, then a full commit id the advertisement already
    contains. There is no "closest match" -- installing a different commit than
    the one the user named is exactly the outcome an inspection screen exists to
    prevent.
    """
    if not wanted:
        for name in (b"HEAD", b"refs/heads/main", b"refs/heads/master"):
            if refs.get(name):
                return refs[name]
        raise PackageValidationError("the repository advertises no default branch")
    for candidate in (wanted, b"refs/heads/" + wanted, b"refs/tags/" + wanted):
        if refs.get(candidate):
            return refs[candidate]
    lowered = wanted.lower()
    if len(lowered) == 40 and all(character in b"0123456789abcdef" for character in lowered):
        if lowered in set(refs.values()):
            return lowered
        raise PackageValidationError("the repository does not advertise that commit; a shallow fetch cannot reach it")
    raise PackageValidationError("the repository has no branch or tag with that name")


class _BoundedPack:
    """A byte-bounded sink for the received pack.

    Dulwich writes the pack through this callable; it counts as it goes and
    raises the moment the budget is passed, so an oversized pack is refused
    mid-stream rather than buffered and then measured. The advertised size is
    never consulted -- the sender chooses it.
    """

    def __init__(self, file: BinaryIO, limit: int) -> None:
        self.file = file
        self._limit = limit
        self._seen = 0

    def write(self, data: bytes) -> int:
        self._seen += len(data)
        if self._seen > self._limit:
            raise PackageLimitExceeded(f"the repository pack exceeds the {self._limit} byte limit")
        self.file.write(data)
        return len(data)

    def rewind(self) -> None:
        self.file.flush()
        self.file.seek(0)


def fetch_repository(url: str, ref: str, *, allow_local: bool) -> GitFetch:
    """Shallow-fetch one ref into a temporary bare store and index its tree.

    Blocking, and called through ``asyncio.to_thread``: the wire protocol,
    zlib inflation, and tree walk are all CPU/IO work with no awaits, and a
    50 MiB pack would otherwise stall every other request including an
    in-flight turn.

    ``depth=1`` asks the server for exactly the selected commit. The compressed
    pack and Dulwich object store are disk-backed and deleted on return. Before
    Dulwich indexes anything, a streaming preflight bounds each advertised
    object, each delta's result size, and aggregate expansion.
    """
    HttpGitClient, Repo, Commit, Tag, Tree = _dulwich()

    destination = validate_repository_url(url)
    deadline = time.monotonic() + GIT_FETCH_TIMEOUT_SECONDS
    transport = PinnedPoolManager(allow_local=allow_local, deadline=deadline)
    # Dulwich types ``pool_manager`` as a urllib3 ``PoolManager``; this one is a
    # deliberate stand-in offering exactly the two members the client uses, which
    # is the whole point -- there is no configuration, proxy, or credential path
    # on it to inherit.
    client = HttpGitClient(destination.origin, pool_manager=cast(Any, transport))
    wanted = ref.encode("utf-8") if ref else b""
    chosen: dict[str, bytes] = {}

    def determine_wants(refs, depth=None):
        target = select_ref(refs, wanted)
        chosen["sha"] = target
        return [target]

    with tempfile.TemporaryDirectory(prefix="orb-extension-git-") as temp_dir:
        repo = Repo.init_bare(temp_dir)
        incoming_path = os.path.join(temp_dir, "incoming.pack")
        with open(incoming_path, "w+b") as incoming:
            pack = _BoundedPack(incoming, MAX_SOURCE_BYTES)
            try:
                client.fetch_pack(
                    _repository_path(destination),
                    cast(Any, determine_wants),
                    repo.get_graph_walker(),
                    pack.write,
                    depth=1,
                )
            except (PackageValidationError, PackageLimitExceeded):
                raise
            except Exception as exc:
                # Dulwich's protocol errors quote the URL and sometimes the
                # response body; neither belongs in a message the manager renders.
                raise PackageParseError(f"the repository could not be fetched ({type(exc).__name__})") from None

            pack.rewind()
            _preflight_pack(incoming)
            incoming.seek(0)
            _inflate(repo.object_store, incoming)

        sha = chosen.get("sha")
        if not sha:
            raise PackageValidationError("the repository advertised no usable ref")
        commit, commit_sha = _peel_commit(repo.object_store, sha, Commit, Tag)
        try:
            tree = repo.object_store[cast(Any, commit.tree)]
        except KeyError:
            raise PackageValidationError("the repository did not send the selected commit's tree") from None
        if not isinstance(tree, Tree):
            raise PackageValidationError("the selected commit has no tree")
        source = index_tree(repo.object_store, tree, Tree)
        return GitFetch(commit_id=commit_sha.decode("ascii"), source=source)


def _repository_path(destination: Destination) -> str:
    """The path component the Git client requests, always absolute."""
    target = destination.url.split("://", 1)[1]
    return "/" + target.split("/", 1)[1] if "/" in target else "/"


def _inflate(store, file: BinaryIO) -> None:
    """Index the preflighted pack in the temporary disk object store.

    Through the object store's own thin-pack reader rather than a hand-rolled
    inflater: delta resolution against objects the sender assumed we already had
    is the part of the format most worth not reimplementing. The caller supplies
    a temporary disk-backed store, so malformed or oversized input is discarded
    with the temporary repository.
    """
    if not file.read(1):
        raise PackageValidationError("the repository sent no objects")
    file.seek(0)
    try:
        store.add_thin_pack(file.read, file.read, max_input_size=MAX_SOURCE_BYTES)
    except (PackageValidationError, PackageLimitExceeded):
        raise
    except Exception as exc:
        raise PackageParseError(f"the repository pack could not be read ({type(exc).__name__})") from None


def _peel_commit(store, sha: bytes, commit_type, tag_type):
    """Dereference an annotated tag chain to its commit, with a hard depth."""
    current_sha = sha
    for _depth in range(8):
        try:
            obj = store[cast(Any, current_sha)]
        except KeyError:
            raise PackageValidationError("the repository did not send the selected ref") from None
        if isinstance(obj, commit_type):
            return obj, current_sha
        if not isinstance(obj, tag_type):
            raise PackageValidationError("the selected ref does not name a commit")
        _target_type, target_sha = obj.object
        current_sha = bytes(target_sha)
    raise PackageValidationError("the selected ref contains too many nested annotated tags")


def _read_exact(file: BinaryIO, size: int, *, what: str) -> bytes:
    data = file.read(size)
    if len(data) != size:
        raise PackageParseError(f"the repository pack ended while reading {what}")
    return data


def _delta_result_size(prefix: bytes) -> int:
    """Read the base-size varint and result-size varint from delta data."""
    offset = 0
    for label in ("base", "result"):
        value = 0
        shift = 0
        while True:
            if offset >= len(prefix):
                raise PackageParseError(f"the repository pack has a truncated delta {label} size")
            byte = prefix[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 63:
                raise PackageParseError(f"the repository pack has an invalid delta {label} size")
        if label == "result":
            return value
    raise AssertionError("delta size parser did not return")  # pragma: no cover


def _inflate_one(file: BinaryIO, advertised_size: int, *, is_delta: bool) -> tuple[int, int | None]:
    """Consume one zlib member with bounded output.

    Returns ``(inflated_instruction_bytes, delta_result_bytes_or_none)``. The
    maximum-output argument prevents a lying zlib stream from allocating past
    the per-object limit before the advertised-size mismatch is reported.
    """
    inflater = zlib.decompressobj()
    total = 0
    prefix = bytearray()
    pending = b""
    while not inflater.eof:
        chunk = pending or file.read(64 * 1024)
        pending = b""
        if not chunk:
            raise PackageParseError("the repository pack contains a truncated compressed object")
        while chunk and not inflater.eof:
            remaining = MAX_GIT_OBJECT_BYTES - total
            if remaining < 0:
                raise PackageLimitExceeded(f"a repository object exceeds the expanded limit of {MAX_GIT_OBJECT_BYTES} bytes")
            output = inflater.decompress(chunk, remaining + 1)
            total += len(output)
            if total > MAX_GIT_OBJECT_BYTES:
                raise PackageLimitExceeded(f"a repository object exceeds the expanded limit of {MAX_GIT_OBJECT_BYTES} bytes")
            if len(prefix) < 32:
                prefix.extend(output[: 32 - len(prefix)])
            chunk = inflater.unconsumed_tail
        if inflater.eof:
            unused = inflater.unused_data
            if unused:
                file.seek(-len(unused), os.SEEK_CUR)
    if total != advertised_size:
        raise PackageParseError("a repository object does not match its advertised expanded size")
    return total, _delta_result_size(bytes(prefix)) if is_delta else None


def _preflight_pack(file: BinaryIO) -> None:
    """Bound compressed-pack expansion before Dulwich indexes any object."""
    file.seek(0)
    header = _read_exact(file, 12, what="its header")
    signature, version, count = struct.unpack(">4sII", header)
    if signature != b"PACK" or version not in (2, 3):
        raise PackageParseError("the repository sent an unsupported pack header")
    if count > MAX_TREE_ENTRIES * 4:
        raise PackageLimitExceeded("the repository pack contains too many objects")

    expanded_total = 0
    for _ in range(count):
        first = _read_exact(file, 1, what="an object header")[0]
        object_type = (first >> 4) & 0x07
        advertised_size = first & 0x0F
        shift = 4
        byte = first
        while byte & 0x80:
            byte = _read_exact(file, 1, what="an object size")[0]
            advertised_size |= (byte & 0x7F) << shift
            shift += 7
            if shift > 63:
                raise PackageParseError("the repository pack has an invalid object size")
        is_delta = object_type in (6, 7)
        if object_type == 6:
            byte = _read_exact(file, 1, what="a delta base")[0]
            while byte & 0x80:
                byte = _read_exact(file, 1, what="a delta base")[0]
        elif object_type == 7:
            _read_exact(file, 20, what="a delta base id")
        elif object_type not in (1, 2, 3, 4):
            raise PackageParseError("the repository pack contains an unsupported object type")
        elif advertised_size > MAX_GIT_OBJECT_BYTES:
            raise PackageLimitExceeded(f"a repository object exceeds the expanded limit of {MAX_GIT_OBJECT_BYTES} bytes")

        inflated, result_size = _inflate_one(file, advertised_size, is_delta=is_delta)
        if result_size is not None and result_size > MAX_GIT_OBJECT_BYTES:
            raise PackageLimitExceeded(f"a repository delta expands past the object limit of {MAX_GIT_OBJECT_BYTES} bytes")
        expanded_total += max(inflated, result_size or 0)
        if expanded_total > MAX_GIT_EXPANDED_BYTES:
            raise PackageLimitExceeded(
                f"repository objects exceed the aggregate expanded limit of {MAX_GIT_EXPANDED_BYTES} bytes"
            )


__all__ = [
    "GIT_FETCH_TIMEOUT_SECONDS",
    "MAX_REF_CHARS",
    "GitFetch",
    "GitTreeSource",
    "GitUnavailable",
    "PinnedPoolManager",
    "dulwich_available",
    "fetch_repository",
    "index_tree",
    "resolve_pinned",
    "select_ref",
    "validate_ref",
    "validate_repository_url",
    "walk_tree",
]
