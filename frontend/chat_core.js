import { api } from "./api.js";
import { renderTurnError } from "./chat_error.js";
import {
  _refreshWorkflowViewportObserver,
  _renderWorkflowArtifacts,
  _renderWorkflowRejection,
} from "./chat_workflow.js";
import { reconcileChildren } from "./dom_reconcile.js";
import { sceneEmptyStateHtml, speakerAvatarCell, speakerLabel } from "./group_cast.js";
import { CHEVRON_LEFT_ICON, CHEVRON_RIGHT_ICON, EDIT_ICON_PATHS } from "./icons.js";
import { preserveScrollDistance } from "./scroll_follow.js";
import { effectiveWorkflowEnabled, S, subscribe } from "./state.js";
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
import { messageProposalsHtml } from "./world_proposals.js";

export function canStartGeneration() {
  if (S.isStreaming || S.proseRewriteMsgId) return false;
  return requestSendPermission();
}

subscribe("cast", () => renderMessages());

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

export function setMessages(serverMsgs) {
  const normalized = normalizeMessages(serverMsgs);
  if (S.isStreaming) {
    const pending = S.messages.filter((m) => !m.id);
    S.messages = pending.length ? [...normalized, ...pending] : normalized;
  } else {
    S.messages = normalized;
  }
  const liveIds = new Set(S.messages.map((m) => m.id).filter((id) => id != null));
  S.rejectedWorkflowAtts = S.rejectedWorkflowAtts.filter((r) => liveIds.has(r.message_id));
}

export const ICON_EDIT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15">${EDIT_ICON_PATHS}</svg>`;
export const ICON_REGEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>`;
export const ICON_REROLL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none"/><circle cx="16" cy="8" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="8" cy="16" r="1.4" fill="currentColor" stroke="none"/><circle cx="16" cy="16" r="1.4" fill="currentColor" stroke="none"/></svg>`;
export const ICON_DEL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`;
export const ICON_CLEAR = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/></svg>`;
export const ICON_SUPER_REGEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>`;
export const ICON_MAGIC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>`;
export const ICON_PROSE_REWRITE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H18v14H6.5A2.5 2.5 0 0 0 4 19.5z"/><path d="M4 5.5v14"/><path d="M8 7h6"/><path d="M8 11h4"/><path d="m15 19 1.5 1.5L21 16"/></svg>`;
export const ICON_SEND = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
export const ICON_CHEVRON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="6 9 12 15 18 9"/></svg>`;
export const ICON_FORK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
export const ICON_NOTE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M12 18v-5"/><path d="M9.5 15.5h5"/></svg>`;

export function buildMsgToolbar(m, childByParent = null) {
  const isAssistant = m.role === "assistant";
  const isGreeting = isAssistant && !m.parent_id;
  const childAssistant = isAssistant
    ? null
    : childByParent
      ? childByParent.get(m.id) || null
      : S.messages.find((c) => c.parent_id === m.id && c.role === "assistant");
  const regenTargetId = isAssistant ? m.id : childAssistant?.id;
  const canRegen = !isGreeting && (isAssistant || !!childAssistant || !!m.id);

  const editBtn = `<button onclick="${m.id ? `startEdit(${m.id})` : `startEditPending()`}" title="Edit">${ICON_EDIT}</button>`;

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
      ? `<button class="msg-btn-magic" onclick="toggleMagicInput(${m.id})" title="Magic Rewrite">${ICON_MAGIC}</button>`
      : "";

  const canProseRewrite =
    isAssistant &&
    m.id &&
    (m.content || "").trim() &&
    S.settings?.local_ml_config?.prose_rewriter?.variant &&
    S.settings?.local_ml_enabled?.prose_rewriter !== false;
  const proseRewriteTitle = m.has_writer_draft ? "Rewrite original Writer draft" : "Rewrite this message";
  const proseRewriteBtn = canProseRewrite
    ? `<button class="msg-btn-prose-rewrite" onclick="rewriteMessageProse(${m.id})" title="${proseRewriteTitle}"${S.proseRewriteMsgId ? " disabled" : ""}>${ICON_PROSE_REWRITE}</button>`
    : "";

  const magicInput =
    isAssistant && m.id && !isGreeting && S.magicInputMsgId === m.id
      ? `<span class="magic-input-wrap" id="magic-wrap-${m.id}"><input class="magic-input" type="text" placeholder="Direction/Fix..." id="magic-input-${m.id}" onkeydown="handleMagicKey(event,${m.id})" autofocus><button class="magic-apply" onclick="submitMagicRewrite(${m.id})" title="Apply">${ICON_SEND}</button></span>`
      : "";

  const noteBtn =
    m.id && !isGreeting && S.directionNotesRecord
      ? `<button onclick="addUserDirectionNote(${m.id})" title="Add direction note">${ICON_NOTE}</button>`
      : "";

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

  return `${editBtn}${forkBtn}${regenBtn}${superRegenBtn}${magicBtn}${proseRewriteBtn}${magicInput}${noteBtn}${slopBtn}${_renderExtraButtons(m)}${delBtn}${diffBtn}`;
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
      <img loading="lazy" decoding="async" src="data:${mime};base64,${b64}" alt="${esc(filename)}">
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
    return;
  }
  if ($("home-stats-grid") !== grid) return;
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

