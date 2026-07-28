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
    set_active_writer_resolver,
    set_extension_enabled_flag,
    set_extension_load_status,
    set_extension_permissions,
)
from ...workflows.contracts import LoadStatus
from ...workflows.registry import RESERVED_WORKFLOW_IDS, get_workflow
from . import content_store, execution, git_source, staging
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
SOURCE_GIT = "git"
"""The two source kinds. They differ only in where the bytes came from: both
compile through the same reader interface, produce the same digest for the same
content, and stage the same token -- so an update inspected from a Git URL diffs
cleanly against a revision that was installed from a local archive."""


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


async def inspect_git(
    url: str,
    ref: str = "",
    *,
    allow_local: bool = False,
    extension_id: str | None = None,
) -> Inspection:
    """Fetch, compile, and stage one revision from an HTTPS Git URL.

    An install when *extension_id* is ``None`` and an update otherwise, because
    the two differ only in what the resulting token is bound to -- the fetch,
    the compile, the digest, and the consent screen are identical. Routing them
    through one function is what keeps a Git update from acquiring its own
    slightly different validation.

    Nothing about the fetch is trusted afterwards: the recorded source, ref, and
    commit ride in the staging token's payload, so the apply request cannot name
    a different origin than the one that was inspected.
    """
    provenance = {"source_kind": SOURCE_GIT, "source_url": url, "requested_ref": ref}
    if extension_id is not None:
        row = await get_extension_package(extension_id)
        if row is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")

    compiled, commit_id = await asyncio.to_thread(_compile_git, url, ref, allow_local)
    provenance["commit_id"] = commit_id

    if extension_id is None:
        return _stage(compiled, operation="install", payload=provenance)
    if compiled.extension_id != extension_id:
        raise LifecycleError(
            f"this repository declares id {compiled.extension_id!r}; an update must keep the installed id {extension_id!r}"
        )
    row = await get_extension_package(extension_id)
    installed = current_state().get(extension_id)
    return _stage(
        compiled,
        operation="update",
        observed_active_digest=row["active_digest"] if row else "",
        active=installed.compiled if installed else None,
        installed=installed,
        payload=provenance,
    )


def _compile_git(url: str, ref: str, allow_local: bool) -> tuple[CompiledPackage, str]:
    fetched = git_source.fetch_repository(url, git_source.validate_ref(ref), allow_local=allow_local)
    compiled = compile_package(fetched.source)
    content_store.materialize(compiled.files)
    return compiled, fetched.commit_id


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
    payload: dict[str, Any] | None = None,
) -> Inspection:
    staged = staging.stage(
        operation=operation,  # type: ignore[arg-type]
        extension_id=compiled.extension_id,
        digest=compiled.digest,
        observed_active_digest=observed_active_digest,
        payload=payload,
    )
    return Inspection(
        staged=staged,
        compiled=compiled,
        operation=operation,
        active=active,
        installed=installed,
    )


def _provenance(staged: StagedOperation) -> dict[str, Any]:
    """Where a staged revision came from, as the token recorded it.

    Read from the token rather than from the apply request. The frontend echoes
    back a token and a grant list; letting it also name the source URL would let
    a stored package claim an origin it was never fetched from, which is the one
    piece of provenance the manager displays and the update flow reuses.
    """
    return {
        "source_kind": str(staged.payload.get("source_kind") or SOURCE_ARCHIVE),
        "source_url": str(staged.payload.get("source_url") or ""),
        "requested_ref": str(staged.payload.get("requested_ref") or ""),
        "commit_id": (str(staged.payload["commit_id"]) if staged.payload.get("commit_id") else None),
    }


# ── application ─────────────────────────────────────────────────────────────


