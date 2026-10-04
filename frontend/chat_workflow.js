import { registerActions } from "./actions.js";
import { api } from "./api.js";
import {
  ICON_CHEVRON,
  ICON_DEL,
  ICON_REGEN,
  ICON_REROLL,
  renderMessages,
  setMessages,
  setWorkflowMessagePresentation,
} from "./chat_core.js";
import { clearWorkflowPhase, setWorkflowPhase, workflowPhaseLabel } from "./chat_inspector.js";
import { renderDefaultWidget } from "./default_widget.js";
import { patchHtml } from "./dom_reconcile.js";
import { showConfirmModal } from "./modal.js";
import { sseEvents, streamPost } from "./sse.js";
import { conversationState, effectiveWorkflowEnabled, S } from "./state.js";
import { broadcastWorkflowMutation, requestSendPermission, setWorkflowMutationCallback } from "./tabLock.js";
import { $, boolFlag, convUrl, esc, escAttr, markChatProgrammaticScroll, toast } from "./utils.js";
import { startWorkflowJob, stopButtonState } from "./workflow_jobs.js";

function _isAttachmentEvicted(att) {
  return boolFlag(att.evicted);
}

function _evictedAttachmentHtml(msg, att) {
  const filename = esc(att.filename || att.workflow_id || "artifact");
  const canRehydrate = !!att.seed;
  let btn;
  if (!canRehydrate) {
    btn = `<span class="workflow-rehydrate-disabled" title="No stored seed -- bytes cannot be recovered">Bytes evicted</span>`;
  } else if (!effectiveWorkflowEnabled(att.workflow_id)) {
    btn = `<span class="workflow-rehydrate-disabled" title="Re-enable ${escAttr(_workflowLabel(att))} to restore">Workflow off</span>`;
  } else {
    const stop = stopButtonState(_workflowRehydrateInFlight.get(att.id)?.job, "Rehydrate");
    btn = `<button class="workflow-rehydrate-button${stop.cls}"${stop.attrs} data-wf-action="artifact:rehydrate" data-msg-id="${msg.id}" data-att-id="${att.id}"><span>Rehydrate</span></button>`;
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
  const stop = stopButtonState(_runningAction(att, "regen"), "Regenerate");
  return `<button class="workflow-regen-button${stop.cls}"${stop.attrs} data-wf-action="artifact:regenerate" data-msg-id="${msg.id}" data-att-id="${att.id}">${ICON_REGEN}</button>`;
}

function _workflowRerollButtonHtml(msg, att) {
  const wid = att.workflow_id;
  if (!wid) return "";
  const entry = S.workflowManifest.find((w) => w.id === wid);
  if (!entry) return "";
  if (!effectiveWorkflowEnabled(wid)) return "";
  const stop = stopButtonState(_runningAction(att, "reroll"), "Reroll");
  return `<button class="workflow-reroll-button${stop.cls}"${stop.attrs} data-wf-action="artifact:reroll" data-msg-id="${msg.id}" data-att-id="${att.id}">${ICON_REROLL}</button>`;
}

// The job of the regenerate or reroll (*kind*) running on *att*'s group.
function _runningAction(att, kind) {
  const run = _workflowActionInFlight.get(att.parent_attachment_id || att.id);
  return run?.kind === kind ? run.job : null;
}

/** Return the attachment render job, or null. Its Stop control calls `job.stop()`. */
export function workflowActionJob(msgId, attId) {
  return (
    _workflowActionInFlight.get(_resolveWorkflowRootId(msgId, attId))?.job ||
    _workflowRehydrateInFlight.get(attId)?.job ||
    null
  );
}

function _activeAttachmentForGroup(atts, root) {
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

function _workflowRejectionChipHtml(entries) {
  if (!entries.length) return "";
  const items = entries.map((r) => `${esc(r.filename || r.workflow_id || "artifact")} (${esc(r.reason)})`).join(", ");
  return `<div class="workflow-rejected-warning">Workflow attachment(s) rejected: ${items}</div>`;
}

function _workflowLabel(att) {
  const entry = S.workflowManifest.find((w) => w.id === att.workflow_id);
  return entry?.display_name || att.workflow_id || "artifact";
}

function _workflowPlacement(wid) {
  return S.workflowAttachmentPlacements[wid]?.placement || "artifact";
}

function _notifyWorkflowRerollSuccess(wid, msgId, attId) {
  const fn = S.workflowRerollSuccess[wid];
  if (typeof fn !== "function") return;
  try {
    fn(msgId, attId);
  } catch (e) {
    console.error(`reroll success callback threw (${wid}):`, e);
  }
}

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
  if (_workflowPlacement(atts[0]?.workflow_id) === "actions") return "";
  const instanceId = `ws-${msg.id}-${rootId}`;
  const total = atts.length;
  const root = atts.find((a) => a.id === rootId) || atts[0];
  const idx = _activeIndexForGroup(atts, root);
  const active = atts[idx];
  const minimized = _workflowMinimized.has(rootId);
  const rawLabel = _workflowLabel(active);
  const label = esc(rawLabel);
  const labelAttr = escAttr(rawLabel);
  const countBadge = minimized && total > 1 ? ` <span class="workflow-artifact-label-count">(${total})</span>` : "";
  const header = `<div class="workflow-artifact-header" data-wf-action="artifact:toggleMinimize">
      <span class="workflow-artifact-label" title="${labelAttr}">${label}${countBadge}</span>
      <div class="workflow-artifact-controls">
        <button class="workflow-chrome-btn workflow-min-btn${minimized ? " collapsed" : ""}" title="${minimized ? "Expand" : "Minimize"}" aria-expanded="${minimized ? "false" : "true"}" data-wf-action="artifact:toggleMinimize">${ICON_CHEVRON}</button>
        <button class="workflow-chrome-btn workflow-del-btn" title="Delete" data-wf-action="artifact:delete">${ICON_DEL}</button>
      </div>
    </div>`;
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
        widgetHtml =
          renderer({
            att: active,
            buttons: { regen: regenBtn, reroll: rerollBtn },
            defaultHtml,
            siblings: atts,
            msgId: msg.id,
            rootId,
            job: _runningAction(active, "regen"),
          }) || "";
      } catch (e) {
        console.error("widget for", active.workflow_id, "att", active.id, "threw:", e);
        widgetHtml = defaultHtml;
      }
    } else {
      widgetHtml = defaultHtml;
    }
    bodyHtml = `<div class="workflow-widget" data-workflow-id="${escAttr(active.workflow_id)}" data-attachment-id="${active.id}">${widgetHtml}</div>`;
  }
  const indicator = total > 1 ? `<span class="workflow-artifact-counter">${idx + 1} / ${total}</span>` : "";
  const prevDisabled = total <= 1 || idx === 0 ? " disabled" : "";
  const nextDisabled = total <= 1 || idx === total - 1 ? " disabled" : "";
  return `<div class="workflow-artifact-swipe" id="${instanceId}" data-msg-id="${msg.id}" data-root-id="${rootId}">
    ${header}
    <div class="workflow-artifact-nav">
      <button class="workflow-swipe-btn prev"${prevDisabled} data-wf-action="artifact:step" data-delta="-1">${ICON_CHEVRON}</button>
      <div class="workflow-artifact-body">${bodyHtml}</div>
      <button class="workflow-swipe-btn next"${nextDisabled} data-wf-action="artifact:step" data-delta="1">${ICON_CHEVRON}</button>
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

function _renderWorkflowArtifacts(msg) {
  const groups = _workflowAttachmentGroups(msg).filter((g) => _workflowPlacement(g.atts[0]?.workflow_id) !== "actions");
  if (!groups.length) return "";
  const containers = groups.map((g) => _renderWorkflowSwipeContainer(msg, g.rootId, g.atts));
  return `<div class="workflow-artifacts">${containers.join("")}</div>`;
}

function _renderWorkflowRejection(msg) {
  const rejected = S.rejectedWorkflowAtts.filter((r) => r.message_id === msg.id && r.originating_attachment_id == null);
  return _workflowRejectionChipHtml(rejected);
}

const _workflowSwipeInFlight = new Map();

function _reapplyInFlightSwipes() {
  for (const [rootId, { msgId, activeId }] of _workflowSwipeInFlight) {
    const m = S.messages.find((x) => x.id === msgId);
    if (!m || !Array.isArray(m.workflow_attachments)) continue;
    const root = m.workflow_attachments.find((a) => a.id === rootId);
    if (root) root.active_sibling_id = activeId;
  }
}

// The message and attachment group behind *msgId*/*rootId*, or {} when either is gone.
function _workflowGroup(msgId, rootId) {
  const msg = S.messages.find((m) => m.id === msgId);
  const group = msg && _workflowAttachmentGroups(msg).find((g) => g.rootId === rootId);
  return group ? { msg, group } : {};
}

// The swipe box _renderWorkflowSwipeContainer draws for a group.
const _swipeBox = (msgId, rootId) => document.getElementById(`ws-${msgId}-${rootId}`);

// Keep the paging arrow stationary: preserve card height and compensate for scroll shifts.
function _replaceSwipeKeepingArrow(el, html, delta) {
  const arrowSel = `.workflow-swipe-btn.${delta < 0 ? "prev" : "next"}`;
  const before = el.querySelector(arrowSel)?.getBoundingClientRect().top;
  const height = el.offsetHeight;
  const id = el.id;
  el.outerHTML = html;
  const next = document.getElementById(id);
  if (!next) return;
  next.style.minHeight = `${height}px`;
  const after = next.querySelector(arrowSel)?.getBoundingClientRect().top;
  const ct = $("chat-messages");
  if (!ct || before == null || after == null || after === before) return;
  markChatProgrammaticScroll(400);
  ct.scrollBy({ top: after - before, behavior: "instant" });
}

/** Page an artifact group *delta* takes from the one shown. */
export function stepWorkflowVariant(msgId, rootId, delta) {
  return _activateWorkflowVariant(msgId, rootId, (_atts, cur) => cur + delta);
}

/** Swap *siblingId* in place, save the choice, and notify other tabs. Works during streaming. */
export function activateWorkflowVariant(msgId, rootId, siblingId) {
  return _activateWorkflowVariant(msgId, rootId, (atts) => atts.findIndex((a) => a.id === siblingId));
}

// *pick* answers the index to show, from the group's attachments and the shown index.
async function _activateWorkflowVariant(msgId, rootId, pick) {
  const { msg, group } = _workflowGroup(msgId, rootId);
  if (!group || group.atts.length <= 1) return;
  // A toolbar artifact repaints with its message; a swipe box is replaced in place.
  const inToolbar = _workflowPlacement(group.atts[0]?.workflow_id) === "actions";
  const el = inToolbar ? null : _swipeBox(msgId, rootId);
  if (!inToolbar && !el) return;
  if (_workflowSwipeInFlight.has(rootId)) return;
  const root = group.atts.find((a) => a.id === rootId) || group.atts[0];
  const cur = _activeIndexForGroup(group.atts, root);
  const next = pick(group.atts, cur);
  if (next === cur || next < 0 || next >= group.atts.length) return;
  if (!requestSendPermission()) return;
  const delta = next - cur;
  const newActiveId = group.atts[next].id;
  _workflowSwipeInFlight.set(rootId, { msgId, activeId: newActiveId });
  if (root) root.active_sibling_id = newActiveId;
  if (inToolbar) {
    renderMessages();
    _scrollArtifactIntoView(msgId, rootId);
  } else {
    _replaceSwipeKeepingArrow(el, _renderWorkflowSwipeContainer(msg, rootId, group.atts), delta);
  }
  try {
    await api.post(convUrl(S.activeConvId, "messages", msgId, "workflow-attachments", rootId, "activate"), {
      sibling_id: newActiveId,
    });
    _workflowViewportPendingIds.add(newActiveId);
    _scheduleWorkflowViewportFlush();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    console.warn("workflow-attachments activate POST failed", e);
  } finally {
    _workflowSwipeInFlight.delete(rootId);
  }
}

const _workflowRehydrateInFlight = new Map();

/** Re-render an evicted artifact from its stored recipe; *btn* becomes its Stop button. */
export async function rehydrateWorkflowAttachment(msgId, attId, btn) {
  if (!S.activeConvId) return;
  const convId = S.activeConvId;
  const running = _workflowRehydrateInFlight.get(attId);
  if (running) return running.job.stop();
  if (!requestSendPermission()) return;
  const job = startWorkflowJob({ convId, title: "Stop restoring", messageId: msgId, attachmentId: attId });
  _workflowRehydrateInFlight.set(attId, { msgId, job });
  job.show(btn);
  const container = btn.closest(".workflow-artifact-swipe, [data-root-id]");
  const wid = _resolveWorkflowId(msgId, attId);
  const ch = `workflow:${wid || "op"}:rehydrate:${attId}`;
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "restoring..."), conversationState(convId));
    await api.post(job.url(convUrl(convId, "messages", msgId, "workflow-attachments", attId, "rehydrate")), {});
    await refreshConversationMessages(msgId, convId);
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    // A 409 is a stop, or a restore another request already made: the saved row decides.
    if (e?.status === 409 || job.stopping) {
      try {
        await refreshConversationMessages(msgId, convId);
        broadcastWorkflowMutation({ convId, msgId });
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
    clearWorkflowPhase(ch, conversationState(convId));
    _workflowRehydrateInFlight.delete(attId);
    job.end();
  }
}

const _workflowActionInFlight = new Map();

const _workflowDeleteInFlight = new Map();

function _resolveWorkflowRootId(msgId, attId) {
  const msg = S.messages.find((m) => m.id === msgId);
  const atts = msg?.workflow_attachments;
  if (!atts) return attId;
  const att = atts.find((a) => a.id === attId);
  if (!att) return attId;
  return att.parent_attachment_id || attId;
}

function _resolveWorkflowId(msgId, attId) {
  const msg = S.messages.find((m) => m.id === msgId);
  const att = msg?.workflow_attachments?.find((a) => a.id === attId);
  return att?.workflow_id || null;
}

export function mergeWorkflowRejections(msgId, originatingId, incoming, convId = S.activeConvId) {
  const state = conversationState(convId);
  state.rejectedWorkflowAtts = state.rejectedWorkflowAtts
    .filter((r) => !(r.message_id === msgId && r.originating_attachment_id === originatingId))
    .concat(incoming.map((e) => ({ ...e, message_id: msgId })));
}

function _isNetworkError(e) {
  return e instanceof TypeError && e.status === undefined;
}

function _showActionFailure(container, cls, action, e) {
  if (!container || container.querySelector(`.${cls}`)) return;
  const reason = typeof e?.message === "string" ? e.message.trim() : "";
  const cap = document.createElement("div");
  cap.className = cls;
  cap.textContent = reason && e?.status && e.status !== 500 ? `${action} failed: ${reason}` : `${action} failed`;
  container.appendChild(cap);
}

// A stream that ends without a verdict throws a status-less TypeError, so the caller recovers the sibling.
// The workflow's own events (`ctx.emit`) go to its registered event handlers.
async function _regenerateStreamed(path, wid, onPhase, onSibling) {
  const resp = await streamPost(path, {});
  if (!resp.ok) throw Object.assign(new Error((await resp.json().catch(() => ({}))).detail), { status: resp.status });
  for await (const { event, data } of sseEvents(resp.body)) {
    const payload = JSON.parse(data);
    if (event === "phase_status") onPhase(payload.label);
    else if (event === "regenerate_sibling") await onSibling();
    else if (event === "regenerate_done") return payload;
    else if (event === "regenerate_error") throw Object.assign(new Error(payload.detail), { status: payload.status });
    else _dispatchWorkflowEvent(wid, event, payload);
  }
  throw new TypeError("regenerate stream ended without a result");
}

function _dispatchWorkflowEvent(wid, event, payload) {
  const entry = S.workflowEventHandlers[event];
  if (!wid || entry?.workflowId !== wid || typeof entry.handler !== "function") return;
  try {
    entry.handler(payload, null);
  } catch (e) {
    console.error("workflow event handler for", event, "threw:", e);
  }
}

// Told on every outcome, because a failed or stopped run repaints nothing.
function _notifyWorkflowRegenerateSettled(wid, msgId, rootId) {
  const fn = S.workflowRegenerateSettled[wid];
  if (typeof fn !== "function") return;
  try {
    fn(msgId, rootId);
  } catch (e) {
    console.error(`regenerate settled callback threw (${wid}):`, e);
  }
}

function _rootSiblingIds(msg, rootId) {
  const atts = msg?.workflow_attachments || [];
  return new Set(atts.filter((a) => (a.parent_attachment_id || a.id) === rootId).map((a) => a.id));
}

async function _workflowGroupInFlight(convId, msgId, rootId) {
  try {
    const r = await api.get(convUrl(convId, "messages", msgId, "workflow-attachments", rootId, "in-flight"));
    return !!r?.in_flight;
  } catch (e) {
    // A status means the server answered, just not with a state (404 once the row is gone, 5xx): nothing is running.
    // Only a transport failure leaves the question open, and there waiting is still the right answer.
    return e?.status === undefined;
  }
}

// Whether *msgs* holds a sibling under *rootId* that was not in *before*.
function _siblingLanded(msgs, msgId, rootId, before) {
  const now = _rootSiblingIds(
    msgs.find((m) => m.id === msgId),
    rootId,
  );
  return [...now].some((id) => !before.has(id));
}

// Show a fetched conversation; a sibling that *landed* is scrolled to and announced.
function _showSiblings(convId, msgId, rootId, msgs, landed, onLanded) {
  if (landed) onLanded?.();
  _applyWorkflowMessages(msgs, convId);
  if (S.activeConvId !== convId) return;
  if (landed) {
    _scrollArtifactIntoView(msgId, rootId);
    broadcastWorkflowMutation({ convId, msgId });
  }
}

// *follow*, for a run that saves several siblings, is called as each lands, and
// recovery then lasts until the run ends rather than stopping at the first.
async function _recoverWorkflowSibling(convId, msgId, rootId, before, onSuccess, follow = null) {
  let deadline = Date.now() + 200_000;
  // After a dropped request, a new sibling confirms success. Require two idle polls
  // to cover requests still waiting for the lock, checking for the sibling first.
  const seen = new Set(before);
  let landed = false;
  let idle = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    let msgs;
    try {
      msgs = await api.get(convUrl(convId, "messages"));
    } catch {
      continue;
    }
    if (_siblingLanded(msgs, msgId, rootId, seen)) {
      if (!follow) {
        _showSiblings(convId, msgId, rootId, msgs, true, onSuccess);
        return true;
      }
      landed = true;
      for (const id of _rootSiblingIds(
        msgs.find((m) => m.id === msgId),
        rootId,
      ))
        seen.add(id);
      // The next sibling may take a whole render again.
      deadline = Date.now() + 200_000;
      await follow(msgs);
    }
    if (await _workflowGroupInFlight(convId, msgId, rootId)) idle = 0;
    else if (++idle >= 2) return landed;
  }
  return landed;
}

async function _recoverWorkflowDeletion(convId, msgId, rootId, aid) {
  const deadline = Date.now() + 200_000;
  // Same shape as _recoverWorkflowSibling: the row still being there is not a failure while some request holds the
  // group, but two consecutive "nothing running" answers mean the delete is not coming.
  let idle = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    let msgs;
    try {
      msgs = await api.get(convUrl(convId, "messages"));
    } catch {
      continue;
    }
    const msg = msgs.find((m) => m.id === msgId);
    if ((msg?.workflow_attachments || []).some((a) => a.id === aid)) {
      if (await _workflowGroupInFlight(convId, msgId, rootId)) idle = 0;
      else if (++idle >= 2) return false;
      continue;
    }
    _applyWorkflowMessages(msgs, convId);
    if (!_rootSiblingIds(msg, rootId).size) {
      _workflowMinimized.delete(rootId);
      _persistWorkflowMinimized();
      mergeWorkflowRejections(msgId, rootId, [], convId);
    }
    if (S.activeConvId === convId) {
      _reapplyInFlightSwipes();
      renderMessages();
    }
    broadcastWorkflowMutation({ convId, msgId });
    return true;
  }
  return false;
}

function _scrollArtifactIntoView(msgId, rootId = null) {
  const sel = rootId != null ? `#ws-${msgId}-${rootId}` : `.message[data-msg-id="${msgId}"] .workflow-artifact-swipe`;
  const find = () => {
    const found = $("chat-messages")?.querySelectorAll(sel) || [];
    return found[found.length - 1] || null;
  };
  const el = find();
  if (!el) return;
  const show = () => {
    const ct = $("chat-messages");
    const target = find();
    if (!ct || !target) return;
    const r = target.getBoundingClientRect();
    const box = ct.getBoundingClientRect();
    if (r.top >= box.top && r.bottom <= box.bottom) return;
    markChatProgrammaticScroll(400);
    target.scrollIntoView({ behavior: "smooth", block: r.height <= ct.clientHeight ? "center" : "start" });
  };
  const showWhenVisible = () => {
    if (document.hidden)
      document.addEventListener("visibilitychange", () => requestAnimationFrame(show), { once: true });
    else requestAnimationFrame(show);
  };
  const pending = [...el.querySelectorAll("img")].filter((i) => !i.complete);
  if (!pending.length) return showWhenVisible();
  const loaded = pending.map((img) => {
    img.loading = "eager";
    return new Promise((res) => {
      img.addEventListener("load", res, { once: true });
      img.addEventListener("error", res, { once: true });
    });
  });
  Promise.race([Promise.all(loaded), new Promise((res) => setTimeout(res, 2000))]).then(showWhenVisible);
}

