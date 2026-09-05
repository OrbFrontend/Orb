import { messageBody } from "./utils.js";

const SANCTIONED_VARIANTS = new Set(["highlight", "underline", "pulse"]);

let _active = null;
let _seq = 0;

export function startTextEffect({ msgId, effectId, grain = "word", variant = "highlight" } = {}) {
  clearTextEffect();
  if (!SANCTIONED_VARIANTS.has(variant)) {
    console.error("startTextEffect: unknown variant", variant, "(effect", `${effectId}) -- using highlight`);
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
  const body = messageBody(_active.msgId);
  if (!body) return;
  const attr = _active.grain === "sentence" ? "data-sent" : "data-seg";
  const cls = `fx-${_active.variant}`;
  for (const span of body.querySelectorAll(`.seg[${attr}="${unitIndex}"]`)) {
    span.classList.toggle(cls, on);
  }
}
