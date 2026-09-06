"""Cache workflow attachment bytes with size accounting and recovery-aware eviction."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from ..database.connection import get_db
from ..database.queries.messages import register_workflow_attachment_persister
from ..database.queries.workflow_attachments import (
    EVICTED_MARKER,
    _encode_metadata_field,
    _staging_root,
    insert_workflow_attachment_row,
)
from .registry import get_workflow

logger = logging.getLogger(__name__)

# EVICTED_MARKER is re-exported from the database boundary above (where it
# describes the persisted ``data_b64`` shape) so eviction's callers -- the
# routes, toolkit, and image_gen references -- import it from this module.


class RehydrateAlreadyDoneError(ValueError):
    """Raised by ``rehydrate_attachment`` when the row already holds bytes.

    Subclass of ``ValueError`` so broader ``except ValueError`` handlers
    in the rehydrate path still catch it as a write-refusal class; the
    HTTP route catches it specifically and maps to 409 (race lost: a
    concurrent rehydrate already restored the bytes).
    """


# Reason strings tagged onto rejected attachment dicts by both helpers.
# Every rejected entry carries a ``reason`` key; route and SSE projection
# layers read that key verbatim into their JSON response. The strings are
# part of the response contract -- frontend chips display them.
#
# - OVERSIZE_NO_METADATA_REASON: attachment size exceeds the cache budget
#   AND lacks the ``seed`` + ``generation_metadata`` fields needed to
#   rehydrate later; marker-storing would create a permanently
#   unrecoverable row, so the helper drops the entry instead.
# - WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON: the producing workflow is not
#   registered with ``produces_artifacts=True``; only declared artifact
#   workflows may persist attachments to ``workflow_attachments``.
#
# The route layer additionally prepends VALIDATOR-emitted rejections to
# ``rejected_workflow_atts``, each carrying its own per-gate reason
# string from validate_workflow_attachment_shape(). Helper-class entries
# use the constants here; pre-validator entries use the validator's
# per-gate strings.
OVERSIZE_NO_METADATA_REASON = "too large to cache, no recovery metadata"
WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON = "workflow does not declare produces_artifacts"


def project_rejected_attachment(a: Mapping[str, Any], originating_attachment_id: int | None) -> dict:
    """Client-facing projection of one rejected-attachment record.

    The HTTP routes and the SSE persistence layer both surface rejections in
    this shape. ``reason`` falls back to ``OVERSIZE_NO_METADATA_REASON`` for
    helper-class entries that carry none; ``originating_attachment_id`` is the
    group root the rejection relates to (``None`` for first-write rejections,
    which have no DB row yet).
    """
    return {
        "filename": a.get("filename"),
        "workflow_id": a.get("workflow_id"),
        "mime": a.get("mime"),
        "reason": a.get("reason") or OVERSIZE_NO_METADATA_REASON,
        "originating_attachment_id": originating_attachment_id,
    }


def _is_produces_artifacts_workflow(workflow_id: str) -> bool:
    """True iff ``workflow_id`` resolves to a registered workflow whose
    ``produces_artifacts`` is True. Unregistered ids return False so an
    attachment carrying a stale workflow_id is refused at the cache
    boundary."""
    w = get_workflow(workflow_id)
    return bool(w and w.produces_artifacts)


def _lru3_key(c: dict) -> float:
    """Eviction sort key. Smallest comes out first.

    Key is the row's *oldest* known access counter -- the last element of
    `recent_accesses` (or the only element when len < 3). Smallest counter
    means "longest time since this row was accessed even at K=3 depth".

    Rows with empty or missing `recent_accesses` are protected (sort to
    the end via +inf). The birth-counts-as-access invariant should keep
    every byte-bearing row populated; +inf is defensive against malformed
    JSON or migration leftovers.
    """
    ra = c.get("recent_accesses")
    if not ra:
        return float("inf")
    return float(ra[-1])


def select_lru3_victim(candidates: list[dict]) -> int | None:
    """Pick a single eviction victim by ``_lru3_key``. Returns id or None.

    The atomic insert/rehydrate paths in this module cover a byte shortfall
    via :func:`plan_eviction` rather than calling this helper, so the
    single-victim path is a separate pinned interface for the unit tests
    that exercise the LRU-3 ordering in isolation.
    """
    evictable = [c for c in candidates if c.get("rehydratable", True)]
    if not evictable:
        return None
    return min(evictable, key=_lru3_key)["id"]


def plan_eviction(candidates: list[dict], shortfall: int) -> list[dict]:
    """Oldest-first (LRU-3) eviction prefix covering *shortfall* bytes.

    Returns the victim candidate dicts in eviction order; empty when
    *shortfall* is already non-positive. Shared by the rehydrate,
    single-insert, and batch-insert paths so all three budget the cache
    with identical ordering. Candidates explicitly marked
    ``rehydratable=False`` are pinned and skipped.
    """
    victims: list[dict] = []
    if shortfall <= 0:
        return victims
    for victim in sorted(candidates, key=_lru3_key):
        if shortfall <= 0:
            break
        if not victim.get("rehydratable", True):
            continue
        victims.append(victim)
        shortfall -= victim["size"]
    return victims


def _decode_recent_accesses(raw: Any) -> list[int] | None:
    """Decode a stored ``recent_accesses`` JSON column value.

    Returns the list of int counters, or ``None`` when the value is missing,
    malformed JSON, not a list, or contains any non-int entry. The
    birth-counts-as-access invariant should keep every byte-bearing row
    well-formed; ``None`` is defensive against malformed data or migration
    leftovers.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(v, int) for v in parsed):
        return None
    return parsed


