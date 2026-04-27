import { S } from "./state.js";
import {
  $,
  esc,
  formatProse,
  formatProseWithDiff,
  sentenceDiff,
  toast,
  scrollToBottom,
  scrollToMessage,
  avatarUrl,
  convUrl,
  formatRelativeDate,
  resolvePlaceholders,
} from "./utils.js";
import { api } from "./api.js";
import { showModal, closeModal, showConfirmModal } from "./modal.js";
import { renderCharacters, loadCharacters, refreshCharacters } from "./library.js";
import { activateAndPrioritizeWorld, deactivateWorld } from "./lorebooks.js";
import { validate } from "./validate.js";
import { requestSendPermission } from "./tabLock.js";

function canStartGeneration() {
  if (S.isStreaming) return false;
  return requestSendPermission();
}

function normalizeMessages(msgs) {
  if (!Array.isArray(msgs)) return msgs;
  for (const m of msgs) {
    if (m.attachments && Array.isArray(m.attachments)) {
      for (const att of m.attachments) {
        if (att.data_b64 != null && att.b64 == null) att.b64 = att.data_b64;
        if (att.mime_type != null && att.mime == null) att.mime = att.mime_type;
      }
    }
  }
  return msgs;
}

const ICON_EDIT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
const ICON_REGEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>`;
const ICON_DEL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`;
const ICON_CLEAR = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/></svg>`;

// ── Attachments rendering
function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function renderAttachments(attachments) {
  if (!attachments || attachments.length === 0) return "";
  const items = attachments
    .map((att) => {
      const b64 = att.b64 || att.data_b64 || "";
      const mime = att.mime || att.mime_type || "image/jpeg";
      const filename = att.filename || "image";
      const size = att.size || 0;
      return `
    <div class="attachment-item">
      <img src="data:${mime};base64,${b64}" alt="${esc(filename)}">
      <div class="attachment-info">
        <div class="attachment-name">${esc(filename)}</div>
        <div class="attachment-size">${formatBytes(size)}</div>
      </div>
    </div>
  `;
    })
    .join("");
  return `<div class="attachments">${items}</div>`;
}

// ── Generation Phase
const PHASE_ORDER = { pending: 0, directing: 0, generating: 1, refining: 2 };
const PHASE_LABELS = {
  pending: "Waiting for response…",
  directing: "Director analyzing scene…",
  generating: "Generating response…",
  refining: "Refining response…",
};
let _refineTimer = null;

function setGenerationPhase(phase) {
  if (!phase) {
    S.generationPhase = null;
  } else if (S.generationPhase && PHASE_ORDER[phase] < PHASE_ORDER[S.generationPhase]) {
    return; // never go backwards
  } else {
    S.generationPhase = phase;
  }
  const el = $("generation-status");
  if (!S.generationPhase) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.querySelector(".gen-text").textContent = PHASE_LABELS[S.generationPhase] || "Processing…";
  el.querySelector(".gen-dot").className = "gen-dot" + (S.generationPhase === "refining" ? " spin" : "");
}

function smoothUpdateBody(el, newHtml, onComplete) {
  if (!el || el.innerHTML === newHtml) return;
  const prev = el.offsetHeight;
  el.innerHTML = newHtml;
  const next = el.scrollHeight;
  if (Math.abs(next - prev) > 4) {
    el.style.height = prev + "px";
    el.style.overflow = "hidden";
    el.offsetHeight; // force reflow
    el.style.transition = "height 0.3s ease";
    el.style.height = next + "px";
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      el.style.height = "";
      el.style.overflow = "";
      el.style.transition = "";
      onComplete?.();
    };
    el.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 350); // fallback
  } else {
    onComplete?.();
  }
}

function finalizeStreamingDiv(lastMsg) {
  const body = S.streamingBodyEl;
  if (!body) return false;
  const div = body.closest(".message");
  if (!div || !div.isConnected || !lastMsg || lastMsg.role !== "assistant" || !lastMsg.id) return false;

  div.setAttribute("data-msg-id", lastMsg.id);
  body.removeAttribute("id");

  const bodyHtml =
    S.pendingRefineDiff && S.showEditorDiff
      ? formatProseWithDiff(S.pendingRefineDiff.ops)
      : formatProse(resolvePlaceholders(lastMsg.content));
  smoothUpdateBody(body, bodyHtml, () => scrollToBottom(true));

  const tb = div.querySelector(".msg-toolbar");
  if (tb) {
    const diffBtn =
      S.pendingRefineDiff?.msgId && lastMsg.id === S.pendingRefineDiff.msgId && S.showEditorDiff
        ? `<button onclick="clearRefineDiff()" title="Clear diff highlights" class="btn-clear-diff">✕ Diff</button>`
        : "";
    const editBtn = S.hasMultipleTabs
      ? `<button disabled title="Close other tabs to edit">${ICON_EDIT}</button>`
      : `<button onclick="startEdit(${lastMsg.id})" title="Edit">${ICON_EDIT}</button>`;
    const regenBtn = S.hasMultipleTabs
      ? `<button disabled title="Close other tabs to regenerate">${ICON_REGEN}</button>`
      : `<button onclick="regenerate(${lastMsg.id})" title="Regenerate">${ICON_REGEN}</button>`;
    tb.innerHTML = `${editBtn}${regenBtn}<button onclick="deleteMessage(${lastMsg.id})" title="Delete message, siblings, and all children" class="msg-btn-del">${ICON_DEL}</button>${diffBtn}`;
  }

  const bc = lastMsg.branch_count || 1;
  if (bc > 1) {
    const bi = lastMsg.branch_index || 0;
    const roleEl = div.querySelector(".msg-role");
    if (roleEl && !roleEl.querySelector(".swipe-nav")) {
      roleEl.insertAdjacentHTML(
        "beforeend",
        `<span class="swipe-nav">
        <button onclick="event.stopPropagation();switchBranch(${lastMsg.prev_branch_id})" ${!lastMsg.prev_branch_id ? "disabled" : ""}>◀</button>
        <span class="swipe-counter">${bi + 1}/${bc}</span>
        <button onclick="event.stopPropagation();switchBranch(${lastMsg.next_branch_id})" ${!lastMsg.next_branch_id ? "disabled" : ""}>▶</button>
      </span>`,
      );
    }
  }

  return true;
}

function scheduleRefineTimer() {
  clearTimeout(_refineTimer);
  _refineTimer = setTimeout(() => {
    if (S.isStreaming && S.generationPhase === "generating") setGenerationPhase("refining");
  }, 1500);
}

function clearRefineTimer() {
  clearTimeout(_refineTimer);
  _refineTimer = null;
}

// ── Conversations
export async function loadConversations() {
  S.conversations = await api.get("/conversations");
}

export function resetChatUI() {
  S.activeCharId = null;
  S.activeConvId = null;
  S.messages = [];
  S.lastDirectorData = null;
  S.directorState = null;
  $("chat-title-text").textContent = "Select a character";
  $("chat-avatar").textContent = "📜";
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
  renderMessages();
  renderInspector();
}

export async function selectChar(id, source = "recent") {
  if (S.activeCharId === id || S._selectCharLock) return;
  S._selectCharLock = true;
  try {
    const oldWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
    S.activeCharId = id;
    renderCharacters();
    const existing = S.conversations.find((c) => c.character_card_id === id);
    if (existing) {
      // If selecting from library modal, bump conversation's updated_at
      if (source === "library") {
        try {
          await api.post(`/conversations/${existing.id}/touch`);
          // Update local conversation's updated_at to now
          existing.updated_at = new Date().toISOString();
        } catch (e) {
          // silently fail, not critical
          console.warn("Failed to touch conversation:", e);
        }
      }
      await selectConversation(existing.id);
    } else {
      try {
        const conv = await api.post("/conversations", { character_card_id: id });
        await loadConversations();
        await selectConversation(conv.id);
      } catch (e) {
        toast(e.message, true);
      }
    }
    const newWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
    if (oldWorldId && oldWorldId !== newWorldId) {
      deactivateWorld(oldWorldId);
    }
    // Refresh the recent characters panel to reflect updated timestamps
    refreshCharacters();
  } finally {
    S._selectCharLock = false;
  }
}

export async function newConvForChar(id) {
  try {
    const oldWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
    const conv = await api.post("/conversations", { character_card_id: id });
    await loadConversations();
    S.activeCharId = id;
    renderCharacters();
    await selectConversation(conv.id);
    const newWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
    if (oldWorldId && oldWorldId !== newWorldId) {
      deactivateWorld(oldWorldId);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

export async function selectConversation(id) {
  const oldWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
  S.activeConvId = id;
  S.lastDirectorData = null;
  const conv = S.conversations.find((c) => c.id === id);
  if (conv?.character_card_id && S.activeCharId !== conv.character_card_id) {
    S.activeCharId = conv.character_card_id;
    renderCharacters();
  }
  $("chat-title-text").textContent = conv ? conv.title || conv.character_name : "";
  const av = $("chat-avatar");
  if (conv?.character_card_id) {
    av.innerHTML = `<img src="${avatarUrl(conv.character_card_id)}?t=${Date.now()}" onerror="this.parentElement.textContent='📜'" onclick="showAvatarPopup()" style="cursor:pointer">`;
  } else {
    av.textContent = "📜";
  }
  $("chat-input").disabled = false;
  $("send-btn").disabled = false;

  // If the character has a linked lorebook, activate it and move it to the top
  if (conv?.character_card_id) {
    const char = (S.allCharacters || []).find((c) => c.id === conv.character_card_id);
    if (char?.world_id) {
      activateAndPrioritizeWorld(char.world_id);
    }
  }

  const newWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
  if (oldWorldId && oldWorldId !== newWorldId) {
    deactivateWorld(oldWorldId);
  }

  S.messages = normalizeMessages(await api.get(convUrl(id, "messages")));
  S.directorState = await api.get(convUrl(id, "director"));
  S.editingMsgId = null;
  renderMessages();
  renderInspector();
  scrollToBottom();
}

async function deleteConversation(id) {
  showConfirmModal(
    {
      title: "Delete Conversation",
      message: "Are you sure you want to delete this conversation?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/conversations/" + id);
        if (S.activeConvId === id) {
          S.activeConvId = null;
          S.messages = [];
          $("chat-input").disabled = true;
          $("send-btn").disabled = true;
          renderMessages();
        }
        await loadConversations();
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export async function deleteConversationFromModal(id) {
  showConfirmModal(
    {
      title: "Delete Conversation",
      message: "Are you sure you want to delete this conversation?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/conversations/" + id);
        if (S.activeConvId === id) {
          S.activeConvId = null;
          S.messages = [];
          $("chat-input").disabled = true;
          $("send-btn").disabled = true;
          renderMessages();
        }
        await showConvHistoryModal();
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export async function showConvHistoryModal() {
  if (!S.activeCharId) {
    toast("Select a character first", true);
    return;
  }
  await loadConversations();
  const convs = S.conversations.filter((c) => c.character_card_id === S.activeCharId);
  if (!convs.length) {
    toast("No conversations yet", true);
    return;
  }
  const char = S.characters.find((c) => c.id === S.activeCharId);
  const charName = char ? char.name : "Character";
  const items = convs
    .map((c) => {
      const isActive = c.id === S.activeConvId;
      const preview = esc((c.last_message_preview || "").substring(0, 80));
      const title = esc(c.title || c.character_name || "Untitled");
      const ts = c.updated_at || c.created_at;
      return `<div class="conv-history-item${isActive ? " active-conv" : ""}" onclick="closeModal();selectConversation('${c.id}')">
      <div class="conv-history-meta">
        <span class="conv-history-title">${title}</span>
        <span class="conv-history-date">${formatRelativeDate(ts)}</span>
        <button class="conv-history-delete" title="Delete conversation" onclick="event.stopPropagation();deleteConversationFromModal('${c.id}')">&#x2715;</button>
      </div>
      ${
        preview
          ? `<div class="conv-history-preview">${preview}</div>`
          : `<div class="conv-history-preview" style="color:var(--text-muted);font-style:italic">No messages yet</div>`
      }
    </div>`;
    })
    .join("");
  showModal(`
    <h2>Conversations — ${esc(charName)}</h2>
    <div style="margin:-8px -24px 0;max-height:60vh;overflow-y:auto;">${items}</div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`);
}

// ── Title Edit
let _titleEditBackup = "";

export function startEditTitle() {
  if (!S.activeConvId) return;
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  if (!conv) return;
  const area = $("chat-title-text");
  if (!area) return;
  _titleEditBackup = area.textContent;

  const input = document.createElement("input");
  input.type = "text";
  input.id = "chat-title-input";
  input.className = "chat-title-input";
  input.value = _titleEditBackup;
  input.addEventListener("keydown", handleTitleEditKey);
  input.addEventListener("blur", saveTitleEdit);

  area.replaceWith(input);
  input.focus();
  input.select();
}

export function handleTitleEditKey(e) {
  if (e.key === "Enter") {
    e.preventDefault();
    saveTitleEdit();
  }
  if (e.key === "Escape") {
    e.preventDefault();
    cancelTitleEdit();
  }
}

export async function saveTitleEdit() {
  const inp = $("chat-title-input");
  if (!inp) return;
  const newTitle = inp.value.trim();
  if (!newTitle) {
    cancelTitleEdit();
    return;
  }
  const validation = validate.validateConversationTitle(newTitle);
  if (!validation.valid) {
    toast(validation.error, true);
    cancelTitleEdit();
    return;
  }
  if (newTitle === _titleEditBackup) {
    cancelTitleEdit();
    return;
  }
  try {
    const updated = await api.put("/conversations/" + S.activeConvId, { title: newTitle });
    const conv = S.conversations.find((c) => c.id === S.activeConvId);
    if (conv) conv.title = updated.title;
    const div = document.createElement("div");
    div.className = "chat-title";
    div.id = "chat-title-text";
    div.textContent = updated.title || conv?.character_name || "";
    inp.replaceWith(div);
    _titleEditBackup = "";
    toast("Title updated");
  } catch (e) {
    toast(e.message, true);
    cancelTitleEdit();
  }
}

export function cancelTitleEdit() {
  const inp = $("chat-title-input");
  if (!inp) return;
  const div = document.createElement("div");
  div.className = "chat-title";
  div.id = "chat-title-text";
  div.textContent = _titleEditBackup;
  inp.replaceWith(div);
  _titleEditBackup = "";
}

// ── Messages
function getCharName() {
  const c = S.conversations.find((c) => c.id === S.activeConvId);
  return c?.character_name || "Assistant";
}

export function renderMessages() {
  const ct = $("chat-messages");
  const distFromBottom = ct.scrollHeight - ct.scrollTop - ct.clientHeight;
  let streamingEl = null;
  let badgeEl = null;
  if (S.isStreaming) {
    streamingEl = S.streamingBodyEl?.closest(".message") ?? null;
    badgeEl = document.getElementById("active-director-badge");
  }
  if (!S.activeConvId) {
    ct.innerHTML = '<div class="empty-state"><div class="icon">📜</div><div>Select a character to begin</div></div>';
  } else if (!S.messages.length) {
    ct.innerHTML =
      '<div class="empty-state"><div class="icon">📜</div><div>Start writing to begin the scene</div></div>';
  } else {
    let msgs = S.messages;
    if (S.isStreaming && S.streamCutoffIndex != null) {
      msgs = S.messages.slice(0, S.streamCutoffIndex);
    }
    ct.innerHTML = msgs
      .map((m) => {
        const isEditing = S.editingMsgId !== null && S.editingMsgId === m.id;
        const bc = m.branch_count || 1;
        const bi = m.branch_index || 0;
        const branchHtml =
          bc > 1
            ? `
        <span class="swipe-nav">
          <button onclick="event.stopPropagation();switchBranch(${m.prev_branch_id})" ${!m.prev_branch_id ? "disabled" : ""}>◀</button>
          <span class="swipe-counter">${bi + 1}/${bc}</span>
          <button onclick="event.stopPropagation();switchBranch(${m.next_branch_id})" ${!m.next_branch_id ? "disabled" : ""}>▶</button>
        </span>`
            : "";
        const editBtn =
          S.hasMultipleTabs || !m.id
            ? `<button disabled title="${S.hasMultipleTabs ? "Close other tabs to edit" : ""}">${ICON_EDIT}</button>`
            : `<button onclick="startEdit(${m.id})" title="Edit">${ICON_EDIT}</button>`;
        const childAssistant =
          m.role === "user" ? S.messages.find((child) => child.parent_id === m.id && child.role === "assistant") : null;
        const regenTargetId = m.role === "assistant" ? m.id : childAssistant?.id;
        const canRegen = m.role === "assistant" || childAssistant || (m.role === "user" && m.id);
        const regenBtn =
          S.hasMultipleTabs || !canRegen
            ? `<button disabled title="${S.hasMultipleTabs ? "Close other tabs to regenerate" : ""}">${ICON_REGEN}</button>`
            : `<button onclick="${regenTargetId ? `regenerate(${regenTargetId})` : `continueFromUser()`}" title="Regenerate">${ICON_REGEN}</button>`;
        const delBtn = m.id
          ? `<button onclick="deleteMessage(${m.id})" title="Delete message, siblings, and all children" class="msg-btn-del">${ICON_DEL}</button>`
          : `<button disabled class="msg-btn-del">${ICON_DEL}</button>`;
        const diffBtn =
          S.pendingRefineDiff?.msgId && m.id === S.pendingRefineDiff.msgId && S.showEditorDiff
            ? `<button onclick="clearRefineDiff()" title="Clear diff highlights" class="btn-clear-diff">${ICON_CLEAR}</button>`
            : "";
        const toolbar = isEditing
          ? ""
          : `
        <div class="msg-toolbar">
          ${editBtn}${regenBtn}${delBtn}${diffBtn}
        </div>`;
        const body = isEditing
          ? `
        <div class="msg-edit-area">
          <textarea id="edit-textarea-${m.id}" rows="5">${esc(m.content)}</textarea>
          <div class="msg-edit-actions">
            <button class="btn btn-sm" onclick="cancelEdit()">Cancel</button>
            <button class="btn btn-sm btn-accent" onclick="saveEdit(${m.id},'${m.role}')">Save</button>
          </div>
        </div>`
          : `<div class="msg-body">${
              S.pendingRefineDiff?.msgId && m.id === S.pendingRefineDiff.msgId && S.showEditorDiff
                ? formatProseWithDiff(S.pendingRefineDiff.ops)
                : formatProse(resolvePlaceholders(m.content))
            }</div>`;
        const attachmentsHtml = renderAttachments(m.attachments);
        return `<div class="message ${m.role}" data-msg-id="${m.id}">
        <div class="msg-role">${m.role === "user" ? "You" : esc(getCharName())} ${branchHtml}</div>
        ${body}${attachmentsHtml}${toolbar}
      </div>`;
      })
      .join("");
  }
  if (badgeEl) ct.appendChild(badgeEl);
  // Don't show streaming box when editing a message (looks ugly)
  // Also hide for a short time after cancelling edit during streaming
  if (streamingEl && !S.editingMsgId && !S.hideStreamingBox && !S.hideUntilBaked) ct.appendChild(streamingEl);
  // Restore scroll position synchronously so the browser never paints a jump.
  // Near-bottom → snap to bottom; otherwise preserve distance from bottom.
  if (distFromBottom <= 50) {
    ct.scrollTop = ct.scrollHeight;
  } else {
    ct.scrollTop = Math.max(0, ct.scrollHeight - ct.clientHeight - distFromBottom);
  }
}

export function startEdit(msgId) {
  S.editingMsgId = msgId;
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
  const ta = $("edit-textarea-" + msgId);
  if (ta) {
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        cancelEdit();
      }
    });
    ta.focus();
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    ta.style.height = "auto";
    const lineH = parseFloat(getComputedStyle(ta).lineHeight) || 20;
    ta.style.height = Math.max(lineH * 3, ta.scrollHeight) + "px";
  }
}

export function cancelEdit() {
  // If streaming is active, hide the streaming box for a short time after cancelling edit
  if (S.isStreaming) {
    S.hideStreamingBox = true;
    // Clear the flag after 2 seconds or when streaming ends (whichever comes first)
    setTimeout(() => {
      S.hideStreamingBox = false;
    }, 2000);
  }
  S.editingMsgId = null;
  renderMessages();
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
        S.messages = normalizeMessages(await api.del(convUrl(S.activeConvId, "messages", msgId)));
        S.lastDirectorData = null;
        // Re-fetch director state so moods are correct after deletion
        S.directorState = await api.get(convUrl(S.activeConvId, "director"));
        renderMessages();
        renderInspector();
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
    // Save current scroll position before rendering
    const ct = $("chat-messages");
    const scrollTop = ct ? ct.scrollTop : 0;

    S.messages = normalizeMessages(await api.post(convUrl(S.activeConvId, "messages", msgId, "switch-branch"), {}));
    S.lastDirectorData = null;
    // Re-fetch director state so moods are correct for this branch
    S.directorState = await api.get(convUrl(S.activeConvId, "director"));
    renderMessages();
    renderInspector();

    // Restore scroll position instead of scrolling to bottom
    if (ct) ct.scrollTop = scrollTop;
  } catch (e) {
    toast(e.message, true);
  }
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
  const trimmed = content.trim();
  S.editingMsgId = null;

  try {
    await api.post(convUrl(S.activeConvId, "messages", msgId, "edit"), { content, regenerate: false });
    S.messages = normalizeMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    toast("Message edited");
  } catch (e) {
    toast(e.message, true);
  }
}

// ── Streaming Helpers
function setStreaming(active) {
  S.isStreaming = active;
  $("send-btn").style.display = active ? "none" : "flex";
  $("stop-btn").style.display = active ? "flex" : "none";
  const cm = $("chat-messages");
  if (cm) cm.classList.toggle("streaming", active);
}

export function stopGeneration() {
  if (S.abortController) S.abortController.abort();
  if (S.activeConvId) {
    fetch("/api" + convUrl(S.activeConvId, "stop"), { method: "POST" }).catch(() => {});
  }
}

function createStreamingDiv() {
  const div = document.createElement("div");
  div.className = "message assistant";
  div.innerHTML = `<div class="msg-role">${esc(getCharName())}</div>
    <div class="msg-body" id="streaming-body">
      <span class="typing-indicator"><span></span><span></span><span></span></span>
    </div>
    <div class="msg-toolbar">
      <button disabled>${ICON_EDIT}</button>
      <button disabled>${ICON_REGEN}</button>
      <button disabled class="msg-btn-del">${ICON_DEL}</button>
    </div>`;
  S.streamingBodyEl = div.querySelector(".msg-body");
  return div;
}

function patchPendingUserMessage(pendingMsg) {
  const freshMsg = S.messages.find((m) => m.role === "user" && m.id && m.content === pendingMsg.content);
  if (!freshMsg) return;
  const div = document.querySelector('.message.user[data-msg-id="null"]');
  if (!div) return;
  div.setAttribute("data-msg-id", freshMsg.id);
  const tb = div.querySelector(".msg-toolbar");
  if (tb) {
    const childAssistant = S.messages.find((m) => m.parent_id === freshMsg.id && m.role === "assistant");
    const regenTargetId = childAssistant?.id;
    const regenBtn = S.hasMultipleTabs
      ? `<button disabled title="Close other tabs to regenerate">${ICON_REGEN}</button>`
      : `<button onclick="${regenTargetId ? `regenerate(${regenTargetId})` : `continueFromUser()`}" title="Regenerate">${ICON_REGEN}</button>`;
    tb.innerHTML = `<button onclick="startEdit(${freshMsg.id})" title="Edit">${ICON_EDIT}</button>${regenBtn}<button onclick="deleteMessage(${freshMsg.id})" title="Delete message, siblings, and all children" class="msg-btn-del">${ICON_DEL}</button>`;
  }
}

async function afterStream() {
  const preservedContent = S.streamingContent;
  const pendingUserMsg = S.pendingUserMsg || null;
  const wasAborted = S.wasAborted;
  S.abortController = null;
  S.streamCutoffIndex = null;
  S.streamingContent = null;
  S.pendingUserMsg = null;
  S.wasAborted = false;
  S.hideStreamingBox = false; // Ensure streaming box is visible after streaming ends
  clearRefineTimer();
  setGenerationPhase(null);

  if (!S.activeConvId) {
    S.streamingBodyEl = null;
    setStreaming(false);
    $("send-btn").disabled = false;
    renderMessages();
    renderInspector();
    return;
  }

  if (wasAborted) {
    await new Promise((r) => setTimeout(r, 500));
  }

  try {
    S.messages = normalizeMessages(await api.get(convUrl(S.activeConvId, "messages")));
    S.directorState = await api.get(convUrl(S.activeConvId, "director"));
    // Update the conversation's updated_at timestamp so refreshCharacters() can
    // correctly place the active character at the top of the recent list.
    if (S.activeConvId) {
      const conv = S.conversations?.find((c) => c.id === S.activeConvId);
      if (conv) conv.updated_at = new Date().toISOString();
    }
  } catch (e) {
    toast("Failed to sync messages: " + e.message, true);
  }

  if (pendingUserMsg) {
    const hasUserMsg = S.messages.some((m) => m.role === "user" && m.content === pendingUserMsg.content);
    if (!hasUserMsg) S.messages.push(pendingUserMsg);
  }

  if (preservedContent?.trim()) {
    const lastMsg = S.messages[S.messages.length - 1];
    if (!lastMsg || lastMsg.role !== "assistant") {
      S.messages.push({
        role: "assistant",
        content: preservedContent,
        id: null,
        branch_count: 1,
        branch_index: 0,
        prev_branch_id: null,
        next_branch_id: null,
      });
    }
  }

  setStreaming(false);
  $("send-btn").disabled = false;

  // Anchor the pending diff to the specific message ID it was generated for,
  // so branch navigation doesn't show stale diffs on the wrong message.
  if (S.pendingRefineDiff) {
    const lastAssistant = [...S.messages].reverse().find((m) => m.role === "assistant" && m.id);
    S.pendingRefineDiff.msgId = lastAssistant?.id ?? null;
  }

  // Finalize the streaming div in-place — no DOM destruction, no flash
  const lastMsg = S.messages[S.messages.length - 1];
  const finalized = finalizeStreamingDiv(lastMsg);
  S.streamingBodyEl = null;

  if (finalized) {
    // Streaming div already updated in-place — no full re-render needed.
    // Only patch the pending user message if one exists (sendMessage path).
    if (pendingUserMsg) patchPendingUserMessage(pendingUserMsg);
  } else {
    renderMessages();
  }
  renderInspector();
  scrollToBottom(true);
  refreshCharacters();
}

async function processSSEStream(resp, container, msgDiv, signal) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "",
    fullResponse = "",
    rewrittenResponse = null,
    firstToken = true,
    currentEvent = null;

  // Clear any diff from the previous turn
  S.pendingRefineDiff = null;

  // Reset reasoning state for this generation turn
  S.reasoningDirector = "";
  S.reasoningWriter = "";
  S.reasoningEditor = "";
  S.reasoningPassActive = 0; // tracks streaming progress (for dot lighting)
  S.reasoningPassSelected = 0; // tracks what the user is viewing
  S.reasoningUserOverride = false; // true when user has manually clicked a dot

  if (signal) signal.addEventListener("abort", () => reader.cancel());

  while (true) {
    const { done, value } = await reader.read();
    if (done || signal?.aborted) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();

    for (const line of lines) {
      if (line.startsWith("event: ")) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith("data: ") && currentEvent) {
        const data = line.slice(6);
        handleSSEEvent(
          currentEvent,
          data,
          container,
          msgDiv,
          () => {
            if (firstToken) {
              firstToken = false;
              if (!msgDiv.isConnected && !S.hideUntilBaked) container.appendChild(msgDiv);
              if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = "";
            }
            fullResponse += data.replace(/\\n/g, "\n");
            S.streamingContent = rewrittenResponse || fullResponse;
            if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = formatProse(rewrittenResponse || fullResponse);
            scrollToBottom();
          },
          (text) => {
            rewrittenResponse = text;
            S.streamingContent = text;
            if (S.streamingBodyEl) {
              const html =
                S.pendingRefineDiff && S.showEditorDiff
                  ? formatProseWithDiff(S.pendingRefineDiff.ops)
                  : formatProse(text);
              smoothUpdateBody(S.streamingBodyEl, html, scrollToBottom);
            } else {
              scrollToBottom();
            }
          },
        );
        currentEvent = null;
      }
    }
  }
  // reader.cancel() resolves read() with done:true rather than throwing, so
  // re-throw here so callers can set S.wasAborted and wait for the backend.
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
}

function handleSSEEvent(event, data, container, msgDiv, onToken, onRewrite) {
  switch (event) {
    case "director_start":
      setGenerationPhase("directing");
      S.lastDirectorData = null;
      renderInspector();
      break;
    case "director_done": {
      try {
        S.lastDirectorData = JSON.parse(data);
      } catch (_) {}
      _advanceReasoningPass(1); // director done → move to Writer dot
      renderInspector();
      break;
    }
    case "prompt_rewritten":
      try {
        const d = JSON.parse(data);
        const lastUser =
          [...S.messages].reverse().find((m) => m.role === "user" && !m.id) ||
          [...S.messages].reverse().find((m) => m.role === "user");
        if (lastUser) lastUser.content = d.refined_message;
        if (S.pendingUserMsg) S.pendingUserMsg.content = d.refined_message;
        if (S.isStreaming) {
          const userBodies = document.querySelectorAll("#chat-messages .message.user .msg-body");
          const last = userBodies[userBodies.length - 1];
          if (last) last.innerHTML = formatProse(d.refined_message);
        } else {
          renderMessages();
        }
      } catch (_) {}
      break;
    case "token":
      setGenerationPhase("generating");
      onToken();
      scheduleRefineTimer();
      break;
    case "writer_rewrite":
      clearRefineTimer();
      setGenerationPhase("refining");
      _advanceReasoningPass(2); // writer done, editor starting → move to Editor dot
      try {
        const refined = JSON.parse(data).refined_text;
        // S.streamingContent still holds the writer's unrefined text at this point
        const original = resolvePlaceholders(S.streamingContent || "");
        const refinedResolved = resolvePlaceholders(refined);
        S.pendingRefineDiff = { original, ops: sentenceDiff(original, refinedResolved) };
        onRewrite(refined);
      } catch (_) {}
      break;
    case "reasoning": {
      try {
        const d = JSON.parse(data);
        const passKey = d.pass; // "director" | "writer" | "editor"
        const delta = d.delta;
        const stateKey = "reasoning" + passKey.charAt(0).toUpperCase() + passKey.slice(1);
        S[stateKey] = (S[stateKey] || "") + delta;

        const passIdx = REASONING_PASSES.findIndex((p) => p.key === passKey);
        // Advance the streaming-progress dot if this token is from a later pass
        _advanceReasoningPass(passIdx);

        const viewingThisPass = S.reasoningPassSelected === passIdx;
        let box = document.getElementById("reasoning-box");
        if (!box) {
          // Box not in DOM yet — bootstrap via renderInspector, then write full accumulated text
          renderInspector();
          box = document.getElementById("reasoning-box");
          if (box) {
            box.textContent = S[stateKey];
            box.scrollTop = box.scrollHeight;
          }
        } else if (viewingThisPass) {
          // Only append to the visible box when the user is viewing this pass
          box.textContent += delta;
          box.scrollTop = box.scrollHeight;
        }
      } catch (_) {}
      break;
    }
    case "editor_done": {
      try {
        const d = JSON.parse(data);
        if (d.tool_calls?.length) {
          if (!S.lastDirectorData) S.lastDirectorData = {};
          S.lastDirectorData.tool_calls = [...(S.lastDirectorData.tool_calls || []), ...d.tool_calls];
          renderInspector();
        }
      } catch (_) {}
      break;
    }
    case "error":
      toast("Error: " + data, true);
      break;
  }
}

function agentPayload() {
  return { enable_agent: S.agentEnabled };
}

export async function continueFromUser() {
  if (!S.activeConvId || !canStartGeneration()) return;
  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role !== "user") {
    toast("Last message is not a user message", true);
    return;
  }
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
    const resp = await fetch("/api" + convUrl(S.activeConvId, "continue"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(agentPayload()),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === "AbortError") S.wasAborted = true;
    else toast("Connection error: " + e.message, true);
  }
  await afterStream();
}

// ── Send Message
export async function sendMessage() {
  if (!S.activeConvId || !canStartGeneration()) return;

  const inp = $("chat-input");
  let content = inp.value.trim();

  // Guard against double user turns: if the last message is already from the user,
  // ask the backend to generate a response for it without creating a new message.
  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role === "user" && lastMsg.id) {
    inp.value = "";
    inp.style.height = "auto";
    await continueFromUser();
    return;
  }

  if (!content) return;

  // Resolve {{user}} and {{char}} placeholders before sending
  content = resolvePlaceholders(content);

  setStreaming(true);
  setGenerationPhase("pending");
  inp.value = "";
  inp.style.height = "auto";
  $("send-btn").disabled = true;

  const attachments = [...S.attachments];
  S.attachments.length = 0;
  updateAttachmentPreview();
  const userMsg = {
    role: "user",
    content,
    id: null,
    branch_count: 1,
    branch_index: 0,
    prev_branch_id: null,
    next_branch_id: null,
    attachments,
  };
  S.messages.push(userMsg);
  S.pendingUserMsg = userMsg;
  renderMessages();
  scrollToBottom();

  const ct = $("chat-messages");
  const msgDiv = createStreamingDiv();

  S.abortController = new AbortController();
  try {
    const resp = await fetch("/api" + convUrl(S.activeConvId, "send"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, attachments, ...agentPayload() }),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === "AbortError") {
      S.wasAborted = true;
    } else {
      toast("Connection error: " + e.message, true);
    }
  }
  await afterStream();
}

// ── Regenerate
export async function regenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  setStreaming(true);
  setGenerationPhase("pending");
  $("send-btn").disabled = true;

  const idx = S.messages.findIndex((m) => m.id === msgId);
  S.streamCutoffIndex = idx >= 0 ? idx : S.messages.length;

  // Update UI to show only messages up to the regenerated message
  renderMessages();

  const ct = $("chat-messages");
  const msgDiv = createStreamingDiv();
  if (!S.hideUntilBaked) ct.appendChild(msgDiv);
  scrollToBottom();
  S.abortController = new AbortController();
  try {
    const resp = await fetch("/api" + convUrl(S.activeConvId, "messages", msgId, "regenerate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(agentPayload()),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === "AbortError") {
      S.wasAborted = true;
    } else {
      toast("Error: " + e.message, true);
    }
  }
  await afterStream();
}

// ── Inspector — Reasoning stepper rail

const REASONING_PASSES = [
  { key: "director", label: "Director", color: "var(--accent-dim)" },
  { key: "writer", label: "Writer", color: "var(--accent-dim)" },
  { key: "editor", label: "Editor", color: "var(--accent-dim)" },
];

// Advance the streaming-progress dot to `targetIdx` only if it's further ahead.
// Always auto-switches the selected view when a new pass begins (once per transition),
// but within a pass the user's manual selection is respected.
function _advanceReasoningPass(targetIdx) {
  if (targetIdx <= S.reasoningPassActive) return;
  S.reasoningPassActive = targetIdx;
  S.reasoningPassSelected = targetIdx; // auto-switch view to the new pass
  S.reasoningUserOverride = false; // reset so in-pass tokens don't fight the user
  const existing = document.getElementById("reasoning-section");
  if (existing) _refreshReasoningSection();
}

function _buildReasoningHtml() {
  // reasoningPassActive tracks streaming progress (for dot lighting/lines).
  // reasoningPassSelected tracks what the user is viewing.
  const streamIdx = S.reasoningPassActive;
  const selectedIdx = S.reasoningPassSelected;
  const dotsHtml = REASONING_PASSES.map((p, i) => {
    const hasText = !!S["reasoning" + p.key.charAt(0).toUpperCase() + p.key.slice(1)];
    const isStreaming = i === streamIdx;
    const isSelected = i === selectedIdx;
    const lit = hasText || isStreaming;
    const enabled = S.reasoningEnabled[p.key] !== false;
    const dotStyle = [
      `background:${lit ? p.color : "var(--bg-elevated)"}`,
      `color:${lit ? "#fff" : "var(--text-muted)"}`,
      `border:2px solid ${isSelected ? "var(--accent)" : lit ? p.color : "var(--border)"}`,
      isSelected ? "box-shadow:0 0 0 2px var(--accent)" : "",
      !enabled ? "opacity:0.4" : "",
    ]
      .filter(Boolean)
      .join(";");
    const lineColor = i < streamIdx ? REASONING_PASSES[i + 1].color : "var(--border)";
    const checkId = `reasoning-enabled-${p.key}`;
    return (
      `<div class="reasoning-dot-col">
        <button class="reasoning-dot" onclick="selectReasoningPass(${i})" style="${dotStyle}">${i + 1}</button>
        <label class="reasoning-enabled-label" for="${checkId}">
          <input type="checkbox" id="${checkId}" ${enabled ? "checked" : ""} onchange="toggleReasoningPass('${p.key}')">
          <span>on</span>
        </label>
      </div>` + (i < 2 ? `<div class="reasoning-rail-line" style="background:${lineColor}"></div>` : "")
    );
  }).join("");

  const selectedPass = REASONING_PASSES[selectedIdx];
  const currentText = S["reasoning" + selectedPass.key.charAt(0).toUpperCase() + selectedPass.key.slice(1)] || "";
  const openAttr = S.reasoningOpen ? " open" : "";

  return `<details class="inspector-block reasoning-section" id="reasoning-section"${openAttr} ontoggle="S.reasoningOpen=this.open">
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">▶</span>
      <h4 style="margin:0;display:inline">Reasoning</h4>
    </summary>
    <div style="margin-top:8px">
      <div class="reasoning-stepper">
        ${dotsHtml}
        <span class="reasoning-pass-label">${esc(selectedPass.label)}</span>
      </div>
      <div class="reasoning-box" id="reasoning-box">${esc(currentText)}</div>
    </div>
  </details>`;
}

function _refreshReasoningSection() {
  const existing = document.getElementById("reasoning-section");
  if (!existing) return;
  existing.outerHTML = _buildReasoningHtml();
  // Auto-scroll the newly rendered box to bottom only when viewing the streaming pass
  if (!S.reasoningUserOverride) {
    const box = document.getElementById("reasoning-box");
    if (box) box.scrollTop = box.scrollHeight;
  }
}

export function selectReasoningPass(idx) {
  S.reasoningPassSelected = idx;
  S.reasoningUserOverride = true;
  _refreshReasoningSection();
}

export async function toggleReasoningPass(passKey) {
  S.reasoningEnabled[passKey] = !S.reasoningEnabled[passKey];
  _refreshReasoningSection();
  await api.put("/settings", { reasoning_enabled_passes: { ...S.reasoningEnabled } });
}

// ── Inspector
export function clearRefineDiff() {
  S.pendingRefineDiff = null;
  renderMessages();
}

export function toggleInspector() {
  const inspector = $("inspector");
  const toolsPanel = $("tools-panel");
  const wasOpen = inspector.classList.contains("open");

  if (wasOpen) {
    inspector.classList.remove("open");
  } else {
    toolsPanel.classList.remove("open");
    inspector.classList.add("open");
  }
}

export function renderInspector() {
  if (S.isStreaming && S.lastDirectorData === null) {
    $("inspector-content").innerHTML = `${_buildReasoningHtml()}
       <div style="color:var(--text-muted);font-size:12px;display:flex;align-items:center;gap:8px">
         <span class="typing-indicator"><span></span><span></span><span></span></span> Director thinking…
       </div>`;
    const _rb = document.getElementById("reasoning-box");
    if (_rb) _rb.scrollTop = _rb.scrollHeight;
    return;
  }

  // Check if we have any director data to display
  const hasDirectorData =
    (S.directorState && Object.keys(S.directorState).length > 0) ||
    (S.lastDirectorData && Object.keys(S.lastDirectorData).length > 0);

  if (!hasDirectorData) {
    $("inspector-content").innerHTML = `${_buildReasoningHtml()}
       <div style="color:var(--text-muted);font-size:12px;">
         Send a message to see director output
       </div>`;
    return;
  }

  const ds = S.directorState || {};
  const ld = S.lastDirectorData || {};
  const activeIds = ld.active_moods || ds.active_moods || [];
  const stylesHtml = S.moodFragments
    .map((f) => `<span class="style-tag ${activeIds.includes(f.id) ? "active" : ""}">${esc(f.label)}</span>`)
    .join("");
  const lat = ld.agent_latency_ms || 0;
  const tc = ld.tool_calls || [];
  const inj = ld.injection_block || "";
  $("inspector-content").innerHTML = `
    <div class="inspector-block"><h4>Moods</h4>
      <div>${stylesHtml || '<span style="color:var(--text-muted);font-size:12px">None</span>'}</div>
    </div>
    ${_buildReasoningHtml()}
    ${
      lat
        ? `<div class="inspector-block"><h4>Agent Latency</h4>
               <div style="font-size:12px;color:var(--text-secondary)">${lat}ms</div></div>`
        : ""
    }
    ${
      tc.length
        ? `<div class="inspector-block"><h4>Tool Calls</h4>
                    <div class="injection-box">${esc(tc.map((c) => JSON.stringify(c)).join("\n\n"))}</div></div>`
        : ""
    }
    ${
      inj
        ? `<div class="inspector-block"><h4>Injection Block</h4>
               <div class="injection-box">${esc(inj)}</div></div>`
        : ""
    }`;
  // Scroll the freshly rendered reasoning box to bottom
  const _rb = document.getElementById("reasoning-box");
  if (_rb) _rb.scrollTop = _rb.scrollHeight;
}

export function showAvatarPopup() {
  if (!S.activeCharId) return;
  const popup = document.getElementById("avatar-popup");
  if (!popup) return;
  if (!popup.classList.contains("hidden")) {
    hideAvatarPopup();
    return;
  }
  const img = document.getElementById("avatar-popup-image");
  if (img) img.src = `/api/characters/${S.activeCharId}/avatar?t=${Date.now()}`;
  popup.classList.remove("hidden");
}

export function hideAvatarPopup() {
  const popup = document.getElementById("avatar-popup");
  if (popup) popup.classList.add("hidden");
}
