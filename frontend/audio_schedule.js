const DEFAULT_STOP_ON = { newTurn: true, convSwitch: true };

const SILENCE_MAX_SEC = 600;

function _finite(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function _clamp(value, lo, hi) {
  return value < lo ? lo : value > hi ? hi : value;
}

function inlineKey(b64) {
  if (b64.length <= 256) return `b64:${b64}`;
  let h = 2166136261;
  const step = Math.max(1, Math.floor(b64.length / 256));
  for (let i = 0; i < b64.length; i += step) {
    h ^= b64.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return `b64:${b64.length}:${(h >>> 0).toString(36)}`;
}

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
    return { sourceKey: `row:${seg.row}`, source: { row: seg.row }, start, end };
  }
  const mime = typeof seg.mime === "string" ? seg.mime : "";
  return { sourceKey: inlineKey(seg.b64), source: { b64: seg.b64, mime }, start, end };
}

export function buildSchedule(segments, durationByKey, clockNow, lead = 0) {
  const steps = [];
  const base = clockNow + lead;
  let when = base;
  for (const seg of segments) {
    if (seg.silent) {
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

export function onEndedDecision({ planToken, channelToken, loop }) {
  if (planToken !== channelToken) return "ignore";
  return loop ? "restart" : "stop";
}

export function shouldStopOn(stopOn, event) {
  if (stopOn && typeof stopOn === "object" && event in stopOn) return stopOn[event] === true;
  return DEFAULT_STOP_ON[event] === true;
}
