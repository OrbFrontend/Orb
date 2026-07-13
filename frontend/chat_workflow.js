// Workflow attachment widgets: the per-message swipe/regen/reroll/rehydrate/
// delete controls, the cross-tab mutation listener, and the viewport-visibility
// access reporter. Split out of chat.js; rendering entry points
// (_renderWorkflowArtifacts / _renderWorkflowRejection / _refreshWorkflowViewportObserver)
// are consumed by chat_core's renderMessages, and the public surface is
// re-exported from chat.js.
import { api } from "./api.js";
import { ICON_CHEVRON, ICON_DEL, ICON_REGEN, ICON_REROLL, renderMessages, setMessages } from "./chat_core.js";
import { clearWorkflowPhase, setWorkflowPhase, workflowPhaseLabel } from "./chat_inspector.js";
import { renderDefaultWidget } from "./default_widget.js";
import { closeModal, showModal } from "./modal.js";
import { effectiveWorkflowEnabled, S } from "./state.js";
import { broadcastWorkflowMutation, requestSendPermission, setWorkflowMutationCallback } from "./tabLock.js";
import { convUrl, esc, toast } from "./utils.js";

// Eviction sentinel for workflow attachment bytes -- must match
// `EVICTED_MARKER` in backend/workflows/attachment_cache.py.
const WORKFLOW_ATT_EVICTED_MARKER = "[evicted]";

function _isAttachmentEvicted(att) {
  const v = att.b64 || att.data_b64 || "";
  return v === WORKFLOW_ATT_EVICTED_MARKER;
}

function _evictedAttachmentHtml(msg, att) {
  const filename = esc(att.filename || att.workflow_id || "artifact");
  const canRehydrate = !!att.seed;
  let btn;
  if (!canRehydrate) {
    btn = `<span class="workflow-rehydrate-disabled" title="No stored seed -- bytes cannot be recovered">Bytes evicted</span>`;
  } else if (!effectiveWorkflowEnabled(att.workflow_id)) {
    // Rehydrate re-runs the workflow's generative hook (gated server-side when
    // off), so the action is suppressed; the evicted-card display is consumption
    // and stays. Restoring the bytes requires re-enabling the workflow.
    btn = `<span class="workflow-rehydrate-disabled" title="Re-enable ${esc(_workflowLabel(att))} to restore">Workflow off</span>`;
  } else {
    btn = `<button class="workflow-rehydrate-button" onclick="event.stopPropagation();workflowRehydrate(${msg.id},${att.id},this)">Rehydrate</button>`;
  }
  return `<div class="workflow-artifact-evicted">
    <span class="workflow-artifact-evicted-label">${filename}</span>
    ${btn}
  </div>`;
}

function _workflowRegenButtonHtml(msg, att) {
  const wid = att.workflow_id;
  if (!wid) return "";
  const entry = S.workflowManifest.find((w) => w.id === wid);
  if (!entry) return "";
  if (!effectiveWorkflowEnabled(wid)) return "";
  return `<button class="workflow-regen-button" title="Regenerate" onclick="event.stopPropagation();workflowRegenerate(${msg.id},${att.id},this)">${ICON_REGEN}</button>`;
}

function _workflowRerollButtonHtml(msg, att) {
  const wid = att.workflow_id;
  if (!wid) return "";
  const entry = S.workflowManifest.find((w) => w.id === wid);
  if (!entry) return "";
  if (!effectiveWorkflowEnabled(wid)) return "";
  return `<button class="workflow-reroll-button" title="Reroll" onclick="event.stopPropagation();workflowReroll(${msg.id},${att.id},this)">${ICON_REROLL}</button>`;
}

function _activeAttachmentForGroup(atts, root) {
  // active_sibling_id lives on the root row only; NULL renders the
  // newest sibling as active.
  if (!atts.length) return null;
  if (atts.length === 1) return atts[0];
  const activeId = root?.active_sibling_id;
  if (activeId == null) return atts[atts.length - 1];
  const found = atts.find((a) => a.id === activeId);
  return found || atts[atts.length - 1];
}

