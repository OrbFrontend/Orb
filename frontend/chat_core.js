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
import { effectiveWorkflowEnabled, S } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import {
  $,
  avatarCell,
  avatarUrl,
  esc,
  escAttr,
  escHandlerArg,
  formatBytes,
  formatProse,
  formatProseWithDiff,
  resolvePlaceholders,
} from "./utils.js";
import { segmentBody } from "./workflow_segmentation.js";
import { markClickable } from "./workflow_text_interaction.js";

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
export const ICON_SEND = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
export const ICON_CHEVRON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="6 9 12 15 18 9"/></svg>`;
export const ICON_FORK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
export const ICON_NOTE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M12 18v-5"/><path d="M9.5 15.5h5"/></svg>`;

export function buildMsgToolbar(m, childByParent = null) {
  const isAssistant = m.role === "assistant";
  const isGreeting = isAssistant && !m.parent_id;
  // childByParent is a precomputed Map(parent_id → assistant child) built once
  // per render to avoid an O(N) scan of S.messages per user message (O(N²) total).
  // Fall back to a direct find when called outside renderMessages (e.g. single
  // toolbar repaint).
  const childAssistant = isAssistant
    ? null
    : childByParent
      ? childByParent.get(m.id) || null
      : S.messages.find((c) => c.parent_id === m.id && c.role === "assistant");
  const regenTargetId = isAssistant ? m.id : childAssistant?.id;
  const canRegen = !isGreeting && (isAssistant || !!childAssistant || !!m.id);

  const editBtn = `<button onclick="${m.id ? `startEdit(${m.id})` : `startEditPending()`}" title="Edit">${ICON_EDIT}</button>`;

  // Edit & Fork: only for persisted user messages. Forks the conversation by
  // saving the edit as a new sibling and generating a fresh reply, leaving the
  // original branch intact. The pending (unsaved) user bubble has no siblings
  // to fork, so it's omitted there.
  const forkBtn =
    m.role === "user" && m.id
      ? `<button onclick="startForkEdit(${m.id})" title="Edit &amp; Fork">${ICON_FORK}</button>`
      : "";

  const regenBtn = isGreeting
    ? ""
    : !canRegen
      ? `<button disabled>${ICON_REGEN}</button>`
      : `<button onclick="${regenTargetId ? `regenerate(${regenTargetId})` : `continueFromUser()`}" title="Regenerate">${ICON_REGEN}</button>`;

  const superRegenBtn =
    isAssistant && m.id && !isGreeting
      ? `<button onclick="superRegenerate(${m.id})" title="Super Regenerate">${ICON_SUPER_REGEN}</button>`
      : "";

  const magicBtn =
    isAssistant && m.id && !isGreeting
      ? `<button onclick="toggleMagicInput(${m.id})" title="Magic Rewrite">${ICON_MAGIC}</button>`
      : "";

  const magicInput =
    isAssistant && m.id && !isGreeting && S.magicInputMsgId === m.id
      ? `<span class="magic-input-wrap" id="magic-wrap-${m.id}"><input class="magic-input" type="text" placeholder="Direction/Fix..." id="magic-input-${m.id}" onkeydown="handleMagicKey(event,${m.id})" autofocus><button class="magic-apply" onclick="submitMagicRewrite(${m.id})" title="Apply">${ICON_SEND}</button></span>`
      : "";

  // Shows on any persisted, non-greeting message: a note anchors to its own message and both
  // roles are valid anchors, so a note on a user message is as legitimate as one on a reply.
  // Gated on the master recording switch -- the same switch that surfaces the Notes panel --
  // so the panel the button opens is always reachable when the button shows.
  const noteBtn =
    m.id && !isGreeting && S.directionNotesRecord
      ? `<button onclick="addUserDirectionNote(${m.id})" title="Add direction note">${ICON_NOTE}</button>`
      : "";

  // Read-only local classifier; no persistence, so multi-tab is fine. Shown on
  // every assistant message (greeting included). Gated on the AI-slop toggle
  // (default on) -- 503s to a helpful toast if the model isn't downloaded.
  const slopBtn =
    isAssistant && m.id && S.settings?.local_ml_enabled?.slop_classifier !== false
      ? `<button class="msg-btn-slop" onclick="scoreSlop(${m.id},this)" title="Score AI-slop">AI</button>`
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

  return `${editBtn}${forkBtn}${regenBtn}${superRegenBtn}${magicBtn}${magicInput}${noteBtn}${slopBtn}${_renderExtraButtons(m)}${delBtn}${diffBtn}`;
}

