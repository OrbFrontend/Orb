// Core chat spine: message normalization, the message toolbar + icons, the
// main renderMessages paint, and the context-size counter. These are the
// low-level pieces the feature modules (workflow, inspector, stream, messages,
// conversations) build on. Split out of chat.js; the public surface is
// re-exported from chat.js.
import { api } from "./api.js";
import {
  _refreshWorkflowViewportObserver,
  _renderWorkflowArtifacts,
  _renderWorkflowRejection,
} from "./chat_workflow.js";
import { S } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import { segmentBody } from "./workflow_segmentation.js";
import { markClickable } from "./workflow_text_interaction.js";
import { $, esc, formatBytes, formatProse, formatProseWithDiff, resolvePlaceholders } from "./utils.js";

export function canStartGeneration() {
  if (S.isStreaming) return false;
  return requestSendPermission();
}

function normalizeMessages(msgs) {
  if (!Array.isArray(msgs)) return msgs;
  for (const m of msgs) {
    for (const field of ["user_attachments", "workflow_attachments"]) {
      const list = m[field];
      if (!Array.isArray(list)) continue;
      for (const att of list) {
        if (att.data_b64 != null && att.b64 == null) att.b64 = att.data_b64;
        if (att.mime_type != null && att.mime == null) att.mime = att.mime_type;
        if (typeof att.consumption_metadata === "string") {
          try {
            att.consumption_metadata = JSON.parse(att.consumption_metadata);
          } catch (e) {
            console.warn("workflow attachment", att.id, "has malformed consumption_metadata:", e);
            att.consumption_metadata = null;
          }
        }
      }
    }
  }
  return msgs;
}

// Safe replacement for S.messages from a server response.
// During streaming, local-pending entries (id: null) are preserved because the
// server doesn't know about them yet — replacing blindly drops them from the DOM.
export function setMessages(serverMsgs) {
  const normalized = normalizeMessages(serverMsgs);
  if (S.isStreaming) {
    const pending = S.messages.filter((m) => !m.id);
    S.messages = pending.length ? [...normalized, ...pending] : normalized;
  } else {
    S.messages = normalized;
  }
  // Drop rejection records whose message is no longer present (deleted
  // message or conversation switch). Keeps the flat list bounded.
  const liveIds = new Set(S.messages.map((m) => m.id).filter((id) => id != null));
  S.rejectedWorkflowAtts = S.rejectedWorkflowAtts.filter((r) => liveIds.has(r.message_id));
}