async def apply_install(
    *,
    token: str,
    approved: list[dict[str, Any]],
    enabled: bool,
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
            content_digest=compiled.digest,
            manifest=compiled.manifest_json(),
            extension_api=compiled.manifest.extension_api,
            version=compiled.manifest.version,
            contract_fingerprint=compiled.contract_fingerprint,
            approved_permissions=grants,
            enabled=enabled,
            load_status=status,
            load_error=error,
            declared_secret_names=tuple(secret.name for secret in compiled.manifest.secrets),
            **_provenance(staged),
        )
        generation = await _republish(compiled.extension_id)
        if enabled:
            await execution.allow_new_invocations(compiled.extension_id)
        else:
            await execution.block_new_invocations(compiled.extension_id)
        return generation


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
        # A rollback carries no provenance of its own -- it re-activates content
        # already in the store -- so the package's recorded source is left alone
        # rather than overwritten with the empty archive default.
        provenance = _provenance(staged) if staged.payload.get("source_kind") else {"commit_id": None}
        applied = await activate_extension_revision(
            extension_id=extension_id,
            content_digest=compiled.digest,
            manifest=compiled.manifest_json(),
            extension_api=compiled.manifest.extension_api,
            version=compiled.manifest.version,
            contract_fingerprint=compiled.contract_fingerprint,
            approved_permissions=grants,
            load_status=status,
            load_error=error,
            expected_active_digest=staged.observed_active_digest,
            declared_secret_names=tuple(secret.name for secret in compiled.manifest.secrets),
            **provenance,
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
        row = await get_extension_package(extension_id)
        if row is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        was_enabled = bool(row["enabled"])
        if not enabled:
            await execution.block_new_invocations(extension_id)
        try:
            await set_extension_enabled_flag(extension_id, enabled)
        except Exception:
            if not enabled and was_enabled:
                await execution.allow_new_invocations(extension_id)
            raise
        generation = await _republish(extension_id)
        if enabled:
            await execution.allow_new_invocations(extension_id)
        return generation


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


async def set_writer_tool_active(extension_id: str, active: bool) -> int:
    """Select or clear the one active Writer resolver, then republish.

    Selecting is refused unless the contribution is currently *usable* --
    installed, available, enabled, granted, and unblocked. Storing a preference
    for something that cannot run would put a control in the manager whose only
    observable effect is a diagnostic, and the user would have no way to tell
    "selected but ineligible" from "the feature is broken". Deselecting is
    always permitted, including for a package that has since become
    ineligible, or a revoked grant would strand the selection.

    The write clears the previous selection in the same transaction, so there
    is no instant at which two packages both claim the resolver, and the
    republish advances the generation -- the selected schema is part of the
    Writer's tool blob, so a stale catalog response after this must be
    discardable exactly as it is after an enable.
    """
    async with extension_lifecycle_lock():
        installed = current_state().get(extension_id)
        if installed is None or await get_extension_package(extension_id) is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        if active:
            reason = writer_tool_ineligibility(installed)
            if reason is not None:
                raise LifecycleError(reason)
        if not await set_active_writer_resolver(extension_id, active):
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        return await _republish(extension_id)


def writer_tool_ineligibility(installed) -> str | None:
    """Why this package cannot be the active resolver right now, or ``None``.

    One predicate, read by the activation route and by the catalog projection,
    so the manager never offers a control the route would refuse.
    """
    compiled = installed.compiled
    if compiled is None or compiled.manifest.writer_tool is None:
        return "this extension does not contribute a Writer tool"
    if installed.load_status is not LoadStatus.AVAILABLE:
        return "this extension's active revision cannot run on this Orb build"
    if not bool(installed.row["enabled"]):
        return "enable the extension before selecting it as the Writer resolver"
    if "writer tool" in installed.blocked:
        return "grant the permissions its Writer tool needs before selecting it"
    return None


async def uninstall(extension_id: str) -> int:
    """Remove registration and secrets; leave namespaced data inert.

    Deliberately not a purge. Conversation, message, character, and config state
    stay under the extension id so a reinstall picks up where the user left off,
    and ``settings.workflow_enabled`` keeps an explicit "off". ``purge_data`` is
    the separate destructive operation, and the manager lists the preserved data
    so it stays reachable.
    """
    async with extension_lifecycle_lock():
        row = await get_extension_package(extension_id)
        if row is None:
            raise LifecycleError(f"extension {extension_id!r} is not installed")
        await execution.block_new_invocations(extension_id)
        try:
            await delete_extension_package(extension_id)
        except Exception:
            if bool(row["enabled"]):
                await execution.allow_new_invocations(extension_id)
            raise
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
        # The gate is independent of the registry snapshot: an old turn may
        # still hold a callable that was captured before the disabled overlay.
        await execution.block_new_invocations(extension_id)
        if await get_extension_package(extension_id) is not None:
            await set_extension_enabled_flag(extension_id, False)
            await _republish(extension_id)
        await execution.drain_invocations(extension_id)
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
        logger.info(
            "extension content store: collected %d unreferenced revision(s)",
            len(removed),
        )
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
        return (
            "incompatible",
            f"this Orb build does not provide: {', '.join(compiled.unavailable)}",
        )
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