async def _get_budget_bytes_on(db) -> int:
    rows = list(await db.execute_fetchall("SELECT attachment_cache_budget_bytes FROM settings WHERE id = 1"))
    return int(rows[0]["attachment_cache_budget_bytes"]) if rows else 0


# Single source of truth: bytes live in data_b64. The byte count is derived from
# the column's length rather than stored separately, so no second column can
# drift from the bytes it claims to describe. Base64 math: every 4 input chars
# encode 3 bytes, minus 1 for each trailing '=' padding char.
_SIZE_EXPR = "((length(data_b64) / 4) * 3) - (length(data_b64) - length(rtrim(data_b64, '='))) AS size"


async def _byte_bearing_candidates_on(db) -> list[dict]:
    rows = list(
        await db.execute_fetchall(
            f"SELECT id, {_SIZE_EXPR}, recent_accesses, seed, generation_metadata "  # nosec B608
            "FROM workflow_attachments WHERE data_b64 != ? ORDER BY id ASC",
            (EVICTED_MARKER,),
        )
    )
    return [
        {
            "id": r["id"],
            "size": int(r["size"] or 0),
            "recent_accesses": _decode_recent_accesses(r["recent_accesses"]),
            "rehydratable": _stored_rehydratable(r["seed"], r["generation_metadata"]),
        }
        for r in rows
    ]


def _covered(victims: list[dict], shortfall: int) -> bool:
    """Whether an eviction plan releases enough bytes for ``shortfall``."""
    return shortfall <= 0 or sum(v["size"] for v in victims) >= shortfall