export const ICON_EDIT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
export const ICON_REGEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>`;
export const ICON_REROLL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none"/><circle cx="16" cy="8" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="8" cy="16" r="1.4" fill="currentColor" stroke="none"/><circle cx="16" cy="16" r="1.4" fill="currentColor" stroke="none"/></svg>`;
export const ICON_DEL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`;
export const ICON_CLEAR = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/></svg>`;
export const ICON_SUPER_REGEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>`;
export const ICON_MAGIC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>`;
export const ICON_CHEVRON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="6 9 12 15 18 9"/></svg>`;
export const ICON_FORK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;

export function buildMsgToolbar(m) {
  const isAssistant = m.role === "assistant";
  const isGreeting = isAssistant && !m.parent_id;
  const childAssistant = !isAssistant ? S.messages.find((c) => c.parent_id === m.id && c.role === "assistant") : null;
  const regenTargetId = isAssistant ? m.id : childAssistant?.id;
  const canRegen = !isGreeting && (isAssistant || !!childAssistant || !!m.id);

  const editBtn = S.hasMultipleTabs
    ? `<button disabled title="Close other tabs to edit">${ICON_EDIT}</button>`
    : `<button onclick="${m.id ? `startEdit(${m.id})` : `startEditPending()`}" title="Edit">${ICON_EDIT}</button>`;

  // Edit & Fork: only for persisted user messages. Forks the conversation by
  // saving the edit as a new sibling and generating a fresh reply, leaving the
  // original branch intact. The pending (unsaved) user bubble has no siblings
  // to fork, so it's omitted there.
  const forkBtn =
    m.role === "user" && m.id
      ? S.hasMultipleTabs
        ? `<button disabled title="Close other tabs to fork">${ICON_FORK}</button>`
        : `<button onclick="startForkEdit(${m.id})" title="Edit &amp; Fork">${ICON_FORK}</button>`
      : "";

  const regenBtn = isGreeting
    ? ""
    : S.hasMultipleTabs || !canRegen
      ? `<button disabled title="${S.hasMultipleTabs ? "Close other tabs to regenerate" : ""}">${ICON_REGEN}</button>`
      : `<button onclick="${regenTargetId ? `regenerate(${regenTargetId})` : `continueFromUser()`}" title="Regenerate">${ICON_REGEN}</button>`;

  const superRegenBtn =
    isAssistant && m.id && !isGreeting
      ? S.hasMultipleTabs
        ? `<button disabled title="Close other tabs to regenerate">${ICON_SUPER_REGEN}</button>`
        : `<button onclick="superRegenerate(${m.id})" title="Super Regenerate">${ICON_SUPER_REGEN}</button>`
      : "";

  const magicBtn =
    isAssistant && m.id && !isGreeting
      ? S.hasMultipleTabs
        ? `<button disabled title="Close other tabs to use Magic">${ICON_MAGIC}</button>`
        : `<button onclick="toggleMagicInput(${m.id})" title="Magic Rewrite">${ICON_MAGIC}</button>`
      : "";

  const magicInput =
    isAssistant && m.id && !isGreeting && S.magicInputMsgId === m.id
      ? `<input class="magic-input" type="text" placeholder="Direction/Fix…" id="magic-input-${m.id}" onkeydown="handleMagicKey(event,${m.id})" autofocus>`
      : "";

  const delBtn = !m.id
    ? `<button disabled class="msg-btn-del">${ICON_DEL}</button>`
    : isGreeting
      ? ""
      : `<button onclick="deleteMessage(${m.id})" title="Delete message, siblings, and all children" class="msg-btn-del">${ICON_DEL}</button>`;

  const diffBtn =
    S.pendingRefineDiff?.msgId && m.id === S.pendingRefineDiff.msgId && S.showEditorDiff
      ? `<button onclick="clearRefineDiff()" title="Clear diff highlights" class="btn-clear-diff">${ICON_CLEAR}</button>`
      : "";

  return `${editBtn}${forkBtn}${regenBtn}${superRegenBtn}${magicBtn}${magicInput}${_renderExtraButtons(m)}${delBtn}${diffBtn}`;
}

function _renderExtraButtons(msg) {
  if (!S.workflowMessageButtonRenderers.length) return "";
  let html = "";
  for (const fn of S.workflowMessageButtonRenderers) {
    try {
      const piece = fn(msg);
      if (typeof piece === "string" && piece) html += piece;
    } catch (e) {
      console.error("workflow message button renderer threw:", e);
    }
  }
  return html;
}

// ── Attachments rendering
function renderUserAttachments(userAtts) {
  if (!userAtts || userAtts.length === 0) return "";
  const items = userAtts
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

// ── Messages
export function getCharName() {
  const c = S.conversations.find((c) => c.id === S.activeConvId);
  return c?.character_name || "Assistant";
}

export function renderMessages(forceBottom = false) {
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
        const isForkEditing = S.forkEditMsgId !== null && S.forkEditMsgId === m.id;
        const isEditing =
          (S.editingMsgId !== null && S.editingMsgId === m.id) || (!m.id && S.editingPendingUserMsg) || isForkEditing;
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
        const toolbar = isEditing ? "" : `<div class="msg-toolbar">${buildMsgToolbar(m)}</div>`;
        const taId = m.id ? `edit-textarea-${m.id}` : `edit-textarea-pending`;
        const editActions = isForkEditing
          ? `<button class="btn btn-sm" onclick="cancelForkEdit()">Cancel</button>
            <button class="btn btn-sm btn-accent" onclick="saveForkEdit(${m.id})">Fork</button>`
          : `<button class="btn btn-sm" onclick="${m.id ? `cancelEdit()` : `cancelEditPending()`}">Cancel</button>
            <button class="btn btn-sm btn-accent" onclick="${m.id ? `saveEdit(${m.id},'${m.role}')` : `saveEditPending()`}">Save</button>`;
        const body = isEditing
          ? `
        <div class="msg-edit-area">
          <textarea id="${taId}" rows="5">${esc(m.content)}</textarea>
          <div class="msg-edit-actions">
            ${editActions}
          </div>
        </div>`
          : `<div class="msg-body">${
              S.pendingRefineDiff?.msgId && m.id === S.pendingRefineDiff.msgId && S.showEditorDiff
                ? formatProseWithDiff(S.pendingRefineDiff.ops)
                : formatProse(resolvePlaceholders(m.content))
            }</div>`;
        const attachmentsHtml = renderUserAttachments(m.user_attachments);
        const workflowArtifactsHtml = _renderWorkflowArtifacts(m);
        const rejectionHtml = _renderWorkflowRejection(m);
        return `<div class="message ${m.role}" data-msg-id="${m.id}">
        <div class="msg-role">${m.role === "user" ? "You" : esc(getCharName())} ${branchHtml}</div>
        ${body}${attachmentsHtml}${workflowArtifactsHtml}${rejectionHtml}${toolbar}
      </div>`;
      })
      .join("");
  }
  if (badgeEl) ct.appendChild(badgeEl);
  // Keep streaming box visible while editing; only hide if explicitly flagged
  if (streamingEl && !S.hideStreamingBox && !S.hideUntilBaked) ct.appendChild(streamingEl);
  // Restore scroll position synchronously so the browser never paints a jump.
  // behavior:"instant" is required because #chat-messages sets scroll-behavior:
  // smooth in CSS — a plain scrollTop assignment would animate.
  // Fresh conversation loads pass forceBottom so they land at the bottom on the
  // first paint instead of relying on the prior conversation's scroll state.
  // Otherwise: near-bottom → snap to bottom; else preserve distance from bottom.
  const targetTop =
    forceBottom || distFromBottom <= 50
      ? ct.scrollHeight
      : Math.max(0, ct.scrollHeight - ct.clientHeight - distFromBottom);
  ct.scrollTo({ top: targetTop, behavior: "instant" });
  if (!S.isStreaming) updateContextCounter();
  _refreshWorkflowViewportObserver();
  _segmentRenderedMessages();
}

// Wraps body words in addressable `.seg` spans and marks the clickable ones for
// messages a workflow effect or click handler can target. No-op when no
// workflow registers either feature, and for a body shown in editor-diff review
// (deleted text must not become addressable, and diff layout would shift the
// unit numbering); such a message is segmented on the next clean render.
export function _applyWorkflowTextSegments(bodyEl, msg) {
  segmentBody(bodyEl);
  markClickable(bodyEl, msg);
}

function _segmentRenderedMessages() {
  if (!S.workflowTextEffects.length && !S.workflowClickHandlers.length) return;
  for (const el of document.querySelectorAll("#chat-messages .message[data-msg-id]")) {
    const msgId = Number(el.dataset.msgId);
    if (!Number.isInteger(msgId) || msgId <= 0) continue;
    if (S.pendingRefineDiff?.msgId === msgId && S.showEditorDiff) continue;
    const msg = S.messages.find((m) => m.id === msgId);
    if (!msg) continue;
    const body = el.querySelector(".msg-body");
    if (body) _applyWorkflowTextSegments(body, msg);
  }
}

// Unused by current callers but retained as a surgical single-toolbar repaint
// primitive for future use.
function refreshMessageToolbar(msgId) {
  if (!msgId) return;
  const msg = S.messages.find((m) => m.id === msgId);
  const toolbar = document.querySelector(`[data-msg-id="${msgId}"] .msg-toolbar`);
  if (msg && toolbar) toolbar.innerHTML = buildMsgToolbar(msg);
}

export function updateContextCounter() {
  fetchContextSize();
}

// Soft-fails to null instead of throwing: the context counter is a non-critical
// HUD value, so a failed fetch should leave the display untouched, not error.
async function getContextSize(convId) {
  const r = await fetch(`/api/conversations/${convId}/context-size`);
  if (!r.ok) return null;
  return r.json();
}

async function fetchContextSize() {
  if (!S.activeConvId) return;
  try {
    const data = await getContextSize(S.activeConvId);
    if (data) {
      S.contextSize = data;
      renderContextSize();
    }
  } catch (e) {
    /* ignore */
  }
}

export function renderContextSize() {
  const el = document.getElementById("inspector-context-size");
  if (!el) return;
  const data = S.contextSize;
  if (!data) {
    el.outerHTML = `<div class="inspector-block" id="inspector-context-size"><div style="color:var(--text-muted);font-size:12px;">—</div></div>`;
    return;
  }
  const total = data.total_tokens_est;
  const rows = Object.entries(data.breakdown)
    .filter(([_, v]) => v.tokens_est > 0)
    .sort((a, b) => b[1].tokens_est - a[1].tokens_est)
    .map(([key, val]) => {
      const pct = ((val.tokens_est / total) * 100).toFixed(0);
      const label = key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
      return `<div class="ctx-row">
        <span class="ctx-label">${esc(label)}</span>
        <span class="ctx-bar"><span class="ctx-bar-fill" style="width:${pct}%"></span></span>
        <span class="ctx-tokens">${val.tokens_est.toLocaleString()}</span>
      </div>`;
    })
    .join("");
  const openAttr = S.contextSizeOpen ? " open" : "";
  el.outerHTML = `<details class="inspector-block ctx-section" id="inspector-context-size"${openAttr} ontoggle="S.contextSizeOpen=this.open;saveInspectorOpenStates()">
    <summary class="ctx-summary">
      <span class="reasoning-summary-arrow">▶</span>
      <span class="ctx-total">~${total.toLocaleString()} tokens <span class="ctx-msgs">(${data.message_count} msgs)</span></span>
    </summary>
    <div class="ctx-rows">${rows}</div>
  </details>`;
}