// Stop cancels the render server-side; the request then answers 409, or with
// the sibling if it was already being saved. Either way the saved rows decide.
async function _syncAfterStop(convId, msgId, rootId, before, onLanded) {
  const token = S.conversationViewToken;
  let msgs;
  try {
    msgs = await api.get(convUrl(convId, "messages"));
  } catch (e) {
    console.warn("sync after stopping a workflow render failed", e);
    return;
  }
  if (S.activeConvId !== convId || S.conversationViewToken !== token) return;
  _showSiblings(convId, msgId, rootId, msgs, _siblingLanded(msgs, msgId, rootId, before), onLanded);
}

// A second press on a running regenerate or reroll is its Stop.
function _startWorkflowAction(msgId, attId, btn, kind, title) {
  const rootId = _resolveWorkflowRootId(msgId, attId);
  const running = _workflowActionInFlight.get(rootId);
  if (running) {
    if (running.kind === kind) running.job.stop();
    return null;
  }
  if (!requestSendPermission()) return null;
  const job = startWorkflowJob({ convId: S.activeConvId, title, messageId: msgId, attachmentId: rootId });
  _workflowActionInFlight.set(rootId, { msgId, kind, job });
  job.show(btn);
  return { rootId, job };
}

/** Render a new take of an artifact; *btn* becomes its Stop button. */
export async function regenerateWorkflowAttachment(msgId, attId, btn) {
  if (!S.activeConvId) return;
  const started = _startWorkflowAction(msgId, attId, btn, "regen", "Stop regenerating");
  if (!started) return;
  const { rootId, job } = started;
  const container = btn.closest(".workflow-artifact-swipe, [data-root-id]");
  const wid = _resolveWorkflowId(msgId, attId);
  const ch = `workflow:${wid || "op"}:regen:${rootId}`;
  const convId = S.activeConvId;
  const beforeSiblings = _rootSiblingIds(
    S.messages.find((m) => m.id === msgId),
    rootId,
  );
  // A workflow that renders several variants saves each as it lands; only the
  // first is scrolled to, so a user who scrolled away to read is left there.
  let landed = 0;
  const showLanded = async (fetched = null) => {
    const token = S.conversationViewToken;
    try {
      const msgs = fetched || (await api.get(convUrl(convId, "messages")));
      _applyWorkflowMessages(msgs, convId, token);
      if (S.activeConvId !== convId || S.conversationViewToken !== token) return;
      if (!landed++) _scrollArtifactIntoView(msgId, rootId);
      broadcastWorkflowMutation({ convId, msgId });
    } catch (e) {
      console.warn("showing a regenerated variant failed", e);
    }
  };
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "regenerating..."), conversationState(convId));
    const result = await _regenerateStreamed(
      job.url(convUrl(convId, "messages", msgId, "workflow-attachments", attId, "regenerate")),
      wid,
      (label) => setWorkflowPhase(ch, label, conversationState(convId)),
      showLanded,
    );
    const incoming = result && Array.isArray(result.rejected_workflow_atts) ? result.rejected_workflow_atts : [];
    mergeWorkflowRejections(msgId, rootId, incoming, convId);
    await refreshConversationMessages(msgId, convId);
    if (S.activeConvId !== convId) return;
    if (!landed) _scrollArtifactIntoView(msgId, rootId);
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    if (job.stopping) await _syncAfterStop(convId, msgId, rootId, beforeSiblings);
    else if (
      _isNetworkError(e) &&
      ((await _recoverWorkflowSibling(convId, msgId, rootId, beforeSiblings, null, showLanded)) || landed)
    ) {
    } else {
      console.error("Regenerate failed:", e);
      _showActionFailure(container, "workflow-regen-error", "Regenerate", e);
    }
  } finally {
    clearWorkflowPhase(ch, conversationState(convId));
    _workflowActionInFlight.delete(rootId);
    job.end();
    _notifyWorkflowRegenerateSettled(wid, msgId, rootId);
  }
}