async def rehydrate_attachment(attachment_id: int, data: bytes, *, consumption_metadata: dict | None = None) -> None:
    """Restore evicted bytes into an attachment row."""
    import base64

    new_size = len(data)
    data_b64 = base64.b64encode(bytes(data)).decode("ascii")
    cm_json = (
        _encode_metadata_field(consumption_metadata, "consumption_metadata", "<rehydrate>", "<rehydrate>")
        if consumption_metadata is not None
        else None
    )

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = list(
            await db.execute_fetchall(
                "SELECT data_b64 FROM workflow_attachments WHERE id = ?",
                (attachment_id,),
            )
        )
        if not rows:
            raise LookupError(f"workflow_attachment {attachment_id!r} does not exist")
        if rows[0]["data_b64"] != EVICTED_MARKER:
            raise RehydrateAlreadyDoneError(f"workflow_attachment {attachment_id!r} bytes are present; rehydrate refused")

        budget = await _get_budget_bytes_on(db)
        if new_size > budget:
            raise ValueError(
                f"workflow_attachment {attachment_id!r} size {new_size} exceeds cache budget {budget}; refusing to rehydrate"
            )

        candidates = await _byte_bearing_candidates_on(db)
        candidates = [c for c in candidates if c["id"] != attachment_id]
        occupied = sum(c["size"] for c in candidates)
        shortfall = (occupied + new_size) - budget
        victims = plan_eviction(candidates, shortfall)
        if not _covered(victims, shortfall):
            raise ValueError(f"workflow_attachment {attachment_id!r} cannot fit without evicting unrecoverable artifacts")
        for victim in victims:
            await _evict_on(db, victim["id"])

        # Reset recent_accesses alongside the bytes write so the post-rehydrate
        # row matches the birth-as-access invariant: a single freshly-assigned
        # counter, equivalent to a brand-new insert. Without the reset, the
        # pre-eviction counters survive (eviction only touches data_b64) and
        # _lru3_key reads a stale oldest entry, making the just-rehydrated row
        # the next eviction leader -- defeating the user's "give me this back"
        # intent on the very next insert.
        if cm_json is not None:
            await db.execute(
                "UPDATE workflow_attachments SET data_b64 = ?, recent_accesses = NULL, consumption_metadata = ? WHERE id = ?",
                (data_b64, cm_json, attachment_id),
            )
        else:
            await db.execute(
                "UPDATE workflow_attachments SET data_b64 = ?, recent_accesses = NULL WHERE id = ?",
                (data_b64, attachment_id),
            )
        await _record_access_inner(db, [attachment_id])
        await db.commit()


async def _evict_on(db, attachment_id: int) -> None:
    rows = list(
        await db.execute_fetchall(
            "SELECT data_b64, seed, generation_metadata FROM workflow_attachments WHERE id = ?",
            (attachment_id,),
        )
    )
    if not rows or rows[0]["data_b64"] == EVICTED_MARKER:
        return
    if not _stored_rehydratable(rows[0]["seed"], rows[0]["generation_metadata"]):
        raise ValueError(f"workflow_attachment {attachment_id!r} has no usable recovery metadata; eviction refused")
    await db.execute(
        "UPDATE workflow_attachments SET data_b64 = ? WHERE id = ?",
        (EVICTED_MARKER, attachment_id),
    )


async def evict(attachment_id: int) -> None:
    """Sentinel-mark the row: overwrite ``data_b64`` with EVICTED_MARKER,
    leave every other column intact. The preserved columns each carry
    weight on rehydration: ``seed`` + ``generation_metadata`` let the
    workflow regenerate the bytes deterministically; ``filename`` /
    ``mime_type`` / ``workflow_id`` / ``parent_attachment_id`` keep the
    row renderable + groupable while it is byteless. Byte count is not
    preserved -- it is derived from ``data_b64``'s length at read time,
    so an evicted row reports 0 (the sentinel decodes to 0 bytes) and
    rehydrate sizes itself against the bytes the caller provides."""
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _evict_on(db, attachment_id)
        await db.commit()


async def _aged_candidates_on(db, cutoff: str | None) -> list[tuple[int, int]]:
    """``(id, size)`` for every byte-bearing, evictable row older than ``cutoff``.

    ``cutoff`` is an ISO-8601 UTC string (None = no age limit). ``created_at``
    is stored in that same format, so a plain string compare orders correctly
    -- the trick queries/stats.py already documents and relies on.

    ``_stored_rehydratable`` is applied here in Python rather than as SQL: it
    JSON-decodes ``generation_metadata`` and shape-checks it, which a
    ``seed IS NOT NULL`` test cannot approximate.
    """
    sql = f"SELECT id, {_SIZE_EXPR}, seed, generation_metadata FROM workflow_attachments WHERE data_b64 != ?"  # nosec B608
    params: tuple[Any, ...] = (EVICTED_MARKER,)
    if cutoff is not None:
        sql += " AND created_at < ?"
        params = (EVICTED_MARKER, cutoff)
    rows = list(await db.execute_fetchall(sql, params))
    return [(int(r["id"]), int(r["size"] or 0)) for r in rows if _stored_rehydratable(r["seed"], r["generation_metadata"])]


