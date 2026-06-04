"""Workflow-attachment byte cache.

Single chokepoint for byte writes into the `workflow_attachments`
table -- size accounting and eviction live behind one entry point.

Eviction marker:
    The literal sentinel string EVICTED_MARKER replaces an evicted row's
    `data_b64` column. Other columns (seed, generation_metadata,
    filename, mime_type, etc.) stay intact so a subsequent rehydrate
    can recover the bytes from stored parameters.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..database.connection import get_db
from ..database.queries.messages import register_workflow_attachment_persister
from ..database.queries.workflow_attachments import (
    EVICTED_MARKER,
    _encode_metadata_field,
    insert_workflow_attachment_row,
)

from .registry import get_workflow

logger = logging.getLogger(__name__)

# EVICTED_MARKER is re-exported from the database boundary above (where it
# describes the persisted ``data_b64`` shape) so the eviction layer here and
# the route layer in main.py can keep importing it from this module.


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

    The atomic insert/rehydrate paths in this module precompute the full
    eviction set via ``sorted(candidates, key=_lru3_key)`` and peel a
    prefix rather than calling this helper, so the single-victim path is
    a separate pinned interface for the unit tests that exercise the LRU-3
    ordering in isolation.
    """
    if not candidates:
        return None
    return min(candidates, key=_lru3_key)["id"]


async def _get_budget_bytes_on(db) -> int:
    rows = list(await db.execute_fetchall("SELECT attachment_cache_budget_bytes FROM settings WHERE id = 1"))
    return int(rows[0]["attachment_cache_budget_bytes"]) if rows else 0


async def get_budget_bytes() -> int:
    async with get_db() as db:
        return await _get_budget_bytes_on(db)


async def _byte_bearing_candidates_on(db) -> list[dict]:
    # Single source of truth: bytes live in data_b64. The byte count is
    # computed from the column's length rather than stored separately so
    # there is no way for a separate column to drift from the bytes it
    # claims to describe. Base64 math: every 4 input chars encode 3 bytes
    # minus 1 for each trailing '=' padding char.
    rows = list(
        await db.execute_fetchall(
            "SELECT id, "
            "((length(data_b64) / 4) * 3) - (length(data_b64) - length(rtrim(data_b64, '='))) AS size, "
            "recent_accesses "
            "FROM workflow_attachments WHERE data_b64 != ? ORDER BY id ASC",
            (EVICTED_MARKER,),
        )
    )
    out: list[dict] = []
    for r in rows:
        ra_raw = r["recent_accesses"]
        ra: list[int] | None = None
        if ra_raw:
            try:
                parsed = json.loads(ra_raw)
                if isinstance(parsed, list) and all(isinstance(v, int) for v in parsed):
                    ra = parsed
            except (TypeError, ValueError):
                ra = None
        out.append({"id": r["id"], "size": int(r["size"] or 0), "recent_accesses": ra})
    return out