function _activeIndexForGroup(atts, root) {
  const active = _activeAttachmentForGroup(atts, root);
  if (!active) return 0;
  const idx = atts.indexOf(active);
  return idx >= 0 ? idx : 0;
}

// Returns "" for an empty list so callers can concat the result unconditionally.
// Entries with originating_attachment_id null vs a root_id are split upstream
// (one renders as a footer chip, the other beside a widget); this helper does
// not distinguish them.
function _workflowRejectionChipHtml(entries) {
  if (!entries.length) return "";
  const items = entries.map((r) => `${esc(r.filename || r.workflow_id || "artifact")} (${esc(r.reason)})`).join(", ");
  return `<div class="workflow-rejected-warning">Workflow attachment(s) rejected: ${items}</div>`;
}

// Human label for a widget's chrome header: the owning workflow's manifest
// display_name (its human-readable name), else the raw workflow id. The
// attachment filename is deliberately not used -- a per-file name like
// "speech.mp3" is noise next to the workflow that produced it.
function _workflowLabel(att) {
  const entry = S.workflowManifest.find((w) => w.id === att.workflow_id);
  return entry?.display_name || att.workflow_id || "artifact";
}

// Minimized workflow-artifact groups, keyed by root attachment id. Persisted to
// localStorage so a collapsed widget stays collapsed across reloads and tabs.
// A stale id (its attachment deleted) is harmless -- it never matches a rendered
// group; _deleteWorkflowAttachment drops the id when its group is deleted.
const WF_MINIMIZED_LS_KEY = "orb.workflowMinimized";

function _loadWorkflowMinimized() {
  try {
    const arr = JSON.parse(localStorage.getItem(WF_MINIMIZED_LS_KEY) || "[]");
    return new Set(Array.isArray(arr) ? arr.filter((x) => Number.isInteger(x)) : []);
  } catch {
    return new Set();
  }
}

const _workflowMinimized = _loadWorkflowMinimized();

function _persistWorkflowMinimized() {
  try {
    localStorage.setItem(WF_MINIMIZED_LS_KEY, JSON.stringify([..._workflowMinimized]));
  } catch (e) {
    console.warn("persist workflow-minimized failed", e);
  }
}

