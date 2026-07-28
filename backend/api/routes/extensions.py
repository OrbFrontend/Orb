"""Community-extension routes: lifecycle, on-demand actions, and read surfaces.

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
  a URL, a method, or a handler. ``/views/{view}`` resolves against the declared
  view map, ``/resources/{resource}`` against a fixed host adapter table, and
  ``/assets/{path}`` against the compiled asset map -- never by joining a
  request path onto a directory.

Errors map by kind rather than by string matching: a malformed package is a
400, a package that is not installed is a 404, and anything that changed
between inspection and application is a 409 telling the user to inspect again.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, HTTPException, Request, Response, UploadFile

from ...database import (
    get_character_card,
    get_conversation,
    get_messages,
    get_settings,
    get_workflow_character_state,
    get_workflow_config,
    get_workflow_state,
    list_configured_secret_names,
    list_extension_secret_names,
    namespaced_state_owners,
)
from ...features.extensions import (
    adapters,
    catalog,
    lifecycle,
    resources,
    secrets,
    staging,
)
from ...features.extensions.compiler import (
    CARD_INPUT_PROPERTY,
    LIBRARY_CARD_ACTIONS_SLOT,
    derive_view_runtime_requirements,
)
from ...features.extensions.contracts import Capability, EffectEnvelope
from ...features.extensions.errors import FlowError, PackageError, PackageIncompatible
from ...features.extensions.interpreter import ModelLane
from ...features.extensions.limits import MAX_SOURCE_BYTES
from ...features.extensions.runtime import current_state
from ...inference import (
    AbortToken,
    agent_lane_from_settings,
    client_from_settings,
    writer_tool_incompatibility,
)
from ...workflows.contracts import LoadStatus, WorkflowSource
from ...workflows.enablement import effective_workflow_enabled
from ..schemas import (
    ExtensionActionRequest,
    ExtensionGitInspect,
    ExtensionInstallRequest,
    ExtensionPermissionsUpdate,
    ExtensionPurgeRequest,
    ExtensionSecretsUpdate,
    ExtensionStateWrite,
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
        raise HTTPException(
            status_code=413,
            detail=f"package archive exceeds the {MAX_SOURCE_BYTES} byte limit",
        )
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
    configured = await list_configured_secret_names()
    endpoint_diagnostic = _writer_endpoint_diagnostic(settings)
    return {
        "runtime_generation": state.generation,
        "extensions": [
            catalog.catalog_entry(
                entry,
                settings,
                configured_secrets=configured.get(entry.id, {}),
                endpoint_diagnostic=endpoint_diagnostic,
            )
            for entry in state.list()
        ],
        "orphaned_data": catalog.orphaned_data(owners, state),
    }


def _writer_endpoint_diagnostic(settings) -> str:
    """Why the configured Writer endpoint cannot host a Writer tool, or ``""``.

    Resolved per request rather than stored on the package: it is a fact about
    the *endpoint*, and changing endpoints must change the answer without
    touching a single installed extension.
    """
    return writer_tool_incompatibility(
        settings.get("endpoint_url", "") or "",
        settings.get("model_name", "") or "",
        settings.get("completion_mode", "chat") or "chat",
    )


@router.get("/{extension_id}")
async def api_get_extension(extension_id: str):
    entry = await _require_installed(extension_id)
    settings = await get_settings()
    declared = [
        {"name": row["name"], "updated_at": row["updated_at"]} for row in await list_extension_secret_names(extension_id)
    ]
    return {
        "runtime_generation": current_state().generation,
        **catalog.detail_entry(
            entry,
            settings,
            secret_names=declared,
            endpoint_diagnostic=_writer_endpoint_diagnostic(settings),
        ),
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


@router.post("/inspect")
async def api_inspect_extension_git(body: ExtensionGitInspect):
    """Fetch an HTTPS Git repository and return its consent screen.

    The bytes arrive through the installer's own bounded transport -- the flow
    HTTP client's URL policy, address validation, and pinning, applied per
    redirect hop -- and are then compiled by exactly the same code an uploaded
    archive goes through. Nothing is registered, granted, enabled, or published
    here, and the resolved commit rides in the staging token rather than in the
    install request, so the apply call cannot claim a different origin.
    """
    try:
        inspection = await lifecycle.inspect_git(body.url, body.ref, allow_local=body.allow_local)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
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


@router.post("/{extension_id}/inspect-update-git")
async def api_inspect_extension_update_git(extension_id: str, body: ExtensionGitInspect):
    """Stage an update by re-fetching the package's repository.

    The Git counterpart of ``inspect-update``: same compile, same diff, same
    token contract, and the same rejection of a manifest whose id changed. It
    exists as its own route rather than as a mode on ``inspect-update`` because
    that one takes a multipart upload, and a route whose body shape depends on a
    flag is a route two clients read differently.
    """
    try:
        inspection = await lifecycle.inspect_git(
            body.url,
            body.ref,
            allow_local=body.allow_local,
            extension_id=extension_id,
        )
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


@router.put("/{extension_id}/writer-tool-active")
async def api_set_writer_tool_active(extension_id: str, body: Annotated[dict, Body(...)]):
    """Select or clear this package as the one active Writer resolver.

    A host-owned control with no package input beyond the id in the path: an
    extension cannot select itself, and it cannot select or deselect another.
    The lifecycle lock serializes it against installs and permission changes,
    the write clears the prior selection in the same transaction, and the
    republish advances the generation because the selected schema is part of
    the Writer's tool blob.
    """
    active = body.get("active")
    if not isinstance(active, bool):
        raise HTTPException(status_code=422, detail="'active' must be a boolean")
    try:
        generation = await lifecycle.set_writer_tool_active(extension_id, active)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from None
    return _envelope(generation, {"active": active})


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


@router.put("/{extension_id}/secrets")
async def api_set_extension_secrets(extension_id: str, body: ExtensionSecretsUpdate):
    """Set or clear declared secrets. Write-only, by construction.

    The response carries names and timestamps because that is all the read path
    can produce -- there is no query in the codebase that returns a stored value
    to a route. Values reach exactly one consumer, the host HTTP client, and
    only as a substitution inside a request it is already building.

    Names are checked against the *active* manifest's declaration, so a secret
    left behind by an older revision can be cleared but not re-set under a
    contract the user was never shown.
    """
    entry = await _require_installed(extension_id)
    if entry.compiled is None:
        raise HTTPException(
            status_code=409,
            detail=entry.diagnostic or "this extension is not available",
        )
    declared = [s.name for s in entry.compiled.manifest.secrets]
    try:
        await secrets.write_secrets(extension_id, declared, body.values)
    except secrets.SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    stored = [{"name": row["name"], "updated_at": row["updated_at"]} for row in await list_extension_secret_names(extension_id)]
    return _envelope(current_state().generation, {"secrets": stored})


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
    assert entry.compiled is not None

    conversation_id = body.conversation_id
    conv = await get_conversation(conversation_id) if conversation_id else None
    if conversation_id and conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    character_id = await _resolve_action_card(entry, action, body, conv)
    card = await get_character_card(character_id) if character_id else None
    if character_id and card is None:
        raise HTTPException(status_code=404, detail="character card not found")
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
            compiled=entry.compiled,
            action_name=action,
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


async def _resolve_action_card(entry, action: str, body: ExtensionActionRequest, conv) -> str | None:
    """Decide which character card this invocation is bound to.

    Three paths, and they are three because the *source* of the identifier
    decides which grants it costs:

    * **A hook-shaped action.** No card identifier anywhere; ``ctx.character``
      is the conversation's card, exactly as in a hook.
    * **A ``library.card_actions`` click.** The host supplies the card because
      the user clicked a command on it. The extension must actually place a
      command in that slot -- proved here against the compiled manifest, not
      taken from the request -- so the relaxed check is tied to a placement the
      user consented to. ``library.cards.read`` is not required: the package
      never named the card.
    * **Package input declaring ``card_id``.** The package chooses the card, so
      it needs both ``context.read`` scoped to ``character`` and
      ``library.cards.read``.
      Enumeration is not the only way to reach a card, and an extension holding
      an id from its own state would otherwise read any card in the library
      under a grant whose consent text says "the character in this
      conversation".

    Resolving a card also rebinds the ``character`` state scope to it, which is
    intended -- a per-card record belongs on the card it describes -- and is why
    the second path is gated by two grants rather than one.
    """
    granted = entry.granted

    def require(capability: Capability, parameter: str | None = None) -> None:
        if (capability.value, parameter) not in granted:
            detail = capability.value if parameter is None else f"{capability.value} for {parameter}"
            raise HTTPException(
                status_code=403,
                detail=f"this extension has not been granted {detail!r}",
            )

    if body.slot is not None:
        if body.slot != LIBRARY_CARD_ACTIONS_SLOT:
            raise HTTPException(status_code=400, detail=f"unknown action slot {body.slot!r}")
        manifest = entry.compiled.manifest
        commands = {c.id: c for c in manifest.commands}
        placed = any(
            placement.slot == LIBRARY_CARD_ACTIONS_SLOT
            and placement.command is not None
            and commands.get(placement.command) is not None
            and commands[placement.command].action == action
            for placement in manifest.placements
        )
        if not placed:
            raise HTTPException(
                status_code=403,
                detail=f"extension {entry.id!r} places no {LIBRARY_CARD_ACTIONS_SLOT!r} command for action {action!r}",
            )
        if not body.card_id:
            raise HTTPException(status_code=422, detail="a library card action needs a card")
        # The placement is the authority for treating this identifier as
        # host-supplied. A static manifest declaration is not enough after the
        # user revokes the live slot grant.
        require(Capability.UI_CONTRIBUTE, LIBRARY_CARD_ACTIONS_SLOT)
        require(Capability.CONTEXT_READ, "character")
        return body.card_id

    if body.card_id is not None:
        # Only the slot path may supply one out of band. Accepting it here would
        # be the dual-grant check's bypass, spelled as a convenience.
        raise HTTPException(
            status_code=422,
            detail="'card_id' is only accepted with a UI slot that supplies it",
        )

    declared = body.input.get(CARD_INPUT_PROPERTY)
    if isinstance(declared, str) and declared:
        require(Capability.CONTEXT_READ, "character")
        require(Capability.LIBRARY_CARDS_READ)
        return declared

    return conv.get("character_card_id") if conv else None


def _resolve_action(entry, action: str, settings):
    """Resolve a publishable action binding, or raise the right refusal.

    Every gate the runtime state machine defines, in order: available, enabled,
    declared, and not blocked. They are distinct answers on purpose -- "this
    package needs a newer Orb", "you turned it off", "no such action", and "you
    revoked the permission it needs" are four different things for a user to do
    something about.
    """
    if entry.compiled is None or entry.load_status is not LoadStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=entry.diagnostic or "this extension is not available",
        )
    if not effective_workflow_enabled(entry.id, settings, WorkflowSource.COMMUNITY):
        raise HTTPException(status_code=409, detail="this extension is disabled")
    binding = entry.compiled.manifest.actions.get(action)
    if binding is None:
        raise HTTPException(
            status_code=404,
            detail=f"extension {entry.id!r} declares no action {action!r}",
        )
    if f"action {action!r}" in entry.blocked:
        raise HTTPException(
            status_code=403,
            detail=f"action {action!r} is not published: {entry.diagnostic}",
        )
    flow = entry.compiled.flows.get(binding.flow)
    if flow is None:
        raise HTTPException(
            status_code=409,
            detail="the action's flow is missing from the compiled revision",
        )
    return binding, flow


# ── views, resources, assets ────────────────────────────────────────────────


def _available(entry, settings):
    """The three runtime gates a content route shares, as distinct refusals."""
    if entry.compiled is None or entry.load_status is not LoadStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=entry.diagnostic or "this extension is not available",
        )
    if not effective_workflow_enabled(entry.id, settings, WorkflowSource.COMMUNITY):
        raise HTTPException(status_code=409, detail="this extension is disabled")
    return entry.compiled


@router.get("/{extension_id}/views/{view_id}")
async def api_get_extension_view(extension_id: str, view_id: str, conversation_id: str | None = None):
    """Serve one compiled view: its component tree plus its resolved data.

    The *compiled* tree, from the immutable revision record -- never a package
    file reopened at request time. The renderer receives host-validated JSON
    whose every string it will place with ``textContent``, and whose data came
    from host resource adapters that checked their own grants.

    A data source the current grants do not cover yields an ``error`` entry for
    that source rather than failing the whole view: a partially granted package
    should render the parts it may, with the rest saying why.
    """
    entry = await _require_installed(extension_id)
    settings = await get_settings()
    compiled = _available(entry, settings)
    binding = compiled.manifest.views.get(view_id)
    if binding is None:
        raise HTTPException(
            status_code=404,
            detail=f"extension {extension_id!r} declares no view {view_id!r}",
        )
    if f"view {view_id!r}" in entry.blocked:
        raise HTTPException(
            status_code=403,
            detail=f"view {view_id!r} is not published: {entry.diagnostic}",
        )
    view = compiled.views.get(binding.source)
    if view is None:
        raise HTTPException(status_code=409, detail="the view is missing from the compiled revision")

    data, errors = await _view_data(entry, view, conversation_id)
    runtime_requirements = derive_view_runtime_requirements(view)
    state = await _view_state(entry, view, conversation_id, runtime_requirements)
    needs_config = (
        Capability.STATE_READ.value,
        "config",
    ) in runtime_requirements or any(
        source.kind == "state" and source.scope == "config" for _, source in resources.data_sources_for_view(view)
    )
    config: dict[str, Any] = {}
    if needs_config and (Capability.STATE_READ.value, "config") in entry.granted:
        config = await get_workflow_config(extension_id) or {}
    return {
        "runtime_generation": current_state().generation,
        "id": view_id,
        "label": binding.label,
        "view": view.model_dump(mode="json"),
        "data": data,
        "errors": errors,
        "config": config,
        "state": state,
    }


async def _view_data(entry, view, conversation_id: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, source in resources.data_sources_for_view(view):
        if source.kind == "state":
            requirement = (Capability.STATE_READ.value, source.scope)
            if requirement not in entry.granted:
                errors[name] = f"this extension has not been granted {Capability.STATE_READ.value!r} for {source.scope!r}"
            continue  # granted state sources are projected below
        try:
            data[name] = await resources.resolve_resource(
                resources.ResourceRequest(
                    extension_id=entry.id,
                    resource=source.resource,
                    granted=entry.granted,
                    conversation_id=conversation_id,
                )
            )
        except resources.ResourceError as exc:
            errors[name] = str(exc)
    return data, errors


async def _view_state(entry, view, conversation_id: str | None, runtime_requirements) -> dict[str, Any]:
    """The extension's own slots, for the scopes this view declares.

    Only the declared ones: rendering a view that reads nothing must not
    project state the package never asked for, and a scope with no entity in
    this request is absent rather than empty.
    """
    extension_id = entry.id
    scopes = {
        parameter
        for capability, parameter in runtime_requirements
        if capability == Capability.STATE_READ.value and parameter not in (None, "config")
    }
    for _, source in resources.data_sources_for_view(view):
        if source.kind == "state" and source.scope != "config":
            scopes.add(source.scope)

    state: dict[str, Any] = {}
    for scope in sorted(scopes):
        if (Capability.STATE_READ.value, scope) not in entry.granted:
            continue
        if scope == "conversation" and conversation_id:
            state["conversation"] = await get_workflow_state(conversation_id, extension_id) or {}
        elif scope == "character" and conversation_id:
            conv = await get_conversation(conversation_id)
            card_id = conv.get("character_card_id") if conv else None
            if card_id:
                state["character"] = await get_workflow_character_state(card_id, extension_id) or {}
    return state


@router.put("/{extension_id}/state")
async def api_write_extension_state(extension_id: str, body: ExtensionStateWrite):
    """Save a bound form's draft into this extension's own namespaced slots.

    Host-generated, which is the point: a view's form controls bind to declared
    paths, and *Orb* turns the resulting draft into a write. A package cannot
    render its own save button that stores state, because storing state is not
    something a component does.

    Every ordinary check still applies -- the scope must be granted, the values
    must fit the per-scope slot cap, the write happens under the same locks and
    inside the same transaction any flow's ``state.set`` would use. The only
    thing that differs is who authored the intent.
    """
    entry = await _require_installed(extension_id)
    settings = await get_settings()
    _available(entry, settings)
    try:
        generation = await adapters.write_view_state(
            entry,
            body.updates,
            conversation_id=body.conversation_id,
        )
    except FlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return _envelope(generation, {"saved": sorted(body.updates)})


@router.get("/{extension_id}/resources/{resource:path}")
async def api_get_extension_resource(
    extension_id: str,
    resource: str,
    conversation_id: str | None = None,
    cursor: str | None = None,
):
    """Serve one named host resource under the extension's current grants.

    A named resource, never a query: the path segment indexes a fixed adapter
    table, and every field, bound, and page size belongs to that adapter. There
    is no parameter through which a package selects columns, filters rows, or
    widens a page.
    """
    entry = await _require_installed(extension_id)
    settings = await get_settings()
    _available(entry, settings)
    try:
        payload = await resources.resolve_resource(
            resources.ResourceRequest(
                extension_id=extension_id,
                resource=resource,
                granted=entry.granted,
                conversation_id=conversation_id,
                cursor=cursor,
            )
        )
    except resources.ResourceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    return {"runtime_generation": current_state().generation, **payload}


@router.get("/{extension_id}/assets/{path:path}")
async def api_get_extension_asset(extension_id: str, path: str):
    """Serve one validated package asset from its digest-owned content.

    The request path is looked up in the compiled asset map; it is never joined
    onto a directory. An asset that is not in that map does not exist as far as
    this route is concerned, whatever the filesystem holds -- which is what
    makes traversal, symlinks, and case collisions non-events here rather than
    things to defend against a second time.
    """
    entry = await _require_installed(extension_id)
    settings = await get_settings()
    compiled = _available(entry, settings)
    media_type = compiled.asset_types.get(path)
    if media_type is None:
        raise HTTPException(status_code=404, detail="no such asset")
    content = compiled.files.get(path)
    if content is None:
        raise HTTPException(status_code=404, detail="no such asset")
    return Response(
        content=content.canonical_bytes(),
        media_type=media_type,
        headers={
            # Sniffed and allowlisted at compile time; nosniff stops the browser
            # from second-guessing that decision, and the digest in the cache key
            # is why a long max-age is safe -- content is immutable per revision.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{path.rsplit("/", 1)[-1]}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


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
