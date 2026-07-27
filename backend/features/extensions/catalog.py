"""Host-owned projections: what the extension manager renders.

Consent controls, status lines, and diagnostics are Orb components populated
from these server-side projections, never from a package view. That is why the
projection is a function here rather than a shape the frontend assembles: the
frontend sends back an opaque staging token plus the exact normalized grants it
was given, and it never has to reconstruct a permission from a label it
displayed.

Every string a package authored (name, description, permission ``reason``,
diagnostics) leaves here as plain text in a JSON value. Nothing in this module
produces markup, a URL the browser will fetch, an event name, or a callback
name -- the renderer's job is to put these into ``textContent``, and this
module's job is to never hand it anything that would be wasted there.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ...workflows.contracts import WorkflowSource
from ...workflows.enablement import effective_workflow_enabled
from ...workflows.registry import list_workflows
from . import telemetry
from .compiler import CompiledPackage, grant_key
from .contracts import CONFIG_VIEW_ID, Capability
from .lifecycle import Inspection
from .runtime import InstalledExtension, RuntimeState

# Human-readable consent copy, keyed by capability. The vocabulary is closed, so
# this table is total by construction -- a new capability that forgot its line
# would show a bare identifier in a consent dialog, which is the one place a
# user must not have to guess.
CAPABILITY_COPY: dict[str, str] = {
    Capability.CONTEXT_INPUT_READ.value: "Read the message you send each turn.",
    Capability.CONTEXT_DRAFT_READ.value: "Read the reply Orb wrote, before you see it.",
    Capability.CONTEXT_HISTORY_READ.value: "Read a recent window of this conversation's messages.",
    Capability.CONTEXT_CHARACTER_READ.value: "Read the active character's text fields.",
    Capability.CONTEXT_PERSONA_READ.value: "Read your persona's name and description — your own self-description.",
    Capability.CONVERSATION_TREE_READ.value: "Read the structure of every branch in this conversation.",
    Capability.CONVERSATION_TREE_PREVIEWS.value: "Also read text previews from branches you are not currently on.",
    Capability.CONVERSATION_BRANCH_ACTIVATE.value: "Switch which branch of the conversation is active.",
    Capability.LIBRARY_CARDS_READ.value: "List your characters and read each one it is run against.",
    Capability.LOREBOOK_READ.value: "Read this character's lorebook, including entries no message has triggered.",
    Capability.DIRECTION_NOTES_READ.value: "Read the direction notes on this branch.",
    Capability.PROMPT_CONTEXT_APPEND.value: "Add its own text to the prompt sent to the model each turn.",
    Capability.DRAFT_REPLACE.value: "Rewrite the reply before it reaches you.",
    Capability.CARD_TAGS_WRITE.value: "Change the tags on the character a command is run against.",
    Capability.MODEL_CALL.value: "Make its own model calls. This consumes tokens and may cost money.",
    Capability.STATE_READ.value: "Read data it has previously stored.",
    Capability.STATE_WRITE.value: "Store data of its own.",
    Capability.ARTIFACT_WRITE.value: "Attach generated files to messages.",
    Capability.NETWORK_REQUEST.value: "Send requests to an external server.",
    Capability.UI_CONTRIBUTE.value: "Add its own panels and menu entries to Orb.",
    Capability.FRAGMENT_TYPE_CONTRIBUTE.value: "Add new interactive-fragment types you can use in characters.",
}

_LOUD_CAPABILITIES = frozenset(
    {
        Capability.MODEL_CALL.value,
        Capability.NETWORK_REQUEST.value,
        Capability.CONVERSATION_TREE_PREVIEWS.value,
        # Enumeration is what makes a package library-wide -- the write it
        # enables is single-card. Reach is granted once, visibly, by the
        # capability that enumerates.
        Capability.LIBRARY_CARDS_READ.value,
        # The user's own self-description, and the strongest trigger for the
        # combination banner below.
        Capability.CONTEXT_PERSONA_READ.value,
        # Untriggered lorebook entries are content the model may never have
        # seen, which makes this a stronger read than the history window.
        Capability.LOREBOOK_READ.value,
    }
)
"""Grants the manager must not let scroll past as one more checkbox line."""

_DATA_READING_CAPABILITIES = frozenset(
    {
        Capability.CONTEXT_INPUT_READ.value,
        Capability.CONTEXT_DRAFT_READ.value,
        Capability.CONTEXT_HISTORY_READ.value,
        Capability.CONTEXT_CHARACTER_READ.value,
        Capability.CONTEXT_PERSONA_READ.value,
        Capability.CONVERSATION_TREE_READ.value,
        Capability.CONVERSATION_TREE_PREVIEWS.value,
        Capability.LIBRARY_CARDS_READ.value,
        Capability.LOREBOOK_READ.value,
        Capability.DIRECTION_NOTES_READ.value,
        Capability.STATE_READ.value,
    }
)
"""Grants that let a package see conversation, character, lorebook, persona, or
history data -- one half of the combination the banner exists for."""

COMBINATION_BANNER = (
    "This extension can send data it reads — your conversations, characters, "
    "lorebook, or persona — to the external servers it is allowed to contact."
)
"""Host-authored, never package-influenced.