function _renderWorkflowSwipeContainer(msg, rootId, atts) {
  const instanceId = `ws-${msg.id}-${rootId}`;
  const total = atts.length;
  const root = atts.find((a) => a.id === rootId) || atts[0];
  const idx = _activeIndexForGroup(atts, root);
  const active = atts[idx];
  const minimized = _workflowMinimized.has(rootId);
  const label = esc(_workflowLabel(active));
  // The variant count rides the label only while collapsed; expanded widgets
  // already show it in the swipe counter below the body.
  const countBadge = minimized && total > 1 ? ` <span class="workflow-artifact-label-count">(${total})</span>` : "";
  // Framework chrome: a label plus minimize/delete controls, distinct from the
  // author-owned widget body and the regen/reroll buttons that live inside it.
  // Minimize is a local view toggle (no tab lock); delete mutates server state
  // and routes through a confirm dialog.
  const header = `<div class="workflow-artifact-header">
      <span class="workflow-artifact-label" title="${label}">${label}${countBadge}</span>
      <div class="workflow-artifact-controls">
        <button class="workflow-chrome-btn workflow-min-btn${minimized ? " collapsed" : ""}" title="${minimized ? "Expand" : "Minimize"}" aria-expanded="${minimized ? "false" : "true"}" onclick="event.stopPropagation();workflowToggleMinimize('${instanceId}')">${ICON_CHEVRON}</button>
        <button class="workflow-chrome-btn workflow-del-btn" title="Delete" onclick="event.stopPropagation();workflowDeleteAttachment('${instanceId}')">${ICON_DEL}</button>
      </div>
    </div>`;
  // Rejections that target this swipe group's root render as a sibling of the
  // swipe card under .workflow-artifacts, not inside it, so the user sees which
  // artifact failed without a full-width banner crowding the artifact card.
  const widgetRejected = S.rejectedWorkflowAtts.filter(
    (r) => r.message_id === msg.id && r.originating_attachment_id === rootId,
  );
  const rejectionChip = _workflowRejectionChipHtml(widgetRejected);
  if (minimized) {
    return `<div class="workflow-artifact-swipe minimized" id="${instanceId}" data-msg-id="${msg.id}" data-root-id="${rootId}">
    ${header}
  </div>${rejectionChip}`;
  }
  const regenBtn = _workflowRegenButtonHtml(msg, active);
  const rerollBtn = _workflowRerollButtonHtml(msg, active);
  const actionButtons = regenBtn + rerollBtn;
  let bodyHtml;
  if (_isAttachmentEvicted(active)) {
    bodyHtml = _evictedAttachmentHtml(msg, active) + actionButtons;
  } else {
    const defaultHtml = renderDefaultWidget(active) + actionButtons;
    const renderer = S.workflowAttachmentRenderers[active.workflow_id];
    let widgetHtml;
    if (typeof renderer === "function") {
      try {
        widgetHtml = renderer({ att: active, buttons: { regen: regenBtn, reroll: rerollBtn }, defaultHtml }) || "";
      } catch (e) {
        console.error("widget for", active.workflow_id, "att", active.id, "threw:", e);
        widgetHtml = defaultHtml;
      }
    } else {
      widgetHtml = defaultHtml;
    }
    bodyHtml = `<div class="workflow-widget" data-workflow-id="${esc(active.workflow_id)}" data-attachment-id="${active.id}">${widgetHtml}</div>`;
  }
  const indicator = total > 1 ? `<span class="workflow-artifact-counter">${idx + 1} / ${total}</span>` : "";
  // No cycling: each arrow dies at its end of the list (also when there is only
  // one sibling).
  const prevDisabled = total <= 1 || idx === 0 ? " disabled" : "";
  const nextDisabled = total <= 1 || idx === total - 1 ? " disabled" : "";
  return `<div class="workflow-artifact-swipe" id="${instanceId}" data-msg-id="${msg.id}" data-root-id="${rootId}">
    ${header}
    <div class="workflow-artifact-nav">
      <button class="workflow-swipe-btn"${prevDisabled} onclick="event.stopPropagation();workflowArtifactStep('${instanceId}',-1)">&#9664;</button>
      <div class="workflow-artifact-body">${bodyHtml}</div>
      <button class="workflow-swipe-btn"${nextDisabled} onclick="event.stopPropagation();workflowArtifactStep('${instanceId}',1)">&#9654;</button>
    </div>
    ${indicator}
  </div>${rejectionChip}`;
}

function _workflowAttachmentGroups(msg) {
  const workflowAtts = msg.workflow_attachments || [];
  if (!workflowAtts.length) return [];
  const byId = new Map();
  for (const a of workflowAtts) byId.set(a.id, a);
  const groups = new Map();
  for (const a of workflowAtts) {
    const parent = a.parent_attachment_id;
    const rootId = parent && byId.has(parent) ? parent : a.id;
    if (!groups.has(rootId)) groups.set(rootId, []);
    groups.get(rootId).push(a);
  }
  const list = [];
  for (const [rootId, atts] of groups) {
    atts.sort((a, b) => a.id - b.id);
    list.push({ rootId, atts });
  }
  list.sort((a, b) => a.rootId - b.rootId);
  return list;
}

export function _renderWorkflowArtifacts(msg) {
  const groups = _workflowAttachmentGroups(msg);
  if (!groups.length) return "";
  const containers = groups.map((g) => _renderWorkflowSwipeContainer(msg, g.rootId, g.atts));
  return `<div class="workflow-artifacts">${containers.join("")}</div>`;
}

// Renders rejections whose originating_attachment_id is null -- SSE
// assistant-persist rejections for which no DB row exists to attach to.
// Per-widget rejections (root_id-tagged) are rendered by
// _renderWorkflowSwipeContainer instead.
export function _renderWorkflowRejection(msg) {
  const rejected = S.rejectedWorkflowAtts.filter((r) => r.message_id === msg.id && r.originating_attachment_id == null);
  return _workflowRejectionChipHtml(rejected);
}

