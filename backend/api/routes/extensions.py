"""Community-extension lifecycle routes: inspect, install, update, rollback,
enable, permissions, uninstall, and purge.

Every mutating route follows the same shape, and the shape is the point:

* **Two phases.** An inspect call compiles the package, shows identity,
  compatibility, derived requirements, and the permission diff, and returns an
  opaque staging token. The apply call sends that token plus the exact
  normalized grants the user approved. No route accepts a digest, a file path,
  or a permission the server did not just derive and display.
* **One envelope out.** Lifecycle responses are the fixed
  :class:`~backend.features.extensions.contracts.EffectEnvelope`: data, a closed
  vocabulary of effects, and the runtime generation. The frontend owns the
  effect-to-refetch mapping, so a package string never becomes an event name, a
  DOM selector, or a fetch URL.
* **No package-selected routing.** There is no route whose path, method, or
  handler a package influences. ``/actions/{action}`` looks its argument up in
  the compiled manifest's declared action map; a package supplies an id, never
  a URL, a method, or a handler. The ``/views``, ``/resources``, and ``/assets``
  routes are still absent -- they serve package-derived *content*, and belong
  with the renderer and host resources that validate it.

Errors map by kind rather than by string matching: a malformed package is a
400, a package that is not installed is a 404, and anything that changed
between inspection and application is a 409 telling the user to inspect again.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile

from ...database import (
    get_character_card,
    get_conversation,
    get_messages,
    get_settings,
    list_extension_secret_names,
    namespaced_state_owners,
)
from ...features.extensions import adapters, catalog, lifecycle, staging
from ...features.extensions.contracts import EffectEnvelope
from ...features.extensions.errors import FlowError, PackageError, PackageIncompatible
from ...features.extensions.interpreter import ModelLane
from ...features.extensions.limits import MAX_SOURCE_BYTES
from ...features.extensions.runtime import current_state
from ...inference import AbortToken, agent_lane_from_settings, client_from_settings
from ...workflows.contracts import LoadStatus
from ...workflows.enablement import effective_workflow_enabled
from ..schemas import (
    ExtensionActionRequest,
    ExtensionInstallRequest,
    ExtensionPermissionsUpdate,
    ExtensionPurgeRequest,
    ExtensionUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extensions")


def _envelope(generation: int, data: Any = None) -> dict[str, Any]:
    """The fixed host effect envelope every lifecycle response returns.

    Always carries ``extension.catalog``: install, update, rollback, enable,
    permission change, uninstall, and purge all change what the manager and the
    workflow manifest should be showing. ``runtime_generation`` rides along so a
    response computed against a catalog the client has already replaced can be
    discarded rather than merged.
    """
    return EffectEnvelope.model_validate(
        {
            "data": data,
            "effects": [{"resource": "extension.catalog"}],
            "runtime_generation": generation,
        }
    ).model_dump(mode="json")


async def _read_upload(file: UploadFile) -> bytes:
    """Read an uploaded archive within the source budget.

    One byte past the limit, so "exactly at the limit" and "the client is still
    sending" are distinguishable without buffering the overflow.
    """
    data = await file.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"package archive exceeds the {MAX_SOURCE_BYTES} byte limit")
    if not data:
        raise HTTPException(status_code=400, detail="no package archive was uploaded")
    return data


async def _watch_action_disconnect(
    request: Request,
    abort_token: AbortToken,
    *,
    poll_seconds: float = 0.25,
) -> None:
    """Abort an on-demand action when its owning HTTP connection disappears."""
    try:
        while not abort_token.is_aborted:
            if await request.is_disconnected():
                abort_token.abort()
                return
            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        pass


def _package_error(exc: PackageError) -> HTTPException:
    """Map a package rejection to a status the manager can act on.

    An ``extension_api`` this build does not implement is a 409: the package is
    well-formed and the user's next step is to update Orb, which is a different
    action from fixing a malformed manifest. Both messages are already sanitized
    by the failure vocabulary -- they name a path, a limit, or a field, never
    package content.
    """
    status = 409 if isinstance(exc, PackageIncompatible) else 400
    return HTTPException(status_code=status, detail=str(exc))


def _lifecycle_error(exc: lifecycle.LifecycleError) -> HTTPException:
    if isinstance(exc, lifecycle.LifecycleConflict):
        return HTTPException(status_code=409, detail=str(exc))
    status = 404 if "is not installed" in str(exc) else 400
    return HTTPException(status_code=status, detail=str(exc))


async def _require_installed(extension_id: str):
    entry = current_state().get(extension_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"extension {extension_id!r} is not installed")
    return entry


# ── catalog ─────────────────────────────────────────────────────────────────


@router.get("")
async def api_list_extensions():
    """The installed catalog, its generation, and any orphaned namespaced data.

    ``orphaned_data`` is not decoration: normal uninstall preserves an
    extension's conversation/message/character/config state so a reinstall picks
    up where the user left off, and without listing it by id that data would be
    neither visible nor purgeable.
    """
    state = current_state()
    settings = await get_settings()
    owners = await namespaced_state_owners()
    return {
        "runtime_generation": state.generation,
        "extensions": [catalog.catalog_entry(entry, settings) for entry in state.list()],
        "orphaned_data": catalog.orphaned_data(owners, state),
    }


@router.get("/{extension_id}")
async def api_get_extension(extension_id: str):
    entry = await _require_installed(extension_id)
    settings = await get_settings()
    secrets = [
        {"name": row["name"], "updated_at": row["updated_at"]} for row in await list_extension_secret_names(extension_id)
    ]
    return {
        "runtime_generation": current_state().generation,
        **catalog.detail_entry(entry, settings, secret_names=secrets),
    }


# ── inspect / install ───────────────────────────────────────────────────────


@router.post("/inspect-file")
async def api_inspect_extension_file(file: Annotated[UploadFile, File(...)]):
    """Compile an uploaded ``.orbext`` and return its consent screen.

    Nothing is registered, granted, enabled, or published here. The compiled
    file set *is* written into the content store so the token names bytes that
    already exist -- an inspection the user abandons leaves an unreferenced
    directory that the next lifecycle mutation or restart collects.
    """
    data = await _read_upload(file)
    try:
        inspection = await lifecycle.inspect_archive(data)
    except PackageError as exc:
        raise _package_error(exc) from None
    return catalog.inspection_view(inspection)


@router.post("/install")
async def api_install_extension(body: ExtensionInstallRequest):
    try:
        generation = await lifecycle.apply_install(
            token=body.token,
            approved=body.permissions,
            enabled=body.enabled,
        )
    except staging.StagingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    except PackageError as exc:
        raise _package_error(exc) from None
    return _envelope(generation)


# ── update / rollback ───────────────────────────────────────────────────────


@router.post("/{extension_id}/inspect-update")
async def api_inspect_extension_update(extension_id: str, file: Annotated[UploadFile, File(...)]):
    data = await _read_upload(file)
    try:
        inspection = await lifecycle.inspect_update(extension_id, data)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    except PackageError as exc:
        raise _package_error(exc) from None
    return catalog.inspection_view(inspection)


@router.post("/{extension_id}/inspect-rollback")
async def api_inspect_extension_rollback(extension_id: str):
    """Stage a rollback to the retained previous revision.

    Rollback gets its own inspect route rather than an implicit mode on
    ``/rollback`` because it is an inspected operation with a real permission
    diff: restoring a revision must not restore a capability the user has since
    revoked, and that can only be shown before it is applied.
    """
    try:
        inspection = await lifecycle.inspect_rollback(extension_id)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    except PackageError as exc:
        raise _package_error(exc) from None
    return catalog.inspection_view(inspection)


@router.post("/{extension_id}/update")
async def api_update_extension(extension_id: str, body: ExtensionUpdateRequest):
    return await _apply_revision(extension_id, body)


@router.post("/{extension_id}/rollback")
async def api_rollback_extension(extension_id: str, body: ExtensionUpdateRequest):
    """Apply a staged rollback.

    Shares ``apply_update``'s path deliberately: update and rollback differ only
    in where the bytes came from, and both must compare-and-set the active
    digest under the lifecycle lock. Two apply paths would be two sets of
    ordering guarantees that could drift apart.
    """
    return await _apply_revision(extension_id, body)


async def _apply_revision(extension_id: str, body: ExtensionUpdateRequest) -> dict[str, Any]:
    try:
        generation = await lifecycle.apply_update(
            extension_id=extension_id,
            token=body.token,
            approved=body.permissions,
        )
    except staging.StagingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    except PackageError as exc:
        raise _package_error(exc) from None
    return _envelope(generation)


# ── enablement, permissions, removal ────────────────────────────────────────


@router.post("/{extension_id}/enabled")
async def api_set_extension_enabled(extension_id: str, body: Annotated[dict, Body(...)]):
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")
    try:
        generation = await lifecycle.set_enabled(extension_id, enabled)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    return _envelope(generation, {"enabled": enabled})


@router.put("/{extension_id}/permissions")
async def api_set_extension_permissions(extension_id: str, body: ExtensionPermissionsUpdate):
    """Replace the approved grant set with a subset of the manifest's request.

    Expansion beyond what the manifest asks for is rejected here rather than
    normalized away, so an attempt to widen a grant is an error the user sees.
    Reduction takes effect immediately: the republish inside this call may
    unpublish entry points that depended on what was just revoked.
    """
    try:
        generation = await lifecycle.set_permissions(extension_id, body.permissions)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    except PackageError as exc:
        raise _package_error(exc) from None
    return _envelope(generation)


@router.delete("/{extension_id}")
async def api_uninstall_extension(extension_id: str):
    """Remove registration and secrets. Namespaced data survives on purpose."""
    try:
        generation = await lifecycle.uninstall(extension_id)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    return _envelope(generation)


# ── on-demand actions ───────────────────────────────────────────────────────


@router.post("/{extension_id}/actions/{action}")
async def api_run_extension_action(
    extension_id: str,
    action: str,
    body: ExtensionActionRequest,
    request: Request,
):
    """Run one declared action and return the fixed host effect envelope.

    The route resolves an *exact* compiled binding: ``action`` indexes the
    manifest's declared actions and nothing else. There is no path a package
    influences, no handler it names, and no URL it supplies -- a view's button
    dispatches an action id, and this is where that id is looked up.

    A disconnect watcher aborts the shared model token and the interpreter
    checks that token at every step and again at the commit boundary.
    """
    runtime = current_state()
    entry = runtime.get(extension_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"extension {extension_id!r} is not installed")
    generation = runtime.generation
    settings = await get_settings()
    binding, flow = _resolve_action(entry, action, settings)

    conversation_id = body.conversation_id
    conv = await get_conversation(conversation_id) if conversation_id else None
    if conversation_id and conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    character_id = conv.get("character_card_id") if conv else None
    card = await get_character_card(character_id) if character_id else None
    history = await get_messages(conversation_id) if conversation_id else []
    abort_token = AbortToken()
    client = client_from_settings(settings, abort_token=abort_token)
    agent_client, agent_model = agent_lane_from_settings(
        settings,
        writer_client=client,
        abort_token=abort_token,
    )

    watcher = asyncio.create_task(_watch_action_disconnect(request, abort_token))

    try:
        envelope = await adapters.run_action(
            extension_id=extension_id,
            flow=flow,
            action_input=body.input,
            input_schema=binding.input_schema,
            output_schema=binding.output_schema,
            conversation_id=conversation_id,
            character_id=character_id,
            lanes={
                "writer": ModelLane(client=client, model=settings["model_name"]),
                "agent": ModelLane(client=agent_client, model=agent_model),
            },
            is_cancelled=lambda: abort_token.is_aborted,
            ctx_fields={
                "card": card,
                "history": history,
                "last_user_message": next((m["content"] for m in reversed(history) if m["role"] == "user"), ""),
            },
            generation=generation,
        )
    except FlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
    return envelope


def _resolve_action(entry, action: str, settings):
    """Resolve a publishable action binding, or raise the right refusal.

    Every gate the runtime state machine defines, in order: available, enabled,
    declared, and not blocked. They are distinct answers on purpose -- "this
    package needs a newer Orb", "you turned it off", "no such action", and "you
    revoked the permission it needs" are four different things for a user to do
    something about.
    """
    if entry.compiled is None or entry.load_status is not LoadStatus.AVAILABLE:
        raise HTTPException(status_code=409, detail=entry.diagnostic or "this extension is not available")
    if not effective_workflow_enabled(entry.id, settings):
        raise HTTPException(status_code=409, detail="this extension is disabled")
    binding = entry.compiled.manifest.actions.get(action)
    if binding is None:
        raise HTTPException(status_code=404, detail=f"extension {entry.id!r} declares no action {action!r}")
    if f"action {action!r}" in entry.blocked:
        raise HTTPException(status_code=403, detail=f"action {action!r} is not published: {entry.diagnostic}")
    flow = entry.compiled.flows.get(binding.flow)
    if flow is None:
        raise HTTPException(status_code=409, detail="the action's flow is missing from the compiled revision")
    return binding, flow


@router.post("/{extension_id}/purge-data")
async def api_purge_extension_data(extension_id: str, body: ExtensionPurgeRequest):
    """Two-phase destructive purge: preview first, then confirm with its token.

    Available for an uninstalled id too, which is the point -- "uninstall but
    preserve data" would otherwise make that data permanently unreachable. The
    confirmation is bound to the preview's token so a stale tab cannot trigger a
    purge broader than the counts the user actually read.
    """
    try:
        if body.token is None:
            staged, counts = await lifecycle.preview_purge(extension_id)
            return {
                "token": staged.token,
                "counts": counts,
                "total": sum(counts.values()),
                "runtime_generation": current_state().generation,
            }
        generation, removed = await lifecycle.apply_purge(extension_id, body.token)
    except staging.StagingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    return _envelope(generation, {"removed": removed, "total": sum(removed.values())})
