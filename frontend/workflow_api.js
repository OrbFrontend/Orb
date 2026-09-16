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
import {
  clearWorkflowPhase,
  refreshConversationMessages,
  renderMessages,
  selectWorkflowPipelinePass,
  setWorkflowPhase,
} from "./chat.js";
import { closeModal, setModalCloseGuard, showModal } from "./modal.js";
import { sseEvents, streamPost } from "./sse.js";
import { effectiveWorkflowEnabled, localMlReady, S, subscribe } from "./state.js";
import { broadcastWorkflowMutation } from "./tabLock.js";
import { convUrl, esc, escAttr, fromMessageBody, notifyError, toast } from "./utils.js";
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

// Workflow modules use this facade for registration, requests, and playback.

export const WORKFLOW_API_VERSION = 5;

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
  localMlReady,
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

export function registerAttachmentRenderer(wid, fn, options = {}) {
  if (typeof wid !== "string" || !wid) {
    console.error("registerAttachmentRenderer: workflow id required", wid);
    return;
  }
  if (typeof fn !== "function") {
    console.error(`registerAttachmentRenderer: fn must be a function (${wid})`);
    return;
  }
  S.workflowAttachmentRenderers[wid] = fn;
  S.workflowAttachmentPlacements[wid] = {
    placement: options?.placement === "actions" ? "actions" : "artifact",
  };
}

export function registerRerollParams(wid, fn) {
  S.workflowRerollParams[wid] = fn;
}

export function registerRerollSuccess(wid, fn) {
  if (typeof wid !== "string" || !wid) {
    console.error("registerRerollSuccess: workflow id required", wid);
    return;
  }
  if (typeof fn !== "function") {
    console.error(`registerRerollSuccess: fn must be a function (${wid})`);
    return;
  }
  S.workflowRerollSuccess[wid] = fn;
}

const _actions = new Map(); // action name -> handler
let _actionsWired = false;

// The events `data-wf-on` may name. Drag events are here so a workflow can
// declare a drop target in markup like any other control: a drop target is
// three events on one element -- `dragover` must preventDefault or the browser
// never fires `drop` -- so `data-wf-on` takes a space-separated LIST and the
// handler switches on `e.type`.
const _ACTION_EVENTS = ["click", "change", "dragover", "dragleave", "drop"];

function _dispatchAction(e, type) {
  const el = e.target.closest?.("[data-wf-action]");
  // A workflow's own surfaces (widgets, panels, message buttons) all sit outside
  // the bubble. Inside it is model markup, which never gets to name an action.
  if (!el || fromMessageBody(el)) return;
  if (!(el.dataset.wfOn || "click").split(/\s+/).includes(type)) return;
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
  for (const type of _ACTION_EVENTS) document.addEventListener(type, (e) => _dispatchAction(e, type));
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

let _repaintQueued = false;

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

export function getGroupCast() {
  if (!S.groupCast) return null;
  return S.groupCast.members.map((member) => ({
    id: member.id,
    name: member.display_name,
    card_id: member.character_card_id || null,
    muted: Boolean(member.muted),
  }));
}

export function getMessages() {
  return S.messages;
}

export function getManifestEntry(wid) {
  return S.workflowManifest.find((w) => w.id === wid) || null;
}

export function canMutate() {
  return !S.hasMultipleTabs;
}

export function getWorkflowState(wid) {
  return S.workflowState[wid];
}

export function setWorkflowState(wid, v) {
  S.workflowState[wid] = v;
}
