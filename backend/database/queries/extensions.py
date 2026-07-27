"""Row-level helpers for the three community-extension tables, plus the
namespaced-data selection every purge and orphan listing shares.

Two responsibilities live here, and they are deliberately the same module:

* **Package metadata** -- ``extension_packages`` / ``extension_revisions`` /
  ``extension_secrets`` CRUD. ``install_package`` and ``activate_revision``
  take a single connection so an activation commits package row, revision row,
  grants, and enablement together; a lifecycle mutation that could half-apply
  would leave the runtime overlay describing a revision the database does not
  record.
* **Namespaced user data** -- the four state slots plus attachments and
  fragment instances an extension id owns. Preview and destructive delete call
  the *same* selection helpers, because a purge preview that counted rows a
  different predicate then deleted would be worse than no preview at all.

Nothing here consults the registry, compiles a package, or decides policy. The
one-way rule holds: ``database/`` never imports ``features/extensions/``, so an
uninstalled provider's rows stay ordinary inert JSON to this layer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import aiosqlite

from ..connection import get_db
from ..models import (
    ExtensionPackageRow,
    ExtensionPackageRuntimeRow,
    ExtensionRevisionRow,
    ExtensionSecretRow,
)

# The four JSON columns that hold one ``{extension_id: {...}}`` map each. Every
# namespaced read, count, and delete iterates this table rather than repeating
# four near-identical statements -- adding a fifth state scope should be one
# tuple, not four more copies of a JSON path expression.
_STATE_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("settings", "workflow_config", "id"),
    ("conversations", "workflow_state", "id"),
    ("messages", "workflow_state", "id"),
    ("character_cards", "workflow_state", "id"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def commit_extension_state(
    extension_id: str,
    updates: Mapping[str, Mapping[str, Any]],
    *,
    conversation_id: str | None,
    character_id: str | None,
    validate: Callable[[], None] | None = None,
) -> None:
    """Commit every database-backed extension state scope atomically.

    ``validate`` is a synchronous dependency-inversion hook run after
    ``BEGIN IMMEDIATE`` and immediately before the first write. The community
    runtime uses it to re-check live grants and its complete staged effect set
    without making ``database/`` import the feature layer. A concurrent
    permission mutation cannot pass its own write transaction between this
    check and these updates.
    """
    unknown = set(updates) - {"config", "conversation", "character"}
    if unknown:
        raise ValueError(f"unsupported extension state scope(s): {sorted(unknown)}")

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if validate is not None:
                validate()
            if "config" in updates:
                payload = dict(updates["config"])
                if payload:
                    await db.execute(
                        "UPDATE settings "
                        "SET workflow_config = json_set(COALESCE(workflow_config, '{}'), '$.' || ?, json(?)) "
                        "WHERE id = 1",
                        (extension_id, json.dumps(payload)),
                    )
                else:
                    await db.execute(
                        "UPDATE settings "
                        "SET workflow_config = json_remove(COALESCE(workflow_config, '{}'), '$.' || ?) "
                        "WHERE id = 1",
                        (extension_id,),
                    )
            if "conversation" in updates and conversation_id is not None:
                payload = dict(updates["conversation"])
                if payload:
                    await db.execute(
                        "UPDATE conversations "
                        "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                        "WHERE id = ?",
                        (extension_id, json.dumps(payload), conversation_id),
                    )
                else:
                    await db.execute(
                        "UPDATE conversations "
                        "SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) "
                        "WHERE id = ?",
                        (extension_id, conversation_id),
                    )
            if "character" in updates and character_id is not None:
                payload = dict(updates["character"])
                if payload:
                    await db.execute(
                        "UPDATE character_cards "
                        "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                        "WHERE id = ?",
                        (extension_id, json.dumps(payload), character_id),
                    )
                else:
                    await db.execute(
                        "UPDATE character_cards "
                        "SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) "
                        "WHERE id = ?",
                        (extension_id, character_id),
                    )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


# ── packages ────────────────────────────────────────────────────────────────


async def list_extension_packages() -> list[ExtensionPackageRuntimeRow]:
    """Every installed package, oldest install first.

    Deterministic order so the catalog, the startup compile log, and the
    published overlay agree on sequence without each imposing its own sort.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT p.*, r.contract_fingerprint AS active_contract_fingerprint "
                "FROM extension_packages AS p "
                "LEFT JOIN extension_revisions AS r "
                "ON r.extension_id = p.id AND r.content_digest = p.active_digest "
                "ORDER BY p.installed_at, p.id"
            )
        )
    return [cast(ExtensionPackageRuntimeRow, dict(r)) for r in rows]