async def aged_artifact_size(cutoff: str | None) -> tuple[int, int]:
    """``(count, bytes)`` that ``evict_older_than(cutoff)`` would release.

    Read-only preview for the cleanup UI, so the age choice is made against
    real numbers. Same candidate set as the evictor, so the preview cannot
    disagree with what the cleanup then does.
    """
    async with get_db() as db:
        candidates = await _aged_candidates_on(db, cutoff)
    return len(candidates), sum(size for _, size in candidates)


async def evict_older_than(cutoff: str | None) -> tuple[int, int]:
    """Sentinel-evict every evictable artifact created before ``cutoff``
    (None = every artifact regardless of age). Returns ``(count, bytes_freed)``.

    Bulk counterpart to ``evict``: same marker, same preserved columns, so an
    age-based cleanup stays as reversible as a budget eviction -- the images
    come back through the normal rehydrate button.

    Rows without usable recovery metadata are skipped, not destroyed. Today
    that is TTS audio, which stores no seed.
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        candidates = await _aged_candidates_on(db, cutoff)
        for attachment_id, _ in candidates:
            await _evict_on(db, attachment_id)
        await db.commit()
    return len(candidates), sum(size for _, size in candidates)


async def _record_access_inner(db, attachment_ids: list[int]) -> None:
    """Counter bump + recent_accesses prepend over an existing connection.

    Caller owns the transaction lifecycle -- this helper does not commit.

    Empty id list is a no-op. The settings row is expected to exist
    (seeded at init); if it doesn't, the function returns silently and
    the caller's commit covers whatever prior writes occurred.
    """
    if not attachment_ids:
        return
    n = len(attachment_ids)

    await db.execute(
        "UPDATE settings SET attachment_access_counter = attachment_access_counter + ? WHERE id = 1",
        (n,),
    )
    rows = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))
    if not rows:
        return
    end = int(rows[0]["attachment_access_counter"])

    for i, att_id in enumerate(attachment_ids):
        assigned = end - n + 1 + i
        cur_rows = list(
            await db.execute_fetchall(
                "SELECT recent_accesses FROM workflow_attachments WHERE id = ?",
                (att_id,),
            )
        )
        if not cur_rows:
            continue
        cur = _decode_recent_accesses(cur_rows[0]["recent_accesses"]) or []
        new_list = ([assigned] + cur)[:3]
        await db.execute(
            "UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?",
            (json.dumps(new_list), att_id),
        )


async def record_access(attachment_ids: list[int]) -> None:
    """Record an attachment access and update its recency."""
    if not attachment_ids:
        return
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _record_access_inner(db, attachment_ids)
        await db.commit()


def _estimate_size(attachment: dict) -> int:
    """Size for the eviction-budget check, without reading the file twice.

    Inline-data attachments report exact byte counts via ``len``. Path-shape
    entries use ``os.path.getsize``; an unreadable path raises ``OSError``
    up to the caller. The row helper's later ``open()`` would have raised
    on the same path -- surfacing the failure here means the eviction loop
    never runs for a doomed insert, so the cache never evicts real bytes
    to make room for a row that won't materialize.
    """
    raw = attachment.get("data")
    if isinstance(raw, (bytes, bytearray)):
        return len(raw)
    path = attachment.get("path")
    if isinstance(path, str):
        # Confine to the staging root before stat (see _staging_root).
        resolved = os.path.realpath(path)
        if not resolved.startswith(_staging_root() + os.sep):
            raise ValueError("path escapes the workflow staging root")
        return os.path.getsize(resolved)
    return 0


def _is_rehydratable(attachment: dict) -> bool:
    """Gate for marker-insertion: only atts carrying both seed (non-empty
    string) and strictly JSON-serializable generation_metadata (dict) can be
    safely stored as evicted markers, because rehydrate needs both to
    reproduce the bytes later.
    Atts lacking either field would become permanently unrecoverable rows
    if marker-stored, so they are refused (single-row) or dropped from the
    batch (batch). Empty-dict metadata is allowed -- some workflows
    regenerate deterministically from seed alone."""
    seed = attachment.get("seed")
    md = attachment.get("generation_metadata")
    return isinstance(seed, str) and bool(seed) and _serializable_metadata_dict(md)


def _serializable_metadata_dict(value: object) -> bool:
    """True for dicts that survive the strict JSON storage contract."""
    if not isinstance(value, dict):
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _stored_rehydratable(seed: object, generation_metadata: object) -> bool:
    """Apply the recovery contract to the database's encoded row shape."""
    if not isinstance(seed, str) or not seed or not isinstance(generation_metadata, str):
        return False
    try:
        decoded = json.loads(generation_metadata)
    except (TypeError, ValueError):
        return False
    return _serializable_metadata_dict(decoded)


