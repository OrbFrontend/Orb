// Routes clicks on message-body word units to the workflows that claim them.
// Framework owns the single delegated listener and the popover; workflows
// register `{claims, onClick}` and receive plain descriptors, never a DOM node
// -- the boundary that lets a workflow act on text without touching the DOM.
// Disambiguation is reached by hover caret on desktop and long-press on touch
// specifically so a quick tap stays a single action and a horizontal drag stays
// branch-swipe (the gesture this UI must not collide with).

import { S } from "./state.js";
import { segDescriptor } from "./workflow_segmentation.js";

const HOLD_MS = 500;
const MOVE_CANCEL_PX = 10;

let _delegated = false;
let _swallowClick = false; // drops the synthetic click a touch long-press emits on lift
let _holdTimer = null;
let _holdStart = null;
let _holdSpan = null;
let _popoverEl = null;
let _onDocClick = null;
let _caretEl = null;
let _caretSpan = null;
let _caretHideTimer = null;

function _claimantsFor(ctx) {
  const matched = [];
  for (const h of S.workflowClickHandlers) {
    try {
      if (h.claims(ctx)) matched.push(h);
    } catch (e) {
      console.error("workflow click claims() threw for handler", h.id, e);
    }
  }
  // Stable sort keeps registration (manifest) order for equal priorities.
  return matched.sort((a, b) => b.priority - a.priority);
}

function _fire(handler, ctx) {
  try {
    handler.onClick(ctx, ctx.msgId);
  } catch (e) {
    console.error("workflow click handler", handler.id, "threw:", e);
  }
}

function _ctxFromSpan(span) {
  const msgEl = span.closest(".message[data-msg-id]");
  if (!msgEl) return null;
  const msgId = Number(msgEl.dataset.msgId);
  if (!Number.isInteger(msgId) || msgId <= 0) return null; // a pending message renders data-msg-id="null"
  const msg = S.messages.find((m) => m.id === msgId);
  if (!msg) return null;
  return segDescriptor(span, { msgId, role: msg.role });
}

// `.seg-multi` marks units more than one workflow claims; the touch and hover
// disambiguation handlers key off this class, so it must be set here at render.
export function markClickable(bodyEl, msg) {
  if (!bodyEl || !S.workflowClickHandlers.length) return;
  for (const span of bodyEl.querySelectorAll(".seg")) {
    const ctx = segDescriptor(span, { msgId: msg.id, role: msg.role });
    const claimants = _claimantsFor(ctx);
    if (!claimants.length) continue;
    span.classList.add("seg-clickable");
    if (claimants.length > 1) span.classList.add("seg-multi");
  }
}

function _onClick(e) {
  if (_swallowClick) {
    _swallowClick = false;
    return;
  }
  if (e.target.closest(".wf-seg-caret")) return; // the caret has its own handler
  const span = e.target.closest(".seg.seg-clickable");
  if (!span) return;
  const ctx = _ctxFromSpan(span);
  if (!ctx) return;
  const claimants = _claimantsFor(ctx);
  if (!claimants.length) return;
  _fire(claimants[0], ctx);
}

function _onTouchStart(e) {
  if (e.touches.length !== 1) return;
  const span = e.target.closest(".seg-multi");
  if (!span) return;
  const t = e.touches[0];
  _holdStart = { x: t.clientX, y: t.clientY };
  _holdSpan = span;
  clearTimeout(_holdTimer);
  _holdTimer = setTimeout(() => {
    _holdTimer = null;
    _swallowClick = true; // the popover open must not also fire the top claimant
    const ctx = _ctxFromSpan(span);
    if (ctx) _openPopover(_claimantsFor(ctx), ctx, span.getBoundingClientRect());
  }, HOLD_MS);
}

function _onTouchMove(e) {
  if (!_holdTimer) return;
  const t = e.touches[0];
  if (!t) return;
  if (Math.hypot(t.clientX - _holdStart.x, t.clientY - _holdStart.y) > MOVE_CANCEL_PX) {
    clearTimeout(_holdTimer);
    _holdTimer = null;
  }
}