async function _rerollWorkflowAttachment(msgId, attId, btn) {
  if (!S.activeConvId) return;
  const started = _startWorkflowAction(msgId, attId, btn, "reroll", "Stop rerolling");
  if (!started) return;
  const { rootId, job } = started;
  const container = btn.closest(".workflow-artifact-swipe, [data-root-id]");
  const wid = _resolveWorkflowId(msgId, attId);
  const ch = `workflow:${wid || "op"}:reroll:${rootId}`;
  const convId = S.activeConvId;
  const beforeSiblings = _rootSiblingIds(
    S.messages.find((m) => m.id === msgId),
    rootId,
  );
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "rerolling..."), conversationState(convId));
    let extra = null;
    try {
      extra = S.workflowRerollParams[wid]?.(msgId, attId) || null;
    } catch (e) {
      console.error("reroll params callback threw:", e);
    }
    const result = await api.post(
      job.url(convUrl(convId, "messages", msgId, "workflow-attachments", attId, "reroll-gen")),
      extra ? { params: extra } : {},
    );
    if (result?.attachment_id != null) _notifyWorkflowRerollSuccess(wid, msgId, attId);
    const incoming = result && Array.isArray(result.rejected_workflow_atts) ? result.rejected_workflow_atts : [];
    mergeWorkflowRejections(msgId, rootId, incoming, convId);
    await refreshConversationMessages(msgId, convId);
    if (S.activeConvId !== convId) return;
    _scrollArtifactIntoView(msgId, rootId);
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    if (job.stopping)
      await _syncAfterStop(convId, msgId, rootId, beforeSiblings, () =>
        _notifyWorkflowRerollSuccess(wid, msgId, attId),
      );
    else if (
      _isNetworkError(e) &&
      (await _recoverWorkflowSibling(convId, msgId, rootId, beforeSiblings, () =>
        _notifyWorkflowRerollSuccess(wid, msgId, attId),
      ))
    ) {
    } else {
      console.error("Reroll failed:", e);
      _showActionFailure(container, "workflow-reroll-error", "Reroll", e);
    }
  } finally {
    clearWorkflowPhase(ch, conversationState(convId));
    _workflowActionInFlight.delete(rootId);
    job.end();
  }
}