def validate_workflow_attachment_shape(attachment: Any) -> tuple[bool, str | None]:
    """Validate a workflow-attachment payload."""
    # Defense-in-depth: today's only caller (regenerate route) pre-filters
    # non-dicts before invoking, but the gate stays so unit tests pin the
    # exhaustive contract and future callers don't need to re-derive it.
    if not isinstance(attachment, dict):
        return False, "not a dict"
    workflow_id = attachment.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        return False, "workflow_id must be a non-empty string"
    filename = attachment.get("filename")
    if not isinstance(filename, str):
        return False, "filename must be a string"
    mime = attachment.get("mime")
    if not isinstance(mime, str):
        return False, "mime must be a string"
    has_data = "data" in attachment
    has_path = "path" in attachment
    if has_data == has_path:
        return False, "exactly one of 'data' or 'path' required"
    if has_data:
        data = attachment["data"]
        if not isinstance(data, (bytes, bytearray)):
            return False, "data must be bytes"
        if not data:
            return False, "data is empty"
    else:
        path = attachment["path"]
        if not isinstance(path, str):
            return False, "path must be a string"
        # Confine to the staging root before stat (see _staging_root).
        resolved = os.path.realpath(path)
        if not resolved.startswith(_staging_root() + os.sep):
            return False, "path is outside the workflow staging area"
        try:
            if not os.path.isfile(resolved):
                return False, "path does not exist or is not a regular file"
            if os.path.getsize(resolved) == 0:
                return False, "path points at an empty file"
        except OSError:
            return False, "path is not stat-able"
    return True, None


async def _check_flat_parent_on(db, parent_id: int, expected_message_id: int) -> None:
    """Verify a parent attachment belongs to the expected message."""
    rows = list(
        await db.execute_fetchall(
            "SELECT parent_attachment_id, message_id FROM workflow_attachments WHERE id = ?",
            (parent_id,),
        )
    )
    if not rows:
        raise LookupError(f"parent_attachment_id {parent_id!r} does not exist")
    if rows[0]["parent_attachment_id"] is not None:
        raise ValueError(
            f"parent_attachment_id {parent_id!r} is itself a sibling "
            f"(its parent={rows[0]['parent_attachment_id']!r}); "
            f"workflow_attachments groups must stay flat -- pass the root id"
        )
    if rows[0]["message_id"] != expected_message_id:
        raise ValueError(
            f"parent_attachment_id {parent_id!r} belongs to message "
            f"{rows[0]['message_id']!r}, not {expected_message_id!r}; "
            f"workflow_attachments groups are intra-message -- the parent "
            f"root must be attached to the same message as the new sibling"
        )


