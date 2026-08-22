// THE plugin surface (ABI v3). A workflow module under frontend/workflows/**
// imports from this file and NOTHING else in the app (only its own relative
// files besides). Everything a plugin is allowed to touch — registrars, HTTP/DOM
// helpers, the audio engine, text effects, framework calls, and a small set of
// state accessors — is re-exported or wrapped here, so the plugin never reaches
// into state.js / chat.js / audio_player.js / etc. directly.
//
// STABILITY POLICY — additive only. New exports may be added; an existing export
// NEVER changes name or signature. That single rule is the extensibility
// contract, and scripts/check_frontend_layers.py enforces it: it diffs this
// file's exports against a frozen snapshot, so an accidental rename/removal fails
// CI. Bump WORKFLOW_API_VERSION only when adding surface (still additive).
//
// The pre-facade deep imports (`/static/chat.js`, `/static/state.js`, …) remain
// the deprecated-but-stable ABI v1 for EXTERNAL plugins; no in-repo plugin uses
// them, so the layer check's plugin-import rule is absolute in-repo.
import { api } from "./api.js";
import {
  channelState,
  onChannel,
  pauseChannel,
  playAudio,
  replayChannel,
  resumeChannel,
  seekChannel,
  setChannelRepeat,
  setChannelVolume,
  stopAll,
  stopChannel,
} from "./audio_player.js";
// The barrel pulls in the chat spine, which touches the DOM at module load. It is
// already in the graph (app.js imported it at boot), so this adds no second eval.
import {
  clearWorkflowPhase,
  refreshConversationMessages,
  renderMessages,
  selectWorkflowPipelinePass,
  setWorkflowPhase,
} from "./chat.js";
import { closeModal, setModalCloseGuard, showModal } from "./modal.js";
import { sseEvents, streamPost } from "./sse.js";
import { effectiveWorkflowEnabled, S, subscribe } from "./state.js";
import { broadcastWorkflowMutation } from "./tabLock.js";
import { convUrl, esc, escAttr, notifyError, toast } from "./utils.js";
import {
  registerClickHandler,
  registerTextEffect,
  registerWorkflowEventHandler,
  registerWorkflowInspectorCard,
  registerWorkflowMessageButton,
  registerWorkflowPipeline,
  registerWorkflowToolsPanelCard,
} from "./workflow_registry.js";
import { messageSegments } from "./workflow_segmentation.js";
import { clearTextEffect, startTextEffect } from "./workflow_text_effects.js";

export const WORKFLOW_API_VERSION = 3;

// ── Pass-through surface ─────────────────────────────────────────────────────
// Registrars (7 of the 9; registerAttachmentRenderer + registerAction are below).
// HTTP / DOM helpers.
// Audio engine (shared channels; the framework mounts the transport bar).
// Text units + transient text effects.
// Chat / framework hooks.
export {
  api,
  broadcastWorkflowMutation,
  channelState,
  clearTextEffect,
  clearWorkflowPhase,
  closeModal,
  convUrl,
  effectiveWorkflowEnabled,
  esc,
  escAttr,
  messageSegments,
  notifyError,
  onChannel,
  pauseChannel,
  playAudio,
  refreshConversationMessages,
  registerClickHandler,
  registerTextEffect,
  registerWorkflowEventHandler,
  registerWorkflowInspectorCard,
  registerWorkflowMessageButton,
  registerWorkflowPipeline,
  registerWorkflowToolsPanelCard,
  replayChannel,
  resumeChannel,
  seekChannel,
  selectWorkflowPipelinePass,
  setChannelRepeat,
  setChannelVolume,
  setModalCloseGuard,
  setWorkflowPhase,
  showModal,
  sseEvents,
  startTextEffect,
  stopAll,
  stopChannel,
  streamPost,
  subscribe,
  toast,
};

// ── registerAttachmentRenderer ───────────────────────────────────────────────
// Wraps the raw `S.workflowAttachmentRenderers[wid] = fn` author slot so a plugin
// never touches S. The renderer is a consumption surface (it replays already-
// produced bytes), so it is intentionally NOT gated by the workflow toggle.
export function registerAttachmentRenderer(wid, fn) {
  if (typeof wid !== "string" || !wid) {
    console.error("registerAttachmentRenderer: workflow id required", wid);
    return;
  }
  if (typeof fn !== "function") {
    console.error(`registerAttachmentRenderer: fn must be a function (${wid})`);
    return;
  }
  S.workflowAttachmentRenderers[wid] = fn;
}