function _toggleWorkflowMinimized(msgId, rootId) {
  const { msg, group } = _workflowGroup(msgId, rootId);
  const el = _swipeBox(msgId, rootId);
  if (!group || !el) return;
  if (_workflowMinimized.has(rootId)) _workflowMinimized.delete(rootId);
  else _workflowMinimized.add(rootId);
  _persistWorkflowMinimized();
  el.outerHTML = _renderWorkflowSwipeContainer(msg, rootId, group.atts);
}

/** Ask, then delete the take shown or the whole artifact group. */
export function deleteWorkflowAttachment(msgId, rootId) {
  const { group } = _workflowGroup(msgId, rootId);
  if (!group) return;
  const root = group.atts.find((a) => a.id === rootId) || group.atts[0];
  const idx = _activeIndexForGroup(group.atts, root);
  const active = group.atts[idx];
  const total = group.atts.length;
  const label = esc(_workflowLabel(active));
  const convId = S.activeConvId;
  const remove = (scope) => () => _sendAttachmentDelete(msgId, rootId, active.id, scope, convId);
  if (total <= 1) {
    showConfirmModal(
      {
        title: "Delete attachment",
        message: `Delete <strong>${label}</strong>? This cannot be undone.`,
        confirmText: "Delete",
      },
      remove("group"),
    );
    return;
  }
  showConfirmModal({
    title: "Delete attachment",
    message: `<strong>${label}</strong> has ${total} variants. Delete only the one you are viewing (${idx + 1} / ${total}), or the whole attachment and every variant?`,
    actions: [
      { label: "Delete this variant", run: remove("variant") },
      { label: `Delete all ${total}`, run: remove("group") },
    ],
  });
}