async def insert_workflow_attachment(
    message_id: int, attachment: dict, *, mark_active: bool = True
) -> tuple[int | None, dict | None]:
    """Cache and insert one workflow attachment."""
    parent_id = attachment.get("parent_attachment_id")
    new_size = _estimate_size(attachment)
    workflow_id = attachment.get("workflow_id") or ""

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")

        if not _is_produces_artifacts_workflow(workflow_id):
            # Only declared artifact workflows may persist rows; reject
            # before any DB writes so BEGIN IMMEDIATE rolls back clean.
            return (None, {**attachment, "reason": WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON})

        if isinstance(parent_id, int) and not isinstance(parent_id, bool):
            await _check_flat_parent_on(db, parent_id, message_id)

        budget = await _get_budget_bytes_on(db)
        insert_as_marker = new_size > budget
        if insert_as_marker and not _is_rehydratable(attachment):
            return (None, {**attachment, "reason": OVERSIZE_NO_METADATA_REASON})

        candidates = await _byte_bearing_candidates_on(db)
        occupied = sum(c["size"] for c in candidates)
        shortfall = 0 if insert_as_marker else (occupied + new_size) - budget
        victims = plan_eviction(candidates, shortfall)

        # Pinned legacy rows may leave too little safe capacity for this
        # artifact. Preserve the new row as a marker when it is recoverable;
        # otherwise reject it instead of destroying an irreplaceable old row.
        if not insert_as_marker and not _covered(victims, shortfall):
            if not _is_rehydratable(attachment):
                return (None, {**attachment, "reason": OVERSIZE_NO_METADATA_REASON})
            insert_as_marker = True
            victims = plan_eviction(candidates, occupied - budget)

        for victim in victims:
            await _evict_on(db, victim["id"])

        new_id = await insert_workflow_attachment_row(message_id, attachment, db=db, insert_as_evicted=insert_as_marker)
        # Birth-counts-as-access: every new row starts with one counter entry
        # so it is never eviction-eligible by virtue of an empty access log.
        await _record_access_inner(db, [new_id])

        if mark_active and isinstance(parent_id, int) and not isinstance(parent_id, bool):
            await _set_active_sibling_on(db, parent_id, new_id)

        await db.commit()

    return (new_id, None)


async def _set_active_sibling_on(db, root_id: int, sibling_id: int | None) -> None:
    await db.execute(
        "UPDATE workflow_attachments SET active_sibling_id = ? WHERE id = ?",
        (sibling_id, root_id),
    )


async def insert_workflow_attachments(
    message_id: int,
    attachments: list[dict],
    *,
    db=None,
    mark_active: bool = True,
) -> tuple[list[int], list[dict]]:
    """Cache and insert workflow attachments atomically."""
    if not attachments:
        return [], []

    # Step 0: policy partition. Attachments whose workflow_id does not
    # resolve to a produces_artifacts=True workflow are routed to
    # rejected_atts and excluded from byte accounting; they never touch
    # the DB and never trigger eviction of valid byte-bearing rows.
    rejected_idx_policy: set[int] = {
        i for i, att in enumerate(attachments) if not _is_produces_artifacts_workflow(att.get("workflow_id") or "")
    }
    effective_indices = [i for i in range(len(attachments)) if i not in rejected_idx_policy]

    # OSError on a bad path-shape size surfaces before any DB work
    # (write-lock not yet taken), so the eviction loop never runs for an
    # insert that would have failed to materialize.
    sizes: dict[int, int] = {i: _estimate_size(attachments[i]) for i in effective_indices}
    new_total = sum(sizes.values())

    async def _run(conn) -> tuple[list[int], list[dict]]:
        # Enforce the caller-owned-transaction contract: the read-then-
        # evict-then-insert sequence below relies on the write lock to
        # keep the candidate snapshot stable. Self-managed branch below
        # acquires BEGIN IMMEDIATE before calling in, so this guard only
        # ever fires for a caller-provided `db` that skipped it.
        if not getattr(conn, "in_transaction", False):
            raise RuntimeError(
                "insert_workflow_attachments: caller-provided db must hold "
                "an active write transaction (BEGIN IMMEDIATE) before "
                "invoking; the read-then-evict-then-insert sequence relies "
                "on the write lock to keep the candidate snapshot stable"
            )

        # Validate every supplied parent_attachment_id under the write lock
        # before any plan or write. Same invariant as the single-row path:
        # siblings must hang off a root, never off another sibling. Dedup so
        # repeated parents in one batch cost a single SELECT. Policy-rejected
        # entries are skipped -- they will never insert, so their parent
        # ids do not need verification.
        seen_parents: set[int] = set()
        for i in effective_indices:
            pid = attachments[i].get("parent_attachment_id")
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue
            if pid in seen_parents:
                continue
            seen_parents.add(pid)
            await _check_flat_parent_on(conn, pid, message_id)

        budget = await _get_budget_bytes_on(conn)
        existing = await _byte_bearing_candidates_on(conn)
        occupied = sum(c["size"] for c in existing)
        pinned_occupied = sum(c["size"] for c in existing if not c["rehydratable"])
        new_byte_capacity = max(0, budget - pinned_occupied)

        # Step A: marker/reject new atts biggest-first until the new
        # byte-bearing total fits in budget. Markering one big new att
        # can spare many small existing rows from eviction, so this is
        # the first lever to pull. Tie-break by input index for
        # determinism. Policy-rejected indices are excluded.
        plan_mark_new: set[int] = set()
        rejected_idx_oversize: set[int] = set()
        new_byte_total = new_total
        if new_byte_total > new_byte_capacity:
            indexed = sorted(effective_indices, key=lambda i: (-sizes[i], i))
            for i in indexed:
                if new_byte_total <= new_byte_capacity:
                    break
                if not _is_rehydratable(attachments[i]):
                    rejected_idx_oversize.add(i)
                else:
                    plan_mark_new.add(i)
                new_byte_total -= sizes[i]

        # Step B: evict existing oldest-first for any residual shortfall
        # (occupied + new_byte_total > budget). Runtime over-budget
        # state (occupied alone > budget after a settings shrink) also
        # converges here on the next write.
        for victim in plan_eviction(existing, (occupied + new_byte_total) - budget):
            await _evict_on(conn, victim["id"])

        new_ids_by_input_idx: dict[int, int] = {}
        rejected_atts: list[dict] = []
        for i, att in enumerate(attachments):
            if i in rejected_idx_policy:
                rejected_atts.append({**att, "reason": WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON})
                continue
            if i in rejected_idx_oversize:
                rejected_atts.append({**att, "reason": OVERSIZE_NO_METADATA_REASON})
                continue
            new_id = await insert_workflow_attachment_row(
                message_id,
                att,
                db=conn,
                insert_as_evicted=(i in plan_mark_new),
            )
            new_ids_by_input_idx[i] = new_id

        new_ids = [new_ids_by_input_idx[i] for i in sorted(new_ids_by_input_idx.keys())]
        await _record_access_inner(conn, new_ids)

        if mark_active:
            for i, att in enumerate(attachments):
                if i not in new_ids_by_input_idx:
                    continue
                parent_id = att.get("parent_attachment_id")
                if isinstance(parent_id, int) and not isinstance(parent_id, bool):
                    await _set_active_sibling_on(conn, parent_id, new_ids_by_input_idx[i])

        return new_ids, rejected_atts

    if db is not None:
        return await _run(db)
    async with get_db() as own_db:
        await own_db.execute("BEGIN IMMEDIATE")
        result = await _run(own_db)
        await own_db.commit()
        return result


