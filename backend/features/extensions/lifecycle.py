"""The package lifecycle: inspect, install, update, rollback, enable, uninstall,
purge, and the startup reconciliation that rebuilds all of it after a restart.

Every source follows one path, and this module is where that path is a single
function rather than a convention:

    bounded source read -> canonical package tree -> strict parse ->
    reference-graph validation -> compile -> derive requirements ->
    consent diff -> durable content -> database transaction ->
    atomic runtime-snapshot publish

Inspection stops before "durable content" only in the sense that it does not
*commit*: it does materialize the compiled file set into the content store, so
the token it hands back names bytes that already exist and a crash between the
two requests leaves collectable garbage instead of a dangling promise. What
inspection never does is register, grant, enable, or publish.

Ordering inside the critical section is fixed and load-bearing:

1. Content is made durable **first** (fsync + atomic rename, in
   :mod:`.content_store`).
2. Package/revision/grant metadata commits **second**, in one transaction.
3. The runtime overlay publishes **third**.

A crash between 1 and 2 leaves an unreferenced content directory that startup
collects. A crash between 2 and 3 leaves committed metadata that startup
deterministically reloads. Neither leaves a published record pointing at a
half-written directory -- the state no reader could detect.

The lifecycle lock serializes these mutations against each other. It does not
serialize turns: readers hold their own captured snapshot, and publishing swaps
one immutable reference.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from ...core.locks import extension_lifecycle_lock
from ...database import (
    activate_extension_revision,
    delete_extension_package,
    extension_data_preview,
    get_extension_package,
    install_extension_package,
    list_extension_packages,
    purge_extension_data,
    referenced_digests,
    set_extension_enabled_flag,
    set_extension_load_status,
    set_extension_permissions,
)
from ...workflows.registry import RESERVED_WORKFLOW_IDS, get_workflow
from . import content_store, staging
from .compiler import CompiledPackage, compile_package, grant_key
from .contracts import EXTENSION_ID_PATTERN
from .errors import PackageError, PackageValidationError
from .runtime import (
    InstalledExtension,
    current_state,
    load_installed,
    pinned_digests,
    publish,
)
from .sources import ArchiveSource, StoredSource
from .staging import StagedOperation

logger = logging.getLogger(__name__)

SOURCE_ARCHIVE = "archive"
"""The only source kind Phase 1 implements. Git installation arrives with the
bounded Dulwich fetch in Phase 4 and writes ``'git'`` into the same column."""


class LifecycleError(Exception):
    """A lifecycle operation cannot proceed as asked.

    Distinct from :class:`~.errors.PackageError`, which means "this package is
    not valid". This means "this package may be fine, but not here, not now, or
    not under that id" -- an id collision, a stale digest, a missing package.
    The API layer maps the two to different statuses.
    """


class LifecycleConflict(LifecycleError):
    """The world changed between inspection and application (HTTP 409)."""


@dataclass(frozen=True, slots=True)
class Inspection:
    """What the user is shown before consenting, plus the token that binds it."""

    staged: StagedOperation
    compiled: CompiledPackage
    operation: str
    active: CompiledPackage | None = None
    installed: InstalledExtension | None = None


# ── inspection ──────────────────────────────────────────────────────────────


async def inspect_archive(data: bytes, *, operation: str = "install") -> Inspection:
    """Read, validate, compile, stage, and materialize one ``.orbext`` archive.

    The parse/hash/write work runs in a worker thread: it is CPU- and IO-bound
    with no awaits of its own, and a 25 MiB package would otherwise stall the
    event loop for every other request including an in-flight turn.
    """
    compiled = await asyncio.to_thread(_compile_archive, data)
    return _stage(compiled, operation=operation)


def _compile_archive(data: bytes) -> CompiledPackage:
    with ArchiveSource(data) as source:
        compiled = compile_package(source)
    content_store.materialize(compiled.files)
    return compiled


async def inspect_update(extension_id: str, data: bytes) -> Inspection:
    """Stage an update, diffing against the currently active revision.

    Rejects a manifest whose id changed: an "update" that installs a different
    package would inherit the outgoing package's grants, state, and namespaced
    data under a name the user never approved for it.
    """
    row = await get_extension_package(extension_id)
    if row is None:
        raise LifecycleError(f"extension {extension_id!r} is not installed")
    compiled = await asyncio.to_thread(_compile_archive, data)
    if compiled.extension_id != extension_id:
        raise LifecycleError(
            f"this package declares id {compiled.extension_id!r}; an update must keep the installed id {extension_id!r}"
        )
    installed = current_state().get(extension_id)
    return _stage(
        compiled,
        operation="update",
        observed_active_digest=row["active_digest"],
        active=installed.compiled if installed else None,
        installed=installed,
    )


async def inspect_rollback(extension_id: str) -> Inspection:
    """Stage a rollback to the retained previous revision.

    Rollback is an inspected operation like any other. It shows the prior
    manifest and its permission diff, and the resulting install is granted only
    what the user approves now -- restoring a revision cannot restore a
    capability they have since revoked.
    """
    row = await get_extension_package(extension_id)
    if row is None:
        raise LifecycleError(f"extension {extension_id!r} is not installed")
    previous = row["previous_digest"]
    if not previous:
        raise LifecycleError(f"extension {extension_id!r} has no previous revision to roll back to")
    if not content_store.exists(previous):
        raise LifecycleError("the previous revision's content is no longer on this machine")
    compiled = await asyncio.to_thread(_compile_stored, previous)
    installed = current_state().get(extension_id)
    return _stage(
        compiled,
        operation="rollback",
        observed_active_digest=row["active_digest"],
        active=installed.compiled if installed else None,
        installed=installed,
    )


def _compile_stored(digest: str) -> CompiledPackage:
    return compile_package(StoredSource(content_store.content_path(digest)))


def _stage(
    compiled: CompiledPackage,
    *,
    operation: str,
    observed_active_digest: str = "",
    active: CompiledPackage | None = None,
    installed: InstalledExtension | None = None,
) -> Inspection:
    staged = staging.stage(
        operation=operation,  # type: ignore[arg-type]
        extension_id=compiled.extension_id,
        digest=compiled.digest,
        observed_active_digest=observed_active_digest,
    )
    return Inspection(staged=staged, compiled=compiled, operation=operation, active=active, installed=installed)


# ── application ─────────────────────────────────────────────────────────────


async def apply_install(
    *,
    token: str,
    approved: list[dict[str, Any]],
    enabled: bool,
    source_kind: str = SOURCE_ARCHIVE,
    source_url: str = "",
    requested_ref: str = "",
) -> int:
    """Redeem an install token and activate the revision. Returns the generation.

    An explicit install may enable the package as part of the same confirmed
    operation, or land disabled. It never *re-enables* a package the user
    previously switched off unless this confirmed operation explicitly asks for
    a value. The package row and authoritative settings switch commit together.
    """
    staged = staging.redeem(token, operation="install")
    async with extension_lifecycle_lock():
        await _assert_id_available(staged.extension_id)
        compiled = await _recompile(staged)
        grants = _normalized_grants(approved, compiled)
        status, error = _initial_status(compiled)
        await install_extension_package(
            extension_id=compiled.extension_id,
            source_kind=source_kind,
            source_url=source_url,
            requested_ref=requested_ref,
            content_digest=compiled.digest,
            manifest=compiled.manifest_json(),
            extension_api=compiled.manifest.extension_api,
            version=compiled.manifest.version,
            commit_id=None,
            contract_fingerprint=compiled.contract_fingerprint,
            approved_permissions=grants,
            enabled=enabled,
            load_status=status,
            load_error=error,
        )
        return await _republish(compiled.extension_id)


async def apply_update(*, extension_id: str, token: str, approved: list[dict[str, Any]]) -> int:
    """Redeem an update or rollback token and swap the active revision.

    The compare-and-set against the digest observed at inspection is what makes
    a rejected update leave the prior files, metadata, grants, and runtime
    snapshot completely untouched: nothing is written before it succeeds.
    """
    staged = _redeem_revision_token(token, extension_id)
    async with extension_lifecycle_lock():
        row = await get_extension_package(extension_id)
        if row is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        if row["active_digest"] != staged.observed_active_digest:
            raise LifecycleConflict("the active revision changed since this package was inspected; inspect it again")
        compiled = await _recompile(staged)
        grants = _normalized_grants(approved, compiled)
        status, error = _initial_status(compiled)
        applied = await activate_extension_revision(
            extension_id=extension_id,
            content_digest=compiled.digest,
            manifest=compiled.manifest_json(),
            extension_api=compiled.manifest.extension_api,
            version=compiled.manifest.version,
            commit_id=None,
            contract_fingerprint=compiled.contract_fingerprint,
            approved_permissions=grants,
            load_status=status,
            load_error=error,
            expected_active_digest=staged.observed_active_digest,
        )
        if not applied:
            raise LifecycleConflict("the active revision changed while applying; inspect the package again")
        return await _republish(extension_id)


async def set_enabled(extension_id: str, enabled: bool) -> int:
    """Flip the per-extension switch and republish.

    Enablement does not recompile anything, but it changes what the frontend
    should be showing, so it still advances the generation through a republish
    -- a catalog response that arrives after a disable must be discardable.
    """
    async with extension_lifecycle_lock():
        if await get_extension_package(extension_id) is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        await set_extension_enabled_flag(extension_id, enabled)
        return await _republish(extension_id)


async def set_permissions(extension_id: str, approved: list[dict[str, Any]]) -> int:
    """Replace the approved grant set with a normalized subset of the request.

    Only a subset of the *active manifest's* request is accepted, so a package
    cannot widen its own grants through any route it controls. Reduction takes
    effect on the next publish, which happens before this returns and may
    unpublish entry points that depended on what was revoked.
    """
    async with extension_lifecycle_lock():
        installed = current_state().get(extension_id)
        if installed is None or await get_extension_package(extension_id) is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        if installed.compiled is None:
            raise LifecycleError("this revision could not be compiled, so its permissions cannot be edited")
        grants = _normalized_grants(approved, installed.compiled)
        await set_extension_permissions(extension_id, grants)
        return await _republish(extension_id)


async def uninstall(extension_id: str) -> int:
    """Remove registration and secrets; leave namespaced data inert.

    Deliberately not a purge. Conversation, message, character, and config state
    stay under the extension id so a reinstall picks up where the user left off,
    and ``settings.workflow_enabled`` keeps an explicit "off". ``purge_data`` is
    the separate destructive operation, and the manager lists the preserved data
    so it stays reachable.
    """
    async with extension_lifecycle_lock():
        if await get_extension_package(extension_id) is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        await delete_extension_package(extension_id)
        return await _republish(extension_id)


async def preview_purge(extension_id: str) -> tuple[StagedOperation, dict[str, int]]:
    """Count exactly what a purge would remove and bind a token to that count.

    The destructive confirmation carries this token, so a stale UI cannot
    trigger a purge broader than the preview the user actually read.
    """
    _assert_purge_target(extension_id)
    counts, selection_fingerprint = await extension_data_preview(extension_id)
    staged = staging.stage(
        operation="purge",
        extension_id=extension_id,
        digest="",
        payload={"counts": counts, "selection_fingerprint": selection_fingerprint},
    )
    return staged, counts


async def apply_purge(extension_id: str, token: str) -> tuple[int, dict[str, int]]:
    """Disable, unpublish, then delete every namespaced trace of an extension.

    Order matters: the package is switched off and republished *before* the
    delete, so no new invocation can start against the data being removed and
    commit it back after the response. It stays disabled afterward -- a purge
    that silently re-enabled the package would invite it to repopulate what was
    just deleted.
    """
    _assert_purge_target(extension_id)
    staged = staging.redeem(token, operation="purge", extension_id=extension_id)
    expected_fingerprint = staged.payload.get("selection_fingerprint")
    if not isinstance(expected_fingerprint, str):
        raise LifecycleConflict("the purge preview is no longer valid; preview the purge again")
    async with extension_lifecycle_lock():
        if await get_extension_package(extension_id) is not None:
            await set_extension_enabled_flag(extension_id, False)
            await _republish(extension_id)
        removed = await purge_extension_data(extension_id, expected_fingerprint=expected_fingerprint)
        if removed is None:
            raise LifecycleConflict("stored extension data changed since preview; preview the purge again")
        return await _republish(extension_id), removed


# ── startup ─────────────────────────────────────────────────────────────────


async def reconcile() -> int:
    """Compile every installed revision and publish one snapshot. Boot path.

    Runs after ``init_db()``/migrations and before the app serves requests. A
    package whose content is missing, whose manifest no longer validates, or
    which needs a newer Orb is marked and skipped; the built-ins and every other
    package still load. Nothing here fetches from the network -- restoring a
    database onto a machine without the content files must not silently reach
    out to a recorded commit.
    """
    staging.clear()
    rows = await list_extension_packages()
    installed = await asyncio.to_thread(load_installed, rows)
    generation = publish(installed)
    for entry in installed:
        status, error = entry.load_status.value, entry.diagnostic
        if entry.row["load_status"] != status or entry.row["load_error"] != error:
            await set_extension_load_status(entry.id, status, error)
        if entry.load_status.value != "available":
            logger.warning("extension %r is %s: %s", entry.id, status, error or "no detail")
    await collect_content_garbage()
    return generation


async def collect_content_garbage() -> list[str]:
    """Drop stored revisions nothing references any more."""
    keep = await referenced_digests()
    keep |= pinned_digests()
    keep |= staging.pinned_digests()
    removed = content_store.collect_garbage(keep)
    if removed:
        logger.info("extension content store: collected %d unreferenced revision(s)", len(removed))
    return removed


# ── internals ───────────────────────────────────────────────────────────────


def _redeem_revision_token(token: str, extension_id: str) -> StagedOperation:
    """Accept either an update or a rollback token for the same apply path.

    They differ only in where the bytes came from; both re-compile from the
    content store and both compare-and-set the active digest, so one apply path
    is one set of ordering guarantees instead of two that could drift. Both
    operations go into a single ``redeem`` call because redemption burns the
    token even when it rejects -- trying one and falling back to the other would
    consume the token on the first attempt.
    """
    return staging.redeem(token, operation=("update", "rollback"), extension_id=extension_id)


async def _recompile(staged: StagedOperation) -> CompiledPackage:
    """Re-derive the revision from durable content and revalidate its digest.

    The token is not trusted to describe the package; it only says which digest
    to look at. Recompiling here is what makes "revalidate the staged digest and
    token before commit" a check rather than a comment -- if the content store
    no longer holds those exact bytes, the install fails instead of activating
    something else.
    """
    if not content_store.exists(staged.digest):
        raise LifecycleConflict("the inspected package content is no longer staged; inspect it again")
    try:
        compiled = await asyncio.to_thread(_compile_stored, staged.digest)
    except PackageError as exc:
        raise LifecycleError(f"the staged package no longer compiles: {exc}") from None
    if compiled.digest != staged.digest or compiled.extension_id != staged.extension_id:
        raise LifecycleConflict("the staged package content changed since inspection; inspect it again")
    return compiled


async def _assert_id_available(extension_id: str) -> None:
    """Reject ids that collide with a built-in, a reserved slot, or a package."""
    if extension_id in RESERVED_WORKFLOW_IDS:
        raise LifecycleError(f"extension id {extension_id!r} is reserved by Orb")
    if get_workflow(extension_id) is not None and current_state().get(extension_id) is None:
        raise LifecycleError(f"extension id {extension_id!r} collides with a built-in workflow")
    if await get_extension_package(extension_id) is not None:
        # Reinstall over an existing package is an update, and updates carry a
        # permission diff against what is already granted. Routing it here would
        # skip that diff.
        raise LifecycleError(f"extension {extension_id!r} is already installed; use the update flow")


def _assert_purge_target(extension_id: str) -> None:
    """Only community namespaces may be purged, installed or orphaned."""
    if re.fullmatch(EXTENSION_ID_PATTERN, extension_id) is None:
        raise LifecycleError(f"extension id {extension_id!r} is invalid")
    if extension_id in RESERVED_WORKFLOW_IDS:
        raise LifecycleError(f"extension id {extension_id!r} is reserved by Orb")
    if get_workflow(extension_id) is not None and current_state().get(extension_id) is None:
        raise LifecycleError(f"extension id {extension_id!r} belongs to a built-in workflow")


def _normalized_grants(approved: list[dict[str, Any]], compiled: CompiledPackage) -> list[dict[str, Any]]:
    """Keep only approved entries that the active manifest actually requests.

    Comparison is on the normalized permission *value*, never a display string,
    so a reworded consent line cannot widen a grant and an unrecognized entry is
    dropped rather than stored. Order follows the manifest so the stored set is
    a deterministic function of (manifest, approvals).
    """
    requested = compiled.requested_permissions()
    wanted = {grant_key(entry) for entry in approved if isinstance(entry, dict)}
    unknown = wanted - {grant_key(entry) for entry in requested}
    if unknown:
        raise PackageValidationError("approved permissions include entries this package does not request")
    return [entry for entry in requested if grant_key(entry) in wanted]


def _initial_status(compiled: CompiledPackage) -> tuple[str, str]:
    """The load status a freshly activated revision starts at.

    A revision needing features this build lacks still installs -- installed and
    available are separate axes, and the user's next step ("update Orb") differs
    from what a rejection would suggest ("this package is broken").
    """
    if compiled.unavailable:
        return "incompatible", f"this Orb build does not provide: {', '.join(compiled.unavailable)}"
    return "available", ""


async def _republish(extension_id: str) -> int:
    """Recompile everything installed, publish, and invalidate stale consent.

    Whole-catalog rather than incremental: every lifecycle mutation republishes
    the complete overlay, so there is no window in which half of it is new. Any
    consent screen open for the mutated package described a state that no longer
    exists, so its tokens go too.
    """
    staging.discard(extension_id)
    rows = await list_extension_packages()
    generation = publish(await asyncio.to_thread(load_installed, rows))
    await collect_content_garbage()
    return generation
