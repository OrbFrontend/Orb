// Per-message interactions: edit / edit-pending / edit-and-fork, director-log
// inspection, delete, branch switching, and the keyboard / touch branch
// navigation. Split out of chat.js; the public surface is re-exported from
// chat.js.
import { api, streamPost } from "./api.js";
import { canStartGeneration, renderMessages, setMessages } from "./chat_core.js";
import { renderInspector } from "./chat_inspector.js";
import {
  afterStream,
  agentPayload,
  createStreamingDiv,
  processSSEStream,
  setGenerationPhase,
  setStreaming,
} from "./chat_stream.js";
import { showConfirmModal } from "./modal.js";
import { S } from "./state.js";
import { $, convUrl, resolvePlaceholders, scrollToBottom, scrollToMessage, toast } from "./utils.js";
import { validate } from "./validate.js";

export function startEdit(msgId) {
  S.editingMsgId = msgId;
  S.forkEditMsgId = null;
  S.editingPendingUserMsg = false;
  renderMessages();
  // If editing the latest message, scroll to bottom so it's at the bottom of view.
  // Otherwise, center-focus on the message being edited.
  const msgEl = document.querySelector(`[data-msg-id="${msgId}"]`);
  const isLatest = msgEl && !msgEl.nextElementSibling;
  if (isLatest) {
    scrollToBottom(true);
  } else {
    scrollToMessage(msgId);
  }
  focusEditTextarea($("edit-textarea-" + msgId), cancelEdit);
  const msg = S.messages.find((m) => m.id === msgId);
  const inspectId =
    msg?.role === "assistant" ? msgId : S.messages.find((c) => c.parent_id === msgId && c.role === "assistant")?.id;
  if (inspectId) inspectMessage(inspectId);
}

export function cancelEdit() {
  S.editingMsgId = null;
  S.editingPendingUserMsg = false;
  renderMessages();
}

// Open the "Edit & Fork" textarea on a user message. Mirrors startEdit but
// targets a separate state flag; submitting (saveForkEdit) forks the
// conversation instead of editing in place.
export function startForkEdit(msgId) {
  S.forkEditMsgId = msgId;
  S.editingMsgId = null;
  S.editingPendingUserMsg = false;
  renderMessages();
  const msgEl = document.querySelector(`[data-msg-id="${msgId}"]`);
  const isLatest = msgEl && !msgEl.nextElementSibling;
  if (isLatest) {
    scrollToBottom(true);
  } else {
    scrollToMessage(msgId);
  }
  focusEditTextarea($("edit-textarea-" + msgId), cancelForkEdit);
  // Surface the director data for the reply that currently follows this message.
  const childAssistant = S.messages.find((c) => c.parent_id === msgId && c.role === "assistant");
  if (childAssistant) inspectMessage(childAssistant.id);
}

export function cancelForkEdit() {
  S.forkEditMsgId = null;
  renderMessages();
}

export async function inspectMessage(msgId) {
  if (!S.activeConvId) return;
  try {
    S.inspectedMsgId = msgId;
    S.inspectedDirectorData = await api.get(convUrl(S.activeConvId, "messages", msgId, "director-log"));
    S.reasoningDirector = S.inspectedDirectorData.reasoning_director || "";
    S.reasoningWriter = S.inspectedDirectorData.reasoning_writer || "";
    S.reasoningEditor = S.inspectedDirectorData.reasoning_editor || "";
    const highestPassIdx = S.reasoningEditor ? 2 : S.reasoningWriter ? 1 : 0;
    S.reasoningPassActive = highestPassIdx;
    S.reasoningPassSelected = highestPassIdx;
    S.reasoningUserOverride = false;
    renderInspector();
  } catch (e) {
    // If the log doesn't exist (e.g. very old messages before logs were added), silently ignore
    S.inspectedDirectorData = null;
    renderInspector();
  }
}

export function clearInspectedMessage() {
  S.inspectedMsgId = null;
  S.inspectedDirectorData = null;
  renderInspector();
}

function focusEditTextarea(ta, onEscape) {
  if (!ta) return;
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onEscape();
    }
  });
  ta.focus();
  ta.selectionStart = ta.selectionEnd = ta.value.length;
  ta.style.height = "auto";
  const lineH = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height = Math.max(lineH * 3, ta.scrollHeight) + "px";
}

