import { api } from "./api.js";
import {
  canStartGeneration,
  ensureIndexInWindow,
  RENDER_WINDOW_SIZE,
  renderMessages,
  setMessages,
} from "./chat_core.js";
import { clearWorkflowPhase, renderInspector, setWorkflowPhase } from "./chat_inspector.js";
import { runStreamRequest, turnPayload } from "./chat_stream.js";
import { renderDirectionNotesPanel } from "./direction_notes_panel.js";
import { confirmDelete } from "./modal.js";
import { isUtilityPanelOpen } from "./panels.js";
import { sseEvents, streamPost } from "./sse.js";
import { S } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import {
  $,
  convUrl,
  formatProse,
  initChatScrollFollow,
  resolvePlaceholders,
  scrollToBottom,
  scrollToMessage,
  setChatFollowing,
  toast,
} from "./utils.js";
import { validate } from "./validate.js";

export function startEdit(msgId) {
  S.editingMsgId = msgId;
  S.forkEditMsgId = null;
  S.editingPendingUserMsg = false;
  ensureIndexInWindow(S.messages.findIndex((m) => m.id === msgId));
  renderMessages();
  focusEditTextarea($(`edit-textarea-${msgId}`), cancelEdit);
  scrollToMessage(msgId);
}

export function cancelEdit() {
  const msgId = S.editingMsgId;
  S.editingMsgId = null;
  S.editingPendingUserMsg = false;
  renderMessages();
  if (msgId != null) scrollToMessage(msgId);
}

export function startForkEdit(msgId) {
  S.forkEditMsgId = msgId;
  S.editingMsgId = null;
  S.editingPendingUserMsg = false;
  ensureIndexInWindow(S.messages.findIndex((m) => m.id === msgId));
  renderMessages();
  focusEditTextarea($(`edit-textarea-${msgId}`), cancelForkEdit);
  scrollToMessage(msgId);
  const childAssistant = S.messages.find((c) => c.parent_id === msgId && c.role === "assistant");
  if (childAssistant) inspectMessage(childAssistant.id);
}

export function cancelForkEdit() {
  const msgId = S.forkEditMsgId;
  S.forkEditMsgId = null;
  renderMessages();
  if (msgId != null) scrollToMessage(msgId);
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
  } catch (_e) {
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
  ta.focus({ preventScroll: true });
  ta.selectionStart = ta.selectionEnd = ta.value.length;
  ta.style.height = "auto";
  const lineH = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height = `${Math.max(lineH * 3, ta.scrollHeight)}px`;
  const messageEl = ta.closest(".message");
  if (messageEl) messageEl.style.containIntrinsicSize = `auto ${messageEl.offsetHeight}px`;
}

export async function deleteMessage(msgId) {
  if (S.isStreaming) return;
  if (!requestSendPermission()) return;
  let detail = "Delete this message, all its siblings, and all their children?";
  if (S.groupCast) {
    try {
      const preview = await api.get(convUrl(S.activeConvId, "messages", msgId, "delete-preview"));
      const count = preview.assistant_count || 0;
      detail = `Delete this message, all its siblings, and all their children? This removes ${count} group ${count === 1 ? "reply" : "replies"}.`;
    } catch (_e) {}
  }
  confirmDelete("Message", detail, async () => {
    try {
      setMessages(await api.del(convUrl(S.activeConvId, "messages", msgId)));
      S.lastDirectorData = null;
      S.directorState = await api.get(convUrl(S.activeConvId, "director"));
      renderMessages();
      clearInspectedMessage();
      if (isUtilityPanelOpen("direction-notes-panel")) await renderDirectionNotesPanel();
      setChatFollowing(true);
      scrollToBottom();
      toast("Message deleted");
    } catch (e) {
      toast(e.message, true);
    }
  });
}

const PROSE_REWRITE_CHANNEL = "prose-rewrite";