async def get_extension_package(extension_id: str) -> ExtensionPackageRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM extension_packages WHERE id = ?", (extension_id,)))
    return cast(ExtensionPackageRow, dict(rows[0])) if rows else None


async def get_extension_revision(extension_id: str, content_digest: str) -> ExtensionRevisionRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM extension_revisions WHERE extension_id = ? AND content_digest = ?",
                (extension_id, content_digest),
            )
        )
    return cast(ExtensionRevisionRow, dict(rows[0])) if rows else None


async def referenced_digests() -> set[str]:
    """Every content digest the database still points at.

    Content garbage collection subtracts this (and the digests pinned by live
    runtime snapshots) from what is on disk. Both columns count: the previous
    digest is what rollback restores, so collecting it would turn "roll back"
    into "refetch, if the source still exists".
    """
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT active_digest, previous_digest FROM extension_packages"))
    digests: set[str] = set()
    for row in rows:
        digests.add(row["active_digest"])
        if row["previous_digest"]:
            digests.add(row["previous_digest"])
    return digests


async def _set_workflow_enabled(db: aiosqlite.Connection, extension_id: str, enabled: bool) -> None:
    cursor = await db.execute(
        "UPDATE settings SET workflow_enabled = json_set(COALESCE(workflow_enabled, '{}'), '$.' || ?, json(?)) WHERE id = 1",
        (extension_id, json.dumps(bool(enabled))),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("settings singleton is missing")


async def _prune_revisions(
    db: aiosqlite.Connection,
    extension_id: str,
    keep_digests: set[str],
) -> None:
    ordered = sorted(keep_digests)
    placeholders = ", ".join("?" for _ in ordered)
    await db.execute(
        f"DELETE FROM extension_revisions WHERE extension_id = ? AND content_digest NOT IN ({placeholders})",
        (extension_id, *ordered),
    )


async def _upsert_revision(
    db: aiosqlite.Connection,
    *,
    extension_id: str,
    content_digest: str,
    manifest: str,
    extension_api: int,
    version: str,
    commit_id: str | None,
    contract_fingerprint: str,
) -> None:
    """Record a revision while keeping the original ``first_seen_at``.

    Re-consenting to the same package bytes may still produce a new contract
    fingerprint after Orb's compiler semantics change. Update that consent
    record without pretending the content itself was first seen again.
    """
    await db.execute(
        "INSERT INTO extension_revisions "
        "(extension_id, content_digest, manifest, extension_api, version, commit_id, contract_fingerprint, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(extension_id, content_digest) DO UPDATE SET "
        "contract_fingerprint = excluded.contract_fingerprint",
        (
            extension_id,
            content_digest,
            manifest,
            extension_api,
            version,
            commit_id,
            contract_fingerprint,
            _now(),
        ),
    )


async def install_extension_package(
    *,
    extension_id: str,
    source_kind: str,
    source_url: str,
    requested_ref: str,
    content_digest: str,
    manifest: str,
    extension_api: int,
    version: str,
    commit_id: str | None,
    contract_fingerprint: str,
    approved_permissions: list[dict[str, Any]],
    enabled: bool,
    load_status: str,
    load_error: str,
) -> None:
    """Create a package registration, revision, grants, and switch atomically.

    The upsert is for idempotence, not for reinstall: the lifecycle layer routes
    an install over an existing package to the update flow, so the conflict
    branch only fires if a commit is retried. Preserving ``installed_at`` there
    keeps a retry from looking like a fresh install.

    The settings row is authoritative and the package column is its catalog
    mirror. Writing both before the same commit prevents a crash from leaving a
    requested-disabled package enabled through the missing-key default.
    """
    now = _now()
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT installed_at FROM extension_packages WHERE id = ?",
                (extension_id,),
            )
        )
        installed_at = rows[0]["installed_at"] if rows else now
        await db.execute(
            "INSERT INTO extension_packages "
            "(id, source_kind, source_url, requested_ref, active_digest, previous_digest, approved_permissions, "
            " enabled, load_status, load_error, installed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            " source_kind = excluded.source_kind, source_url = excluded.source_url, "
            " requested_ref = excluded.requested_ref, active_digest = excluded.active_digest, "
            " previous_digest = NULL, approved_permissions = excluded.approved_permissions, "
            " enabled = excluded.enabled, load_status = excluded.load_status, "
            " load_error = excluded.load_error, updated_at = excluded.updated_at",
            (
                extension_id,
                source_kind,
                source_url,
                requested_ref,
                content_digest,
                json.dumps(approved_permissions),
                int(enabled),
                load_status,
                load_error,
                installed_at,
                now,
            ),
        )
        await _upsert_revision(
            db,
            extension_id=extension_id,
            content_digest=content_digest,
            manifest=manifest,
            extension_api=extension_api,
            version=version,
            commit_id=commit_id,
            contract_fingerprint=contract_fingerprint,
        )
        await _prune_revisions(db, extension_id, {content_digest})
        await _set_workflow_enabled(db, extension_id, enabled)
        await db.commit()


