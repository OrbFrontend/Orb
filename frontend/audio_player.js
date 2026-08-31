import {
  buildSchedule,
  locateSegment,
  normalizeSegment,
  onEndedDecision,
  rescheduleFrom,
  shouldStopOn,
} from "./audio_schedule.js";
import { S } from "./state.js";

// Channels mix independently; playback tokens discard stale callbacks.

const EVICTED_MARKER = "[evicted]";

const SCHEDULE_LEAD = 0.02;

const DECODE_CACHE_CAP = 24;

const SILENCE_BUFFER_FRAMES = 128;

let _ctx = null;
let _master = null;
let _seq = 0;
let _barChangeHook = null;
let _silenceBuf = null;

const _channels = new Map(); // channel name -> channel record
const _decodeCache = new Map(); // source key -> decode promise
const _listeners = new Map(); // channel name -> handlers

function _clamp01(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 1;
  return n < 0 ? 0 : n > 1 ? 1 : n;
}

function _ensureCtx() {
  if (_ctx) return _ctx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) {
    console.error("[audio] Web Audio API unavailable; audio player disabled");
    return null;
  }
  _ctx = new Ctor();
  _master = _ctx.createGain();
  _master.connect(_ctx.destination);
  _ctx.resume().catch(() => {});
  return _ctx;
}

function _ensureChannel(name) {
  let ch = _channels.get(name);
  if (ch) return ch;
  const baseGain = _ctx.createGain();
  const userGain = _ctx.createGain();
  baseGain.connect(userGain);
  userGain.connect(_master);
  ch = {
    token: 0,
    baseGain,
    userGain,
    sources: [],
    plan: null,
    startedAt: 0,
    totalDuration: 0,
    segCount: 0,
    playing: false,
    loop: false,
    stopOn: null,
    steps: null,
    paused: false,
    pausedOffset: 0,
    closedToken: 0,
  };
  _channels.set(name, ch);
  return ch;
}

function _ensureSilenceBuffer() {
  if (!_silenceBuf) _silenceBuf = _ctx.createBuffer(1, SILENCE_BUFFER_FRAMES, _ctx.sampleRate);
  return _silenceBuf;
}

function _b64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function _prepareSource(source) {
  if (source.row != null) {
    const att = _findAttachment(source.row);
    if (!att) return { skip: `row ${source.row} not in loaded messages` };
    const b64 = att.b64 || att.data_b64;
    if (b64 === EVICTED_MARKER) return { skip: `row ${source.row} evicted` };
    if (!b64) return { skip: `row ${source.row} has no bytes` };
    return { thunk: () => _b64ToArrayBuffer(b64) };
  }
  return { thunk: () => _b64ToArrayBuffer(source.b64) };
}

function _findAttachment(rowId) {
  for (const m of S.messages || []) {
    const list = m.workflow_attachments;
    if (!Array.isArray(list)) continue;
    for (const att of list) {
      if (att.id === rowId) return att;
    }
  }
  return null;
}

function _decode(key, thunk) {
  let p = _decodeCache.get(key);
  if (p) return p;
  p = Promise.resolve().then(() => _ctx.decodeAudioData(thunk()));
  _decodeCache.set(key, p);
  p.catch(() => {
    if (_decodeCache.get(key) === p) _decodeCache.delete(key);
  });
  while (_decodeCache.size > DECODE_CACHE_CAP) {
    _decodeCache.delete(_decodeCache.keys().next().value);
  }
  return p;
}

function _stopSources(ch) {
  for (const node of ch.sources) {
    try {
      node.onended = null;
      node.stop();
      node.disconnect();
    } catch (_e) {}
  }
  ch.sources = [];
}

function _position(ch) {
  if (ch.paused) return ch.pausedOffset;
  if (!_ctx || !(ch.totalDuration > 0)) return 0;
  const elapsed = _ctx.currentTime - ch.startedAt;
  if (elapsed < 0) return 0;
  return ch.loop ? elapsed % ch.totalDuration : Math.min(elapsed, ch.totalDuration);
}