async function _sendAttachmentDelete(msgId, rootId, activeId, scope, convId) {
  if (!convId) return;
  if (!requestSendPermission()) return;
  if (_workflowDeleteInFlight.has(rootId)) return;
  _workflowDeleteInFlight.set(rootId, msgId);
  const aid = scope === "group" ? rootId : activeId;
  const wid = _resolveWorkflowId(msgId, activeId);
  const ch = `workflow:${wid || "op"}:delete:${rootId}`;
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "deleting..."), conversationState(convId));
    const res = await api.post(convUrl(convId, "messages", msgId, "workflow-attachments", aid, "delete"), {
      scope,
    });
    if (res?.group_empty) {
      _workflowMinimized.delete(rootId);
      _persistWorkflowMinimized();
    } else if (res && typeof res.root_id === "number" && res.root_id !== rootId && _workflowMinimized.has(rootId)) {
      _workflowMinimized.delete(rootId);
      _workflowMinimized.add(res.root_id);
      _persistWorkflowMinimized();
    }
    if (res?.group_empty) mergeWorkflowRejections(msgId, rootId, [], convId);
    await refreshConversationMessages(msgId, convId);
    if (S.activeConvId !== convId) return;
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    if (_isNetworkError(e) && (await _recoverWorkflowDeletion(convId, msgId, rootId, aid))) {
    } else {
      console.error("Delete failed:", e);
      toast("Delete failed", true);
    }
  } finally {
    clearWorkflowPhase(ch, conversationState(convId));
    _workflowDeleteInFlight.delete(rootId);
  }
}