Reading is not the new risk; reading *combined with* network access is. Without
this line the user has to compose two innocuous-looking grants in their head,
which is exactly the inference a consent screen should not require.
"""


def combination_warning(entries: Sequence[Mapping[str, Any]]) -> str | None:
    """The banner text when a grant set both reads data and reaches the network.

    Derived server-side from the *normalized* grant set, so no package string
    influences whether it appears or what it says.
    """
    capabilities = {str(entry.get("capability", "")) for entry in entries}
    if Capability.NETWORK_REQUEST.value not in capabilities:
        return None
    return COMBINATION_BANNER if capabilities & _DATA_READING_CAPABILITIES else None


def permission_view(entry: dict[str, Any], *, granted: bool) -> dict[str, Any]:
    """One consent row: the normalized value, its copy, and its emphasis.

    ``value`` is the permission exactly as the manifest declared it and exactly
    as the install request must echo it back. The rest is presentation, and the
    server never reads it back.
    """
    capability = str(entry.get("capability", ""))
    parameters = {key: value for key, value in entry.items() if key != "capability"}
    return {
        "value": entry,
        "capability": capability,
        "parameters": parameters,
        "description": CAPABILITY_COPY.get(capability, "Use a capability this Orb build does not describe."),
        "emphasis": "high" if capability in _LOUD_CAPABILITIES or _is_weak_origin(parameters) else "normal",
        "granted": granted,
    }


def _is_weak_origin(parameters: dict[str, Any]) -> bool:
    """Plain HTTP and loopback/private hosts warrant the stronger warning.

    A hostname is not resolved here -- resolution happens in the network client
    at request time, where it is revalidated on every redirect. This is the
    consent-time signal from the origin string alone, which is what the user is
    reading.
    """
    origin = parameters.get("origin")
    if not isinstance(origin, str):
        return False
    if origin.startswith("http://"):
        return True
    host = origin.split("://", 1)[-1].split(":", 1)[0].strip("[]").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith(("10.", "192.168.", "169.254.", "172.16."))


def _published_placements(entry: InstalledExtension) -> list[Any]:
    """Placements the current grant set can actually publish.

    A manifest placement is only a request. Runtime permission revocation can
    block its slot, its target action, or its target view independently, and
    the catalog is the publication boundary the frontend consumes. Returning a
    blocked placement would leave a dead command visible; for
    ``library.card_actions`` it would be worse, because that placement is the
    proof that lets a host-supplied card id avoid the enumeration grant.
    """
    compiled = entry.compiled
    if compiled is None:
        return []
    manifest = compiled.manifest
    commands = {command.id: command for command in manifest.commands}
    published: list[Any] = []
    for placement in manifest.placements:
        if (Capability.UI_CONTRIBUTE.value, placement.slot) not in entry.granted:
            continue
        if placement.view is not None and f"view {placement.view!r}" in entry.blocked:
            continue
        if placement.command is not None:
            command = commands.get(placement.command)
            if command is None:
                continue
            if command.action is not None and f"action {command.action!r}" in entry.blocked:
                continue
            if command.opens is not None and f"view {command.opens!r}" in entry.blocked:
                continue
        published.append(placement)
    return published


def catalog_entry(entry: InstalledExtension, settings: Any) -> dict[str, Any]:
    """One installed package as the manager list renders it."""
    manifest = entry.compiled.manifest if entry.compiled else None
    granted_values = _granted_values(entry)
    requested = entry.compiled.requested_permissions() if entry.compiled else []
    return {
        "id": entry.id,
        "name": entry.display_name,
        "version": entry.version,
        "author": manifest.author if manifest else None,
        "description": manifest.description if manifest else None,
        "homepage": manifest.homepage if manifest else None,
        "source_kind": entry.row["source_kind"],
        "source_url": entry.row["source_url"],
        "requested_ref": entry.row["requested_ref"],
        "active_digest": entry.digest,
        "previous_digest": entry.row["previous_digest"],
        "installed_at": entry.row["installed_at"],
        "updated_at": entry.row["updated_at"],
        "enabled": effective_workflow_enabled(entry.id, settings, WorkflowSource.COMMUNITY),
        "load_status": entry.load_status.value,
        "diagnostic": entry.diagnostic,
        "blocked_entry_points": list(entry.blocked),
        "permissions": [permission_view(value, granted=grant_key(value) in granted_values) for value in requested],
        "can_rollback": bool(entry.row["previous_digest"]),
        # Derived from what is *approved*, not from what is requested: revoking
        # the network grant must clear the banner on the detail panel, or the
        # warning stops describing the package the user actually has.
        "combination_warning": combination_warning([v for v in requested if grant_key(v) in granted_values]),
        "telemetry": telemetry.summary(entry.id),
        "placements": [{"slot": p.slot, "view": p.view, "command": p.command} for p in _published_placements(entry)],
        "commands": [
            {
                "id": c.id,
                "label": c.label,
                "icon": c.icon,
                "opens": c.opens,
                "action": c.action,
                "when": c.when,
            }
            for c in (manifest.commands if manifest else [])
        ],
        "config_view": (
            CONFIG_VIEW_ID
            if manifest and CONFIG_VIEW_ID in manifest.views and f"view {CONFIG_VIEW_ID!r}" not in entry.blocked
            else None
        ),
    }


def detail_entry(entry: InstalledExtension, settings: Any, *, secret_names: list[dict[str, Any]]) -> dict[str, Any]:
    """The catalog row plus everything the detail pane adds.

    Commands, views, placements, and contributions are listed as *data* --
    labels and slot names the host renderer will place. Component trees cross
    only the dedicated view route, where live grants and runtime requirements
    are checked before the host renderer receives them.
    """
    view = catalog_entry(entry, settings)
    manifest = entry.compiled.manifest if entry.compiled else None
    view.update(
        {
            "secrets": secret_names,
            "requires": {
                "operations": sorted(manifest.requires.operations) if manifest else [],
                "components": sorted(manifest.requires.components) if manifest else [],
            },
            "views": sorted((manifest.views if manifest else {}).keys()),
            "fragment_types": [
                {"id": f.id, "label": f.label, "namespaced_id": f"{entry.id}:{f.id}"}
                for f in (manifest.contributions.fragment_types if manifest else [])
            ],
            "produces_artifacts": bool(manifest.produces_artifacts) if manifest else False,
            "hooks": _hook_summary(manifest),
        }
    )
    return view


def inspection_view(inspection: Inspection) -> dict[str, Any]:
    """The pre-consent screen: identity, compatibility, limits, and the diff.

    ``permission_diff`` distinguishes added from unchanged from removed. Only
    additions need fresh consent -- a reduction is the user getting *less*
    exposure, and making them re-approve it would train them to click through
    the screen that also shows additions.
    """
    compiled = inspection.compiled
    manifest = compiled.manifest
    previous = inspection.installed
    already = _granted_values(previous) if previous else set()
    requested = compiled.requested_permissions()

    added = [permission_view(value, granted=False) for value in requested if grant_key(value) not in already]
    unchanged = [permission_view(value, granted=True) for value in requested if grant_key(value) in already]
    removed = _removed_permissions(inspection.active, requested)

    return {
        "token": inspection.staged.token,
        "operation": inspection.operation,
        "id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "author": manifest.author,
        "description": manifest.description,
        "homepage": manifest.homepage,
        "content_digest": compiled.digest,
        "extension_api": manifest.extension_api,
        "compatible": not compiled.unavailable,
        "unsupported": list(compiled.unavailable),
        "installed_version": inspection.active.manifest.version if inspection.active else None,
        "active_digest": inspection.staged.observed_active_digest or None,
        "permissions": [permission_view(value, granted=False) for value in requested],
        "permission_diff": {"added": added, "unchanged": unchanged, "removed": removed},
        "combination_warning": combination_warning(requested),
        "origins": manifest.origins(),
        "secrets": [{"name": s.name, "label": s.label, "description": s.description} for s in manifest.secrets],
        "requires": {
            "operations": sorted(manifest.requires.operations),
            "components": sorted(manifest.requires.components),
        },
        "files": sorted(compiled.files),
        "total_bytes": sum(len(content.canonical_bytes()) for content in compiled.files.values()),
        "commands": [{"id": c.id, "label": c.label, "icon": c.icon} for c in manifest.commands],
        "placements": [{"slot": p.slot, "view": p.view, "command": p.command} for p in manifest.placements],
        "fragment_types": [{"id": f.id, "label": f.label} for f in manifest.contributions.fragment_types],
        "produces_artifacts": manifest.produces_artifacts,
        "hooks": _hook_summary(manifest),
    }


def orphaned_data(owners: dict[str, int], state: RuntimeState) -> list[dict[str, Any]]:
    """Namespaced data whose owning extension is not installed.

    Uninstall preserves state on purpose, so without this listing "uninstall but
    keep my data" would make that data permanently unreachable -- invisible in
    the manager and impossible to purge. Built-in workflow ids are subtracted
    here rather than in the query, because ``database/`` consulting the live
    registry would be an upward import.
    """
    known = {w.id for w in list_workflows()} | set(state.packages)
    return [{"id": owner, "records": count} for owner, count in sorted(owners.items()) if owner not in known]


def _hook_summary(manifest: Any) -> dict[str, Any]:
    if manifest is None:
        return {}
    hooks: dict[str, Any] = {}
    if manifest.hooks.pre_pipeline:
        hooks["pre_pipeline"] = {"stage": None}
    if manifest.hooks.post_pipeline:
        hooks["post_pipeline"] = {"stage": manifest.hooks.post_pipeline.stage}
    return hooks


def _removed_permissions(active: CompiledPackage | None, requested: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if active is None:
        return []
    wanted = {grant_key(value) for value in requested}
    return [permission_view(value, granted=True) for value in active.requested_permissions() if grant_key(value) not in wanted]


def _granted_values(entry: InstalledExtension | None) -> set[tuple]:
    if entry is None:
        return set()
    try:
        decoded = json.loads(entry.row["approved_permissions"] or "[]")
    except (TypeError, ValueError):
        return set()
    return {grant_key(value) for value in decoded if isinstance(value, dict)}
