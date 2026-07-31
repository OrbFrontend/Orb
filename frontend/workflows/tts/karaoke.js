// Word-grain karaoke for the TTS workflow: highlights each spoken word as the
// shared audio engine plays a speech clip. The driver subscribes to the "tts"
// channel's lifecycle events and, while a clip plays, maps the engine's
// playback position onto the message's rendered word units through the
// framework text-effect system.
//
// Position -> word mapping re-anchors to the engine's DECODED clip duration:
// stored per-word timings (clip-relative ms) need only be relatively correct,
// since (segmentElapsed / segmentDuration) * lastWordEnd rescales them onto the
// real decoded length. The browser's decode stays the single duration source --
// no server-side audio parsing.

import { channelState, onChannel, registerTextEffect, startTextEffect } from "/static/workflow_api.js";

const CHANNEL = "tts";
const EFFECT_ID = "tts";

function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

// Shared TTS workflow config (the same object the widget and panel hold), read
// live so the "highlight words" toggle takes effect without re-wiring. Display
// only: generation and audio playback never consult it.
let cfg = { show_karaoke: true };

// The single in-flight karaoke run, or null. `play` is the audio session whose
// identity gates us against a foreign "tts" writer (the config-panel voice
// preview shares this channel); `session` is the text-effect handle, created
// lazily on our own play event; `raf` is the pending animation-frame id.
let cur = null;

export function initKaraoke(config) {
  if (config) cfg = config;
  // Word segmentation is already enabled by the workflow's click handler;
  // registering here also labels the effect and keeps karaoke self-contained if
  // that handler is ever removed. startTextEffect does not consult the registry.
  registerTextEffect({ id: EFFECT_ID, label: "Speech karaoke" });
  onChannel(CHANNEL, onEv);
}

// Begin (or replace) a karaoke run for a clip the widget is about to play. The
// effect and the animation loop are NOT started here: playAudio commits its
// plan asynchronously after decodeAudioData, so when the play call returns the
// channel is not yet playing and channelState is null. Arming now would let the
// first frame observe the pre-commit window and tear the run down mid-decode.
// Both are deferred to our own "play" event (fired once the plan commits).
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
    // Only our own committed play (the channel's active, playing plan) arms the
    // loop, and only when the display is enabled. A foreign play -- a voice
    // preview, or a transport replay that minted a fresh token -- fails
    // isActive() and is ignored; it has already cleared `cur` through its
    // preceding close.
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
    // Tear down on every reason. A karaoke->karaoke supersede fires
    // close{superseded} synchronously inside playAudio before the next
    // startKaraoke installs the replacement, so ordering stays correct. A
    // foreign "tts" play also closes us here -- the only path that can clear a
    // PAUSED run's frozen highlight, since a paused run has no pending frame.
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
    // Our clip ended, or a foreign play took the channel.
    _stop();
    return;
  }
  if (!cfg.show_karaoke) {
    // Display toggled off mid-playback: drop the highlight and stop. Generation
    // and audio are unaffected; the next play re-checks the setting.
    _stop();
    return;
  }
  const st = channelState(CHANNEL);
  if (!st?.playing) {
    _arm();
    return;
  }
  if (st.segmentCount !== cur.segPlan.length) {
    // The engine dropped a clip we expected (decode failure / ~0 decoded
    // duration), so its segment index no longer lines up with our plan. Hold
    // rather than highlight the wrong block.
    _arm();
    return;
  }
  const slot = cur.segPlan[st.segmentIndex];
  if (!slot || slot.gap) {
    // Inter-block silence: keep the last word lit until the next clip starts.
    _arm();
    return;
  }
  const block = cur.blocks[slot.block];
  const words = block?.words;
  if (!words?.length) {
    _arm();
    return;
  }
  // Resolved at paint time, not frozen at launch: an autoplayed reply can start
  // before its body is segmented into addressable words, so a value captured up
  // front would be empty. The lookup is memoized in the widget once the body is
  // ready, so the per-frame call is cheap.
  const idxs = cur.getWordIndices()[slot.block];
  if (!idxs || idxs.length !== words.length) {
    // The stored timing count must equal this block's rendered-word count; any
    // backend/frontend tokenizer drift breaks the 1:1 map, so skip the block
    // rather than highlight the wrong word.
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
