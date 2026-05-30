// Universal audio engine shared by every workflow. A workflow points a
// named channel at an ordered list of segments (each a workflow-attachment row or
// inline base64, with an optional time window) and the engine decodes, windows,
// and schedules them gaplessly on the Web Audio clock. Channels mix
// simultaneously; a new play on a channel replaces only that channel, so the
// "last write wins" guarantee is per channel.
//
// Last-write-wins is enforced with a monotonic token stamped on each plan: any
// late callback (a decode that finished after a newer play, a stale source's
// onended) is dropped when its token no longer matches the channel. The engine is
// plain objects in module scope and owns no DOM -- the transport bar lives in
// audio_transport.js and learns of state changes through a repaint hook the
// engine invokes (setBarChangeHook), keeping the dependency one-way
// transport -> engine.

import { S } from "./state.js";
import {
  buildSchedule,
  locateSegment,
  normalizeSegment,
  onEndedDecision,
  rescheduleFrom,
  shouldStopOn,
} from "./audio_schedule.js";

// Mirrors the backend attachment-cache sentinel. Duplicated rather than imported
// from chat.js to keep the dependency one-directional (chat.js imports the
// lifecycle hooks from here).
const EVICTED_MARKER = "[evicted]";

// Small lead so the first source schedules just ahead of the clock rather than
// in the past, which would clip the attack of the first segment.
const SCHEDULE_LEAD = 0.02;

// Bounds decoded-buffer memory. Evicting an entry only forces a later re-decode;
// it never interrupts a playing source, which holds its own buffer reference.
const DECODE_CACHE_CAP = 24;

// One render quantum of zero-filled audio, shared by every silent gap. A gap of
// any length loops this buffer for its scheduled duration, so silence costs no
// per-gap memory.
const SILENCE_BUFFER_FRAMES = 128;

let _ctx = null;
let _master = null;
let _seq = 0;
let _barChangeHook = null;
let _silenceBuf = null;

const _channels = new Map(); // name -> channel record (see _ensureChannel)
const _decodeCache = new Map(); // sourceKey -> Promise<AudioBuffer>
const _listeners = new Map(); // channel name -> Set<handler>

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
  // Created lazily, often inside a user gesture (a click that starts playback),
  // where resume is allowed; the transport's gesture listeners cover the case
  // where the first play comes from a backend event with no gesture in the call
  // stack.
  _ctx.resume().catch(() => {});
  return _ctx;
}

