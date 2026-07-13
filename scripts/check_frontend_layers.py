#!/usr/bin/env python3
"""Frontend layering + plugin-boundary guardrail.

The frontend is flat vanilla ES modules with no build step, so architecture is
enforced by convention + this lint rather than by a bundler. It checks, in order:

  1. Layer import-direction. Every top-level frontend/*.js is assigned a layer
     (LAYERS). A file may import only its own layer or a lower one. The known
     current upward edges live in ALLOWED_UPWARD and shrink as the deferred
     stages (3-5) land; a NEW upward edge fails.
  2. Two ratchets, which may only DECREASE: the count of inline `on*=` handlers
     (the window-bridge surface) and the count of underscore "private"
     cross-module imports. Lower them and drop the ceiling; never raise it.
  3. Plugin boundary. A file under frontend/workflows/** may import only
     `/static/workflow_api.js` and relative `./` paths — never a deep
     `/static/<core>.js`. The allowlist is EMPTY and must stay empty.
  4. ABI snapshot. workflow_api.js's exports must equal FROZEN_ABI exactly, so an
     accidental rename/removal of a plugin-facing export fails CI (additive-only:
     a genuinely new export is added to FROZEN_ABI in the same commit).

Exit non-zero on any violation. Wired into scripts/lint.sh.
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
    "sse.js": 0,
    "validate.js": 0,
    # L1 state + shared pure helpers.
    "state.js": 1,
    "workflow_registry.js": 1,
    "utils.js": 1,
    # L2 services.
    "tabLock.js": 2,
    "audio_schedule.js": 2,
    # L3 ui + audio engine.
    "modal.js": 3,
    "panels.js": 3,
    "chips.js": 3,
    "audio_player.js": 3,
    "audio_transport.js": 3,
    # L4 platform (workflow framework + document/editor primitives).
    "workflow_segmentation.js": 4,
    "workflow_text_effects.js": 4,
    "workflow_text_interaction.js": 4,
    "workflow_loader.js": 4,
    "default_widget.js": 4,
    "document_editor.js": 4,
    "document_probs.js": 4,
    "slop_score.js": 4,
    # L5 features (peers; same-layer imports are allowed, cross-feature is not —
    # the audit's target, enforced loosely here via the layer rule only).
    "chat.js": 5,
    "chat_core.js": 5,
    "chat_stream.js": 5,
    "chat_messages.js": 5,
    "chat_inspector.js": 5,
    "chat_workflow.js": 5,
    "chat_conversations.js": 5,
    "chat_composer.js": 5,
    "document.js": 5,
    "library.js": 5,
    "library_browser.js": 5,
    "library_fragments.js": 5,
    "lorebooks.js": 5,
    "settings.js": 5,
    "settings_models.js": 5,
    "settings_personas.js": 5,
    "presets.js": 5,
    "direction_notes_panel.js": 5,
    "mobile.js": 5,
    # L6 shell / plugin facade.
    "app.js": 6,
    "workflow_api.js": 6,
}

# Upward edges (importer -> imported, both basenames) currently present and
# tolerated. Each is a documented consequence of a not-yet-done stage; the set
# only shrinks. A permanent, deliberate exception is the state <-> workflow_registry
# pair (see state.js): the re-export lives in the layer L1, load-safe by call-time
# deref, so it never appears here (same layer). Seed with the current reality.
ALLOWED_UPWARD: set[tuple[str, str]] = {
    # The boot orchestrator repaints the Tools panel after loading plugin modules
    # (see workflow_loader.js). A stage-5 concern; documented until then.
    ("workflow_loader.js", "settings.js"),
}

# ── 2. Ratchets (may only decrease) ──────────────────────────────────────────
MAX_INLINE_ON = 267  # inline on*= handlers across frontend/ (js + index.html)
MAX_UNDERSCORE_IMPORTS = 10  # underscore-prefixed names imported cross-module

# ── 4. Frozen ABI ────────────────────────────────────────────────────────────
# workflow_api.js's complete export surface (ABI v2, additive-only). A rename or
# removal fails; a genuinely new export is added here in the same commit.
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
    "registerAction",
    # http / dom helpers
    "api",
    "convUrl",
    "esc",
    "escAttr",
    "toast",
    "showModal",
    "closeModal",
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
    "refreshConversationMessages",
    "selectWorkflowPipelinePass",
    "broadcastWorkflowMutation",
    "effectiveWorkflowEnabled",
    "subscribe",
    # state accessors
    "requestRepaint",
    "getActiveConvId",
    "getMessages",
    "getManifestEntry",
    "canMutate",
    "getWorkflowState",
    "setWorkflowState",
}

# ── Parsing helpers ──────────────────────────────────────────────────────────
# Matches `import ... from "path"` and re-export `export ... from "path"`.
_IMPORT_FROM = re.compile(r'(?:import|export)\b[^;]*?\bfrom\s+["\']([^"\']+)["\']', re.DOTALL)
# Braced import/re-export binding list, possibly multiline.
_BRACED = re.compile(r'(?:import|export)\s*(?:type\s+)?\{([^}]*)\}\s*from\s+["\']([^"\']+)["\']', re.DOTALL)
# Inline event handler attribute (on*="...") in a JS template string or HTML.
_INLINE_ON = re.compile(r'\son[a-z]+\s*=\s*"')
# workflow_api.js exports: `export function X`, `export const X`, and re-export lists.
_EXPORT_DECL = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z0-9_]+)")


def rel_basename(importer: Path, spec: str) -> str | None:
    """The imported module's basename if it is a relative import, else None."""
    if not spec.startswith("./") and not spec.startswith("../"):
        return None
    return (importer.parent / spec).resolve().name