export async function rewriteMessageProse(msgId) {
  if (!S.activeConvId || S.isStreaming || S.proseRewriteMsgId) return;
  if (!requestSendPermission()) return;
  const source = S.messages.find((m) => m.id === msgId)?.content || "";
  const abortController = new AbortController();
  const sendBtn = $("send-btn");
  const stopBtn = $("stop-btn");
  let completed = false;
  S.proseRewriteMsgId = msgId;
  S.abortController = abortController;
  sendBtn.disabled = true;
  sendBtn.style.display = "none";
  stopBtn.style.display = "flex";
  stopBtn.title = "Stop prose rewrite";
  renderMessages();
  setWorkflowPhase(PROSE_REWRITE_CHANNEL, "Rewriting prose…");
  try {
    const response = await streamPost(
      convUrl(S.activeConvId, "messages", msgId, "prose-rewrite"),
      {},
      abortController.signal,
    );
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      const error = new Error(body || `Orb returned HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    for await (const { event, data } of sseEvents(response.body, { signal: abortController.signal })) {
      if (event === "prose_rewrite_update") {
        try {
          applyProseRewriteSnapshot(msgId, JSON.parse(data).draft);
        } catch (_) {}
      } else if (event === "prose_rewrite_done") {
        const result = JSON.parse(data);
        completed = true;
        if (result.aborted) toast("Prose rewrite stopped");
        else if (result.warning) toast(`Prose rewriter didn't run: ${result.warning}`, true);
        applyProseRewriteSnapshot(msgId, result.content);
        setMessages(await api.get(convUrl(S.activeConvId, "messages")));
        if (!result.warning && !result.aborted) toast(result.changed ? "Message rewritten" : "No prose changes needed");
      } else if (event === "error") {
        throw new Error(data || "Prose rewrite failed");
      }
    }
    if (!completed) throw new Error("Prose rewrite stream ended before completion");
  } catch (e) {
    if (abortController.signal.aborted || e?.name === "AbortError") toast("Prose rewrite stopped");
    else if (e.status === 503) toast("Enable & download the Prose Rewriter in Settings → Local ML");
    else {
      console.error("prose rewrite failed", e);
      toast("Prose rewrite failed", true);
    }
  } finally {
    if (!completed) applyProseRewriteSnapshot(msgId, source);
    S.proseRewriteMsgId = null;
    if (S.abortController === abortController) S.abortController = null;
    sendBtn.disabled = false;
    sendBtn.style.display = "flex";
    stopBtn.style.display = "none";
    stopBtn.title = "Stop generation";
    clearWorkflowPhase(PROSE_REWRITE_CHANNEL);
    renderMessages();
    if (completed) scrollToMessage(msgId);
  }
}

function applyProseRewriteSnapshot(msgId, content) {
  const message = S.messages.find((m) => m.id === msgId);
  if (message) message.content = content;
  const body = document.querySelector(`#chat-messages .message[data-msg-id="${msgId}"] .msg-body`);
  if (body) body.innerHTML = formatProse(resolvePlaceholders(content));
}

export async function switchBranch(msgId) {
  if (!msgId || S.isStreaming) return;
  if (!requestSendPermission()) return;
  try {
    const currentBranchMsg = S.messages.find((m) => m.next_branch_id === msgId || m.prev_branch_id === msgId);
    const anchorMsgId = currentBranchMsg?.parent_id ?? null;

    const ct = $("chat-messages");
    const anchorEl = anchorMsgId ? ct?.querySelector(`[data-msg-id="${anchorMsgId}"]`) : null;
    const anchorOffset = anchorEl ? anchorEl.offsetTop - ct.scrollTop : null;
    const scrollTop = ct ? ct.scrollTop : 0;

    setMessages(await api.post(convUrl(S.activeConvId, "messages", msgId, "switch-branch"), {}));
    S.lastDirectorData = null;
    S.directorState = await api.get(convUrl(S.activeConvId, "director"));
    renderMessages();
    await inspectMessage(msgId);
    if (isUtilityPanelOpen("direction-notes-panel")) await renderDirectionNotesPanel();

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

function isChatNavBlocked(target) {
  if (S.documentMode) return true;
  if (target) {
    const tag = target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) return true;
  }
  if ($("modal-root")?.innerHTML || $("modal-crop-root")?.innerHTML) return true;
  if (!S.activeConvId) return true;
  if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg) return true;
  return false;
}

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

