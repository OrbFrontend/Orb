"""Git installation: URL policy, ref selection, and the object-tree walk.

No sockets here -- the live fetch is exercised in the Phase 4 integration
suite against a real Dulwich-served repository. What this file pins is the
part that decides *what* gets fetched and *what* survives the walk: the URL
grammar the installer shares with the flow client, the refusal to guess at a
ref the user named, and the tree rules that make "never checkout" more than a
slogan.

The tree cases matter most. A symlink or a submodule in a repository is not a
repository with one bad file; it is a repository doing something the package
format does not permit, and the walk rejects the whole thing rather than
skipping the entry and letting the author believe their layout worked.
"""

from __future__ import annotations

import socket
import struct
from io import BytesIO

import pytest

from backend.features.extensions.errors import (
    PackageError,
    PackageLimitExceeded,
    PackageValidationError,
)
from backend.features.extensions.git_source import (
    MAX_REF_CHARS,
    _preflight_pack,
    index_tree,
    resolve_pinned,
    select_ref,
    validate_ref,
    validate_repository_url,
    walk_tree,
)
from backend.features.extensions.limits import MAX_GIT_OBJECT_BYTES, MAX_TREE_ENTRIES
from backend.features.extensions.network import parse_url

pytest.importorskip("dulwich")


# ── URL and ref policy ──────────────────────────────────────────────────────


def test_a_repository_url_goes_through_the_flow_clients_parser():
    """One parser, so the installer cannot have its own bugs.

    Every one of these is refused by the same function that refuses it for an
    ``http.request``. An installer with a second URL grammar is an installer
    with a second set of holes.
    """
    parsed = validate_repository_url("https://Example.invalid:443/pkg.git")
    assert parsed.origin == "https://example.invalid"

    for url, reason in [
        ("ftp://example.invalid/x.git", "http or https"),
        ("https://user:pw@example.invalid/x.git", "userinfo"),
        ("https://*.example.invalid/x.git", "wildcard"),
        ("https://example.invalid/x.git?token=1", "query string"),
        ("https://example.invalid/x.git#frag", "query string"),
        ("notaurl", "http or https"),
        (42, "must be a string"),
    ]:
        with pytest.raises(PackageValidationError, match=reason):
            validate_repository_url(url)


def test_a_ref_is_a_name_not_a_payload():
    assert validate_ref("") == ""
    assert validate_ref(None) == ""
    assert validate_ref("release/1.2") == "release/1.2"
    assert validate_ref("v1.0.0") == "v1.0.0"
    for bad in ["-flag", "a b", "a..b", "x" * (MAX_REF_CHARS + 1), "ref\nname", 7]:
        with pytest.raises(PackageValidationError):
            validate_ref(bad)


def test_public_plain_http_is_not_enabled_by_the_local_confirmation_flag():
    with pytest.raises(PackageValidationError, match="public Git repositories require HTTPS"):
        resolve_pinned(parse_url("http://example.invalid/repo.git"), allow_local=True)


def test_plain_http_local_repository_must_resolve_only_to_local_addresses(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
    )
    with pytest.raises(PackageValidationError, match="only to local addresses"):
        resolve_pinned(parse_url("http://localhost/repo.git"), allow_local=True)