function _renderExtraButtons(msg) {
  if (!S.workflowMessageButtonRenderers.length) return "";
  let html = "";
  for (const { workflowId, render } of S.workflowMessageButtonRenderers) {
    if (!effectiveWorkflowEnabled(workflowId)) continue;
    try {
      const piece = render(msg);
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

function formatStatNum(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}

async function renderHomeStats() {
  const grid = $("home-stats-grid");
  if (!grid) return;
  let s;
  try {
    s = await api.get("/stats");
  } catch {
    return; // fail silently — fall back to plain empty state
  }
  if ($("home-stats-grid") !== grid) return; // view changed while fetching
  // Any prior conversation is a reliable, zero-cost sign this isn't a first
  // run — drop the onboarding prompt so returning users get a cleaner home.
  if (s.total_conversations > 0) {
    $("home-greeting")?.remove();
    $("home-greeting-icon")?.remove();
  }
  const cards = [
    ["Conversations", s.total_conversations],
    ["Messages", s.total_messages],
    ["Words written", s.total_words],
    ["~Tokens generated", s.estimated_tokens],
  ];
  if (s.storage_bytes > 0) {
    cards.push(["Storage used", formatBytes(s.storage_bytes)]);
  }
  if (s.avg_latency_ms != null) {
    cards.push(["Avg response time", `${(s.avg_latency_ms / 1000).toFixed(1)}s`]);
  }
  const numericCards = cards
    .filter(([, v]) => typeof v !== "number" || v > 0)
    .map(
      ([label, v]) =>
        `<div class="stat-card"><div class="stat-card-value">${
          typeof v === "number" ? formatStatNum(v) : esc(v)
        }</div><div class="stat-card-label">${esc(label)}</div></div>`,
    )
    .join("");
  grid.innerHTML = renderSpotlightCard(s.character_spotlight) + numericCards;
}

// The character spotlight gets a portrait-led hero card rather than a number
// slot: avatar, name, and a message/conversation tally, with a themed eyebrow so
// the stat reads as a story beat instead of a bare value. The server picks the
// theme (e.g. the most-messaged "favorite" or a random "misses you" character).
// When the card still exists, the whole card is clickable and reopens it exactly
// as the library panel would (selectChar).
const SPOTLIGHT_EYEBROWS = {
  favorite: "★ Favorite character",
  missed: "💔 Misses you",
};
function renderSpotlightCard(sp) {
  if (!sp?.name) return "";
  const av = sp.card_id
    ? avatarCell(escAttr(avatarUrl(sp.card_id)), { attrs: 'loading="lazy" decoding="async"' })
    : "👤";
  const msgs = `${formatStatNum(sp.messages)} message${sp.messages === 1 ? "" : "s"}`;
  const convs = `${formatStatNum(sp.conversations)} conversation${sp.conversations === 1 ? "" : "s"}`;
  const clickable = sp.card_id
    ? ` role="button" tabindex="0" onclick="selectChar('${escHandlerArg(sp.card_id)}', 'library')"`
    : "";
  const eyebrow = SPOTLIGHT_EYEBROWS[sp.theme] ?? SPOTLIGHT_EYEBROWS.favorite;
  return `<div class="stat-card stat-card-favorite stat-card-spotlight-${esc(sp.theme)}${sp.card_id ? " stat-card-clickable" : ""}"${clickable}>
      <div class="stat-fav-eyebrow">${esc(eyebrow)}</div>
      <div class="stat-fav-body">
        <div class="stat-fav-avatar">${av}</div>
        <div class="stat-fav-text">
          <div class="stat-fav-name">${esc(sp.name)}</div>
          <div class="stat-fav-count">${msgs} · ${convs}</div>
        </div>
      </div>
    </div>`;
}

// How many trailing messages the window starts with on a fresh open. Tall enough
// to fill a viewport so the first paint looks complete; older messages backfill
// on scroll-up (handled in initAutoscroll) and via the idle full-fill below.
export const RENDER_WINDOW_SIZE = 30;

// Reset the render window to the tail. Called on conversation switch and when a
// new message is appended so newly-relevant content is always in view.
export function resetRenderWindow() {
  S.renderWindowStart = Math.max(0, S.messages.length - RENDER_WINDOW_SIZE);
}

// Ensure a given message index is inside the render window (e.g. before editing
// an off-window message). Returns true if the window was widened.
export function ensureIndexInWindow(idx) {
  if (idx >= 0 && idx < S.renderWindowStart) {
    S.renderWindowStart = idx;
    return true;
  }
  return false;
}

export function renderMessages(forceBottom = false) {
  const ct = $("chat-messages");
  const distFromBottom = ct.scrollHeight - ct.scrollTop - ct.clientHeight;
  let streamingEl = null;
  let badgeEl = null;
  let renderedMsgs = null;
  if (S.isStreaming) {
    streamingEl = S.streamingBodyEl?.closest(".message") ?? null;
    badgeEl = document.getElementById("active-director-badge");
  }
  if (!S.activeConvId) {
    ct.innerHTML =
      '<div class="empty-state"><div class="icon" id="home-greeting-icon">📜</div><div id="home-greeting">Select a character to begin</div><div class="stats-grid" id="home-stats-grid"></div></div>';
    renderHomeStats();
  } else if (!S.messages.length) {
    ct.innerHTML =
      '<div class="empty-state"><div class="icon">📜</div><div>Start writing to begin the scene</div></div>';
  } else {
    let msgs = S.messages;
    if (S.isStreaming && S.streamCutoffIndex != null) {
      msgs = S.messages.slice(0, S.streamCutoffIndex);
    }
    // Windowed render: only paint the trailing slice synchronously. The window
    // always includes the tail, so the regular scroll-to-bottom behavior and all
    // existing callers see the latest messages with no change. Older messages are
    // backfilled lazily on scroll-up and fully filled during idle time below.
    const start = Math.min(Math.max(S.renderWindowStart | 0, 0), msgs.length);
    if (start > 0) msgs = msgs.slice(start);
    renderedMsgs = msgs;
    // Precompute parent_id → assistant child once (was an O(N) find per user
    // message → O(N²)). Built over the full list so a child just below the window
    // edge is still found.
    const childByParent = new Map();
    for (const c of S.messages) {
      if (c.role === "assistant" && c.parent_id != null && !childByParent.has(c.parent_id)) {
        childByParent.set(c.parent_id, c);
      }
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
        const toolbar = isEditing ? "" : `<div class="msg-toolbar">${buildMsgToolbar(m, childByParent)}</div>`;
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
  _segmentRenderedMessages(renderedMsgs);
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

function _segmentRenderedMessages(renderedMsgs) {
  if (!S.workflowTextEffects.length && !S.workflowClickHandlers.length) return;
  if (!renderedMsgs) return;
  // Index the rendered slice by id so each DOM node maps to its message without
  // an O(N) scan of S.messages per element.
  const byId = new Map();
  for (const m of renderedMsgs) if (m.id) byId.set(m.id, m);
  for (const el of document.querySelectorAll("#chat-messages .message[data-msg-id]")) {
    const msgId = Number(el.dataset.msgId);
    if (!Number.isInteger(msgId) || msgId <= 0) continue;
    if (S.pendingRefineDiff?.msgId === msgId && S.showEditorDiff) continue;
    const msg = byId.get(msgId);
    if (!msg) continue;
    const body = el.querySelector(".msg-body");
    if (body) _applyWorkflowTextSegments(body, msg);
  }
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
  } catch (_e) {
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