// Per-rootId in-flight lock for workflowArtifactStep. Two rapid arrow
// clicks on the same root within network RTT can produce overlapping
// POSTs whose responses return in indeterminate order, leaving the
// server's active_sibling_id on the earlier-click sibling while the UI
// shows the later-click one (optimistic paint reconciles only on next
// setMessages refetch). Drop-the-second-click semantic keeps UI and
// server consistent at the cost of one dropped fast click. The value
// is `{ msgId, activeId }` so the cross-tab refetch listener can both
// gate per-msgId and re-apply the in-flight optimistic active_sibling_id
// after any wholesale setMessages it issues.
const _workflowSwipeInFlight = new Map();

window.workflowArtifactStep = async (instanceId, delta) => {
  const el = document.getElementById(instanceId);
  if (!el) return;
  const msgId = Number(el.dataset.msgId);
  const rootId = Number(el.dataset.rootId);
  const msg = S.messages.find((m) => m.id === msgId);
  if (!msg) return;
  const group = _workflowAttachmentGroups(msg).find((g) => g.rootId === rootId);
  if (!group || group.atts.length <= 1) return;
  if (_workflowSwipeInFlight.has(rootId)) return;
  if (!requestSendPermission()) return;
  const root = group.atts.find((a) => a.id === rootId) || group.atts[0];
  const cur = _activeIndexForGroup(group.atts, root);
  const next = cur + delta;
  // No wrap: arrows are disabled at the ends, but guard anyway so a step past
  // either end (rapid clicks, cross-tab races) is a no-op rather than a cycle.
  if (next < 0 || next >= group.atts.length) return;
  const newActiveId = group.atts[next].id;
  _workflowSwipeInFlight.set(rootId, { msgId, activeId: newActiveId });
  // Local mutation first so the swipe feels instant; reconcile on next
  // server fetch if the POST fails.
  if (root) root.active_sibling_id = newActiveId;
  el.outerHTML = _renderWorkflowSwipeContainer(msg, rootId, group.atts);
  try {
    await api.post(convUrl(S.activeConvId, "messages", msgId, "workflow-attachments", rootId, "activate"), {
      sibling_id: newActiveId,
    });
    // Record the swipe as an access on the new sibling so the LRU
    // picker sees the row the user is now viewing as recently
    // accessed. Independent of the per-msg viewport dedup.
    _workflowViewportPendingIds.add(newActiveId);
    _scheduleWorkflowViewportFlush();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    console.warn("workflow-attachments activate POST failed", e);
  } finally {
    _workflowSwipeInFlight.delete(rootId);
  }
};

// Per-attId in-flight lock for workflowRehydrate. Each evicted workflow
// attachment renders its own Rehydrate button; without this lock a fast
// double-click on the same button (or rapid clicks on distinct evicted
// buttons within one message) can produce overlapping POSTs that both
// hit the cache helper's precondition re-check and surface the 409 race.
// Per-tab module scope; cross-tab duplicates are handled by the route's
// 409 mapping below. The value is the owning message id, consumed by
// the cross-tab refetch guard.
const _workflowRehydrateInFlight = new Map();

window.workflowRehydrate = async (msgId, attId, btn) => {
  if (!S.activeConvId) return;
  if (!requestSendPermission()) return;
  if (_workflowRehydrateInFlight.has(attId)) return;
  _workflowRehydrateInFlight.set(attId, msgId);
  btn.disabled = true;
  const container = btn.closest(".workflow-artifact-swipe");
  const wid = _resolveWorkflowId(msgId, attId);
  const ch = `workflow:${wid || "op"}:rehydrate:${attId}`;
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "restoring..."));
    await api.post(convUrl(S.activeConvId, "messages", msgId, "workflow-attachments", attId, "rehydrate"), {});
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    // 409 means a concurrent rehydrate (or this tab's prior in-flight
    // POST) already restored the bytes. End state is correct -- refetch
    // and rerender so the UI reflects the restored bytes, no chip.
    if (e && e.status === 409) {
      try {
        setMessages(await api.get(convUrl(S.activeConvId, "messages")));
        renderMessages();
        broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
      } catch (e2) {
        console.warn("Rehydrate post-409 refetch failed", e2);
      }
    } else {
      console.error("Rehydrate failed:", e);
      if (container && !container.querySelector(".workflow-rehydrate-error")) {
        const cap = document.createElement("div");
        cap.className = "workflow-rehydrate-error";
        cap.textContent = "Rehydrate failed";
        container.appendChild(cap);
      }
    }
  } finally {
    clearWorkflowPhase(ch);
    _workflowRehydrateInFlight.delete(attId);
    btn.disabled = false;
  }
};

