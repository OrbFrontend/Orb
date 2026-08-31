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

// Workflow modules use this facade for registration, requests, and playback.

export const WORKFLOW_API_VERSION = 3;

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

export function registerRerollParams(wid, fn) {
  S.workflowRerollParams[wid] = fn;
}

const _actions = new Map(); // action name -> handler
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