async def activate_extension_revision(
    *,
    extension_id: str,
    content_digest: str,
    manifest: str,
    extension_api: int,
    version: str,
    commit_id: str | None,
    contract_fingerprint: str,
    approved_permissions: list[dict[str, Any]],
    source_kind: str | None = None,
    source_url: str | None = None,
    requested_ref: str | None = None,
    load_status: str,
    load_error: str,
    expected_active_digest: str,
) -> bool:
    """Swap the active digest of an installed package, one transaction.

    Returns False without writing when the current active digest is not
    *expected_active_digest* -- the compare-and-set that makes "an update fails
    with 409 if the active digest changed since inspection" true against a
    concurrent second update rather than only against a slow user.

    The outgoing digest becomes ``previous_digest``, which is the whole rollback
    story: one prior revision, kept until the next activation displaces it.
    """
    now = _now()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = list(
            await db.execute_fetchall(
                "SELECT active_digest, previous_digest FROM extension_packages WHERE id = ?",
                (extension_id,),
            )
        )
        if not rows or rows[0]["active_digest"] != expected_active_digest:
            await db.rollback()
            return False
        previous_digest = expected_active_digest if expected_active_digest != content_digest else rows[0]["previous_digest"]
        sets = [
            "active_digest = ?",
            "previous_digest = ?",
            "approved_permissions = ?",
            "load_status = ?",
            "load_error = ?",
            "updated_at = ?",
        ]
        vals: list[Any] = [
            content_digest,
            previous_digest,
            json.dumps(approved_permissions),
            load_status,
            load_error,
            now,
        ]
        for column, value in (
            ("source_kind", source_kind),
            ("source_url", source_url),
            ("requested_ref", requested_ref),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                vals.append(value)
        await db.execute(
            f"UPDATE extension_packages SET {', '.join(sets)} WHERE id = ?",
            (*vals, extension_id),
        )
        await _upsert_revision(
            db,
            extension_id=extension_id,
            content_digest=content_digest,
            manifest=manifest,
            extension_api=extension_api,
            version=version,
            commit_id=commit_id,
            contract_fingerprint=contract_fingerprint,
        )
        keep_digests = {content_digest}
        if previous_digest:
            keep_digests.add(previous_digest)
        await _prune_revisions(db, extension_id, keep_digests)
        await db.commit()
    return True


async def set_extension_permissions(extension_id: str, approved_permissions: list[dict[str, Any]]) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE extension_packages SET approved_permissions = ?, updated_at = ? WHERE id = ?",
            (json.dumps(approved_permissions), _now(), extension_id),
        )
        await db.commit()