// Per-rootId in-flight lock covering both Regenerate and Reroll. Both ops
// publish their result into the shared S.rejectedWorkflowAtts list via a
// drop-then-append merge keyed by (message_id, root_id). Two ops targeting
// different siblings of the same root therefore share the same merge key;
// without serialization, the second response's drop step would erase the
// first response's just-appended entries. The server already serializes
// per-root via _workflow_root_lock, so this lock matches that grain and
// allows full parallelism across distinct roots. The value is the owning
// message id, consumed by the cross-tab refetch guard.
const _workflowActionInFlight = new Map();

// Falls back to attId when the message or attachment is no longer locally
// known (closure outlived a refetch). attId is always a real id at click
// time, so the fallback still yields a key serializable against itself.
function _resolveWorkflowRootId(msgId, attId) {
  const msg = S.messages.find((m) => m.id === msgId);
  const atts = msg?.workflow_attachments;
  if (!atts) return attId;
  const att = atts.find((a) => a.id === attId);
  if (!att) return attId;
  return att.parent_attachment_id || attId;
}

// The workflow id owning an attachment, for keying that workflow's status pill;
// null when the row has left local state (a closure outliving a refetch).
function _resolveWorkflowId(msgId, attId) {
  const msg = S.messages.find((m) => m.id === msgId);
  const att = msg?.workflow_attachments?.find((a) => a.id === attId);
  return att?.workflow_id || null;
}

// Drops existing entries whose (message_id, originating_attachment_id)
// tuple matches the operation's key, then appends the response entries
// with message_id injected. Drop-then-append guarantees an empty response
// clears stale entries for the same key, and that an operation cannot
// erase entries belonging to a different (msg, originating) key.
export function _mergeWorkflowRejections(msgId, originatingId, incoming) {
  S.rejectedWorkflowAtts = S.rejectedWorkflowAtts
    .filter((r) => !(r.message_id === msgId && r.originating_attachment_id === originatingId))
    .concat(incoming.map((e) => ({ ...e, message_id: msgId })));
}

window.workflowRegenerate = async (msgId, attId, btn) => {
  if (!S.activeConvId) return;
  if (!requestSendPermission()) return;
  const rootId = _resolveWorkflowRootId(msgId, attId);
  if (_workflowActionInFlight.has(rootId)) return;
  _workflowActionInFlight.set(rootId, msgId);
  const container = btn.closest(".workflow-artifact-swipe");
  btn.disabled = true;
  const wid = _resolveWorkflowId(msgId, attId);
  const ch = `workflow:${wid || "op"}:regen:${rootId}`;
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "regenerating..."));
    const result = await api.post(
      convUrl(S.activeConvId, "messages", msgId, "workflow-attachments", attId, "regenerate"),
      {},
    );
    const incoming = result && Array.isArray(result.rejected_workflow_atts) ? result.rejected_workflow_atts : [];
    _mergeWorkflowRejections(msgId, rootId, incoming);
    // Dispatcher writes active_sibling_id = new sibling for each new row,
    // so the refreshed state already points the renderer at the latest.
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    console.error("Regenerate failed:", e);
    if (container && !container.querySelector(".workflow-regen-error")) {
      const cap = document.createElement("div");
      cap.className = "workflow-regen-error";
      cap.textContent = "Regenerate failed";
      container.appendChild(cap);
    }
  } finally {
    clearWorkflowPhase(ch);
    _workflowActionInFlight.delete(rootId);
    btn.disabled = false;
  }
};