// Messages a workflow request of this tab is still changing; a refetch must not repaint them.
function _inFlightMsgIds() {
  return new Set([
    ...Array.from(_workflowRehydrateInFlight.values(), (v) => v.msgId),
    ...Array.from(_workflowActionInFlight.values(), (v) => v.msgId),
    ..._workflowDeleteInFlight.values(),
    ...Array.from(_workflowSwipeInFlight.values(), (v) => v.msgId),
  ]);
}

// Another tab's notice repaints this tab only; announcing it again would echo between the tabs forever.
export function initWorkflowMutationListener() {
  setWorkflowMutationCallback(({ convId, msgId }) => {
    if (convId !== S.activeConvId || (msgId != null && _inFlightMsgIds().has(msgId))) return;
    refreshConversationMessages(null, convId, { announce: false });
  });
}

// Merge attachments without replacing live prose or detaching the stream's DOM nodes.
function _applyWorkflowMessages(msgs, convId = S.activeConvId, token = S.conversationViewToken) {
  if (S.activeConvId !== convId || S.conversationViewToken !== token) return;
  if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg || S.magicInputMsgId != null) {
    S.attachmentInvalidations.set(convId, new Set(msgs.map((msg) => msg.id)));
    return;
  }
  if (!S.isStreaming) {
    setMessages(msgs);
    _reapplyInFlightSwipes();
    renderMessages();
    return;
  }
  const fetched = new Map(msgs.map((msg) => [msg.id, msg]));
  for (const msg of S.messages) {
    const row = fetched.get(msg.id);
    if (msg.id && row) msg.workflow_attachments = row.workflow_attachments || [];
  }
  // Normalize attachment metadata through the usual boundary, retaining the
  // current message objects. setMessages itself appends pending streaming rows.
  setMessages(S.messages.filter((msg) => msg.id));
  _reapplyInFlightSwipes();
  for (const msg of S.messages) {
    if (!msg.id || !fetched.has(msg.id)) continue;
    const el = document.querySelector(`#chat-messages .message[data-msg-id="${msg.id}"]`);
    if (!el) continue;
    const old = el.querySelector(":scope > .workflow-artifacts");
    const html = _renderWorkflowArtifacts(msg);
    if (!html) {
      old?.remove();
      continue;
    }
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const next = tpl.content.firstElementChild;
    if (old) patchHtml(old, next.innerHTML);
    else el.insertBefore(next, el.querySelector(":scope > .msg-toolbar"));
  }
  _refreshWorkflowViewportObserver();
}