// Called just before a reroll POST for this workflow's attachments. Return an object of
// generation params to override (string values only; keys the artifact already recorded),
// or null for the stored ones. The framework swallows a throw -- a broken plugin must not
// break reroll.
export function registerRerollParams(wid, fn) {
  S.workflowRerollParams[wid] = fn;
}

// ── registerAction + the delegated dispatcher ────────────────────────────────
// The plugin-sized slice of the core data-action dispatcher (stage 5 adopts the
// same `data-wf-action` attribute convention so the two converge). A plugin
// wires a button/input by putting `data-wf-action="<wid>:<name>"` on it (plus
// `data-wf-on="change"` for inputs/selects that fire on change instead of
// click), and registering the handler here — no `window.*` global, no inline
// on* attribute. The handler is called `fn(el, event)` where `el` is the element
// carrying the attribute; read any parameters off its `data-*`.
const _actions = new Map(); // "wid:name" -> fn
let _actionsWired = false;

function _dispatchAction(e, type) {
  const el = e.target.closest?.("[data-wf-action]");
  if (!el) return;
  if ((el.dataset.wfOn || "click") !== type) return;
  const fn = _actions.get(el.dataset.wfAction);
  if (!fn) return;
  try {
    fn(el, e);
  } catch (err) {
    console.error(`data-wf-action "${el.dataset.wfAction}" handler threw:`, err);
  }
}

function _wireActionDelegation() {
  if (_actionsWired) return;
  _actionsWired = true;
  document.addEventListener("click", (e) => _dispatchAction(e, "click"));
  document.addEventListener("change", (e) => _dispatchAction(e, "change"));
}

export function registerAction(wid, name, fn) {
  if (typeof wid !== "string" || !wid || typeof name !== "string" || !name) {
    console.error("registerAction: wid and name must be non-empty strings", wid, name);
    return;
  }
  if (typeof fn !== "function") {
    console.error(`registerAction: fn must be a function (${wid}:${name})`);
    return;
  }
  _wireActionDelegation();
  _actions.set(`${wid}:${name}`, fn);
}

// ── State accessors ──────────────────────────────────────────────────────────
// The narrow, read-mostly window into S that closes the last reasons a plugin had
// to import state.js.

let _repaintQueued = false;

// rAF-debounced chat repaint for a plugin that changed something a message body
// renders (e.g. a click-affordance toggle). No-ops while streaming: renderMessages
// repaints from S.messages, which does not yet hold the in-flight reply, and
// afterStream repaints anyway — so a mid-stream repaint would clobber the
// streaming bubble. Making that the facade guarantee keeps plugins safe by default.
export function requestRepaint() {
  if (S.isStreaming || _repaintQueued) return;
  _repaintQueued = true;
  requestAnimationFrame(() => {
    _repaintQueued = false;
    if (!S.isStreaming) renderMessages();
  });
}

export function getActiveConvId() {
  return S.activeConvId;
}

// The open scene's cast, in roster order — `null` in a solo chat, where "the
// character" is a property of the conversation and needs no picking. A workflow
// that keeps per-character settings needs this: in a group the conversation
// names no character, and the one the backend would infer is whoever spoke last,
// which is a reading of history rather than a choice the user made. Pass an
// entry's `id` back as `speaker_member_id` on a trigger call to address that
// member. A fresh array of plain copies each call, so a plugin cannot reach the
// live roster through it.
export function getGroupCast() {
  if (!S.groupCast) return null;
  return S.groupCast.members.map((member) => ({
    id: member.id,
    name: member.display_name,
    card_id: member.character_card_id || null,
    muted: Boolean(member.muted),
  }));
}

// Read-only by contract: mutate a message and the framework will overwrite it on
// the next refetch. Returns the live array; do not push/splice.
export function getMessages() {
  return S.messages;
}

// This workflow's own manifest entry ({id, ...}) from /api/workflows, or null.
export function getManifestEntry(wid) {
  return S.workflowManifest.find((w) => w.id === wid) || null;
}

// Whether this tab may perform mutating workflow actions. Today it is simply
// "not one of several open tabs"; stage 3's capability.js becomes the
// implementation with zero plugin-visible change.
export function canMutate() {
  return !S.hasMultipleTabs;
}

// Opaque per-workflow UI state slot (survives re-renders; not persisted).
export function getWorkflowState(wid) {
  return S.workflowState[wid];
}

export function setWorkflowState(wid, v) {
  S.workflowState[wid] = v;
}