async def set_extension_load_status(extension_id: str, load_status: str, load_error: str) -> None:
    """Record why an installed package publishes no entry points.

    Startup writes this back so the diagnostic survives a restart and shows up
    in the manager without recompiling; it never changes the user's enablement
    or grants, which are separate axes.
    """
    async with get_db() as db:
        await db.execute(
            "UPDATE extension_packages SET load_status = ?, load_error = ? WHERE id = ?",
            (load_status, load_error, extension_id),
        )
        await db.commit()


async def set_extension_enabled_flag(extension_id: str, enabled: bool) -> None:
    """Atomically update the authoritative switch and package-row mirror.

    ``settings.workflow_enabled`` survives uninstall and is what
    ``effective_workflow_enabled`` reads. The package column is the catalog's
    cheap read; neither is allowed to commit without the other.
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE extension_packages SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _now(), extension_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            raise LookupError(f"extension {extension_id!r} is not installed")
        await _set_workflow_enabled(db, extension_id, enabled)
        await db.commit()


async def delete_extension_package(extension_id: str) -> None:
    """Remove registration, revisions, and secrets. Namespaced data survives.

    That asymmetry is the design's: uninstall is reversible and keeps
    conversation/message/character state inert under the extension id, while
    ``purge_extension_data`` is the separate destructive operation. Revisions
    and secrets cascade through their foreign keys.
    """
    async with get_db() as db:
        await db.execute("DELETE FROM extension_packages WHERE id = ?", (extension_id,))
        await db.commit()


# ── secrets ─────────────────────────────────────────────────────────────────


async def list_extension_secret_names(extension_id: str) -> list[ExtensionSecretRow]:
    """Presence metadata only -- ``secret_value`` is never selected here.

    The API is write-only by construction rather than by a caller remembering
    to drop a column: the read path has no statement that returns the value.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT extension_id, name, '' AS secret_value, updated_at FROM extension_secrets "
                "WHERE extension_id = ? ORDER BY name",
                (extension_id,),
            )
        )
    return [cast(ExtensionSecretRow, dict(r)) for r in rows]