function _ensureChannel(name) {
  let ch = _channels.get(name);
  if (ch) return ch;
  // Two gain stages keep the workflow's volume and the user's transport-bar
  // trim independent: source -> baseGain -> userGain -> master. The graph
  // multiplies them, so effective output is base * user and neither writer can
  // overwrite the other. Both default to unity (createGain default 1.0).
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
    // Whole-stream schedule timeline [{ sourceKey, offset, duration, when }],
    // retained so a playback position can be mapped to a segment and so
    // resume/seek can re-lay the remainder. Written only by a full schedule,
    // never by an offset reschedule, so it stays whole-stream.
    steps: null,
    paused: false,
    pausedOffset: 0,
    // Last token whose close was already emitted, so exactly one close fires per
    // audible life across the divergent teardown paths.
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

// Resolves a segment source to a thunk that produces fresh bytes, or a skip
// reason. Row bytes arrive in memory on S.messages (normalizeMessages aliases
// data_b64 -> b64); an evicted row cannot be played without an LLM-backed
// rehydrate, which is a deliberate user action, never a side effect of playback.
function _prepareSource(source) {
  if (source.row != null) {
    const att = _findAttachment(source.row);
    if (!att) return { skip: "row " + source.row + " not in loaded messages" };
    const b64 = att.b64 || att.data_b64;
    if (b64 === EVICTED_MARKER) return { skip: "row " + source.row + " evicted" };
    if (!b64) return { skip: "row " + source.row + " has no bytes" };
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
  // thunk() may throw on malformed base64; folding it into the promise routes
  // that to the same skip-and-warn path as a decodeAudioData rejection.
  p = Promise.resolve().then(() => _ctx.decodeAudioData(thunk()));
  _decodeCache.set(key, p);
  // Drop a failed decode so the bytes can be retried on a later play rather than
  // staying permanently poisoned; concurrent segments still share this attempt.
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
    } catch (e) {
      // already stopped or never started
    }
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

// Buffers come from ch.plan.bufByKey, so resume and seek rebuild the remainder
// without re-decoding. The single-segment native loop is built inline in
// _scheduleSteps and never routed here -- this path is finite sources only.
function _buildSources(ch, token, channel, steps) {
  ch.sources = steps.map((step, idx) => {
    const node = _ctx.createBufferSource();
    node.buffer = ch.plan.bufByKey.get(step.sourceKey);
    node.connect(ch.baseGain);
    // A silent gap loops its short shared buffer; the start() duration bounds it
    // to the gap length and still fires onended.
    if (step.silent) node.loop = true;
    if (idx === steps.length - 1) node.onended = () => _onLastEnded(ch, token, channel);
    node.start(step.when, step.offset, step.duration);
    return node;
  });
}

// Lays a fresh whole-stream schedule on the clock and owns the channel's
// whole-stream totals. Returns true only when at least one segment is playable;
// an empty schedule leaves the channel idle (steps null) so presence checks read
// it as never-played.
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
  // A single whole-list loop plays seamlessly via the native loop on the one
  // source; a multi-segment loop reschedules on end (a sub-frame gap there is
  // acceptable for the rare layered-loop case).
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

// Reschedules a channel's remaining audio from a whole-stream offset, for resume
// and seek. Tears down current sources first (nulling their onended so a stale
// natural-end cannot fire and old audio cannot overlap the new), then lays the
// survivors via the finite path. The whole-stream timeline and its totals stay
// owned by _scheduleSteps and are left intact, so channelState keeps reporting
// whole-stream values across the reschedule; startedAt is back-dated so _position
// reads whole-stream elapsed. Returns the surviving step count.
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
      // A silent gap has no bytes to decode; it shares the silent buffer.
      if (seg.silent) {
        bufByKey.set(key, _ensureSilenceBuffer());
        return;
      }
      const prep = _prepareSource(seg.source);
      if (prep.skip) {
        skipped.add(key);
        console.warn("[audio] channel " + channel + ": skipped (" + prep.skip + ")");
        return;
      }
      try {
        bufByKey.set(key, await _decode(key, prep.thunk));
      } catch (e) {
        skipped.add(key);
        console.warn("[audio] channel " + channel + ": decode failed for " + key + ": " + (e?.message || e));
      }
    }),
  );

  // A newer play on this channel superseded us while decoding.
  if (ch.token !== token) return;

  const playable = normalized.filter((n) => !skipped.has(n.sourceKey));
  // bufByKey must be reachable before scheduling (the finite builder reads it off
  // ch.plan). A schedule that produces no audio -- every clip unplayable, or every
  // window clamped to zero duration -- then retracts the plan, so the channel
  // reads as idle rather than as a phantom with a retained-but-silent plan.
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
  // The plan is retained (not nulled) so the channel can be replayed or
  // repeat-armed after a natural end. The close must emit before playing flips
  // false, while the close-once guard can still see the prior audible state.
  _closeChannel(ch, channel, "ended");
  ch.playing = false;
  ch.sources = [];
  _notifyBar();
}