function _buildSources(ch, token, channel, steps) {
  ch.sources = steps.map((step, idx) => {
    const node = _ctx.createBufferSource();
    node.buffer = ch.plan.bufByKey.get(step.sourceKey);
    node.connect(ch.baseGain);
    if (step.silent) node.loop = true;
    if (idx === steps.length - 1) node.onended = () => _onLastEnded(ch, token, channel);
    node.start(step.when, step.offset, step.duration);
    return node;
  });
}

function _scheduleSteps(ch, token, channel, playable, bufByKey) {
  const durByKey = new Map();
  for (const [k, b] of bufByKey) durByKey.set(k, b.duration);
  const { steps, totalDuration } = buildSchedule(playable, durByKey, _ctx.currentTime, SCHEDULE_LEAD);
  if (!steps.length) {
    ch.playing = false;
    ch.sources = [];
    ch.steps = null;
    ch.totalDuration = 0;
    ch.segCount = 0;
    return false;
  }
  const nativeLoop = ch.loop && steps.length === 1;
  ch.startedAt = steps[0].when;
  ch.totalDuration = totalDuration;
  ch.segCount = steps.length;
  ch.steps = steps;
  ch.playing = true;
  if (nativeLoop) {
    const step = steps[0];
    const node = _ctx.createBufferSource();
    node.buffer = bufByKey.get(step.sourceKey);
    node.connect(ch.baseGain);
    node.loop = true;
    node.loopStart = step.offset;
    node.loopEnd = step.offset + step.duration;
    node.start(step.when, step.offset);
    ch.sources = [node];
  } else {
    _buildSources(ch, token, channel, steps);
  }
  return true;
}

function _scheduleStepsFrom(ch, token, channel, offsetSec) {
  _stopSources(ch);
  const r = rescheduleFrom(ch.steps, offsetSec, _ctx.currentTime, SCHEDULE_LEAD);
  _buildSources(ch, token, channel, r.steps);
  ch.startedAt = _ctx.currentTime + SCHEDULE_LEAD - offsetSec;
  return r.steps.length;
}

async function _startPlan(ch, token, channel, normalized) {
  const bufByKey = new Map();
  const skipped = new Set();
  const uniqueKeys = [...new Set(normalized.map((n) => n.sourceKey))];
  await Promise.all(
    uniqueKeys.map(async (key) => {
      const seg = normalized.find((n) => n.sourceKey === key);
      if (seg.silent) {
        bufByKey.set(key, _ensureSilenceBuffer());
        return;
      }
      const prep = _prepareSource(seg.source);
      if (prep.skip) {
        skipped.add(key);
        console.warn(`[audio] channel ${channel}: skipped (${prep.skip})`);
        return;
      }
      try {
        bufByKey.set(key, await _decode(key, prep.thunk));
      } catch (e) {
        skipped.add(key);
        console.warn(`[audio] channel ${channel}: decode failed for ${key}: ${e?.message || e}`);
      }
    }),
  );

  if (ch.token !== token) return;

  const playable = normalized.filter((n) => !skipped.has(n.sourceKey));
  ch.plan = { playable, bufByKey };
  const ok = _scheduleSteps(ch, token, channel, playable, bufByKey);
  if (!ok) ch.plan = null;
  if (ok) _emit("play", channel, { reason: "start" });
  _notifyBar();
}

function _onLastEnded(ch, token, channel) {
  const decision = onEndedDecision({ planToken: token, channelToken: ch.token, loop: ch.loop });
  if (decision === "ignore") return;
  if (decision === "restart" && ch.plan) {
    _scheduleSteps(ch, token, channel, ch.plan.playable, ch.plan.bufByKey);
    return;
  }
  _closeChannel(ch, channel, "ended");
  ch.playing = false;
  ch.sources = [];
  _notifyBar();
}