async def set_extension_secret(extension_id: str, name: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO extension_secrets (extension_id, name, secret_value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(extension_id, name) DO UPDATE SET secret_value = excluded.secret_value, "
            "updated_at = excluded.updated_at",
            (extension_id, name, value, _now()),
        )
        await db.commit()


async def delete_extension_secret(extension_id: str, name: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM extension_secrets WHERE extension_id = ? AND name = ?", (extension_id, name))
        await db.commit()


# ── namespaced user data ────────────────────────────────────────────────────


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _extension_data_selection(
    db: aiosqlite.Connection,
    extension_id: str,
) -> tuple[dict[str, int], str]:
    """Select stable row identities and hash the exact destructive scope."""

    selection: dict[str, list[str]] = {}
    for table, column, key_column in _STATE_SLOTS:
        rows = list(
            await db.execute_fetchall(
                f"SELECT CAST({key_column} AS TEXT) AS row_id FROM {table} "
                f"WHERE json_type({column}, '$.' || ?) IS NOT NULL ORDER BY {key_column}",
                (extension_id,),
            )
        )
        selection[f"{table}.{column}"] = [str(row["row_id"]) for row in rows]

    rows = list(
        await db.execute_fetchall(
            "SELECT CAST(id AS TEXT) AS row_id FROM workflow_attachments WHERE workflow_id = ? ORDER BY id",
            (extension_id,),
        )
    )
    selection["workflow_attachments"] = [str(row["row_id"]) for row in rows]

    rows = list(
        await db.execute_fetchall(
            "SELECT CAST(id AS TEXT) AS row_id FROM interactive_fragments WHERE field_type LIKE ? ESCAPE '\\' ORDER BY id",
            (f"{_escape_like(extension_id)}:%",),
        )
    )
    selection["interactive_fragments"] = [str(row["row_id"]) for row in rows]

    counts = {key: len(row_ids) for key, row_ids in selection.items()}
    fingerprint = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return counts, fingerprint


async def extension_data_preview(extension_id: str) -> tuple[dict[str, int], str]:
    """Count and fingerprint exactly what a confirmed purge would remove."""

    async with get_db() as db:
        await db.execute("BEGIN")
        try:
            return await _extension_data_selection(db, extension_id)
        finally:
            await db.rollback()


async def purge_extension_data(extension_id: str, *, expected_fingerprint: str) -> dict[str, int] | None:
    """Delete namespaced data only if it still matches the user's preview.

    ``BEGIN IMMEDIATE`` closes the gap between revalidation and deletion. A
    mismatch returns ``None`` without changing any user data.
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        removed, fingerprint = await _extension_data_selection(db, extension_id)
        if fingerprint != expected_fingerprint:
            await db.rollback()
            return None

        for table, column, _key_column in _STATE_SLOTS:
            cur = await db.execute(
                f"UPDATE {table} SET {column} = json_remove(COALESCE({column}, '{{}}'), '$.' || ?) "
                f"WHERE json_type({column}, '$.' || ?) IS NOT NULL",
                (extension_id, extension_id),
            )
            if cur.rowcount != removed[f"{table}.{column}"]:
                await db.rollback()
                return None

        cur = await db.execute("DELETE FROM workflow_attachments WHERE workflow_id = ?", (extension_id,))
        if cur.rowcount != removed["workflow_attachments"]:
            await db.rollback()
            return None

        cur = await db.execute(
            "DELETE FROM interactive_fragments WHERE field_type LIKE ? ESCAPE '\\'",
            (f"{_escape_like(extension_id)}:%",),
        )
        if cur.rowcount != removed["interactive_fragments"]:
            await db.rollback()
            return None

        await db.commit()
    return removed


async def namespaced_state_owners() -> dict[str, int]:
    """Every extension id that owns namespaced data, with a slot count.

    The manager needs this to list *orphaned* data: uninstall preserves state,
    so without a listing keyed by id rather than by installed package, "uninstall
    but keep my data" would make that data permanently unreachable -- neither
    visible nor purgeable.

    Built-in workflow ids share the same JSON maps, so the caller subtracts the
    ids it knows. This layer deliberately does not: ``database/`` consulting the
    live registry would be an upward import.
    """
    owners: dict[str, int] = {}
    async with get_db() as db:
        for table, column, _key_column in _STATE_SLOTS:
            rows = list(
                await db.execute_fetchall(
                    f"SELECT key AS owner, COUNT(*) AS n FROM {table}, json_each({table}.{column}) "
                    f"WHERE {column} IS NOT NULL GROUP BY key"
                )
            )
            for row in rows:
                owners[row["owner"]] = owners.get(row["owner"], 0) + int(row["n"])
        rows = list(
            await db.execute_fetchall(
                "SELECT workflow_id AS owner, COUNT(*) AS n FROM workflow_attachments GROUP BY workflow_id"
            )
        )
        for row in rows:
            owners[row["owner"]] = owners.get(row["owner"], 0) + int(row["n"])
        rows = list(
            await db.execute_fetchall(
                "SELECT substr(field_type, 1, instr(field_type, ':') - 1) AS owner, COUNT(*) AS n "
                "FROM interactive_fragments WHERE instr(field_type, ':') > 1 GROUP BY owner"
            )
        )
        for row in rows:
            owners[row["owner"]] = owners.get(row["owner"], 0) + int(row["n"])
    return owners
