#!/usr/bin/env python3
"""Enforce frontend layers, acyclic imports and workflow boundaries.

Check that markup reaches code only through registered data-wf-action names (no
inline handlers, no window globals, no unregistered names), cross-module private
names, frozen workflow_api exports, and that every other export has an importer.
Plugins may import only their own modules and workflow_api; computed dynamic
imports are rejected. Exit non-zero on violations via lint.sh.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"

# ── 1. Layer manifest ────────────────────────────────────────────────────────
# Lower number = lower layer. A file may import its own layer or lower.
LAYERS = {
    # L0 core leaves — import nothing.
    "api.js": 0,
    "document_saves.js": 0,
    "sse.js": 0,
    "validate.js": 0,
    "scroll_follow.js": 0,
    "text_segmentation.js": 0,
    "icons.js": 0,
    "drag_reorder.js": 0,
    "dom_reconcile.js": 0,
    # The Character Library's search + tag predicate. A leaf on purpose: the filter is the one piece of the browser worth
    # testing directly, and library_browser.js drags in the whole L5 chat chain.
    "library_filter.js": 0,
    # The desktop-width card rescue: measures a rendered bubble and re-widens a
    # collapsed card block. Pure DOM, imports nothing, so it stays a leaf.
    "message_fit.js": 0,
    "generation_status.js": 0,
    # A generation request's stop handshake and the rule for which saved reply
    # is its own. A leaf so the settlement rules can be tested without a DOM.
    "stream_settle.js": 0,
    "expression_segments.js": 0,
    # The card-CSS policy: a tokenizer, an allowlist and the per-message scoper. A leaf so it can be tested without a DOM, which
    # is the whole point of it being a string pass rather than a trip through the CSSOM.
    "message_css.js": 0,
    # The avatar crop box as pure geometry (hit test, move, aspect-locked
    # resize), split from modal.js so it can be tested without a canvas.
    "crop_geometry.js": 0,
    # L1 state + shared pure helpers.
    "state.js": 1,
    "operations.js": 1,
    # The decision-fragment vocabulary: the cached /api/decisions/config read, the outcome-space rule, and the wording for each
    # machine reason the stage reports. Imports only api.js, so it sits with the other shared helpers and all four decision
    # surfaces can read it.
    "decisions.js": 1,
    "model_catalog.js": 1,
    "workflow_registry.js": 1,
    "utils.js": 1,
    # The delegated data-wf-action registry. Low so every feature module can
    # register the handlers for the markup it renders; workflow_api.js re-exports it.
    "actions.js": 1,
    "notify.js": 1,
    # A workflow render the button that started it can stop: its job id, the stop request, and that button's Stop state.
    "workflow_jobs.js": 1,
    # The browser half of prose rendering: DOMPurify, block layout and <style> scoping. Sits beside utils.js because it is what
    # makes utils.js output safe to hand to innerHTML, and imports nothing above it.
    "message_html.js": 1,
    "card_scripts.js": 1,
    # Pure render/state helpers for the Dynamic Worlds review surface; imports
    # only utils.js, so it sits alongside it rather than with the features.
    "world_proposals.js": 1,
    "group_cast.js": 1,
    "library_dedupe_view.js": 1,
    # L2 services.
    "tabLock.js": 2,
    "audio_schedule.js": 2,
    # L3 ui + audio engine.
    "modal.js": 3,
    "panels.js": 3,
    "chips.js": 3,
    # The Inspector's section shell, shared by the panel, the in-chat blocks and the Decisions section beneath both.
    "inspector_section.js": 3,
    "audio_player.js": 3,
    "audio_transport.js": 3,
    "expression_playback.js": 3,
    # L4 platform (workflow framework + document/editor primitives).
    "workflow_segmentation.js": 4,
    "workflow_text_effects.js": 4,
    "workflow_text_interaction.js": 4,
    "workflow_loader.js": 4,
    "default_widget.js": 4,
    "document_editor.js": 4,
    "document_probs.js": 4,
    "slop_score.js": 4,
    # L5 features. Peers may import one another; the cycle check keeps that acyclic.
    "chat.js": 5,
    "chat_core.js": 5,
    "chat_error.js": 5,
    "chat_stream.js": 5,
    "chat_messages.js": 5,
    "chat_inspector.js": 5,
    "chat_decisions.js": 5,
    "message_inspector.js": 5,  # repaints the message list via a hook chat_core.js registers
    "chat_workflow.js": 5,
    "chat_conversations.js": 5,
    "chat_composer.js": 5,
    "document.js": 5,
    "document_audit.js": 5,
    "library.js": 5,
    "library_browser.js": 5,
    "library_sidebar.js": 5,
    "library_manager.js": 5,
    "library_card_generator.js": 5,
    "library_card_scripts.js": 5,
    "library_dedupe.js": 5,
    "library_fragments.js": 5,
    "library_decisions.js": 5,
    "lorebooks.js": 5,
    "settings.js": 5,
    "settings_models.js": 5,
    "settings_personas.js": 5,
    "presets.js": 5,
    "state_panel.js": 5,
    "mobile.js": 5,
    "group_setup.js": 5,
    # L6 shell / plugin facade.
    "app.js": 6,
    "workflow_api.js": 6,
}

# ── 4. Frozen ABI ────────────────────────────────────────────────────────────
# workflow_api.js's complete export surface, additive-only. A rename or removal fails; a genuinely new export is added here in
# the same commit -- and, because that is a new revision of the plugin ABI, `WORKFLOW_API_VERSION` is bumped with it. The check
# below reads that constant back so the number cannot drift from the surface it describes.
FROZEN_ABI = {
    "WORKFLOW_API_VERSION",
    # registrars
    "registerWorkflowPipeline",
    "registerTextEffect",
    "registerClickHandler",
    "registerWorkflowInspectorCard",
    "registerWorkflowToolsPanelCard",
    "registerWorkflowMessageButton",
    "registerWorkflowEventHandler",
    "registerAttachmentRenderer",
    "registerRerollParams",
    "registerRerollSuccess",
    "registerRegenerateSettled",
    "registerAction",
    # http / dom helpers
    "api",
    "convUrl",
    "esc",
    "escAttr",
    "toast",
    "notifyError",
    "showModal",
    "showConfirmModal",
    "closeModal",
    "setModalCloseGuard",
    "sseEvents",
    "streamPost",
    "workflowAttachmentUrl",
    # audio
    "playAudio",
    "stopChannel",
    "stopAll",
    "pauseChannel",
    "resumeChannel",
    "seekChannel",
    "setChannelVolume",
    "setChannelRepeat",
    "replayChannel",
    "channelState",
    "onChannel",
    # text
    "messageSegments",
    "startTextEffect",
    "clearTextEffect",
    # chat / framework
    "setWorkflowPhase",
    "clearWorkflowPhase",
    "startWorkflowJob",
    "stopButtonState",
    "stopWorkflowJob",
    "workflowActionJob",
    "activateWorkflowVariant",
    "refreshConversationMessages",
    "regenerateWorkflowAttachment",
    "rehydrateWorkflowAttachment",
    "stepWorkflowVariant",
    "deleteWorkflowAttachment",
    "selectWorkflowPipelinePass",
    "broadcastWorkflowMutation",
    "effectiveWorkflowEnabled",
    "subscribe",
    # state accessors
    "requestRepaint",
    "getActiveConvId",
    "getGroupCast",
    "getMessages",
    "getManifestEntry",
    "canMutate",
    "getWorkflowState",
    "setWorkflowState",
    "localMlReady",
    "refreshLocalMlStatus",
}

# ── Parsing helpers ──────────────────────────────────────────────────────────
# Matches `import ... from "path"` and re-export `export ... from "path"`.
_IMPORT_FROM = re.compile(r'(?:import|export)\b[^;]*?\bfrom\s+["\']([^"\']+)["\']', re.DOTALL)
_SIDE_EFFECT_IMPORT = re.compile(r'\bimport\s+["\']([^"\']+)["\']')
_DYNAMIC_IMPORT_CALL = re.compile(r"\bimport\s*\(")
_DYNAMIC_IMPORT_LITERAL = re.compile(r'\bimport\s*\(\s*(["\'`])([^"\'`]*?)\1\s*\)', re.DOTALL)
# Braced import/re-export binding list, possibly multiline.
_BRACED = re.compile(r'(?:import|export)\s*(?:type\s+)?\{([^}]*)\}\s*from\s+["\']([^"\']+)["\']', re.DOTALL)
# Inline event handler attribute (on*="...") in HTML or in a JS string, whether the attribute follows whitespace or opens a
# quoted string. `.onclick =` is a DOM property, not markup, and is allowed.
_INLINE_ON = re.compile(r'(?<![\w.$-])on[a-z]{4,}\s*=\s*["\'`]')
# A name published on window/globalThis for markup or another module to reach.
_WINDOW_GLOBAL = re.compile(
    r"\b(?:window|globalThis)\.(?!location\b)[A-Za-z_$][\w$]*\s*=(?!=)|Object\.assign\(\s*(?:window|globalThis)\b"
)
# Action registrations. A plugin registers under its WORKFLOW_ID, its directory name.
_REGISTER_ONE = re.compile(r'registerAction\(\s*(?:"([^"]+)"|WORKFLOW_ID)\s*,\s*"([^"]+)"')
_REGISTER_MANY = re.compile(r'registerActions\(\s*(?:"([^"]+)"|WORKFLOW_ID)\s*,\s*\{(.*?)\n\s*\}\s*\)', re.DOTALL)
_HANDLER_KEY = re.compile(r"^([ \t]+)([A-Za-z_$][\w$]*)\s*:", re.MULTILINE)
# A quoted "scope:name" literal, as markup or a string that becomes markup.
_ACTION_NAME = re.compile(r'["\'`]([a-z][\w-]*):([A-Za-z]\w*)["\'`]')
# Literal attributes must be checked even when their entire scope is misspelled.
# Computed values are checked through their quoted scope:name alternatives above.
_ACTION_ATTRIBUTE = re.compile(r'\bdata-wf-action\s*=\s*["\'`]([^"\'`\s${}<>]+)["\'`]')
# workflow_api.js exports: `export function X`, `export const X`, and re-export lists.
_EXPORT_DECL = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z0-9_]+)")
# Every module's exports and the imports that consume them, for the unused-export check.
_EXPORTED_DECL = re.compile(r"^export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
_EXPORT_LIST = re.compile(r'^export\s*\{([^{}]*)\}\s*(?:from\s+["\']([^"\']+)["\'])?', re.MULTILINE)
_IMPORT_NAMED = re.compile(r'\bimport\s+(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^{}]*)\}\s*from\s+["\']([^"\']+)["\']')
_IMPORT_NAMESPACE = re.compile(r'\bimport\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+["\']([^"\']+)["\']')
# `const { a, b: c } = await import("x")` and `const ns = await import("x")`, the forms tests use.
_DYNAMIC_DESTRUCTURE = re.compile(r'\{([^{}]*)\}\s*=\s*await\s+import\(\s*["\']([^"\']+)["\']\s*\)')
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_DYNAMIC_NAMESPACE = re.compile(r'\b([A-Za-z_$][\w$]*)\s*=\s*await\s+import\(\s*["\']([^"\']+)["\']\s*\)')


def rel_basename(importer: Path, spec: str) -> str | None:
    """The imported module's basename if it is a relative import, else None."""
    if not spec.startswith("./") and not spec.startswith("../"):
        return None
    return (importer.parent / spec).resolve().name