window.workflowReroll = async (msgId, attId, btn) => {
  if (!S.activeConvId) return;
  if (!requestSendPermission()) return;
  const rootId = _resolveWorkflowRootId(msgId, attId);
  if (_workflowActionInFlight.has(rootId)) return;
  _workflowActionInFlight.set(rootId, msgId);
  const container = btn.closest(".workflow-artifact-swipe");
  btn.disabled = true;
  const wid = _resolveWorkflowId(msgId, attId);
  const ch = `workflow:${wid || "op"}:reroll:${rootId}`;
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "rerolling..."));
    const result = await api.post(
      convUrl(S.activeConvId, "messages", msgId, "workflow-attachments", attId, "reroll-gen"),
      {},
    );
    const incoming = result && Array.isArray(result.rejected_workflow_atts) ? result.rejected_workflow_atts : [];
    _mergeWorkflowRejections(msgId, rootId, incoming);
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    console.error("Reroll failed:", e);
    if (container && !container.querySelector(".workflow-reroll-error")) {
      const cap = document.createElement("div");
      cap.className = "workflow-reroll-error";
      cap.textContent = "Reroll failed";
      container.appendChild(cap);
    }
  } finally {
    clearWorkflowPhase(ch);
    _workflowActionInFlight.delete(rootId);
    btn.disabled = false;
  }
};

// View-only collapse of a workflow-artifact group to its header strip so it
// stops taking vertical space. Pure local UI state (per-tab, persisted to
// localStorage), touching no server state -- so unlike regenerate/reroll/swipe
// it is not gated on the single-writer tab lock. Re-renders just this widget in
// place, the same surgical outerHTML swap workflowArtifactStep uses.
window.workflowToggleMinimize = (instanceId) => {
  const el = document.getElementById(instanceId);
  if (!el) return;
  const msgId = Number(el.dataset.msgId);
  const rootId = Number(el.dataset.rootId);
  const msg = S.messages.find((m) => m.id === msgId);
  if (!msg) return;
  const group = _workflowAttachmentGroups(msg).find((g) => g.rootId === rootId);
  if (!group) return;
  if (_workflowMinimized.has(rootId)) _workflowMinimized.delete(rootId);
  else _workflowMinimized.add(rootId);
  _persistWorkflowMinimized();
  el.outerHTML = _renderWorkflowSwipeContainer(msg, rootId, group.atts);
};

// Opens the delete-choice dialog. "Current child" is the variant on screen
// (the active sibling); "parent as a whole" is the entire root group. A
// single-variant group has no fork, so it gets a plain confirm. The chosen
// target is parked in _wfDeleteTarget for workflowConfirmDelete to consume.
let _wfDeleteTarget = null;

window.workflowDeleteAttachment = (instanceId) => {
  const el = document.getElementById(instanceId);
  if (!el) return;
  const msgId = Number(el.dataset.msgId);
  const rootId = Number(el.dataset.rootId);
  const msg = S.messages.find((m) => m.id === msgId);
  if (!msg) return;
  const group = _workflowAttachmentGroups(msg).find((g) => g.rootId === rootId);
  if (!group) return;
  const root = group.atts.find((a) => a.id === rootId) || group.atts[0];
  const idx = _activeIndexForGroup(group.atts, root);
  const active = group.atts[idx];
  const total = group.atts.length;
  const label = esc(_workflowLabel(active));
  _wfDeleteTarget = { msgId, rootId, activeId: active.id };
  if (total <= 1) {
    showModal(`
      <h2>Delete attachment</h2>
      <p>Delete <strong>${label}</strong>? This cannot be undone.</p>
      <div class="workflow-delete-actions">
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn btn-danger" onclick="workflowConfirmDelete('group')">Delete</button>
      </div>`);
    return;
  }
  showModal(`
    <h2>Delete attachment</h2>
    <p><strong>${label}</strong> has ${total} variants. Delete only the one you are viewing (${idx + 1} / ${total}), or the whole attachment and every variant?</p>
    <div class="workflow-delete-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" onclick="workflowConfirmDelete('variant')">Delete this variant</button>
      <button class="btn btn-danger" onclick="workflowConfirmDelete('group')">Delete all ${total}</button>
    </div>`);
};

window.workflowConfirmDelete = (scope) => {
  const t = _wfDeleteTarget;
  _wfDeleteTarget = null;
  closeModal();
  if (!t) return;
  _deleteWorkflowAttachment(t.msgId, t.rootId, t.activeId, scope);
};