// Play an ordered segment list on a named channel. Replaces whatever that channel
// was doing; other channels keep playing and mix. Returns a session whose stop()
// affects this channel only while this plan is still the active one. Never throws
// on bad input -- malformed segments are skipped and logged.
export function playAudio({ channel, segments, loop = false, volume, stopOn } = {}) {
  if (typeof channel !== "string" || !channel) {
    console.error("[audio] playAudio: a channel name is required");
    return { channel: null, stop() {}, isActive: () => false };
  }
  if (!_ensureCtx()) {
    return { channel, stop() {}, isActive: () => false };
  }
  const ch = _ensureChannel(channel);
  // Close the outgoing life before any state reset, so the close-once guard still
  // sees the prior plan's audible/paused state.
  _closeChannel(ch, channel, "superseded");
  // Reset committed-plan state synchronously, before the async decode below sets a
  // new one. Without this a second play landing in the decode window would see the
  // prior plan's stale playing/steps and either emit an unpaired close or
  // reschedule the stale plan over the in-flight one.
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
    else console.warn("[audio] channel " + channel + ": segment " + i + " is malformed, skipped");
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

// Stop and clear one channel. Bumping the token invalidates any decode still in
// flight and any pending end callback for the plan being torn down. The optional
// reason rides the close event; the public single-argument form reports a user
// skip.
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

// Channel base volume (workflow-controlled), 0..1, sticky across plays. The user's
// transport-bar slider multiplies an independent trim on top, so effective output
// is base * user.
export function setChannelVolume(channel, vol) {
  if (!_ensureCtx()) return;
  _ensureChannel(channel).baseGain.gain.value = _clamp01(vol);
}

// User-facing trim multiplied on top of the workflow base gain (effective is
// base * user). Owned by the transport slider, deliberately not exposed as
// author-facing API so a workflow cannot reach for or overwrite the user's volume.
export function setChannelUserVolume(channel, vol) {
  if (!_ensureCtx()) return;
  _ensureChannel(channel).userGain.gain.value = _clamp01(vol);
}

// Current user trim 0..1, for the transport to position its volume slider on a
// rebuild without caching a second copy of an engine-private value. Transport-
// facing, deliberately not exposed as author-facing API.
export function channelUserVolume(channel) {
  const ch = _channels.get(channel);
  return ch ? _clamp01(ch.userGain.gain.value) : 1;
}

// Read-only snapshot at two grains: the whole clip list (stream) and the current
// chunk (segment). Derived on demand; the engine state is the single source of
// truth. Null only when the channel never played or was hard-stopped (no retained
// plan); a paused or naturally-ended channel still reports, so its timing can be
// rendered.
export function channelState(channel) {
  const ch = _channels.get(channel);
  if (!ch || !ch.plan) return null;
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

// Freeze a playing channel at its current position. The sources are torn down
// (Web Audio cannot pause a buffer source), the whole-stream offset is captured,
// and resume rebuilds the remainder from there. The token is left unchanged: the
// plan is intact, so the originating play session stays valid.
export function pauseChannel(channel) {
  const ch = _channels.get(channel);
  if (!ch || !ch.playing || ch.paused) return;
  ch.pausedOffset = _position(ch);
  _stopSources(ch);
  ch.paused = true;
  _emit("pause", channel);
  _notifyBar();
}

// Resume a paused channel from its frozen offset. If that offset is past the end
// (only reachable if the plan changed underneath), degrade to a natural-end close
// rather than leaving a playing-but-silent channel.
export function resumeChannel(channel) {
  const ch = _channels.get(channel);
  if (!ch || !ch.paused) return;
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

// Jump a channel to a whole-stream offset in seconds. A paused channel only moves
// its frozen offset; a live channel reschedules from there (tearing down current
// sources first); a naturally-ended channel re-arms playback as a fresh life from
// the offset, so a finished clip stays scrubbable without a Replay/repeat
// round-trip. The three states are distinguished on ch.paused / ch.playing, since
// a paused channel still has playing true.
export function seekChannel(channel, offsetSec) {
  const ch = _channels.get(channel);
  if (!ch || !ch.plan || ch.steps == null) return;
  const total = ch.totalDuration;
  const clamped = Math.min(Math.max(Number(offsetSec) || 0, 0), total);
  const fromSec = _position(ch);
  if (ch.paused) {
    ch.pausedOffset = clamped;
  } else if (ch.playing) {
    // A seek to the very end leaves nothing to schedule; treat that as the clip
    // finishing rather than a playing-but-silent channel.
    if (_scheduleStepsFrom(ch, ch.token, channel, clamped) === 0) {
      _closeChannel(ch, channel, "ended");
      ch.playing = false;
      ch.sources = [];
      _notifyBar();
      return;
    }
  } else {
    // Naturally ended: re-arm as a fresh audible life from the offset. Mint a new
    // token (the prior end recorded the old one as closed, which would otherwise
    // suppress this life's close), then schedule the remainder. This is a new life,
    // not a move within one, so it emits "play" (start), not "seek". A drop at the
    // very end has nothing to schedule, so the channel stays ended.
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

// Toggle whole-list repeat at runtime. The cases turn on whether the live plan
// carries an end callback: only a single-segment channel scheduled while looping
// runs as a native loop with no onended, and turning that off must reschedule the
// remainder as a finite source (clearing node.loop mid-play is under-specified for
// a windowed buffer and can run past the window). Every other live plan picks up
// the new value at its next natural end, where onEndedDecision reads ch.loop live.
export function setChannelRepeat(channel, on) {
  const ch = _channels.get(channel);
  if (!ch) return;
  const next = !!on;
  if (ch.loop === next) return;

  const nativeLoop = ch.playing && !ch.paused && ch.sources.length === 1 && ch.sources[0].loop === true;
  if (nativeLoop && next === false) {
    // Read the position while ch.loop is still true: _position applies the loop
    // modulo only on the loop branch, so reading it after clearing the flag would
    // return the clamped total once the clip has wrapped, rescheduling an empty
    // (silent) remainder.
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
    // Naturally ended with a retained plan: turning repeat on is a fresh audible
    // life, so it mints a new token. The prior end recorded the old token as
    // closed, which would otherwise suppress this life's close.
    ch.token = ++_seq;
    _scheduleSteps(ch, ch.token, channel, ch.plan.playable, ch.plan.bufByKey);
    ch.playing = true;
    _emit("play", channel, { reason: "repeat" });
  }
  _notifyBar();
}

// Replay a channel's retained plan from the start as a new life. Safe to call
// while still playing: the outgoing life is closed and its sources stopped before
// re-arming, so the event stream stays close-paired and old and new sources do not
// overlap.
export function replayChannel(channel) {
  const ch = _channels.get(channel);
  if (!ch || !ch.plan || ch.steps == null) return;
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

// Subscribe to one channel's lifecycle events (play / pause / close / seek).
// Returns an unsubscribe function; a bad channel or handler yields a no-op
// unsubscribe rather than throwing.
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
  // Snapshot the set so a handler may unsubscribe mid-dispatch; a throwing
  // subscriber is contained so it cannot break playback or sibling handlers.
  for (const handler of [...set]) {
    try {
      handler(event);
    } catch (e) {
      console.warn("[audio] channel " + channel + ": subscriber threw on " + type + ": " + (e?.message || e));
    }
  }
}

// Emit a channel's single close for one audible life. No-op unless the channel was
// audible or paused and this token has not already closed. Performs only the dedup
// and the emit -- no state reset -- so the calling teardown still owns clearing the
// plan/sources, and a caller may read ch.plan immediately after.
function _closeChannel(ch, channel, reason) {
  if (!ch.playing && !ch.paused) return;
  if (ch.closedToken === ch.token) return;
  ch.closedToken = ch.token;
  _emit("close", channel, { reason });
}

// Stop on a new turn / conversation switch. The chat module announces the event;
// the player decides which channels it affects from each channel's stopOn. Gating
// on a retained plan (not on playing) also tears down a naturally-ended channel,
// so its replayable chip does not leak across a conversation switch.
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

// Names of channels with a retained plan (playing, paused, or naturally ended),
// for the transport selector. Hard-stopped and never-played channels are omitted.
export function activeChannels() {
  const names = [];
  for (const [name, ch] of _channels) {
    if (ch.plan) names.push(name);
  }
  return names;
}

// The transport registers its repaint here; the engine invokes it on every
// discrete state change, holding only an opaque function reference so it never
// imports the transport (dependency stays one-way transport -> engine).
export function setBarChangeHook(fn) {
  _barChangeHook = typeof fn === "function" ? fn : null;
}

function _notifyBar() {
  if (!_barChangeHook) return;
  try {
    _barChangeHook();
  } catch (e) {
    console.warn("[audio] transport repaint hook threw: " + (e?.message || e));
  }
}

// Autoplay-policy accessors for the transport's gesture unlock; keep _ctx engine-
// private while the bar DOM and gesture binding live in the transport.
export function isContextSuspended() {
  return !!_ctx && _ctx.state === "suspended";
}

export function resumeContext() {
  return _ctx ? _ctx.resume() : Promise.resolve();
}