def imported_paths(text: str) -> list[str]:
    return (
        _IMPORT_FROM.findall(text)
        + _SIDE_EFFECT_IMPORT.findall(text)
        + [match.group(2) for match in _DYNAMIC_IMPORT_LITERAL.finditer(text)]
    )


def has_computed_dynamic_import(text: str) -> bool:
    return len(_DYNAMIC_IMPORT_CALL.findall(text)) != len(_DYNAMIC_IMPORT_LITERAL.findall(text))


def workflow_import_allowed(path: Path, spec: str, workflow_root: Path = FE / "workflows") -> bool:
    """An import may use the facade or remain inside its own workflow slice."""
    if spec == "/static/workflow_api.js":
        return True
    if not (spec.startswith("./") or spec.startswith("../")) or "${" in spec:
        return False
    own_slice = workflow_root / path.relative_to(workflow_root).parts[0]
    return (path.parent / spec).resolve().is_relative_to(own_slice.resolve())


def import_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """Return one closed import path when the graph contains a cycle."""
    visited: set[str] = set()
    active: list[str] = []
    positions: dict[str, int] = {}

    def visit(module: str) -> list[str] | None:
        visited.add(module)
        positions[module] = len(active)
        active.append(module)
        for dependency in sorted(graph[module]):
            if dependency in positions:
                return active[positions[dependency] :] + [dependency]
            if dependency not in visited:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        active.pop()
        del positions[module]
        return None

    for module in sorted(graph):
        if module not in visited:
            cycle = visit(module)
            if cycle:
                return cycle
    return None