// Delete the on-screen variant (scope "variant") or the whole group (scope
// "group"). The path id is the acted-on attachment; the backend derives the
// group root, and when the root variant of a multi-variant group is removed it
// promotes a survivor and returns the resulting root id.
async function _deleteWorkflowAttachment(msgId, rootId, activeId, scope) {
  if (!S.activeConvId) return;
  if (!requestSendPermission()) return;
  if (_workflowActionInFlight.has(rootId)) return;
  _workflowActionInFlight.set(rootId, msgId);
  const aid = scope === "group" ? rootId : activeId;
  try {
    const res = await api.post(convUrl(S.activeConvId, "messages", msgId, "workflow-attachments", aid, "delete"), {
      scope,
    });
    if (res?.group_empty) {
      _workflowMinimized.delete(rootId);
      _persistWorkflowMinimized();
    } else if (res && typeof res.root_id === "number" && res.root_id !== rootId && _workflowMinimized.has(rootId)) {
      // Promotion changed the group root id; carry the collapsed state across.
      _workflowMinimized.delete(rootId);
      _workflowMinimized.add(res.root_id);
      _persistWorkflowMinimized();
    }
    _mergeWorkflowRejections(msgId, rootId, []);
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    renderMessages();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    console.error("Delete failed:", e);
    toast("Delete failed", true);
  } finally {
    _workflowActionInFlight.delete(rootId);
  }
}

// Skip-silently when a local edit, magic input, in-flight workflow op,
// or active stream would be clobbered by a full setMessages refetch; the
// stale state recovers on the next user-initiated refetch or reload.
export function initWorkflowMutationListener() {
  setWorkflowMutationCallback(async ({ convId, msgId }) => {
    if (convId !== S.activeConvId) return;
    if (S.isStreaming) return;
    if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg || S.magicInputMsgId != null)
      return;
    // All three in-flight maps gate per-msgId: refetching mid-POST on
    // the same message races with the op's own reconcile. Swipe also
    // paints active_sibling_id locally before its POST awaits, so the
    // wholesale setMessages below would drop that optimistic value for
    // any in-flight swipe on a different msgId in the same conversation.
    // The re-apply pass after setMessages restores those optimistic ids
    // until each swipe POST lands and the next refetch carries them.
    const inFlightMsgIds = new Set([
      ..._workflowRehydrateInFlight.values(),
      ..._workflowActionInFlight.values(),
      ...Array.from(_workflowSwipeInFlight.values(), (v) => v.msgId),
    ]);
    if (inFlightMsgIds.has(msgId)) return;
    try {
      setMessages(await api.get(convUrl(S.activeConvId, "messages")));
      for (const [rootId, { msgId: swipeMsgId, activeId }] of _workflowSwipeInFlight) {
        const m = S.messages.find((x) => x.id === swipeMsgId);
        if (!m || !Array.isArray(m.workflow_attachments)) continue;
        const root = m.workflow_attachments.find((a) => a.id === rootId);
        if (root) root.active_sibling_id = activeId;
      }
      renderMessages();
    } catch (e) {
      console.warn("cross-tab workflow refetch failed", e);
    }
  });
}

// Refresh the active conversation into S and repaint, for workflows that insert
// or replace attachments out of band -- from an ON_DEMAND trigger, or
// generation that finishes after the turn -- where renderMessages alone would
// paint stale S.messages. Returns false without refetching while a stream,
// edit, or attachment op on msgId is in flight (each refetches on its own
// completion, so the change still surfaces). msgId is the mutated message, or
// null for a blanket refresh.
export async function refreshConversationMessages(msgId = null) {
  if (!S.activeConvId) return false;
  if (S.isStreaming) return false;
  if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg || S.magicInputMsgId != null)
    return false;
  const inFlight = new Set([
    ..._workflowRehydrateInFlight.values(),
    ..._workflowActionInFlight.values(),
    ...Array.from(_workflowSwipeInFlight.values(), (v) => v.msgId),
  ]);
  if (msgId != null && inFlight.has(msgId)) return false;
  try {
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    // A swipe writes its new active_sibling_id locally before its POST
    // resolves; the refetch above drops that optimistic value, so reapply it
    // for any swipe still in flight.
    for (const [rootId, { msgId: swipeMsgId, activeId }] of _workflowSwipeInFlight) {
      const m = S.messages.find((x) => x.id === swipeMsgId);
      if (!m || !Array.isArray(m.workflow_attachments)) continue;
      const root = m.workflow_attachments.find((a) => a.id === rootId);
      if (root) root.active_sibling_id = activeId;
    }
    renderMessages();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
    return true;
  } catch (e) {
    console.warn("refreshConversationMessages failed", e);
    return false;
  }
}