export const RENDER_WINDOW_SIZE = 30;

export function resetRenderWindow() {
  S.renderWindowStart = Math.max(0, S.messages.length - RENDER_WINDOW_SIZE);
}

export function ensureIndexInWindow(idx) {
  if (idx >= 0 && idx < S.renderWindowStart) {
    S.renderWindowStart = idx;
    return true;
  }
  return false;
}

// The branch pager. finalizeStreamingDiv grafts this onto the bubble it just
// baked instead of repainting the list, so both paths build it from here.
export function swipeNavHtml(m) {
  const bc = m.branch_count || 1;
  if (bc <= 1) return "";
  const bi = m.branch_index || 0;
  return `<span class="swipe-nav">
          <button onclick="event.stopPropagation();switchBranch(${m.prev_branch_id})" ${!m.prev_branch_id ? "disabled" : ""} title="Previous branch" aria-label="Previous branch">${CHEVRON_LEFT_ICON}</button>
          <span class="swipe-counter">${bi + 1}/${bc}</span>
          <button onclick="event.stopPropagation();switchBranch(${m.next_branch_id})" ${!m.next_branch_id ? "disabled" : ""} title="Next branch" aria-label="Next branch">${CHEVRON_RIGHT_ICON}</button>
        </span>`;
}

function _messageHtml(m, childByParent, avatars) {
  const isForkEditing = S.forkEditMsgId !== null && S.forkEditMsgId === m.id;
  const isEditing =
    (S.editingMsgId !== null && S.editingMsgId === m.id) || (!m.id && S.editingPendingUserMsg) || isForkEditing;
  const branchHtml = swipeNavHtml(m);
  const toolbar = isEditing ? "" : `<div class="msg-toolbar">${buildMsgToolbar(m, childByParent)}</div>`;
  const taId = m.id ? `edit-textarea-${m.id}` : `edit-textarea-pending`;
  const [cancelCall, commitCall, commitLabel] = isForkEditing
    ? [`cancelForkEdit()`, `saveForkEdit(${m.id})`, "Fork"]
    : m.id
      ? [`cancelEdit()`, `saveEdit(${m.id},'${m.role}')`, "Save"]
      : [`cancelEditPending()`, `saveEditPending()`, "Save"];
  const editActions = `<button class="btn btn-sm" onclick="${cancelCall}">Cancel</button>
            <button class="btn btn-sm btn-accent" onclick="${commitCall}">${commitLabel}</button>`;
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
  const proposalsHtml = messageProposalsHtml(m);
  const isProseRewriting = !!m.id && m.id === S.proseRewriteMsgId;
  const rewritingHtml = isProseRewriting
    ? `<span class="msg-rewriting"><span class="dot"></span>Rewriting prose…</span>`
    : "";
  // First child of .message and a sibling of .msg-role, not inside it:
  // finalizeStreamingDiv, patchPendingUserMessage, patchParentUserMessage and
  // the user_message_created handler all rebuild .msg-role, and would each have
  // to learn about the portrait if it lived in there.
  return `<div class="message ${m.role}${isProseRewriting ? " prose-rewriting" : ""}" data-msg-id="${m.id}">
        ${avatars ? speakerAvatarCell(m) : ""}<div class="msg-role">${esc(speakerLabel(m))} ${branchHtml}${rewritingHtml}</div>
        ${body}${attachmentsHtml}${workflowArtifactsHtml}${rejectionHtml}${proposalsHtml}${toolbar}
      </div>`;
}

/** Keep an in-flight bubble's gutter in step with the toggle.
 *
 * The streaming node is carried across re-renders rather than rebuilt from the
 * template above, so flipping "Show avatars in chat" mid-turn would otherwise
 * leave it the one message with no portrait -- and leave it that way for good,
 * since finalizeStreamingDiv patches the node in place instead of re-rendering.
 */
function syncStreamingAvatar(el, avatars) {
  const cell = el.querySelector(":scope > .msg-avatar");
  if (avatars && !cell) {
    const msg = { role: "assistant", speaker_member_id: S.currentSpeaker?.member_id ?? null };
    el.insertAdjacentHTML("afterbegin", speakerAvatarCell(msg));
  } else if (!avatars && cell) {
    cell.remove();
  }
}