def registered_actions(path: Path, text: str, workflow_root: Path = FE / "workflows") -> set[str]:
    """Every "scope:name" *text* registers; WORKFLOW_ID is the plugin's directory."""
    plugin = path.relative_to(workflow_root).parts[0] if path.is_relative_to(workflow_root) else None
    names = {f"{scope or plugin}:{name}" for scope, name in _REGISTER_ONE.findall(text)}
    for scope, body in _REGISTER_MANY.findall(text):
        keys = _HANDLER_KEY.findall(body)
        indent = min((len(pad) for pad, _ in keys), default=0)
        names |= {f"{scope or plugin}:{key}" for pad, key in keys if len(pad) == indent}
    return names


def underscore_import_count(text: str) -> int:
    n = 0
    for names, spec in _BRACED.findall(text):
        if not (spec.startswith("./") or spec.startswith("../")):
            continue
        for raw in names.split(","):
            name = raw.split(" as ")[0].strip()
            if name.startswith("_"):
                n += 1
    return n


def resolve_spec(importer: Path, spec: str, frontend: Path = FE) -> Path | None:
    """The file a relative or /static/ import specifier names, else None."""
    if spec.startswith("/static/"):
        return (frontend / spec.removeprefix("/static/")).resolve()
    if spec.startswith(("./", "../")):
        return (importer.parent / spec).resolve()
    return None


