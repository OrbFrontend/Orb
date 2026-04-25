// Tab lock module to prevent multiple tabs from sending input simultaneously
import { S } from "./state.js";
import { toast } from "./utils.js";

const CHANNEL_NAME = "orb-tab-lock";
const TAB_ID = `${Date.now()}-${Math.random()}`;

let broadcastChannel = null;
let onLockStateChange = null;

// Register a callback to be called when the lock state changes
export function setLockStateChangeCallback(callback) {
  onLockStateChange = callback;
}

// Initialize the tab lock system
export function initTabLock() {
  // Check if BroadcastChannel is supported
  if (typeof BroadcastChannel === "undefined") {
    console.warn("BroadcastChannel not supported, tab lock disabled");
    return;
  }

  broadcastChannel = new BroadcastChannel(CHANNEL_NAME);

  // Announce presence to other tabs
  broadcastChannel.postMessage({
    type: "TAB_OPENED",
    tabId: TAB_ID,
    timestamp: Date.now(),
  });

  // Listen for messages from other tabs
  broadcastChannel.onmessage = (event) => {
    const { type, tabId, timestamp } = event.data;

    // Ignore our own messages
    if (tabId === TAB_ID) return;

    switch (type) {
      case "TAB_OPENED":
        // Another tab opened, we now have multiple tabs
        S.hasMultipleTabs = true;
        updateTabLockUI();
        break;

      case "TAB_CLOSED":
        // A tab closed, check if we're the only one left
        // We'll handle this with a heartbeat mechanism
        break;

      case "PING":
        // Respond to ping to confirm we're still here
        broadcastChannel.postMessage({
          type: "PONG",
          tabId: TAB_ID,
          timestamp: Date.now(),
        });
        S.hasMultipleTabs = true;
        updateTabLockUI();
        break;
    }
  };

  // Send initial ping to check for existing tabs
  broadcastChannel.postMessage({
    type: "PING",
    tabId: TAB_ID,
    timestamp: Date.now(),
  });

  // Handle tab close
  window.addEventListener("beforeunload", () => {
    if (broadcastChannel) {
      broadcastChannel.postMessage({
        type: "TAB_CLOSED",
        tabId: TAB_ID,
        timestamp: Date.now(),
      });
    }
  });

  // Update UI initially
  updateTabLockUI();
}

// Check if the current tab can send input
export function canSendInput() {
  return !S.hasMultipleTabs;
}

// Update the UI to reflect tab lock status
function updateTabLockUI() {
  const banner = document.getElementById("tab-lock-banner");
  const chatInput = document.getElementById("chat-input");
  const sendBtn = document.getElementById("send-btn");

  if (S.hasMultipleTabs) {
    // Show warning banner
    if (banner) {
      banner.classList.remove("hidden");
    }
    // Disable input if there's an active conversation
    if (chatInput && S.activeConvId) {
      chatInput.disabled = true;
      chatInput.placeholder = "Multiple tabs detected. Close other tabs to continue.";
    }
    if (sendBtn) {
      sendBtn.disabled = true;
    }
  } else {
    // Hide warning banner
    if (banner) {
      banner.classList.add("hidden");
    }
    // Re-enable input if there's an active conversation and not streaming
    if (chatInput && S.activeConvId && !S.isStreaming) {
      chatInput.disabled = false;
      chatInput.placeholder = "Write your message...";
    }
    if (sendBtn && S.activeConvId && !S.isStreaming) {
      sendBtn.disabled = false;
    }
  }

  // Notify subscribers that lock state changed
  if (onLockStateChange) {
    onLockStateChange(S.hasMultipleTabs);
  }
}

// Request permission to send (call this before sendMessage)
export function requestSendPermission() {
  if (S.hasMultipleTabs) {
    toast("Please close other tabs of this app before sending messages.", true);
    return false;
  }
  return true;
}