async def set_active_sibling(
    root_id: int,
    sibling_id: int | None,
    *,
    expected_message_id: int | None = None,
) -> None:
    """Persist the active attachment variant."""
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        root_rows = list(
            await db.execute_fetchall(
                "SELECT id, parent_attachment_id, message_id FROM workflow_attachments WHERE id = ?",
                (root_id,),
            )
        )
        if not root_rows:
            raise LookupError(f"workflow_attachment root {root_id!r} does not exist")
        root_row = root_rows[0]
        if expected_message_id is not None and root_row["message_id"] != expected_message_id:
            raise LookupError(f"workflow_attachment root {root_id!r} not on message {expected_message_id!r}")
        if root_row["parent_attachment_id"] is not None:
            raise ValueError(f"workflow_attachment {root_id!r} is not a root")
        if sibling_id is not None:
            sib_rows = list(
                await db.execute_fetchall(
                    "SELECT id, parent_attachment_id, message_id FROM workflow_attachments WHERE id = ?",
                    (sibling_id,),
                )
            )
            if not sib_rows:
                raise LookupError(f"workflow_attachment sibling {sibling_id!r} does not exist")
            sib_row = sib_rows[0]
            if sib_row["message_id"] != root_row["message_id"]:
                raise LookupError(f"sibling {sibling_id!r} not on the same message as root {root_id!r}")
            if sib_row["id"] != root_id and sib_row["parent_attachment_id"] != root_id:
                raise ValueError(f"sibling {sibling_id!r} does not belong to root {root_id!r}'s group")
        await _set_active_sibling_on(db, root_id, sibling_id)
        await db.commit()