export function playAudio({ channel, segments, loop = false, volume, stopOn } = {}) {
  if (typeof channel !== "string" || !channel) {
    console.error("[audio] playAudio: a channel name is required");
    return { channel: null, stop() {}, isActive: () => false };
  }
  if (!_ensureCtx()) {
    return { channel, stop() {}, isActive: () => false };
  }
  const ch = _ensureChannel(channel);
  _closeChannel(ch, channel, "superseded");
  ch.playing = false;
  ch.steps = null;
  ch.plan = null;
  ch.paused = false;
  ch.pausedOffset = 0;
  const token = ++_seq;
  ch.token = token;
  ch.loop = !!loop;
  ch.stopOn = stopOn || null;
  if (volume != null) ch.baseGain.gain.value = _clamp01(volume);
  _stopSources(ch);

  const raw = Array.isArray(segments) ? segments : [];
  const normalized = [];
  for (let i = 0; i < raw.length; i++) {
    const n = normalizeSegment(raw[i]);
    if (n) normalized.push(n);
    else console.warn(`[audio] channel ${channel}: segment ${i} is malformed, skipped`);
  }
  _startPlan(ch, token, channel, normalized);

  return {
    channel,
    stop() {
      if (ch.token === token) stopChannel(channel);
    },
    isActive: () => ch.token === token && ch.playing,
  };
}

export function stopChannel(channel, reason = "skipped") {
  const ch = _channels.get(channel);
  if (!ch) return;
  _closeChannel(ch, channel, reason);
  ch.token = ++_seq;
  _stopSources(ch);
  ch.playing = false;
  ch.paused = false;
  ch.pausedOffset = 0;
  ch.plan = null;
  ch.steps = null;
  _notifyBar();
}

export function stopAll() {
  for (const name of _channels.keys()) stopChannel(name, "lifecycle");
}

export function setChannelVolume(channel, vol) {
  if (!_ensureCtx()) return;
  _ensureChannel(channel).baseGain.gain.value = _clamp01(vol);
}

export function setChannelUserVolume(channel, vol) {
  if (!_ensureCtx()) return;
  _ensureChannel(channel).userGain.gain.value = _clamp01(vol);
}

export function channelUserVolume(channel) {
  const ch = _channels.get(channel);
  return ch ? _clamp01(ch.userGain.gain.value) : 1;
}

export function channelState(channel) {
  const ch = _channels.get(channel);
  if (!ch?.plan) return null;
  const elapsed = _position(ch);
  const seg = locateSegment(ch.steps, elapsed);
  const streamRemaining = ch.totalDuration - elapsed;
  return {
    playing: ch.playing,
    paused: ch.paused,
    loop: ch.loop,
    segmentCount: ch.segCount,
    segmentIndex: seg.index,
    stream: {
      elapsedSec: elapsed,
      remainingSec: streamRemaining > 0 ? streamRemaining : 0,
      durationSec: ch.totalDuration,
    },
    segment: {
      elapsedSec: seg.segElapsedSec,
      remainingSec: seg.segRemainingSec,
      durationSec: seg.segDurationSec,
    },
  };
}

export function pauseChannel(channel) {
  const ch = _channels.get(channel);
  if (!ch?.playing || ch.paused) return;
  ch.pausedOffset = _position(ch);
  _stopSources(ch);
  ch.paused = true;
  _emit("pause", channel);
  _notifyBar();
}

export function resumeChannel(channel) {
  const ch = _channels.get(channel);
  if (!ch?.paused) return;
  const survived = _scheduleStepsFrom(ch, ch.token, channel, ch.pausedOffset);
  ch.paused = false;
  ch.pausedOffset = 0;
  if (survived === 0) {
    _closeChannel(ch, channel, "ended");
    ch.playing = false;
    ch.sources = [];
    _notifyBar();
    return;
  }
  _emit("play", channel, { reason: "resume" });
  _notifyBar();
}

