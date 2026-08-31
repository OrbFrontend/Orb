import { S } from "./state.js";
import { toast } from "./utils.js";

const CHANNEL_NAME = "orb-tab-lock";
const TAB_ID = `${Date.now()}-${Math.random()}`;
const HEARTBEAT_MS = 2000;
const PEER_TIMEOUT_MS = 5000;

let broadcastChannel = null;
let onWorkflowMutationCallback = null;
const peers = new Map();
let heartbeatTimer = null;

export function setWorkflowMutationCallback(callback) {
  onWorkflowMutationCallback = callback;
}

export function broadcastWorkflowMutation(payload) {
  if (!broadcastChannel) return;
  broadcastChannel.postMessage({
    type: "WORKFLOW_MUTATION",
    tabId: TAB_ID,
    timestamp: Date.now(),
    payload,
  });
}

function recordPeer(tabId) {
  peers.set(tabId, Date.now());
  recomputeLockState();
}

function dropPeer(tabId) {
  peers.delete(tabId);
  recomputeLockState();
}

function prunePeers() {
  const cutoff = Date.now() - PEER_TIMEOUT_MS;
  let removed = false;
  for (const [id, lastSeen] of peers) {
    if (lastSeen < cutoff) {
      peers.delete(id);
      removed = true;
    }
  }
  if (removed) recomputeLockState();
}

function recomputeLockState() {
  const next = peers.size > 0;
  if (next === S.hasMultipleTabs) return;
  S.hasMultipleTabs = next;
  updateTabLockUI();
}

export function initTabLock() {
  if (typeof BroadcastChannel === "undefined") {
    console.warn("BroadcastChannel not supported, tab lock disabled");
    return;
  }

  broadcastChannel = new BroadcastChannel(CHANNEL_NAME);

  broadcastChannel.postMessage({
    type: "TAB_OPENED",
    tabId: TAB_ID,
    timestamp: Date.now(),
  });

  broadcastChannel.onmessage = (event) => {
    const { type, tabId } = event.data;

    if (tabId === TAB_ID) return;

    switch (type) {
      case "TAB_OPENED":
        recordPeer(tabId);
        break;

      case "TAB_CLOSED":
        dropPeer(tabId);
        break;

      case "PING":
        broadcastChannel.postMessage({
          type: "PONG",
          tabId: TAB_ID,
          timestamp: Date.now(),
        });
        recordPeer(tabId);
        break;

      case "PONG":
        recordPeer(tabId);
        break;

      case "WORKFLOW_MUTATION":
        recordPeer(tabId);
        if (onWorkflowMutationCallback) {
          try {
            onWorkflowMutationCallback(event.data.payload);
          } catch (err) {
            console.warn("workflow mutation handler threw", err);
          }
        }
        break;
    }
  };

  broadcastChannel.postMessage({
    type: "PING",
    tabId: TAB_ID,
    timestamp: Date.now(),
  });

  heartbeatTimer = setInterval(() => {
    broadcastChannel.postMessage({
      type: "PING",
      tabId: TAB_ID,
      timestamp: Date.now(),
    });
    prunePeers();
  }, HEARTBEAT_MS);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || !broadcastChannel) return;
    broadcastChannel.postMessage({
      type: "PING",
      tabId: TAB_ID,
      timestamp: Date.now(),
    });
    prunePeers();
  });

  window.addEventListener("beforeunload", () => {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    if (broadcastChannel) {
      broadcastChannel.postMessage({
        type: "TAB_CLOSED",
        tabId: TAB_ID,
        timestamp: Date.now(),
      });
    }
  });

  updateTabLockUI();
}

function updateTabLockUI() {
  document.getElementById("main")?.classList.toggle("tab-locked", S.hasMultipleTabs);
}

export function requestSendPermission() {
  if (S.hasMultipleTabs) {
    toast("Please close other tabs of this app before sending messages.", true);
    return false;
  }
  return true;
}