def module_exports(path: Path, text: str, frontend: Path = FE) -> dict[str, tuple[Path, str] | None]:
    """Each exported name, mapped to the (module, name) it re-exports, or None when declared here."""
    names: dict[str, tuple[Path, str] | None] = dict.fromkeys(_EXPORTED_DECL.findall(text))
    for body, spec in _EXPORT_LIST.findall(text):
        source = resolve_spec(path, spec, frontend) if spec else None
        for raw in _COMMENT.sub("", body).split(","):
            original, _, public = raw.strip().partition(" as ")
            if original:
                names[(public or original).strip()] = (source, original.strip()) if source else None
    return names


def imported_names(path: Path, text: str, frontend: Path = FE) -> set[tuple[Path, str]]:
    """Every (module, name) *text* imports, statically, through a dynamic import, or as a namespace member."""
    used: set[tuple[Path, str]] = set()
    for body, spec in _IMPORT_NAMED.findall(text) + _DYNAMIC_DESTRUCTURE.findall(text):
        if target := resolve_spec(path, spec, frontend):
            names = (re.split(r"\s+as\s+|:", raw)[0].strip() for raw in _COMMENT.sub("", body).split(","))
            used |= {(target, name) for name in names if name}
    for namespace, spec in _IMPORT_NAMESPACE.findall(text) + _DYNAMIC_NAMESPACE.findall(text):
        if target := resolve_spec(path, spec, frontend):
            members = re.findall(rf"(?<![\w$.]){re.escape(namespace)}\.([A-Za-z_$][\w$]*)", text)
            used |= {(target, name) for name in members}
    return used


