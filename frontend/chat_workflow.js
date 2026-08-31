import { api } from "./api.js";
import { ICON_CHEVRON, ICON_DEL, ICON_REGEN, ICON_REROLL, renderMessages, setMessages } from "./chat_core.js";
import { clearWorkflowPhase, setWorkflowPhase, workflowPhaseLabel } from "./chat_inspector.js";
import { renderDefaultWidget } from "./default_widget.js";
import { closeModal, showModal } from "./modal.js";
import { effectiveWorkflowEnabled, S } from "./state.js";
import { broadcastWorkflowMutation, requestSendPermission, setWorkflowMutationCallback } from "./tabLock.js";
import { $, convUrl, esc, escAttr, markChatProgrammaticScroll, toast } from "./utils.js";

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
    btn = `<span class="workflow-rehydrate-disabled" title="Re-enable ${escAttr(_workflowLabel(att))} to restore">Workflow off</span>`;
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
  const rawLabel = _workflowLabel(active);
  const label = esc(rawLabel);
  const labelAttr = escAttr(rawLabel);
  const countBadge = minimized && total > 1 ? ` <span class="workflow-artifact-label-count">(${total})</span>` : "";
  const header = `<div class="workflow-artifact-header" onclick="workflowToggleMinimize('${instanceId}')">
      <span class="workflow-artifact-label" title="${labelAttr}">${label}${countBadge}</span>
      <div class="workflow-artifact-controls">
        <button class="workflow-chrome-btn workflow-min-btn${minimized ? " collapsed" : ""}" title="${minimized ? "Expand" : "Minimize"}" aria-expanded="${minimized ? "false" : "true"}" onclick="event.stopPropagation();workflowToggleMinimize('${instanceId}')">${ICON_CHEVRON}</button>
        <button class="workflow-chrome-btn workflow-del-btn" title="Delete" onclick="event.stopPropagation();workflowDeleteAttachment('${instanceId}')">${ICON_DEL}</button>
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
        widgetHtml = renderer({ att: active, buttons: { regen: regenBtn, reroll: rerollBtn }, defaultHtml }) || "";
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
      <button class="workflow-swipe-btn prev"${prevDisabled} onclick="event.stopPropagation();workflowArtifactStep('${instanceId}',-1)">${ICON_CHEVRON}</button>
      <div class="workflow-artifact-body">${bodyHtml}</div>
      <button class="workflow-swipe-btn next"${nextDisabled} onclick="event.stopPropagation();workflowArtifactStep('${instanceId}',1)">${ICON_CHEVRON}</button>
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

export function _renderWorkflowRejection(msg) {
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

function _resolveWorkflowWidget(instanceId) {
  const el = document.getElementById(instanceId);
  if (!el) return {};
  const msgId = Number(el.dataset.msgId);
  const rootId = Number(el.dataset.rootId);
  const msg = S.messages.find((m) => m.id === msgId);
  if (!msg) return {};
  const group = _workflowAttachmentGroups(msg).find((g) => g.rootId === rootId);
  if (!group) return {};
  return { el, msgId, rootId, msg, group };
}

window.workflowArtifactStep = async (instanceId, delta) => {
  const { el, msgId, rootId, msg, group } = _resolveWorkflowWidget(instanceId);
  if (!group || group.atts.length <= 1) return;
  if (_workflowSwipeInFlight.has(rootId)) return;
  if (!requestSendPermission()) return;
  const root = group.atts.find((a) => a.id === rootId) || group.atts[0];
  const cur = _activeIndexForGroup(group.atts, root);
  const next = cur + delta;
  if (next < 0 || next >= group.atts.length) return;
  const newActiveId = group.atts[next].id;
  _workflowSwipeInFlight.set(rootId, { msgId, activeId: newActiveId });
  if (root) root.active_sibling_id = newActiveId;
  el.outerHTML = _renderWorkflowSwipeContainer(msg, rootId, group.atts);
  _scrollArtifactIntoView(msgId, rootId);
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
};

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
    _reapplyInFlightSwipes();
    renderMessages();
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
  } catch (e) {
    if (e && e.status === 409) {
      try {
        setMessages(await api.get(convUrl(S.activeConvId, "messages")));
        _reapplyInFlightSwipes();
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

export function _mergeWorkflowRejections(msgId, originatingId, incoming) {
  S.rejectedWorkflowAtts = S.rejectedWorkflowAtts
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

function _rootSiblingIds(msg, rootId) {
  const atts = msg?.workflow_attachments || [];
  return new Set(atts.filter((a) => (a.parent_attachment_id || a.id) === rootId).map((a) => a.id));
}

async function _recoverWorkflowSibling(convId, msgId, rootId, before) {
  const deadline = Date.now() + 200_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    if (S.activeConvId !== convId) return true;
    let msgs;
    try {
      msgs = await api.get(convUrl(convId, "messages"));
    } catch {
      continue;
    }
    const now = _rootSiblingIds(
      msgs.find((m) => m.id === msgId),
      rootId,
    );
    if ([...now].some((id) => !before.has(id))) {
      if (S.activeConvId !== convId) return true;
      setMessages(msgs);
      _reapplyInFlightSwipes();
      renderMessages();
      _scrollArtifactIntoView(msgId, rootId);
      broadcastWorkflowMutation({ convId, msgId });
      return true;
    }
  }
  return false;
}

async function _recoverWorkflowDeletion(convId, msgId, rootId, aid) {
  const deadline = Date.now() + 200_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    if (S.activeConvId !== convId) return true;
    let msgs;
    try {
      msgs = await api.get(convUrl(convId, "messages"));
    } catch {
      continue;
    }
    const msg = msgs.find((m) => m.id === msgId);
    if ((msg?.workflow_attachments || []).some((a) => a.id === aid)) continue;
    if (S.activeConvId !== convId) return true;
    setMessages(msgs);
    if (!_rootSiblingIds(msg, rootId).size) {
      _workflowMinimized.delete(rootId);
      _persistWorkflowMinimized();
      _mergeWorkflowRejections(msgId, rootId, []);
    }
    _reapplyInFlightSwipes();
    renderMessages();
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
  const convId = S.activeConvId;
  const beforeSiblings = _rootSiblingIds(
    S.messages.find((m) => m.id === msgId),
    rootId,
  );
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "regenerating..."));
    const result = await api.post(convUrl(convId, "messages", msgId, "workflow-attachments", attId, "regenerate"), {});
    const incoming = result && Array.isArray(result.rejected_workflow_atts) ? result.rejected_workflow_atts : [];
    _mergeWorkflowRejections(msgId, rootId, incoming);
    setMessages(await api.get(convUrl(convId, "messages")));
    _reapplyInFlightSwipes();
    renderMessages();
    _scrollArtifactIntoView(msgId, rootId);
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    if (_isNetworkError(e) && (await _recoverWorkflowSibling(convId, msgId, rootId, beforeSiblings))) {
    } else {
      console.error("Regenerate failed:", e);
      _showActionFailure(container, "workflow-regen-error", "Regenerate", e);
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
  const convId = S.activeConvId;
  const beforeSiblings = _rootSiblingIds(
    S.messages.find((m) => m.id === msgId),
    rootId,
  );
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "rerolling..."));
    let extra = null;
    try {
      extra = S.workflowRerollParams[wid]?.(msgId, attId) || null;
    } catch (e) {
      console.error("reroll params callback threw:", e);
    }
    const result = await api.post(
      convUrl(convId, "messages", msgId, "workflow-attachments", attId, "reroll-gen"),
      extra ? { params: extra } : {},
    );
    const incoming = result && Array.isArray(result.rejected_workflow_atts) ? result.rejected_workflow_atts : [];
    _mergeWorkflowRejections(msgId, rootId, incoming);
    setMessages(await api.get(convUrl(convId, "messages")));
    _reapplyInFlightSwipes();
    renderMessages();
    _scrollArtifactIntoView(msgId, rootId);
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    if (_isNetworkError(e) && (await _recoverWorkflowSibling(convId, msgId, rootId, beforeSiblings))) {
    } else {
      console.error("Reroll failed:", e);
      _showActionFailure(container, "workflow-reroll-error", "Reroll", e);
    }
  } finally {
    clearWorkflowPhase(ch);
    _workflowActionInFlight.delete(rootId);
    btn.disabled = false;
  }
};

window.workflowToggleMinimize = (instanceId) => {
  const { el, rootId, msg, group } = _resolveWorkflowWidget(instanceId);
  if (!group) return;
  if (_workflowMinimized.has(rootId)) _workflowMinimized.delete(rootId);
  else _workflowMinimized.add(rootId);
  _persistWorkflowMinimized();
  el.outerHTML = _renderWorkflowSwipeContainer(msg, rootId, group.atts);
};

let _wfDeleteTarget = null;

window.workflowDeleteAttachment = (instanceId) => {
  const { msgId, rootId, group } = _resolveWorkflowWidget(instanceId);
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

async function _deleteWorkflowAttachment(msgId, rootId, activeId, scope) {
  if (!S.activeConvId) return;
  if (!requestSendPermission()) return;
  if (_workflowDeleteInFlight.has(rootId)) return;
  _workflowDeleteInFlight.set(rootId, msgId);
  const aid = scope === "group" ? rootId : activeId;
  const wid = _resolveWorkflowId(msgId, activeId);
  const ch = `workflow:${wid || "op"}:delete:${rootId}`;
  const convId = S.activeConvId;
  try {
    setWorkflowPhase(ch, workflowPhaseLabel(wid, "deleting..."));
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
    if (res?.group_empty) _mergeWorkflowRejections(msgId, rootId, []);
    setMessages(await api.get(convUrl(convId, "messages")));
    _reapplyInFlightSwipes();
    renderMessages();
    broadcastWorkflowMutation({ convId, msgId });
  } catch (e) {
    if (_isNetworkError(e) && (await _recoverWorkflowDeletion(convId, msgId, rootId, aid))) {
    } else {
      console.error("Delete failed:", e);
      toast("Delete failed", true);
    }
  } finally {
    clearWorkflowPhase(ch);
    _workflowDeleteInFlight.delete(rootId);
  }
}

export function initWorkflowMutationListener() {
  setWorkflowMutationCallback(async ({ convId, msgId }) => {
    if (convId !== S.activeConvId) return;
    if (S.isStreaming) return;
    if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg || S.magicInputMsgId != null)
      return;
    const inFlightMsgIds = new Set([
      ..._workflowRehydrateInFlight.values(),
      ..._workflowActionInFlight.values(),
      ..._workflowDeleteInFlight.values(),
      ...Array.from(_workflowSwipeInFlight.values(), (v) => v.msgId),
    ]);
    if (inFlightMsgIds.has(msgId)) return;
    try {
      setMessages(await api.get(convUrl(S.activeConvId, "messages")));
      _reapplyInFlightSwipes();
      renderMessages();
    } catch (e) {
      console.warn("cross-tab workflow refetch failed", e);
    }
  });
}

export async function refreshConversationMessages(msgId = null) {
  if (!S.activeConvId) return false;
  if (S.isStreaming) return false;
  if (S.editingMsgId != null || S.forkEditMsgId != null || S.editingPendingUserMsg || S.magicInputMsgId != null)
    return false;
  const inFlight = new Set([
    ..._workflowRehydrateInFlight.values(),
    ..._workflowActionInFlight.values(),
    ..._workflowDeleteInFlight.values(),
    ...Array.from(_workflowSwipeInFlight.values(), (v) => v.msgId),
  ]);
  if (msgId != null && inFlight.has(msgId)) return false;
  try {
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    _reapplyInFlightSwipes();
    renderMessages();
    if (msgId != null) _scrollArtifactIntoView(msgId);
    broadcastWorkflowMutation({ convId: S.activeConvId, msgId });
    return true;
  } catch (e) {
    console.warn("refreshConversationMessages failed", e);
    return false;
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