async def rehydrate_attachment(attachment_id: int, data: bytes, *, consumption_metadata: dict | None = None) -> None:
    """Restore bytes into an evicted workflow_attachments row in place.

    Atomic: one connection holds ``BEGIN IMMEDIATE`` across the
    precondition recheck, eviction prefix, and final UPDATE. Concurrent
    rehydrates of the same id serialize on the SQLite write lock.

    ``consumption_metadata`` overwrites the row's stored value in the same
    transaction as the byte write when a dict is supplied; ``None`` leaves
    the stored value intact. The bytes are the runtime truth and their
    ``consumption_metadata`` is the derived store co-written here, so a
    regeneration that yields byte-different output can replace metadata that
    no longer describes the restored bytes.

    Budget accounting uses ``len(data)`` directly -- the caller already
    holds the bytes to be written, so the precise new byte count is
    known. The row's stored bytes are the single source of truth for
    size; nothing is read from a separate size column.

    Preconditions enforced inside the lock:
      - The row exists. Otherwise raises ``LookupError``.
      - The row holds the EVICTED_MARKER sentinel. Otherwise raises
        ``ValueError`` -- a parallel rehydrate already restored the
        bytes; this call's work is throwaway.
      - ``len(data)`` is not strictly greater than the cache budget.
        Otherwise raises ``ValueError`` before evicting anything;
        restoring an oversized row would evict every other byte-bearing
        row and still not fit.
    """
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
                f"workflow_attachment {attachment_id!r} size {new_size} exceeds "
                f"cache budget {budget}; refusing to rehydrate"
            )

        candidates = await _byte_bearing_candidates_on(db)
        candidates = [c for c in candidates if c["id"] != attachment_id]
        occupied = sum(c["size"] for c in candidates)
        needed = (occupied + new_size) - budget
        if needed > 0:
            for victim in sorted(candidates, key=_lru3_key):
                if needed <= 0:
                    break
                await _evict_on(db, victim["id"])
                needed -= victim["size"]

        # Reset recent_accesses alongside the bytes write so the post-rehydrate
        # row matches the birth-as-access invariant: a single freshly-assigned
        # counter, equivalent to a brand-new insert. Without the reset, the
        # pre-eviction counters survive (eviction only touches data_b64) and
        # _lru3_key reads a stale oldest entry, making the just-rehydrated row
        # the next eviction leader -- defeating the user's "give me this back"
        # intent on the very next insert.
        if cm_json is not None:
            await db.execute(
                "UPDATE workflow_attachments "
                "SET data_b64 = ?, recent_accesses = NULL, consumption_metadata = ? WHERE id = ?",
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
        cur_raw = cur_rows[0]["recent_accesses"]
        cur: list[int] = []
        if cur_raw:
            try:
                parsed = json.loads(cur_raw)
                if isinstance(parsed, list):
                    cur = [v for v in parsed if isinstance(v, int)]
            except (TypeError, ValueError):
                cur = []
        new_list = ([assigned] + cur)[:3]
        await db.execute(
            "UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?",
            (json.dumps(new_list), att_id),
        )


async def record_access(attachment_ids: list[int]) -> None:
    """Bump the global access counter and prepend the assigned counter
    values onto each target row's ``recent_accesses``.

    Ids are assigned counter values in input-list order: the first id
    in the list gets the smallest fresh counter, the last gets the
    largest. Callers that want intra-call ordering encode it as input
    order; the HTTP route forwards the request body's ``ids`` list as-is
    so the frontend controls the assignment. Birth and rehydrate paths
    inside this module call ``_record_access_inner`` directly to share
    the open transaction with their surrounding writes.

    Missing ids (e.g. the row was deleted between client capture and
    the POST landing) are silently skipped. The counter is still
    consumed -- we trade a few wasted counter values for not having to
    roll back the transaction.
    """
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
        return os.path.getsize(path)
    return 0


def _is_rehydratable(attachment: dict) -> bool:
    """Gate for marker-insertion: only atts carrying both seed (non-empty
    string) and generation_metadata (dict) can be safely stored as evicted
    markers, because rehydrate needs both to reproduce the bytes later.
    Atts lacking either field would become permanently unrecoverable rows
    if marker-stored, so they are refused (single-row) or dropped from the
    batch (batch). Empty-dict metadata is allowed -- some workflows
    regenerate deterministically from seed alone."""
    seed = attachment.get("seed")
    md = attachment.get("generation_metadata")
    return isinstance(seed, str) and bool(seed) and isinstance(md, dict)


def validate_workflow_attachment_shape(attachment: Any) -> tuple[bool, str | None]:
    """Pre-flight SHAPE + emptiness check for a workflow-attachment dict.

    Mirrors ``insert_workflow_attachment_row``'s raise gates in
    ``backend/database/queries/workflow_attachments.py``, with one
    extension: ``os.path.getsize == 0`` closes the gap the row helper
    would otherwise catch only after reading the file.

    Reason strings are intentionally shorter rephrasings of the row
    helper's ValueError text -- chosen for frontend chip brevity. The
    unit-test suite pins them verbatim, so changes break the contract.

    Returns ``(True, None)`` on pass; ``(False, reason)`` on first
    failed gate. Residual race: a path that vanishes between this check
    and the row helper's ``open()`` still trips OSError under
    ``BEGIN IMMEDIATE`` and rolls back the batch; outer try/except in
    the route catches as HTTP 500. Sub-millisecond window; accepted
    residual.
    """
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
        try:
            if not os.path.isfile(path):
                return False, "path does not exist or is not a regular file"
            if os.path.getsize(path) == 0:
                return False, "path points at an empty file"
        except OSError:
            return False, "path is not stat-able"
    return True, None


async def _check_flat_parent_on(db, parent_id: int, expected_message_id: int) -> None:
    """Verify parent_id names an existing root attached to expected_message_id.

    Two invariants enforced together because both protect the renderer's
    group resolution (which joins by message_id and walks parent links
    within that scope):

      - The parent is a root (parent_attachment_id IS NULL). A 2+ deep
        tree would put the new sibling outside the group the user sees.
      - The parent belongs to the same message as the new insert. A
        cross-message parent would orphan the new sibling under a root
        the renderer never iterates from the new message's side, and the
        eventual ``_set_active_sibling_on`` would clobber the foreign
        message's active pointer.

    Shared between single-row and batch insert paths so both enforce the
    same invariants.

    Raises ``LookupError`` if the row is missing, ``ValueError`` if the
    row is itself a sibling or belongs to a different message.
    """
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
    """Cache-aware workflow-attachment insertion.

    Returns ``(new_id, None)`` on successful insert (byte-bearing or
    marker), or ``(None, rejected)`` on refusal. ``rejected`` is a shallow
    copy of the input attachment with a ``reason`` key naming the rejection
    class -- one of ``WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON`` (producing
    workflow not declared ``produces_artifacts=True``) or
    ``OVERSIZE_NO_METADATA_REASON`` (oversize and missing the
    ``seed`` + ``generation_metadata`` fields rehydrate needs). Exactly
    one of the two slots is non-None. Mirrors the batch helper's
    ``(new_ids, rejected_atts)`` shape.

    Oversize policy: an attachment whose size exceeds the cache budget
    is marker-inserted (bytes never stored; row carries the EVICTED_MARKER
    sentinel) iff it carries both ``seed`` and ``generation_metadata`` --
    the fields rehydrate needs to reproduce the bytes later. Without
    those fields the att is permanently unrecoverable as a marker, so
    the function rejects instead of inserting. Marker-inserted rows still
    get their parent's ``active_sibling_id`` updated -- the freshly
    inserted variant is the displayed default, and the frontend renders
    a "click Rehydrate" placeholder for marker rows.

    `mark_active` (default True) updates the root row's
    ``active_sibling_id`` to point at the freshly inserted sibling. Set
    False to insert without moving the active pointer (e.g. when
    restoring an older sibling).

    Atomic: one connection holds ``BEGIN IMMEDIATE`` across the parent
    flatness check, eviction prefix, row insert, birth-access record,
    and (optional) active-sibling write.

    Raises:
      ValueError  -- ``parent_attachment_id`` names a row that is itself
                     a sibling (its ``parent_attachment_id`` is not NULL),
                     OR a row belonging to a different message, OR the
                     attachment dict is malformed (empty workflow_id,
                     not exactly one of data/path, wrong types, empty
                     bytes after path-to-bytes normalization).
      LookupError -- ``parent_attachment_id`` or ``message_id`` does
                     not exist.
      OSError     -- path-shape attachment whose path cannot be stat'd.
    """
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

        if not insert_as_marker:
            candidates = await _byte_bearing_candidates_on(db)
            occupied = sum(c["size"] for c in candidates)
            needed = (occupied + new_size) - budget
            if needed > 0:
                for victim in sorted(candidates, key=_lru3_key):
                    if needed <= 0:
                        break
                    await _evict_on(db, victim["id"])
                    needed -= victim["size"]

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
    """Batch-aware atomic insert of workflow attachments.

    Returns ``(new_ids, rejected_atts)``. ``new_ids`` lists the row ids
    actually inserted, in input order with rejected indices skipped.
    ``rejected_atts`` lists the input dicts dropped for rehydratability
    reasons (oversize AND lacking ``seed`` + ``generation_metadata``);
    callers can surface those to the user as a warning.

    Plan-then-execute: with all batch sizes known up front, compute the
    minimal eviction set that lets the post-batch cache fit in budget,
    then execute the plan in a single transaction. The whole batch + the
    caller's surrounding writes commit together when ``db`` is provided.

    Plan, in order:

    1. Marker/reject new attachments biggest-first until the new
       byte-bearing total fits in budget. Markering a big new att can
       spare many small existing rows from eviction, so this runs
       first. Rehydratable atts become markers (insert_as_evicted);
       non-rehydratable ones drop into ``rejected_atts``. Marker rows
       persist with EVICTED_MARKER in ``data_b64`` and recover via
       ``rehydrate_attachment``.
    2. Evict existing byte-bearing rows oldest-first per LRU-3 for any
       residual shortfall (``occupied + new_byte_total > budget``).
       When pre-existing occupancy already exceeds budget (e.g. after
       a runtime settings shrink), step 2 converges toward budget by
       evicting on the next write.

    Birth-as-access fires once for every successfully-inserted row
    (rejected ones never reach the DB at all).

    ``mark_active`` (default True) updates the parent root's
    ``active_sibling_id`` for each inserted new sibling; rejected atts
    are skipped. Last write wins for inserted siblings of the same root.

    When ``db`` is provided, the caller owns the transaction lifecycle.
    When None, this helper opens its own connection, holds
    ``BEGIN IMMEDIATE`` across the read/plan/execute sequence, and
    commits.

    Raises propagate from the underlying row helper (LookupError for a
    missing message or parent, ValueError for non-rehydratability is
    not raised here -- those atts go in rejected_atts; ValueError for
    malformed attachment shape such as flat-parent violation or empty
    data DOES still raise; OSError for unreadable paths during stat).
    The caller's transaction rolls back on any such raise.

    ``rejected_atts`` entries are shallow copies of the input dicts, each
    tagged with a ``reason`` key naming the rejection class:
    ``WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON`` (Step 0 policy partition --
    producing workflow not declared ``produces_artifacts=True``) or
    ``OVERSIZE_NO_METADATA_REASON`` (Step A oversize partition -- attachment
    exceeds budget AND lacks ``seed`` + ``generation_metadata``). Route
    and SSE projection layers read the ``reason`` key verbatim.
    """
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

        # Step A: marker/reject new atts biggest-first until the new
        # byte-bearing total fits in budget. Markering one big new att
        # can spare many small existing rows from eviction, so this is
        # the first lever to pull. Tie-break by input index for
        # determinism. Policy-rejected indices are excluded.
        plan_mark_new: set[int] = set()
        rejected_idx_oversize: set[int] = set()
        new_byte_total = new_total
        if new_byte_total > budget:
            indexed = sorted(effective_indices, key=lambda i: (-sizes[i], i))
            for i in indexed:
                if new_byte_total <= budget:
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
        plan_evict_existing: list[int] = []
        need = (occupied + new_byte_total) - budget
        if need > 0:
            for victim in sorted(existing, key=_lru3_key):
                if need <= 0:
                    break
                plan_evict_existing.append(victim["id"])
                need -= victim["size"]

        for eid in plan_evict_existing:
            await _evict_on(conn, eid)

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
    """Persist the active-sibling choice for a workflow attachment group.

    Validation runs inside the same ``BEGIN IMMEDIATE`` transaction as
    the UPDATE so a concurrent row-delete cannot land between check and
    write. Raises:

    - ``LookupError`` if the root row does not exist, the root is not
      on ``expected_message_id`` (when given), the sibling row does not
      exist, or the sibling sits on a different message than the root.
    - ``ValueError`` if the root is not a root
      (``parent_attachment_id IS NOT NULL``), or the sibling exists on
      the right message but does not belong to the root's group
      (``sibling.id != root.id`` AND
      ``sibling.parent_attachment_id != root_id``).

    ``sibling_id=None`` clears ``active_sibling_id`` and bypasses the
    sibling checks (only the root checks run). ``expected_message_id``
    is optional so internal/test callers that have already verified row
    provenance can pass ``None`` and skip the message-on-root check.

    Internal insert paths bypass this validation entirely by calling
    ``_set_active_sibling_on`` directly inside their own
    ``BEGIN IMMEDIATE`` -- they construct the sibling row in the same
    transaction, so group membership is trivially true.
    """
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


async def delete_workflow_attachments(
    target_id: int,
    *,
    scope: str,
    expected_message_id: int | None = None,
) -> dict:
    """Delete a workflow-attachment variant or a whole group.

    Validation and writes share one ``BEGIN IMMEDIATE`` (matching
    ``set_active_sibling``) so a concurrent mutation cannot land between
    check and write. The group root is derived from the target inside the
    transaction.

    scope "group": delete the root and every sibling.
    scope "variant": delete ``target_id``. When ``target_id`` is the group
      root and siblings survive, the oldest survivor is promoted to root
      (the others are re-parented onto it) before the old root is deleted,
      so the survivors remain one group rather than scattering into
      singletons. Only a root row's annotation reaches the LLM prefix
      (see ``prompt_builder``), so the promoted root inherits the deleted
      root's annotation, keeping the message's model-visible text stable.

    Performs no eviction or access-counter bookkeeping: deleted rows
    release their own byte budget and their access records vanish with
    them.

    Returns ``{"deleted_ids": list[int], "group_empty": bool,
    "root_id": int, "active_sibling_id": int | None}``. ``root_id`` is the
    post-op root (the deleted root id when ``group_empty``).
    ``active_sibling_id`` is meaningful only when ``group_empty`` is False,
    where it may still be None (deleting the active variant reverts the
    group to newest-wins via the ``ON DELETE SET NULL`` foreign key).

    Raises ``LookupError`` (target missing, or not on
    ``expected_message_id``) and ``ValueError`` (scope not
    "variant"/"group").
    """
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
            root_rows = list(
                await db.execute_fetchall(
                    "SELECT active_sibling_id FROM workflow_attachments WHERE id = ?",
                    (root_id,),
                )
            )
            root_active = root_rows[0]["active_sibling_id"] if root_rows else None

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
            return {
                "deleted_ids": del_ids,
                "group_empty": True,
                "root_id": root_id,
                "active_sibling_id": None,
            }

        if target_id != root_id:
            await db.execute("DELETE FROM workflow_attachments WHERE id = ?", (target_id,))
            after = list(
                await db.execute_fetchall(
                    "SELECT active_sibling_id FROM workflow_attachments WHERE id = ?",
                    (root_id,),
                )
            )
            await db.commit()
            return {
                "deleted_ids": [target_id],
                "group_empty": False,
                "root_id": root_id,
                "active_sibling_id": after[0]["active_sibling_id"] if after else None,
            }

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
            return {
                "deleted_ids": [root_id],
                "group_empty": True,
                "root_id": root_id,
                "active_sibling_id": None,
            }
        new_root = survivors[0]
        new_active = root_active if (root_active is not None and root_active != root_id and root_active in survivors) else None
        await db.execute(
            "UPDATE workflow_attachments SET parent_attachment_id = ? WHERE parent_attachment_id = ? AND id != ?",
            (new_root, root_id, new_root),
        )
        # Only a root row's annotation reaches the LLM prefix (prompt_builder), so
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
        return {
            "deleted_ids": [root_id],
            "group_empty": False,
            "root_id": new_root,
            "active_sibling_id": new_active,
        }


# Wire this module's batch persister into the database layer's add_message
# seam (dependency inversion -- the DB layer must not import up into
# backend.workflows). Registered at import; backend.workflows is always
# imported before any workflow attachment reaches add_message.
register_workflow_attachment_persister(insert_workflow_attachments)