// ── Workflow-attachment access tracking
//
// Reports viewport-visible workflow attachments to the backend's LRU-3
// access counter so the cache picker favors rows the user is looking at.
//
// Per-msg dedup (_workflowObservedMsgIds) is required because
// renderMessages destroys and recreates DOM, so the observer must
// re-attach every render; without the set, each re-attach re-reports
// every visible msg.
//
// Swipe-bump is independent of the dedup set -- workflowArtifactStep
// pushes the new active att-id on every swipe so the picker sees the
// row the user just swiped to as recently accessed.

const _workflowViewportPendingIds = new Set();
const _workflowObservedMsgIds = new Set();
let _workflowViewportFlushTimer = null;

// Reset viewport-tracking state, called when opening a conversation so each
// conv-open starts a fresh "what has been reported" session.
export function resetWorkflowViewportState() {
  _workflowObservedMsgIds.clear();
  _workflowViewportPendingIds.clear();
  if (_workflowViewportFlushTimer) {
    clearTimeout(_workflowViewportFlushTimer);
    _workflowViewportFlushTimer = null;
  }
}

function _activeAttachmentIdsForMessage(msg) {
  // Posts the id of the row the renderer is currently displaying per
  // group, not the group root. Birth/rehydrate already record specific
  // row ids (attachment_cache._record_access_inner([att_id])); viewport
  // reports mirror that granularity so the picker's recent_accesses
  // tracks bytes the user actually sees.
  const groups = _workflowAttachmentGroups(msg);
  if (!groups.length) return [];
  const ids = [];
  for (const g of groups) {
    const root = g.atts.find((a) => a.id === g.rootId) || g.atts[0];
    const active = _activeAttachmentForGroup(g.atts, root);
    if (active) ids.push(active.id);
  }
  return ids;
}

const _workflowViewportObserver =
  typeof IntersectionObserver !== "undefined"
    ? new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            const msgId = Number(entry.target.dataset.msgId);
            if (_workflowObservedMsgIds.has(msgId)) continue;
            _workflowObservedMsgIds.add(msgId);
            const msg = S.messages.find((m) => m.id === msgId);
            if (!msg) continue;
            for (const id of _activeAttachmentIdsForMessage(msg)) {
              _workflowViewportPendingIds.add(id);
            }
          }
          if (_workflowViewportPendingIds.size) _scheduleWorkflowViewportFlush();
        },
        { rootMargin: "0px", threshold: 0.1 },
      )
    : null;

function _scheduleWorkflowViewportFlush() {
  if (_workflowViewportFlushTimer) return;
  _workflowViewportFlushTimer = setTimeout(_flushWorkflowViewportReport, 250);
}

async function _flushWorkflowViewportReport() {
  _workflowViewportFlushTimer = null;
  if (!_workflowViewportPendingIds.size || !S.activeConvId) return;
  const ids = [..._workflowViewportPendingIds];
  _workflowViewportPendingIds.clear();
  try {
    await api.post(convUrl(S.activeConvId, "workflow-attachments", "access"), { ids });
  } catch (e) {
    console.warn("workflow-attachments access (viewport) failed", e);
  }
}

export function _refreshWorkflowViewportObserver() {
  if (!_workflowViewportObserver) return;
  _workflowViewportObserver.disconnect();
  for (const el of document.querySelectorAll("#chat-messages .message[data-msg-id]")) {
    const msgId = Number(el.dataset.msgId);
    const msg = S.messages.find((m) => m.id === msgId);
    if (msg?.workflow_attachments?.length) {
      _workflowViewportObserver.observe(el);
    }
  }
}