async def _active_sibling_on(db, root_id: int) -> int | None:
    """Read a group root's ``active_sibling_id``; None when the row is gone."""
    rows = list(await db.execute_fetchall("SELECT active_sibling_id FROM workflow_attachments WHERE id = ?", (root_id,)))
    return rows[0]["active_sibling_id"] if rows else None


def _delete_result(deleted_ids: list[int], *, group_empty: bool, root_id: int, active_sibling_id: int | None) -> dict:
    """The one return shape of :func:`delete_workflow_attachments` (see its docstring)."""
    return {
        "deleted_ids": deleted_ids,
        "group_empty": group_empty,
        "root_id": root_id,
        "active_sibling_id": active_sibling_id,
    }


async def delete_workflow_attachments(
    target_id: int,
    *,
    scope: str,
    expected_message_id: int | None = None,
) -> dict:
    """Delete an attachment variant or group."""
    if scope not in ("variant", "group"):
        raise ValueError(f"scope must be 'variant' or 'group'; got {scope!r}")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = list(
            await db.execute_fetchall(
                "SELECT id, parent_attachment_id, message_id, active_sibling_id, annotation "
                "FROM workflow_attachments WHERE id = ?",
                (target_id,),
            )
        )
        if not rows:
            raise LookupError(f"workflow_attachment {target_id!r} does not exist")
        target = rows[0]
        if expected_message_id is not None and target["message_id"] != expected_message_id:
            raise LookupError(f"workflow_attachment {target_id!r} not on message {expected_message_id!r}")
        root_id = target["parent_attachment_id"] or target_id
        if root_id == target_id:
            root_active = target["active_sibling_id"]
        else:
            root_active = await _active_sibling_on(db, root_id)

        if scope == "group":
            del_ids = [
                x["id"]
                for x in await db.execute_fetchall(
                    "SELECT id FROM workflow_attachments WHERE id = ? OR parent_attachment_id = ?",
                    (root_id, root_id),
                )
            ]
            await db.execute(
                "DELETE FROM workflow_attachments WHERE id = ? OR parent_attachment_id = ?",
                (root_id, root_id),
            )
            await db.commit()
            return _delete_result(del_ids, group_empty=True, root_id=root_id, active_sibling_id=None)

        if target_id != root_id:
            await db.execute("DELETE FROM workflow_attachments WHERE id = ?", (target_id,))
            after = await _active_sibling_on(db, root_id)
            await db.commit()
            return _delete_result([target_id], group_empty=False, root_id=root_id, active_sibling_id=after)

        survivors = [
            x["id"]
            for x in await db.execute_fetchall(
                "SELECT id FROM workflow_attachments WHERE parent_attachment_id = ? ORDER BY id",
                (root_id,),
            )
        ]
        if not survivors:
            await db.execute("DELETE FROM workflow_attachments WHERE id = ?", (root_id,))
            await db.commit()
            return _delete_result([root_id], group_empty=True, root_id=root_id, active_sibling_id=None)
        new_root = survivors[0]
        new_active = root_active if (root_active is not None and root_active != root_id and root_active in survivors) else None
        await db.execute(
            "UPDATE workflow_attachments SET parent_attachment_id = ? WHERE parent_attachment_id = ? AND id != ?",
            (new_root, root_id, new_root),
        )
        # Only a root row's annotation reaches the LLM prefix (prompting.base), so
        # the promoted root inherits the deleted root's annotation; otherwise
        # deleting the root variant would silently change the message's
        # model-visible text.
        await db.execute(
            "UPDATE workflow_attachments SET parent_attachment_id = NULL, annotation = ? WHERE id = ?",
            (target["annotation"], new_root),
        )
        await _set_active_sibling_on(db, new_root, new_active)
        await db.execute("DELETE FROM workflow_attachments WHERE id = ?", (root_id,))
        await db.commit()
        return _delete_result([root_id], group_empty=False, root_id=new_root, active_sibling_id=new_active)


# Wire this module's batch persister into the database layer's add_message
# seam (dependency inversion -- the DB layer must not import up into
# backend.workflows). Registered at import; backend.workflows is always
# imported before any workflow attachment reaches add_message.
register_workflow_attachment_persister(insert_workflow_attachments)
