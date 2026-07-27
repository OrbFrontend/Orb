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
from typing import Any

from ...workflows.enablement import effective_workflow_enabled
from ...workflows.registry import list_workflows
from .compiler import CompiledPackage, grant_key
from .contracts import Capability
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
    Capability.CONVERSATION_TREE_READ.value: "Read the structure of every branch in this conversation.",
    Capability.CONVERSATION_TREE_PREVIEWS.value: "Also read text previews from branches you are not currently on.",
    Capability.CONVERSATION_BRANCH_ACTIVATE.value: "Switch which branch of the conversation is active.",
    Capability.PROMPT_CONTEXT_APPEND.value: "Add its own text to the prompt sent to the model each turn.",
    Capability.DRAFT_REPLACE.value: "Rewrite the reply before it reaches you.",
    Capability.MODEL_CALL.value: "Make its own model calls. This consumes tokens and may cost money.",
    Capability.STATE_READ.value: "Read data it has previously stored.",
    Capability.STATE_WRITE.value: "Store data of its own.",
    Capability.ARTIFACT_WRITE.value: "Attach generated files to messages.",
    Capability.NETWORK_REQUEST.value: "Send requests to an external server.",
    Capability.UI_CONTRIBUTE.value: "Add its own panels and menu entries to Orb.",
    Capability.FRAGMENT_TYPE_CONTRIBUTE.value: "Add new interactive-fragment types you can use in characters.",
}

_LOUD_CAPABILITIES = frozenset(
    {Capability.MODEL_CALL.value, Capability.NETWORK_REQUEST.value, Capability.CONVERSATION_TREE_PREVIEWS.value}
)
"""Grants the manager must not let scroll past as one more checkbox line.

Token cost, sending conversation data off the machine, and reading branches the
user is not on are the three the design calls out as needing to be conspicuous
and separately granted.
"""


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
        "enabled": effective_workflow_enabled(entry.id, settings),
        "load_status": entry.load_status.value,
        "diagnostic": entry.diagnostic,
        "blocked_entry_points": list(entry.blocked),
        "permissions": [permission_view(value, granted=grant_key(value) in granted_values) for value in requested],
        "can_rollback": bool(entry.row["previous_digest"]),
    }


def detail_entry(entry: InstalledExtension, settings: Any, *, secret_names: list[dict[str, Any]]) -> dict[str, Any]:
    """The catalog row plus everything the detail pane adds.

    Commands, views, placements, and contributions are listed as *data* --
    labels and slot names the host renderer will place. No component tree
    crosses this boundary in Phase 1: there is no renderer yet, and shipping the
    tree early would invite a frontend that reads it before the safe renderer
    exists.
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
            "commands": [{"id": c.id, "label": c.label, "icon": c.icon} for c in (manifest.commands if manifest else [])],
            "views": sorted((manifest.views if manifest else {}).keys()),
            "placements": [
                {"slot": p.slot, "view": p.view, "command": p.command} for p in (manifest.placements if manifest else [])
            ],
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