def unused_exports(
    modules: dict[Path, str], consumers: dict[Path, str], roots: set[Path], frontend: Path = FE
) -> dict[Path, set[str]]:
    """Exports nothing imports, per module. *roots* export a public surface and count as used.

    A re-export counts as a use of its source only while something imports the re-export itself.
    """
    exports = {path: module_exports(path, text, frontend) for path, text in modules.items()}
    used: set[tuple[Path, str]] = {(root, name) for root in roots for name in exports.get(root, {})}
    for path, text in {**modules, **consumers}.items():
        used |= imported_names(path, text, frontend)
    pending = list(used)
    while pending:
        path, name = pending.pop()
        source = exports.get(path, {}).get(name)
        if source and source not in used:
            used.add(source)
            pending.append(source)
    unused = {path: {name for name in names if (path, name) not in used} for path, names in exports.items()}
    return {path: names for path, names in unused.items() if names}


def unregistered_actions(text: str, registered: set[str], *, workflow_id: str | None = None) -> set[str]:
    """Unknown literal actions, including a plug-in's WORKFLOW_ID templates."""
    if workflow_id is not None:
        text = text.replace("${WORKFLOW_ID}", workflow_id)
    scopes = {name.split(":")[0] for name in registered}
    used = set(_ACTION_ATTRIBUTE.findall(text))
    used |= {f"{scope}:{name}" for scope, name in _ACTION_NAME.findall(text) if scope in scopes}
    return used - registered