export async function deleteMessage(msgId) {
  if (S.isStreaming) return;
  showConfirmModal(
    {
      title: "Delete Message",
      message: "Delete this message, all its siblings, and all their children?",
      confirmText: "Delete",
    },
    async () => {
      try {
        setMessages(await api.del(convUrl(S.activeConvId, "messages", msgId)));
        S.lastDirectorData = null;
        // Re-fetch director state so moods are correct after deletion
        S.directorState = await api.get(convUrl(S.activeConvId, "director"));
        renderMessages();
        clearInspectedMessage();
        scrollToBottom();
        toast("Message deleted");
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export async function switchBranch(msgId) {
  if (!msgId || S.isStreaming) return;
  try {
    // Use the parent user message as scroll anchor so the viewport doesn't jump
    const currentBranchMsg = S.messages.find((m) => m.next_branch_id === msgId || m.prev_branch_id === msgId);
    const anchorMsgId = currentBranchMsg?.parent_id ?? null;

    const ct = $("chat-messages");
    const anchorEl = anchorMsgId ? ct?.querySelector(`[data-msg-id="${anchorMsgId}"]`) : null;
    const anchorOffset = anchorEl ? anchorEl.offsetTop - ct.scrollTop : null;
    const scrollTop = ct ? ct.scrollTop : 0;

    setMessages(await api.post(convUrl(S.activeConvId, "messages", msgId, "switch-branch"), {}));
    S.lastDirectorData = null;
    // Re-fetch director state so moods are correct for this branch
    S.directorState = await api.get(convUrl(S.activeConvId, "director"));
    renderMessages();
    await inspectMessage(msgId);

    if (anchorMsgId && anchorOffset !== null) {
      const newAnchorEl = ct.querySelector(`[data-msg-id="${anchorMsgId}"]`);
      if (newAnchorEl) ct.scrollTop = newAnchorEl.offsetTop - anchorOffset;
      else ct.scrollTop = scrollTop;
    } else if (ct) {
      ct.scrollTop = scrollTop;
    }
  } catch (e) {
    toast(e.message, true);
  }
}

// Shared gate for arrow-key / touch-swipe branch navigation. Returns true if
// we should ignore the gesture entirely (typing, streaming, modal open, …).
function isChatNavBlocked(target) {
  if (target) {
    const tag = target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) return true;
  }
  if ($("modal-root")?.innerHTML || $("modal-crop-root")?.innerHTML) return true;
  if (!S.activeConvId) return true;
  if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg) return true;
  return false;
}

// Swipe to the prev (dir = -1) or next (dir = +1) branch of the last branched
// message. Returns true if a switch was issued.
function navigateLastBranch(dir) {
  if (S.isStreaming) return false;
  const msgs = S.messages || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if ((m.branch_count || 1) > 1) {
      const target = dir < 0 ? m.prev_branch_id : m.next_branch_id;
      if (target) {
        switchBranch(target);
        return true;
      }
      return false;
    }
  }
  return false;
}

// ── Keyboard navigation for the chat window:
// ←/→ swipe branches on the last branched message, ↑/↓ scroll the chat.
export function handleChatKeyNav(e) {
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const key = e.key;
  if (key !== "ArrowLeft" && key !== "ArrowRight" && key !== "ArrowUp" && key !== "ArrowDown") return;
  if (isChatNavBlocked(e.target)) return;

  if (key === "ArrowLeft" || key === "ArrowRight") {
    if (navigateLastBranch(key === "ArrowLeft" ? -1 : 1)) e.preventDefault();
    return;
  }

  const ct = $("chat-messages");
  if (!ct) return;
  e.preventDefault();
  ct.scrollTop += key === "ArrowUp" ? -60 : 60;
}

// ── Touch swipe navigation: horizontal swipe on the chat area switches
// branches, mirroring the ←/→ keyboard behavior. Vertical-dominant motion is
// ignored so scrolling still works.
export function initChatSwipeNav() {
  const ct = $("chat-messages");
  if (!ct) return;

  const SWIPE_MIN_DX = 50; // px of horizontal travel required
  const SWIPE_MAX_DT = 600; // ms — anything slower is treated as a scroll
  const SWIPE_RATIO = 1.5; // |dx| must exceed |dy| by this factor

  let startX = 0;
  let startY = 0;
  let startT = 0;
  let active = false;

  ct.addEventListener(
    "touchstart",
    (e) => {
      if (e.touches.length !== 1) {
        active = false;
        return;
      }
      // Let taps on the existing swipe buttons / toolbar pass through normally
      const tgt = e.target;
      if (tgt?.closest?.(".swipe-nav, .msg-toolbar, .msg-edit-area, button, a, input, textarea")) {
        active = false;
        return;
      }
      if (isChatNavBlocked(tgt)) {
        active = false;
        return;
      }
      const t = e.touches[0];
      startX = t.clientX;
      startY = t.clientY;
      startT = Date.now();
      active = true;
    },
    { passive: true },
  );

  ct.addEventListener(
    "touchend",
    (e) => {
      if (!active) return;
      active = false;
      const t = e.changedTouches[0];
      if (!t) return;
      const dx = t.clientX - startX;
      const dy = t.clientY - startY;
      const dt = Date.now() - startT;
      if (dt > SWIPE_MAX_DT) return;
      if (Math.abs(dx) < SWIPE_MIN_DX) return;
      if (Math.abs(dx) < Math.abs(dy) * SWIPE_RATIO) return;
      if (isChatNavBlocked(e.target)) return;
      // Swipe left (finger moves left → dx < 0) advances to next, like ▶.
      navigateLastBranch(dx < 0 ? 1 : -1);
    },
    { passive: true },
  );
}

// ── Edit Message
export async function saveEdit(msgId, role) {
  const ta = $("edit-textarea-" + msgId);
  if (!ta) return;
  const content = ta.value;
  const validation = validate.validateEditMessage(content);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  S.editingMsgId = null;
  S.editingPendingUserMsg = false;

  // The /edit route blocks on the per-conversation stream lock for the whole
  // turn (any stream: send, regen, super-regen, magic-rewrite, fork-edit), so
  // awaiting a POST mid-stream would hang Save with no feedback. Queue the edit
  // by message id and let afterStream() persist it once the lock frees; reflect
  // it locally right away. (The id-less pending message goes via saveEditPending.)
  if (S.isStreaming) {
    const idx = S.messages.findIndex((m) => m.id === msgId);
    if (idx >= 0) S.messages[idx].content = content;
    S.queuedEdits[msgId] = content;
    renderMessages();
    return;
  }

  try {
    await api.post(convUrl(S.activeConvId, "messages", msgId, "edit"), { content, regenerate: false });
    // setMessages preserves any id-less pending entries during streaming, so a
    // refetch here won't evict an unpersisted user bubble.
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    toast("Message edited");
  } catch (e) {
    toast(e.message, true);
  }
}

// Submit an "Edit & Fork": persist the edited text as a new sibling of the
// user message and stream a fresh reply. Modeled on sendMessage — an optimistic
// sibling bubble is spliced in front of the original and S.streamCutoffIndex
// hides the original branch while the new one streams; afterStream re-syncs to
// the server's canonical path. The trailing renderMessages() guarantees the
// user row repaints with its sibling swipe-nav (afterStream's in-place finalize
// fast path only adds nav to the assistant bubble).
export async function saveForkEdit(msgId) {
  const ta = $("edit-textarea-" + msgId);
  if (!ta) return;
  const content = ta.value;
  const validation = validate.validateEditMessage(content);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  if (!S.activeConvId || !canStartGeneration()) return;

  const original = S.messages.find((m) => m.id === msgId);
  const resolved = resolvePlaceholders(content.trim());
  S.forkEditMsgId = null;

  // Optimistic sibling inserted just before the original; cut off rendering
  // there so the original message and its descendants are hidden mid-stream.
  const idx = S.messages.findIndex((m) => m.id === msgId);
  const userMsg = {
    role: "user",
    content: resolved,
    id: null,
    branch_count: 1,
    branch_index: 0,
    prev_branch_id: null,
    next_branch_id: null,
    user_attachments: original?.user_attachments ? [...original.user_attachments] : [],
  };
  if (idx >= 0) {
    S.messages.splice(idx, 0, userMsg);
    S.streamCutoffIndex = idx + 1;
  } else {
    S.messages.push(userMsg);
    S.streamCutoffIndex = S.messages.length;
  }
  S.pendingUserMsg = userMsg;
  S.autoscrollEnabled = true;

  setStreaming(true);
  setGenerationPhase("pending");
  $("send-btn").disabled = true;
  renderMessages();

  const ct = $("chat-messages");
  const msgDiv = createStreamingDiv();
  if (!S.hideUntilBaked) ct.appendChild(msgDiv);
  scrollToBottom();

  S.abortController = new AbortController();
  try {
    const resp = await streamPost(
      convUrl(S.activeConvId, "messages", msgId, "fork-edit"),
      { content: resolved, ...agentPayload() },
      S.abortController.signal,
    );
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === "AbortError") S.wasAborted = true;
    else toast("Error: " + e.message, true);
  }
  await afterStream();
  renderMessages();
}

// ── Edit Pending Message
export function startEditPending() {
  S.editingPendingUserMsg = true;
  S.editingMsgId = null;
  S.forkEditMsgId = null;
  renderMessages();
  focusEditTextarea($("edit-textarea-pending"), cancelEditPending);
}

export async function saveEditPending() {
  const ta = $("edit-textarea-pending");
  if (!ta) return;
  const content = ta.value;
  const validation = validate.validateEditMessage(content);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  const trimmed = content.trim();
  S.editingPendingUserMsg = false;

  // Update the pending message in S.messages so the UI reflects the edit immediately
  const pendingIdx = S.messages.findLastIndex((m) => m.role === "user" && !m.id);
  if (pendingIdx >= 0) {
    S.messages[pendingIdx].content = trimmed;
  }

  // If the message already has a backend ID, save immediately; otherwise queue for later
  const lastUser = S.messages.findLast((m) => m.role === "user");
  if (lastUser?.id) {
    saveEdit(lastUser.id, "user");
    return;
  }
  S.pendingUserMsgEdit = trimmed;

  renderMessages();
}

export function cancelEditPending() {
  S.editingPendingUserMsg = false;
  renderMessages();
}
