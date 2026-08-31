import { channelState, onChannel, registerTextEffect, startTextEffect } from "/static/workflow_api.js";

const CHANNEL = "tts";
const EFFECT_ID = "tts";

function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

let cfg = { show_karaoke: true };

let cur = null;

export function initKaraoke(config) {
  if (config) cfg = config;
  registerTextEffect({ id: EFFECT_ID, label: "Speech karaoke" });
  onChannel(CHANNEL, onEv);
}

export function startKaraoke({ msgId, segPlan, blocks, getWordIndices, play }) {
  _stop();
  cur = {
    play,
    session: null,
    segPlan: segPlan || [],
    blocks: blocks || [],
    getWordIndices: typeof getWordIndices === "function" ? getWordIndices : () => ({}),
    msgId,
    raf: 0,
    lastUnit: null,
  };
}

function onEv(ev) {
  if (!cur) return;
  if (ev.type === "play") {
    if (cur.play?.isActive() && cfg.show_karaoke) {
      if (!cur.session) {
        cur.session = startTextEffect({ msgId: cur.msgId, effectId: EFFECT_ID, grain: "word", variant: "highlight" });
      }
      _arm();
    }
  } else if (ev.type === "pause") {
    _cancel();
  } else if (ev.type === "seek") {
    if (cur.session) _arm();
  } else if (ev.type === "close") {
    _stop();
  }
}

function _arm() {
  _cancel();
  cur.raf = requestAnimationFrame(_tick);
}

function _cancel() {
  if (cur?.raf) {
    cancelAnimationFrame(cur.raf);
    cur.raf = 0;
  }
}

function _tick() {
  if (cur) cur.raf = 0;
  if (!cur?.session) return;
  if (!cur.play.isActive()) {
    _stop();
    return;
  }
  if (!cfg.show_karaoke) {
    _stop();
    return;
  }
  const st = channelState(CHANNEL);
  if (!st?.playing) {
    _arm();
    return;
  }
  if (st.segmentCount !== cur.segPlan.length) {
    _arm();
    return;
  }
  const slot = cur.segPlan[st.segmentIndex];
  if (!slot || slot.gap) {
    _arm();
    return;
  }
  const block = cur.blocks[slot.block];
  const words = block?.words;
  if (!words?.length) {
    _arm();
    return;
  }
  const idxs = cur.getWordIndices()[slot.block];
  if (!idxs || idxs.length !== words.length) {
    _arm();
    return;
  }
  const total = words[words.length - 1].end_ms;
  const segDur = st.segment.durationSec;
  const frac = segDur > 0 ? clamp(st.segment.elapsedSec / segDur, 0, 1) : 0;
  const target = frac * total;
  let k = 0;
  for (let i = 0; i < words.length; i++) {
    if (words[i].start_ms <= target) k = i;
    else break;
  }
  const unit = idxs[k];
  if (unit != null && unit !== cur.lastUnit) {
    cur.session.markActive(unit);
    cur.lastUnit = unit;
  }
  if (st.playing && !st.paused) _arm();
}

function _stop() {
  if (!cur) return;
  if (cur.session) cur.session.stop();
  _cancel();
  cur = null;
}