def _pack_object_header(object_type: int, size: int) -> bytes:
    first = (object_type << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    if size:
        first |= 0x80
    out.append(first)
    while size:
        byte = size & 0x7F
        size >>= 7
        if size:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def test_pack_bomb_is_rejected_from_its_header_before_inflation():
    claimed = MAX_GIT_OBJECT_BYTES + 1
    pack = BytesIO(struct.pack(">4sII", b"PACK", 2, 1) + _pack_object_header(3, claimed))
    with pytest.raises(PackageLimitExceeded, match="expanded limit"):
        _preflight_pack(pack)


def test_ref_selection_is_explicit_and_never_approximate():
    """Installing a different commit than the one named is the failure mode a
    consent screen exists to prevent, so there is no closest match."""
    refs = {
        b"HEAD": b"a" * 40,
        b"refs/heads/main": b"a" * 40,
        b"refs/tags/v1": b"b" * 40,
    }
    assert select_ref(refs, b"") == b"a" * 40
    assert select_ref(refs, b"main") == b"a" * 40
    assert select_ref(refs, b"v1") == b"b" * 40
    assert select_ref(refs, b"refs/tags/v1") == b"b" * 40
    assert select_ref(refs, b"a" * 40) == b"a" * 40

    with pytest.raises(PackageValidationError, match="no branch or tag"):
        select_ref(refs, b"nope")
    with pytest.raises(PackageValidationError, match="shallow fetch cannot reach"):
        select_ref(refs, b"c" * 40)
    with pytest.raises(PackageValidationError, match="no default branch"):
        select_ref({}, b"")


# ── the object-tree walk ────────────────────────────────────────────────────


def build(entries: dict[str, tuple[int, bytes]]):
    """A ``(store, tree, Tree)`` triple from ``{path: (mode, content)}``.

    Real Dulwich objects in a real object store, because the walk reads modes
    and shas off them -- a hand-rolled stand-in would agree with whatever the
    walk happened to do.
    """
    from dulwich.object_store import MemoryObjectStore
    from dulwich.objects import Blob, Tree

    store = MemoryObjectStore()
    trees: dict[str, Tree] = {"": Tree()}

    def tree_for(path: str) -> Tree:
        if path not in trees:
            trees[path] = Tree()
        return trees[path]

    for path, (mode, content) in entries.items():
        blob = Blob.from_string(content)
        store.add_object(blob)
        directory, _, name = path.rpartition("/")
        tree_for(directory).add(name.encode(), mode, blob.id)

    # Attach subtrees bottom-up so a parent records its child's final sha.
    for path in sorted((p for p in trees if p), key=lambda p: -p.count("/")):
        store.add_object(trees[path])
        parent, _, name = path.rpartition("/")
        tree_for(parent).add(name.encode(), 0o040000, trees[path].id)
    store.add_object(trees[""])
    return store, trees[""], Tree


def test_a_regular_tree_walks_into_normalized_paths():
    store, tree, Tree = build(
        {
            "orb-extension.json": (0o100644, b"{}"),
            "flows/score.json": (0o100644, b"{}"),
            "bin/tool": (0o100755, b"#!/bin/sh"),
        }
    )
    walked = dict(walk_tree(store, tree, Tree))
    assert sorted(walked) == ["bin/tool", "flows/score.json", "orb-extension.json"]
    assert walked["flows/score.json"] == b"{}"


@pytest.mark.parametrize(
    "mode,reason",
    [
        (0o120000, "symlink"),
        (0o160000, "submodule"),
    ],
)
def test_a_symlink_or_submodule_rejects_the_whole_repository(mode, reason):
    store, tree, Tree = build({"orb-extension.json": (0o100644, b"{}"), "link": (mode, b"target")})
    with pytest.raises(PackageValidationError, match=reason):
        dict(walk_tree(store, tree, Tree))


def test_the_entry_budget_is_charged_as_the_walk_proceeds():
    store, tree, Tree = build({f"f{n}.json": (0o100644, b"{}") for n in range(MAX_TREE_ENTRIES + 5)})
    with pytest.raises(PackageLimitExceeded, match="more than"):
        dict(walk_tree(store, tree, Tree))


def test_the_byte_budget_is_charged_as_the_walk_proceeds(monkeypatch):
    from backend.features.extensions import git_source

    monkeypatch.setattr(git_source, "MAX_REFERENCED_BYTES_TOTAL", 32)
    store, tree, Tree = build({"orb-extension.json": (0o100644, b"x" * 64)})
    with pytest.raises(PackageLimitExceeded, match="total limit"):
        dict(walk_tree(store, tree, Tree))


def test_indexing_requires_a_manifest_at_the_root():
    store, tree, Tree = build({"flows/score.json": (0o100644, b"{}")})
    with pytest.raises(PackageValidationError, match="orb-extension.json"):
        index_tree(store, tree, Tree)


def test_indexing_rejects_a_case_collision():
    """Case-folding filesystems would make these one file; Linux would make them
    two. A package whose meaning depends on which host it landed on is not a
    package Orb can honor."""
    store, tree, Tree = build(
        {
            "orb-extension.json": (0o100644, b"{}"),
            "flows/Score.json": (0o100644, b"{}"),
            "flows/score.json": (0o100644, b"{}"),
        }
    )
    with pytest.raises(PackageError):
        index_tree(store, tree, Tree)


def test_the_indexed_source_reads_only_by_exact_key():
    """The compiler cannot enumerate a repository any more than an archive."""
    store, tree, Tree = build({"orb-extension.json": (0o100644, b"{}"), "README.md": (0o100644, b"hi")})
    source = index_tree(store, tree, Tree)
    assert source.has("README.md")
    assert source.read("orb-extension.json", max_bytes=1024) == b"{}"
    with pytest.raises(PackageValidationError, match="does not contain"):
        source.read("nope.json", max_bytes=1024)
    with pytest.raises(PackageLimitExceeded):
        source.read("README.md", max_bytes=1)