// content-visibility hides an off-screen bubble's real height, so a node has to
// be forced visible before it can be measured. Batch the whole set: add the
// class to every node, read every height, then write. Interleaving a read and a
// write per node costs one forced layout per message instead of one in total.
function _measureIntrinsicSizes(nodes) {
  if (!nodes.length) return;
  for (const el of nodes) el.classList.add("msg-measuring");
  const heights = nodes.map((el) => el.offsetHeight);
  for (let i = 0; i < nodes.length; i++) {
    // A render while the chat pane is hidden measures everything as 0. Leave the
    // stylesheet's placeholder in place rather than pinning the bubble to zero.
    if (heights[i] > 0) nodes[i].style.containIntrinsicSize = `auto ${heights[i]}px`;
    nodes[i].classList.remove("msg-measuring");
  }
}

export function renderMessages(forceBottom = false) {
  const ct = $("chat-messages");
  let renderedMsgs = null;
  // An attribute rather than a body class: the gutter's rules all hang off
  // `#chat-messages[data-avatars="on"]`, where no theme selector can collide
  // with them. Read once per render -- every message resolves the same toggle.
  const avatars = S.showChatAvatars;
  ct.dataset.avatars = avatars ? "on" : "off";
  preserveScrollDistance(
    () => ct,
    50,
    () => {
      let streamingEl = null;
      let badgeEl = null;
      if (S.isStreaming) {
        streamingEl = S.streamingBodyEl?.closest(".message") ?? null;
        badgeEl = document.getElementById("active-director-badge");
      }
      if (!S.activeConvId) {
        ct.innerHTML =
          '<div class="empty-state"><div class="icon" id="home-greeting-icon">📜</div><div id="home-greeting">Select a character to begin</div><div class="stats-grid" id="home-stats-grid"></div></div>';
        renderHomeStats();
      } else if (!S.messages.length) {
        ct.innerHTML = S.groupCast
          ? sceneEmptyStateHtml()
          : '<div class="empty-state"><div class="icon">📜</div><div>Start writing to begin the scene</div></div>';
      } else {
        let msgs = S.messages;
        if (S.isStreaming && S.streamCutoffIndex != null) {
          msgs = S.messages.slice(0, S.streamCutoffIndex);
        }
        const start = Math.min(Math.max(S.renderWindowStart | 0, 0), msgs.length);
        if (start > 0) msgs = msgs.slice(start);
        renderedMsgs = msgs;
        const childByParent = new Map();
        for (const c of S.messages) {
          if (c.role === "assistant" && c.parent_id != null && !childByParent.has(c.parent_id)) {
            childByParent.set(c.parent_id, c);
          }
        }
        // Reuse the bubbles whose markup did not change. A branch swipe or a
        // mid-stream repaint then rebuilds only the rows that actually differ,
        // instead of replaying the whole list's entrance animation and layout.
        const fresh = reconcileChildren(
          ct,
          // An aborted turn can leave two id-less rows in the list (the pending user
          // message and the unpersisted reply), so they key by position, not by role.
          msgs.map((m, i) => ({ key: m.id ? `m${m.id}` : `p${i}`, html: _messageHtml(m, childByParent, avatars) })),
          "msg-swap",
        );
        // Seed the new bubbles' intrinsic sizes before the scroll math below
        // reads scrollHeight, or a node that has never been rendered still
        // counts as the 300px placeholder and the restore lands short.
        _measureIntrinsicSizes(fresh);
      }
      if (badgeEl) ct.appendChild(badgeEl);
      if (streamingEl && !S.hideStreamingBox && !S.hideUntilBaked) {
        syncStreamingAvatar(streamingEl, avatars);
        ct.appendChild(streamingEl);
      }
      renderTurnError(ct);
    },
    { forceBottom },
  );
  if (!S.isStreaming) updateContextCounter();
  _refreshWorkflowViewportObserver();
  _segmentRenderedMessages(renderedMsgs);
}

export function _applyWorkflowTextSegments(bodyEl, msg) {
  segmentBody(bodyEl);
  markClickable(bodyEl, msg);
}

function _segmentRenderedMessages(renderedMsgs) {
  if (!S.workflowTextEffects.length && !S.workflowClickHandlers.length) return;
  if (!renderedMsgs) return;
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

let _contextCounterTimer = null;

// Every repaint asks for this, and the endpoint re-renders the whole prompt to
// estimate it — a burst of swipes would otherwise queue one of the most
// expensive requests in the app behind each click. Coalesce them; the counter
// is a display, so only the last answer matters.
export function updateContextCounter() {
  if (_contextCounterTimer) clearTimeout(_contextCounterTimer);
  _contextCounterTimer = setTimeout(() => {
    _contextCounterTimer = null;
    fetchContextSize();
  }, 250);
}

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
  } catch (_e) {}
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
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="ctx-total">~${total.toLocaleString()} tokens <span class="ctx-msgs">(${data.message_count} msgs)</span></span>
    </summary>
    <div class="ctx-rows">${rows}</div>
  </details>`;
}