def imported_paths(text: str) -> list[str]:
    return _IMPORT_FROM.findall(text)


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


def main() -> int:
    errors: list[str] = []

    top_files = sorted(p for p in FE.glob("*.js"))
    workflow_files = sorted(FE.glob("workflows/**/*.js"))

    # 1. Layer import-direction.
    for path in top_files:
        name = path.name
        if name not in LAYERS:
            errors.append(f"[layer] {name}: unclassified — add it to LAYERS in check_frontend_layers.py")
            continue
        text = path.read_text(encoding="utf-8")
        for spec in imported_paths(text):
            base = rel_basename(path, spec)
            if base is None or base not in LAYERS:
                continue
            hi, lo = LAYERS[name], LAYERS[base]
            if lo > hi and (name, base) not in ALLOWED_UPWARD:
                errors.append(f"[layer] {name} (L{hi}) imports {base} (L{lo}) — upward edge not in ALLOWED_UPWARD")

    # 2a. Inline on*= ratchet (scope: all of frontend/ + index.html).
    inline = 0
    scan = list(FE.rglob("*.js")) + [p for p in (FE / "index.html",) if p.exists()]
    for path in scan:
        inline += len(_INLINE_ON.findall(path.read_text(encoding="utf-8")))
    if inline > MAX_INLINE_ON:
        errors.append(f"[ratchet] inline on*= count {inline} exceeds ceiling {MAX_INLINE_ON} (ratchet may only decrease)")

    # 2b. Underscore cross-module import ratchet.
    us = sum(underscore_import_count(p.read_text(encoding="utf-8")) for p in top_files)
    if us > MAX_UNDERSCORE_IMPORTS:
        errors.append(
            f"[ratchet] underscore cross-module imports {us} exceeds ceiling {MAX_UNDERSCORE_IMPORTS} (may only decrease)"
        )

    # 3. Plugin boundary: workflows/** import only /static/workflow_api.js + ./.
    for path in workflow_files:
        text = path.read_text(encoding="utf-8")
        for spec in imported_paths(text):
            ok = spec == "/static/workflow_api.js" or spec.startswith("./") or spec.startswith("../")
            if not ok:
                rel = path.relative_to(FE)
                errors.append(f"[plugin] {rel}: forbidden import '{spec}' (plugins import only /static/workflow_api.js)")

    # 4. ABI snapshot: workflow_api.js exports must equal FROZEN_ABI. Only real
    # `export` statements count — NOT the `import {...}` blocks above them (the
    # facade imports the same names it re-exports).
    api_text = (FE / "workflow_api.js").read_text(encoding="utf-8")
    exports = set(_EXPORT_DECL.findall(api_text))
    # Re-export blocks: `export { a, b as c };` and `export { a } from "...";`.
    for m in re.finditer(r"export\s*\{([^}]*)\}\s*(?:from\s+[\"'][^\"']+[\"']\s*)?;", api_text):
        for raw in m.group(1).split(","):
            n = raw.split(" as ")[-1].strip()
            if n:
                exports.add(n)
    exports.discard("")
    missing = FROZEN_ABI - exports
    added = exports - FROZEN_ABI
    if missing:
        errors.append(f"[abi] workflow_api.js is MISSING frozen exports (rename/removal breaks plugins): {sorted(missing)}")
    if added:
        errors.append(f"[abi] workflow_api.js has NEW exports not in FROZEN_ABI — add them there (additive-only): {sorted(added)}")

    # Report.
    print(f"frontend layer check: {len(top_files)} modules, inline on*={inline} (max {MAX_INLINE_ON}), "
          f"underscore imports={us} (max {MAX_UNDERSCORE_IMPORTS}), ABI exports={len(exports)}")
    if errors:
        print("\nFRONTEND LAYER CHECK FAILED:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("frontend layer check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
