// Pure scheduling and validation logic for the workflow audio player. No DOM,
// no Web Audio, no global state -- every function is a deterministic transform
// of its arguments, so the windowing math and the last-write-wins rules are
// unit-testable with literal inputs instead of a live AudioContext.

const DEFAULT_STOP_ON = { newTurn: true, convSwitch: true };

// Upper bound on a silent-gap segment; longer requests clamp to this so a stray
// value cannot pin a channel playing for an unbounded span.
const SILENCE_MAX_SEC = 600;

function _finite(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function _clamp(value, lo, hi) {
  return value < lo ? lo : value > hi ? hi : value;
}

// Cache key for an inline-bytes source. Short clips key on the whole string;
// long clips key on length plus a sampled FNV-1a hash, so a recurring multi-
// megabyte clip is matched without rehashing every byte on every play.
function inlineKey(b64) {
  if (b64.length <= 256) return "b64:" + b64;
  let h = 2166136261;
  const step = Math.max(1, Math.floor(b64.length / 256));
  for (let i = 0; i < b64.length; i += step) {
    h ^= b64.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return "b64:" + b64.length + ":" + (h >>> 0).toString(36);
}

// Validates one raw segment and returns a canonical form, or null when the
// segment is malformed (the caller skips it and warns). A segment names exactly
// one kind -- a workflow-attachment row id, inline base64, or a silent gap --
// plus an optional [start, end) window in seconds; an omitted end means the clip
// end. A silent gap carries its own length and ignores the window.
export function normalizeSegment(seg) {
  if (!seg || typeof seg !== "object") return null;
  const hasRow = seg.row != null;
  const hasB64 = typeof seg.b64 === "string" && seg.b64.length > 0;
  const hasSilence = seg.silence != null;
  if (hasRow + hasB64 + hasSilence !== 1) return null;
  if (hasSilence) {
    let dur = _finite(seg.silence, null);
    if (!(dur > 0)) return null;
    if (dur > SILENCE_MAX_SEC) dur = SILENCE_MAX_SEC;
    return { sourceKey: "silence", silent: true, durationSec: dur, start: 0, end: dur };
  }
  const start = _finite(seg.start, 0);
  if (start < 0) return null;
  const end = seg.end == null ? null : _finite(seg.end, null);
  if (hasRow) {
    return { sourceKey: "row:" + seg.row, source: { row: seg.row }, start, end };
  }
  const mime = typeof seg.mime === "string" ? seg.mime : "";
  return { sourceKey: inlineKey(seg.b64), source: { b64: seg.b64, mime }, start, end };
}

// Lays normalized segments end to end on the AudioContext clock. `durationByKey`
// supplies each decoded source's length in seconds; a segment whose source is
// absent from the map, or whose window is empty after clamping, is dropped --
// so the returned steps are exactly what can play. The cumulative `when` is what
// makes consecutive segments gapless.
export function buildSchedule(segments, durationByKey, clockNow, lead = 0) {
  const steps = [];
  const base = clockNow + lead;
  let when = base;
  for (const seg of segments) {
    if (seg.silent) {
      // A silent gap has no decoded buffer; its length is the segment's own
      // duration, not a windowed slice of clip bytes.
      const duration = seg.durationSec;
      if (!(duration > 0)) continue;
      steps.push({ sourceKey: seg.sourceKey, offset: 0, duration, when, silent: true });
      when += duration;
      continue;
    }
    const clipDur = durationByKey.get(seg.sourceKey);
    if (!(clipDur > 0)) continue;
    const offset = _clamp(seg.start, 0, clipDur);
    const end = seg.end == null ? clipDur : _clamp(seg.end, 0, clipDur);
    const duration = end - offset;
    if (!(duration > 0)) continue;
    steps.push({ sourceKey: seg.sourceKey, offset, duration, when });
    when += duration;
  }
  return { steps, totalDuration: when - base };
}

// Locates which segment of a whole-stream timeline a position falls in, with the
// elapsed/remaining/duration within that segment. The walk uses each step's own
// `duration` (clock-independent), not the absolute `when`, so it stays correct
// after a reschedule re-anchors the clock. A position at or past the total clamps
// to the last segment; an empty or absent timeline yields index -1 with zero
// spans, so a read taken before any schedule never throws.
export function locateSegment(steps, positionSec) {
  if (!Array.isArray(steps) || steps.length === 0) {
    return { index: -1, segElapsedSec: 0, segDurationSec: 0, segRemainingSec: 0 };
  }
  const pos = positionSec > 0 ? positionSec : 0;
  let acc = 0;
  for (let i = 0; i < steps.length - 1; i++) {
    const dur = steps[i].duration;
    if (pos < acc + dur) {
      const elapsed = _clamp(pos - acc, 0, dur);
      return { index: i, segElapsedSec: elapsed, segDurationSec: dur, segRemainingSec: dur - elapsed };
    }
    acc += dur;
  }
  const last = steps.length - 1;
  const dur = steps[last].duration;
  const elapsed = _clamp(pos - acc, 0, dur);
  return { index: last, segElapsedSec: elapsed, segDurationSec: dur, segRemainingSec: dur - elapsed };
}

// Re-lays a whole-stream timeline from an arbitrary offset, for resume and seek.
// Segments wholly before `offsetSec` are dropped; the straddling segment is
// trimmed (its `offset` advances and `duration` shrinks by the amount already
// elapsed); later segments are kept whole. Every `when` is re-anchored from
// `clockNow + lead` so the survivors lay out gaplessly from the current clock. An
// offset at or past the total yields no steps.
export function rescheduleFrom(steps, offsetSec, clockNow, lead = 0) {
  const out = [];
  const base = clockNow + lead;
  let when = base;
  let acc = 0;
  const from = offsetSec > 0 ? offsetSec : 0;
  for (const step of steps || []) {
    const segStart = acc;
    acc += step.duration;
    if (acc <= from) continue;
    const into = from > segStart ? from - segStart : 0;
    const duration = step.duration - into;
    if (!(duration > 0)) continue;
    // A silent gap loops one shared tiny buffer, so its read offset stays 0; the
    // trimmed remainder is carried entirely by duration.
    out.push({
      sourceKey: step.sourceKey,
      offset: step.silent ? 0 : step.offset + into,
      duration,
      when,
      silent: step.silent,
    });
    when += duration;
  }
  return { steps: out, totalRemaining: when - base };
}

// Decides what a channel does when its last scheduled source ends. A token that
// no longer matches the channel means a newer plan replaced this one while it
// was finishing, so the stale end must not touch the successor.
export function onEndedDecision({ planToken, channelToken, loop }) {
  if (planToken !== channelToken) return "ignore";
  return loop ? "restart" : "stop";
}

// Whether a channel stops on a given lifecycle event. The per-call `stopOn`
// object overrides per event; any event it omits falls back to the default
// (speech-shaped: stop on a new turn and on a conversation switch).
export function shouldStopOn(stopOn, event) {
  if (stopOn && typeof stopOn === "object" && event in stopOn) return stopOn[event] === true;
  return DEFAULT_STOP_ON[event] === true;
}