export function initChatKeyNav() {
  document.addEventListener("keydown", handleChatKeyNav);
}

const BACKFILL_TRIGGER = 200; // px from top
export function initAutoscroll() {
  const ct = $("chat-messages");
  if (!ct) return;
  initChatScrollFollow(ct, {
    onScroll: () => {
      if (S.renderWindowStart > 0 && ct.scrollTop <= BACKFILL_TRIGGER) {
        S.renderWindowStart = Math.max(0, S.renderWindowStart - RENDER_WINDOW_SIZE);
        renderMessages();
      }
    },
  });
}

export function initChatSwipeNav() {
  const ct = $("chat-messages");
  if (!ct) return;

  const SWIPE_MIN_DX = 50; // minimum horizontal travel in px
  const SWIPE_MAX_DT = 600; // maximum gesture duration in ms
  const SWIPE_RATIO = 1.5; // horizontal-to-vertical travel ratio

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
      navigateLastBranch(dx < 0 ? 1 : -1);
    },
    { passive: true },
  );
}

function readEditDraft(textareaId) {
  const ta = $(textareaId);
  if (!ta) return null;
  const validation = validate.validateEditMessage(ta.value);
  if (!validation.valid) {
    toast(validation.error, true);
    return null;
  }
  return ta.value;
}

export async function saveEdit(msgId, _role) {
  if (!requestSendPermission()) return;
  const content = readEditDraft(`edit-textarea-${msgId}`);
  if (content === null) return;
  S.editingMsgId = null;
  S.editingPendingUserMsg = false;

  if (S.isStreaming) {
    const idx = S.messages.findIndex((m) => m.id === msgId);
    if (idx >= 0) S.messages[idx].content = content;
    S.queuedEdits[msgId] = content;
    renderMessages();
    return;
  }

  try {
    await api.post(convUrl(S.activeConvId, "messages", msgId, "edit"), { content, regenerate: false });
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    scrollToMessage(msgId);
    toast("Message edited");
  } catch (e) {
    toast(e.message, true);
  }
}

export async function saveForkEdit(msgId) {
  const content = readEditDraft(`edit-textarea-${msgId}`);
  if (content === null) return;
  if (!S.activeConvId || !canStartGeneration()) return;

  const original = S.messages.find((m) => m.id === msgId);
  const resolved = resolvePlaceholders(content.trim());
  S.forkEditMsgId = null;

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

  await runStreamRequest(
    convUrl(S.activeConvId, "messages", msgId, "fork-edit"),
    { content: resolved, ...turnPayload() },
    {
      beforeRender() {
        if (idx >= 0) {
          S.messages.splice(idx, 0, userMsg);
          S.streamCutoffIndex = idx + 1;
        } else {
          S.messages.push(userMsg);
          S.streamCutoffIndex = S.messages.length;
        }
        S.pendingUserMsg = userMsg;
        setChatFollowing(true);
      },
      anchorStream: true,
      afterDone: renderMessages,
    },
  );
}

export function startEditPending() {
  S.editingPendingUserMsg = true;
  S.editingMsgId = null;
  S.forkEditMsgId = null;
  renderMessages();
  focusEditTextarea($("edit-textarea-pending"), cancelEditPending);
}

export async function saveEditPending() {
  const content = readEditDraft("edit-textarea-pending");
  if (content === null) return;
  const trimmed = content.trim();
  S.editingPendingUserMsg = false;

  const pendingIdx = S.messages.findLastIndex((m) => m.role === "user" && !m.id);
  if (pendingIdx >= 0) {
    S.messages[pendingIdx].content = trimmed;
  }

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
