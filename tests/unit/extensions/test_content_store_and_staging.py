"""The content store's durability and addressing, and the staging token's rules.

Both modules exist to make a two-request lifecycle safe: the store guarantees
that a digest directory either holds a complete revision or does not exist, and
the token guarantees that the second request refers to the package the first
one showed.
"""

from __future__ import annotations

import os

import pytest

import backend.database.connection as connection
from backend.features.extensions import content_store, staging
from backend.features.extensions.digest import PackageContent, content_digest
from backend.features.extensions.errors import PackageValidationError
from backend.features.extensions.sources import StoredSource


@pytest.fixture(autouse=True)
def _store_root(tmp_path, monkeypatch):
    """Derive the store from a temp DB path, as the preset engine does."""
    monkeypatch.setattr(connection, "DB_PATH", str(tmp_path / "app.db"))
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_tokens():
    staging.clear()
    yield
    staging.clear()


def _files() -> dict[str, PackageContent]:
    return {
        "orb-extension.json": PackageContent.json({"extension_api": 1, "id": "demo"}),
        "assets/icon.png": PackageContent.binary(b"\x89PNG\r\n\x1a\n"),
    }


# ── content store ───────────────────────────────────────────────────────────


def test_store_root_follows_the_database_path(tmp_path):
    assert content_store.store_root() == os.path.join(str(tmp_path), "extensions", "objects")


def test_materialize_writes_every_file_and_returns_the_digest():
    digest = content_store.materialize(_files())
    assert digest == content_digest(_files())
    assert content_store.exists(digest)
    source = StoredSource(content_store.content_path(digest))
    assert source.read("assets/icon.png", max_bytes=64) == b"\x89PNG\r\n\x1a\n"


def test_materialize_is_idempotent_and_does_not_rewrite_live_content():
    digest = content_store.materialize(_files())
    marker = os.path.join(content_store.content_path(digest), "orb-extension.json")
    before = os.stat(marker).st_ino
    assert content_store.materialize(_files()) == digest
    # Same inode: a reinstall or a rollback to a digest still on disk must not
    # replace content an in-flight snapshot still describes.
    assert os.stat(marker).st_ino == before


def test_no_staging_directory_survives_a_successful_materialize():
    content_store.materialize(_files())
    assert not [name for name in os.listdir(content_store.store_root()) if name.startswith(".staging-")]


@pytest.mark.parametrize("bad", ["..", "../../etc", "NOTHEX" * 10, "abc", ""])
def test_a_digest_is_validated_before_it_becomes_a_path(bad: str):
    with pytest.raises(PackageValidationError, match="64 lowercase hex"):
        content_store.content_path(bad)


def test_stored_source_refuses_a_path_that_escapes_its_directory():
    digest = content_store.materialize(_files())
    source = StoredSource(content_store.content_path(digest))
    with pytest.raises(PackageValidationError):
        source.read("../../app.db", max_bytes=16)


def test_garbage_collection_keeps_referenced_revisions_and_drops_the_rest():
    kept = content_store.materialize(_files())
    other = content_store.materialize({"orb-extension.json": PackageContent.json({"extension_api": 1, "id": "gone"})})
    removed = content_store.collect_garbage({kept})
    assert removed == [other]
    assert content_store.exists(kept)
    assert not content_store.exists(other)


def test_garbage_collection_sweeps_abandoned_staging_directories():
    os.makedirs(os.path.join(content_store.store_root(), ".staging-crashed"), exist_ok=True)
    content_store.collect_garbage(set())
    assert not os.path.exists(os.path.join(content_store.store_root(), ".staging-crashed"))


def test_garbage_collection_leaves_directories_it_did_not_write():
    """Deleting an unrecognised directory under the user's data folder is not
    this module's call to make."""
    stray = os.path.join(content_store.store_root(), "notes-from-the-user")
    os.makedirs(stray, exist_ok=True)
    content_store.collect_garbage(set())
    assert os.path.isdir(stray)


def test_usage_counts_revisions_and_their_bytes():
    """What the storage page reports. Missing store is zero, not an error."""
    assert content_store.usage() == (0, 0)
    content_store.materialize(_files())
    revisions, size = content_store.usage()
    assert revisions == 1
    assert size >= sum(len(c.canonical_bytes()) for c in _files().values())


def test_usage_counts_staging_bytes_but_not_as_a_revision():
    """A crash mid-install occupies disk the user should see, under no name."""
    content_store.materialize(_files())
    staging_dir = os.path.join(content_store.store_root(), ".staging-crashed")
    os.makedirs(staging_dir, exist_ok=True)
    with open(os.path.join(staging_dir, "partial.json"), "wb") as fh:
        fh.write(b"x" * 500)
    revisions, size = content_store.usage()
    assert revisions == 1
    assert size >= 500


# ── staging tokens ──────────────────────────────────────────────────────────


def test_a_token_is_single_use():
    staged = staging.stage(operation="install", extension_id="demo", digest="a" * 64)
    assert staging.redeem(staged.token, operation="install").digest == "a" * 64
    with pytest.raises(staging.StagingError):
        staging.redeem(staged.token, operation="install")


def test_a_rejected_redemption_still_burns_the_token():
    """A token that survived a rejection would let a client hunt for the
    operation it was minted for."""
    staged = staging.stage(operation="install", extension_id="demo", digest="a" * 64)
    with pytest.raises(staging.StagingError):
        staging.redeem(staged.token, operation="update")
    with pytest.raises(staging.StagingError):
        staging.redeem(staged.token, operation="install")


def test_a_token_is_bound_to_its_extension():
    staged = staging.stage(operation="update", extension_id="demo", digest="a" * 64)
    with pytest.raises(staging.StagingError):
        staging.redeem(staged.token, operation="update", extension_id="other")


def test_an_expired_token_is_refused(monkeypatch):
    staged = staging.stage(operation="install", extension_id="demo", digest="a" * 64)
    monkeypatch.setattr(staging.time, "monotonic", lambda: staged.expires_at + 1)
    with pytest.raises(staging.StagingError, match="expired"):
        staging.redeem(staged.token, operation="install")


def test_live_tokens_pin_their_content_digest_until_they_expire(monkeypatch):
    staged = staging.stage(operation="install", extension_id="demo", digest="a" * 64)
    assert staging.pinned_digests() == {"a" * 64}

    monkeypatch.setattr(staging.time, "monotonic", lambda: staged.expires_at + 1)
    assert staging.pinned_digests() == set()


def test_a_lifecycle_mutation_discards_that_extension_s_pending_tokens():
    mine = staging.stage(operation="update", extension_id="demo", digest="a" * 64)
    theirs = staging.stage(operation="update", extension_id="other", digest="b" * 64)
    staging.discard("demo")
    with pytest.raises(staging.StagingError):
        staging.redeem(mine.token, operation="update")
    assert staging.redeem(theirs.token, operation="update").extension_id == "other"


def test_pending_tokens_are_bounded():
    tokens = [staging.stage(operation="install", extension_id=f"e{i}", digest="a" * 64) for i in range(40)]
    live = sum(1 for t in tokens if t.token in staging._PENDING)
    assert live <= staging.MAX_PENDING_TOKENS


def test_tokens_are_opaque_and_unguessable():
    a = staging.stage(operation="install", extension_id="demo", digest="a" * 64)
    b = staging.stage(operation="install", extension_id="demo", digest="a" * 64)
    assert a.token != b.token
    assert len(a.token) >= 32
    assert "demo" not in a.token
