// Drives a transient visual effect over a message body's word units (karaoke
// is the canonical case). One effect plays at a time across the whole chat:
// starting a new one halts the previous. The framework owns the DOM and
// applies a curated CSS class, so a workflow can never inject styling or
// fight for screen space.

import { S } from "./state.js";

const SANCTIONED_VARIANTS = new Set(["highlight", "underline", "pulse"]);

// { token, msgId, variant, grain, lastUnit } | null
let _active = null;
let _seq = 0;

// Returns a session whose `markActive(unitIndex)` the workflow calls from its
// own audio events. A monotonic token makes a superseded session's late calls
// no-ops, so a halted effect cannot repaint a message it no longer owns.
export function startTextEffect({ msgId, effectId, grain = "word", variant = "highlight" } = {}) {
  clearTextEffect();
  if (!SANCTIONED_VARIANTS.has(variant)) {
    console.error("startTextEffect: unknown variant", variant, "(effect", effectId + ") -- using highlight");
    variant = "highlight";
  }
  const token = ++_seq;
  _active = { token, msgId, variant, grain: grain === "sentence" ? "sentence" : "word", lastUnit: null };
  return {
    markActive(unitIndex) {
      if (!_active || _active.token !== token) return;
      _paint(_active.lastUnit, false);
      _active.lastUnit = unitIndex;
      _paint(unitIndex, true);
    },
    stop() {
      if (_active && _active.token === token) clearTextEffect();
    },
  };
}

export function clearTextEffect() {
  if (!_active) return;
  _paint(_active.lastUnit, false);
  _active = null;
}

function _paint(unitIndex, on) {
  if (unitIndex == null || !_active) return;
  const body = document.querySelector(`#chat-messages .message[data-msg-id="${_active.msgId}"] .msg-body`);
  if (!body) return;
  const attr = _active.grain === "sentence" ? "data-sent" : "data-seg";
  const cls = "fx-" + _active.variant;
  for (const span of body.querySelectorAll(`.seg[${attr}="${unitIndex}"]`)) {
    span.classList.toggle(cls, on);
  }
}