export function seekChannel(channel, offsetSec) {
  const ch = _channels.get(channel);
  if (!ch?.plan || ch.steps == null) return;
  const total = ch.totalDuration;
  const clamped = Math.min(Math.max(Number(offsetSec) || 0, 0), total);
  const fromSec = _position(ch);
  if (ch.paused) {
    ch.pausedOffset = clamped;
  } else if (ch.playing) {
    if (_scheduleStepsFrom(ch, ch.token, channel, clamped) === 0) {
      _closeChannel(ch, channel, "ended");
      ch.playing = false;
      ch.sources = [];
      _notifyBar();
      return;
    }
  } else {
    ch.token = ++_seq;
    if (_scheduleStepsFrom(ch, ch.token, channel, clamped) === 0) return;
    ch.playing = true;
    _emit("play", channel, { reason: "start" });
    _notifyBar();
    return;
  }
  _emit("seek", channel, { fromSec, toSec: clamped });
  _notifyBar();
}

export function setChannelRepeat(channel, on) {
  const ch = _channels.get(channel);
  if (!ch) return;
  const next = !!on;
  if (ch.loop === next) return;

  const nativeLoop = ch.playing && !ch.paused && ch.sources.length === 1 && ch.sources[0].loop === true;
  if (nativeLoop && next === false) {
    const pos = _position(ch);
    ch.loop = false;
    _scheduleStepsFrom(ch, ch.token, channel, pos);
    _notifyBar();
    return;
  }

  ch.loop = next;

  if (ch.playing) {
    _notifyBar();
    return;
  }
  if (ch.plan && ch.steps != null && next) {
    ch.token = ++_seq;
    _scheduleSteps(ch, ch.token, channel, ch.plan.playable, ch.plan.bufByKey);
    ch.playing = true;
    _emit("play", channel, { reason: "repeat" });
  }
  _notifyBar();
}

export function replayChannel(channel) {
  const ch = _channels.get(channel);
  if (!ch?.plan || ch.steps == null) return;
  _closeChannel(ch, channel, "superseded");
  _stopSources(ch);
  ch.token = ++_seq;
  ch.paused = false;
  ch.pausedOffset = 0;
  _scheduleSteps(ch, ch.token, channel, ch.plan.playable, ch.plan.bufByKey);
  ch.playing = true;
  _emit("play", channel, { reason: "start" });
  _notifyBar();
}

export function onChannel(channel, handler) {
  if (typeof channel !== "string" || !channel || typeof handler !== "function") {
    return () => {};
  }
  let set = _listeners.get(channel);
  if (!set) {
    set = new Set();
    _listeners.set(channel, set);
  }
  set.add(handler);
  return () => {
    const s = _listeners.get(channel);
    if (!s) return;
    s.delete(handler);
    if (s.size === 0) _listeners.delete(channel);
  };
}

function _emit(type, channel, extra = {}) {
  const set = _listeners.get(channel);
  if (!set || set.size === 0) return;
  const event = { type, channel, ...extra };
  for (const handler of [...set]) {
    try {
      handler(event);
    } catch (e) {
      console.warn(`[audio] channel ${channel}: subscriber threw on ${type}: ${e?.message || e}`);
    }
  }
}

function _closeChannel(ch, channel, reason) {
  if (!ch.playing && !ch.paused) return;
  if (ch.closedToken === ch.token) return;
  ch.closedToken = ch.token;
  _emit("close", channel, { reason });
}

export function onTurnStart() {
  _stopForEvent("newTurn");
}

export function onConvSwitch() {
  _stopForEvent("convSwitch");
}

function _stopForEvent(event) {
  for (const [name, ch] of _channels) {
    if (ch.plan && shouldStopOn(ch.stopOn, event)) stopChannel(name, "lifecycle");
  }
}

export function activeChannels() {
  const names = [];
  for (const [name, ch] of _channels) {
    if (ch.plan) names.push(name);
  }
  return names;
}

export function setBarChangeHook(fn) {
  _barChangeHook = typeof fn === "function" ? fn : null;
}

function _notifyBar() {
  if (!_barChangeHook) return;
  try {
    _barChangeHook();
  } catch (e) {
    console.warn(`[audio] transport repaint hook threw: ${e?.message || e}`);
  }
}

export function isContextSuspended() {
  return !!_ctx && _ctx.state === "suspended";
}

export function resumeContext() {
  return _ctx ? _ctx.resume() : Promise.resolve();
}