export async function refreshConversationMessages(msgId = null, convId = S.activeConvId, { announce = true } = {}) {
  if (!convId) return false;
  const token = S.conversationViewToken;
  try {
    const msgs = await api.get(convUrl(convId, "messages"));
    if (S.activeConvId !== convId || S.conversationViewToken !== token) return false;
    _applyWorkflowMessages(msgs, convId);
    if (msgId != null) _scrollArtifactIntoView(msgId);
    if (announce) broadcastWorkflowMutation({ convId, msgId });
    return true;
  } catch (e) {
    console.warn("refreshConversationMessages failed", e);
    return false;
  }
}

export function replayAttachmentInvalidations() {
  const cid = S.activeConvId;
  if (S.attachmentInvalidations.has(cid)) {
    S.attachmentInvalidations.delete(cid);
    return refreshConversationMessages(null, cid, { announce: false });
  }
}

const _workflowViewportPendingIds = new Set();
const _workflowObservedMsgIds = new Set();
let _workflowViewportFlushTimer = null;

export function resetWorkflowViewportState() {
  _workflowObservedMsgIds.clear();
  _workflowViewportPendingIds.clear();
  if (_workflowViewportFlushTimer) {
    clearTimeout(_workflowViewportFlushTimer);
    _workflowViewportFlushTimer = null;
  }
}

function _activeAttachmentIdsForMessage(msg) {
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

function _refreshWorkflowViewportObserver() {
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

setWorkflowMessagePresentation({
  renderArtifacts: _renderWorkflowArtifacts,
  renderRejection: _renderWorkflowRejection,
  refreshViewport: _refreshWorkflowViewportObserver,
});

// The swipe box an artifact control sits in carries its message and group ids.
const _swipeIds = (el) => {
  const box = el.closest(".workflow-artifact-swipe");
  return [Number(box.dataset.msgId), Number(box.dataset.rootId)];
};

registerActions("artifact", {
  regenerate: (el) => regenerateWorkflowAttachment(Number(el.dataset.msgId), Number(el.dataset.attId), el),
  reroll: (el) => _rerollWorkflowAttachment(Number(el.dataset.msgId), Number(el.dataset.attId), el),
  rehydrate: (el) => rehydrateWorkflowAttachment(Number(el.dataset.msgId), Number(el.dataset.attId), el),
  toggleMinimize: (el) => _toggleWorkflowMinimized(..._swipeIds(el)),
  step: (el) => stepWorkflowVariant(..._swipeIds(el), Number(el.dataset.delta)),
  delete: (el) => deleteWorkflowAttachment(..._swipeIds(el)),
});