def main() -> int:
    errors: list[str] = []

    top_files = sorted(p for p in FE.glob("*.js"))
    workflow_files = sorted(FE.glob("workflows/**/*.js"))
    top_by_path = {path.resolve(): path.name for path in top_files}
    graph: dict[str, set[str]] = {path.name: set() for path in top_files}

    # 1. Layer import-direction.
    for path in top_files:
        name = path.name
        if name not in LAYERS:
            errors.append(f"[layer] {name}: unclassified — add it to LAYERS in check_frontend_layers.py")
            continue
        text = path.read_text(encoding="utf-8")
        for spec in imported_paths(text):
            dependency = top_by_path.get((path.parent / spec).resolve())
            if dependency:
                graph[name].add(dependency)
            base = rel_basename(path, spec)
            if base is None or base not in LAYERS:
                continue
            hi, lo = LAYERS[name], LAYERS[base]
            if lo > hi:
                errors.append(f"[layer] {name} (L{hi}) imports {base} (L{lo}) — invert the dependency instead")

    cycle = import_cycle(graph)
    if cycle:
        errors.append(f"[cycle] {' -> '.join(cycle)}")

    # 2a. Markup reaches code only through registered actions (scope: all of frontend/ and index.html, vendor/ excluded).
    scan = [p for p in FE.rglob("*.js") if "vendor" not in p.parts] + [FE / "index.html"]
    texts = {path: path.read_text(encoding="utf-8") for path in scan}
    registered: set[str] = set()
    for path, text in texts.items():
        rel = path.relative_to(FE)
        if n := len(_INLINE_ON.findall(text)):
            errors.append(f"[inline] {rel}: {n} inline on*= handler(s); use data-wf-action and registerAction")
        if n := len(_WINDOW_GLOBAL.findall(text)):
            errors.append(f"[global] {rel}: {n} window global(s); import the name, or register an action")
        if path.suffix == ".js":
            registered |= registered_actions(path, text)
    for path, text in texts.items():
        workflow_id = path.relative_to(FE / "workflows").parts[0] if path in workflow_files else None
        for name in sorted(unregistered_actions(text, registered, workflow_id=workflow_id)):
            errors.append(f'[action] {path.relative_to(FE)}: "{name}" is not registered')

    # 2b. Module-private names stay in their module.
    us = sum(underscore_import_count(p.read_text(encoding="utf-8")) for p in [*top_files, *workflow_files])
    if us:
        errors.append(f"[private] {us} underscore-prefixed import(s) across modules; give the name a public spelling")

    # 3. Plugin boundary: imports remain within the workflow or use its facade.
    for path in workflow_files:
        text = path.read_text(encoding="utf-8")
        if has_computed_dynamic_import(text):
            errors.append(f"[plugin] {path.relative_to(FE)}: computed dynamic import is forbidden")
        for spec in imported_paths(text):
            if not workflow_import_allowed(path, spec):
                rel = path.relative_to(FE)
                errors.append(
                    f"[plugin] {rel}: forbidden import '{spec}' (plugins import only their own files or /static/workflow_api.js)"
                )

    # 4. ABI snapshot: workflow_api.js exports must equal FROZEN_ABI. Only real
    # `export` statements count — NOT the `import {...}` blocks above them (the facade imports the same names it re-exports).
    api_text = (FE / "workflow_api.js").read_text(encoding="utf-8")
    exports = set(_EXPORT_DECL.findall(api_text))
    # Re-export blocks: `export { a, b as c };` and `export { a } from "...";`.
    for m in re.finditer(r"export\s*\{([^}]*)\}\s*(?:from\s+[\"'][^\"']+[\"']\s*)?;", api_text):
        for raw in m.group(1).split(","):
            n = raw.split(" as ")[-1].strip()
            if n:
                exports.add(n)
    exports.discard("")
    version_decl = re.search(r"export\s+const\s+WORKFLOW_API_VERSION\s*=\s*(\d+)\s*;", api_text)
    if version_decl is None:
        errors.append("[abi] workflow_api.js does not declare WORKFLOW_API_VERSION")
        abi_version = "?"
    else:
        abi_version = version_decl.group(1)
    missing = FROZEN_ABI - exports
    added = exports - FROZEN_ABI
    if missing:
        errors.append(f"[abi] workflow_api.js is MISSING frozen exports (rename/removal breaks plugins): {sorted(missing)}")
    if added:
        errors.append(
            f"[abi] workflow_api.js has NEW exports not in FROZEN_ABI — add them there (additive-only): {sorted(added)}"
        )

    # 5. An export is a contract with an importer. workflow_api.js is exempt: plug-ins outside this tree import it.
    modules = {path.resolve(): text for path, text in texts.items() if path.suffix == ".js"}
    tests = ROOT / "tests" / "frontend"
    consumers = {
        path.resolve(): path.read_text(encoding="utf-8") for path in tests.rglob("*") if path.suffix in (".js", ".mjs")
    }
    unused = unused_exports(modules, consumers, {(FE / "workflow_api.js").resolve()})
    for path, names in sorted(unused.items()):
        errors.append(
            f"[export] {path.relative_to(FE)}: nothing imports {', '.join(sorted(names))}; drop the export or the declaration"
        )

    # Report.
    print(
        f"frontend layer check: {len(top_files)} modules, {len(registered)} actions, "
        f"underscore imports={us}, "
        f"ABI v{abi_version} ({len(exports)} exports)"
    )
    if errors:
        print("\nFRONTEND LAYER CHECK FAILED:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("frontend layer check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