function _onTouchEnd() {
  if (_holdTimer) {
    clearTimeout(_holdTimer);
    _holdTimer = null;
  }
}

function _ensureCaret() {
  if (_caretEl) return;
  _caretEl = document.createElement("button");
  _caretEl.className = "wf-seg-caret";
  _caretEl.type = "button";
  _caretEl.setAttribute("aria-label", "More actions for this text");
  _caretEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!_caretSpan) return;
    const ctx = _ctxFromSpan(_caretSpan);
    const rect = _caretEl.getBoundingClientRect();
    _hideCaret();
    if (ctx) _openPopover(_claimantsFor(ctx), ctx, rect);
  });
  _caretEl.addEventListener("mouseenter", () => clearTimeout(_caretHideTimer));
  _caretEl.addEventListener("mouseleave", _scheduleCaretHide);
  document.body.appendChild(_caretEl);
}

function _showCaret(span) {
  _ensureCaret();
  clearTimeout(_caretHideTimer);
  _caretSpan = span;
  const r = span.getBoundingClientRect();
  _caretEl.style.left = `${r.right}px`;
  _caretEl.style.top = `${r.top}px`;
  _caretEl.style.display = "inline-flex";
}

function _hideCaret() {
  if (_caretEl) _caretEl.style.display = "none";
  _caretSpan = null;
}

function _scheduleCaretHide() {
  clearTimeout(_caretHideTimer);
  _caretHideTimer = setTimeout(_hideCaret, 220);
}

function _onMouseOver(e) {
  const span = e.target.closest(".seg-multi");
  if (span) _showCaret(span);
}

function _onMouseOut(e) {
  if (e.target.closest(".seg-multi")) _scheduleCaretHide();
}

function _openPopover(claimants, ctx, anchorRect) {
  _closePopover();
  if (!claimants?.length) return;
  if (claimants.length === 1) {
    _fire(claimants[0], ctx);
    return;
  }
  const pop = document.createElement("div");
  pop.className = "wf-claim-popover";
  for (const h of claimants) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "wf-claim-item";
    item.textContent = h.label || h.id;
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      _closePopover();
      _fire(h, ctx);
    });
    pop.appendChild(item);
  }
  document.body.appendChild(pop);
  _popoverEl = pop;
  // Flip above the unit when it would overflow the bottom edge.
  const pr = pop.getBoundingClientRect();
  let left = anchorRect.left;
  let top = anchorRect.bottom + 4;
  if (left + pr.width > window.innerWidth - 8) left = window.innerWidth - pr.width - 8;
  if (top + pr.height > window.innerHeight - 8) top = anchorRect.top - pr.height - 4;
  pop.style.left = `${Math.max(8, left)}px`;
  pop.style.top = `${Math.max(8, top)}px`;
  // Defer the outside-click listener so the opening interaction does not close it.
  _onDocClick = (e) => {
    if (_popoverEl && !_popoverEl.contains(e.target)) _closePopover();
  };
  setTimeout(() => document.addEventListener("click", _onDocClick, true), 0);
}

function _closePopover() {
  if (_onDocClick) {
    document.removeEventListener("click", _onDocClick, true);
    _onDocClick = null;
  }
  if (_popoverEl) {
    _popoverEl.remove();
    _popoverEl = null;
  }
}

// Listeners live on the stable `#chat-messages` container so they survive the
// per-render rebuild, which replaces all `.message` children via innerHTML.
export function initWorkflowTextInteraction() {
  if (_delegated) return;
  const ct = document.getElementById("chat-messages");
  if (!ct) return;
  _delegated = true;
  ct.addEventListener("click", _onClick);
  ct.addEventListener("touchstart", _onTouchStart, { passive: true });
  ct.addEventListener("touchmove", _onTouchMove, { passive: true });
  ct.addEventListener("touchend", _onTouchEnd);
  ct.addEventListener("mouseover", _onMouseOver);
  ct.addEventListener("mouseout", _onMouseOut);
  ct.addEventListener("scroll", () => {
    _hideCaret();
    _closePopover();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      _hideCaret();
      _closePopover();
    }
  });
  window.addEventListener("resize", () => {
    _hideCaret();
    _closePopover();
  });
}
