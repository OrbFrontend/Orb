import { registerAction } from "./actions.js";
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
  activateWorkflowVariant,
  clearWorkflowPhase,
  deleteWorkflowAttachment,
  refreshConversationMessages,
  regenerateWorkflowAttachment,
  rehydrateWorkflowAttachment,
  renderMessages,
  selectWorkflowPipelinePass,
  setWorkflowPhase,
  stepWorkflowVariant,
  workflowActionJob,
} from "./chat.js";
import { closeModal, setModalCloseGuard, showConfirmModal, showModal } from "./modal.js";
import { refreshLocalMlStatus } from "./settings.js";
import { sseEvents, streamPost } from "./sse.js";
import { effectiveWorkflowEnabled, localMlReady, S, subscribe } from "./state.js";
import { broadcastWorkflowMutation } from "./tabLock.js";
import { convUrl, esc, escAttr, notifyError, toast, workflowAttachmentUrl } from "./utils.js";
import { startWorkflowJob, stopButtonState, stopWorkflowJob } from "./workflow_jobs.js";
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

export const WORKFLOW_API_VERSION = 10;

export {
  activateWorkflowVariant,
  api,
  broadcastWorkflowMutation,
  channelState,
  clearTextEffect,
  clearWorkflowPhase,
  closeModal,
  convUrl,
  deleteWorkflowAttachment,
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
  refreshLocalMlStatus,
  regenerateWorkflowAttachment,
  registerAction,
  registerClickHandler,
  registerTextEffect,
  registerWorkflowEventHandler,
  registerWorkflowInspectorCard,
  registerWorkflowMessageButton,
  registerWorkflowPipeline,
  registerWorkflowToolsPanelCard,
  rehydrateWorkflowAttachment,
  replayChannel,
  resumeChannel,
  seekChannel,
  selectWorkflowPipelinePass,
  setChannelRepeat,
  setChannelVolume,
  setModalCloseGuard,
  setWorkflowPhase,
  showConfirmModal,
  showModal,
  sseEvents,
  startTextEffect,
  startWorkflowJob,
  stepWorkflowVariant,
  stopAll,
  stopButtonState,
  stopChannel,
  stopWorkflowJob,
  streamPost,
  subscribe,
  toast,
  workflowActionJob,
  workflowAttachmentUrl,
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

// Called as `(msgId, rootId)` when a regenerate of *wid* ends, whatever the
// outcome: success, failure, Stop, or recovery after a dropped stream.
export function registerRegenerateSettled(wid, fn) {
  if (typeof wid !== "string" || !wid) {
    console.error("registerRegenerateSettled: workflow id required", wid);
    return;
  }
  if (typeof fn !== "function") {
    console.error(`registerRegenerateSettled: fn must be a function (${wid})`);
    return;
  }
  S.workflowRegenerateSettled[wid] = fn;
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
