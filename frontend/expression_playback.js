import { api } from "./api.js";
import { messageDisplaySource } from "./card_scripts.js";
import { expressionSegments, settledSentences } from "./expression_segments.js";
import { charactersView, localMlReady, notify, S } from "./state.js";
import { toast } from "./utils.js";

export const expressionPlaybackEnabled = () =>
  S.settings.expression_rendering === "expression" && localMlReady("emotion_classifier");

const hasExpressions = (charId) => !!charactersView().find((c) => c.id === charId)?.has_expressions;

/** The character's uploaded expression labels; none without a pack or on failure. */
export async function expressionLabels(charId) {
  if (!hasExpressions(charId)) return [];
  try {
    return (await api.get(`/characters/${charId}/expressions`)).labels || [];
  } catch {
    return [];
  }
}

const PREWARM_INTERVAL_MS = 250;

// This turn's classifier labels by sentence. Sentences the stream has settled
// are classified while the reply generates, so settlement only waits on the
// ones a later pass rewrote and the final one.
let _classified = new Map();
let _warm = null;

function classifyEmotion(cache, text) {
  const key = text.trim();
  let label = cache.get(key);
  if (!label) {
    label = api.post("/local-ml/classify-emotion", { text }).then((r) => r.label);
    cache.set(key, label);
    // Settlement retries a failed sentence and reports the failure itself.
    label.catch(() => cache.get(key) === label && cache.delete(key));
  }
  return label;
}

function replyCharId(msg) {
  return S.groupCast
    ? S.groupCast.members.find((member) => member.id === msg.speaker_member_id)?.character_card_id
    : S.activeCharId;
}

/** A new turn drops the last one's labels and streams until a reply buffers. */
export function beginExpressionPrewarm() {
  endExpressionPrewarm();
  _classified = new Map();
  S.expressionBuffering = false;
}

/**
 * Hold this speaker's reply for playback when its character has expressions.
 * Once held, every later reply in the turn is too, so replies reveal in order.
 * True only when this call started the hold.
 */
export function bufferExpressionReply(speakerMemberId = null) {
  if (S.expressionBuffering || !expressionPlaybackEnabled()) return false;
  if (!hasExpressions(replyCharId({ speaker_member_id: speakerMemberId }))) return false;
  S.expressionBuffering = true;
  _warm = { msg: null, timer: 0, running: false };
  return true;
}

export function endExpressionPrewarm() {
  if (_warm) clearTimeout(_warm.timer);
  _warm = null;
}

/** Classify the streaming reply's settled sentences in the background. */
export function prewarmExpressionLabels(content, speakerMemberId) {
  const warm = _warm;
  if (!warm) return;
  warm.msg = { role: "assistant", content, speaker_member_id: speakerMemberId };
  warm.timer ||= setTimeout(() => {
    warm.timer = 0;
    if (!warm.running) void drainPrewarm(warm);
  }, PREWARM_INTERVAL_MS);
}

// One request at a time, so warming never floods a local classifier that may
// share the machine with generation.
async function drainPrewarm(warm) {
  warm.running = true;
  try {
    while (_warm === warm) {
      const msg = warm.msg;
      if (!hasExpressions(replyCharId(msg))) return;
      const next = settledSentences(messageDisplaySource(msg)).find((text) => !_classified.has(text.trim()));
      if (!next) return;
      try {
        await classifyEmotion(_classified, next);
      } catch {
        if (_warm === warm) endExpressionPrewarm();
      }
    }
  } finally {
    warm.running = false;
  }
}

export function cancelExpressionPlayback() {
  S.expressionPlayback = null;
}

/** The playback, unless the reader left its conversation or its text changed. */
export function activeExpressionPlayback() {
  const p = S.expressionPlayback;
  if (!p) return null;
  const editing = [S.editingMsgId, S.forkEditMsgId, S.proseRewriteMsgId];
  if (
    p.convId !== S.activeConvId ||
    S.documentMode ||
    !expressionPlaybackEnabled() ||
    p.rows.some(
      (row) => editing.includes(row.id) || S.messages.find((msg) => msg.id === row.id)?.content !== row.content,
    )
  ) {
    cancelExpressionPlayback();
    return null;
  }
  return p;
}

export function expressionPlaybackCue() {
  const p = activeExpressionPlayback();
  if (!p) return null;
  const row = p.rows[p.rowIndex];
  return { charId: row.charId, loading: p.loading, label: p.loading ? null : row.runs[p.runIndex].label };
}

export function advanceExpressionPlayback() {
  const p = activeExpressionPlayback();
  if (!p || p.loading || p.complete) return false;
  if (p.runIndex + 1 < p.rows[p.rowIndex].runs.length) p.runIndex++;
  else {
    p.rowIndex++;
    p.runIndex = 0;
  }
  reveal(p);
  return true;
}

function reveal(p) {
  const row = p.rows[p.rowIndex];
  row.visibleEnd = row.runs[p.runIndex].end;
  p.complete = p.rowIndex === p.rows.length - 1 && p.runIndex === row.runs.length - 1;
  notify("expression-playback");
}

/** Start from saved, authoritative prose; never an Editor progress preview. */
export async function startExpressionPlayback(messages) {
  cancelExpressionPlayback();
  endExpressionPrewarm();
  // Take this turn's labels; a later turn starts its own.
  const cached = _classified;
  _classified = new Map();
  if (!expressionPlaybackEnabled() || !messages.length) return;
  const p = {
    convId: S.activeConvId,
    rows: messages.map((msg) => ({
      id: msg.id,
      content: msg.content,
      source: messageDisplaySource(msg),
      charId: replyCharId(msg),
      visibleEnd: 0,
      runs: null,
    })),
    rowIndex: 0,
    runIndex: 0,
    loading: true,
    complete: false,
  };
  S.expressionPlayback = p;
  const current = () => activeExpressionPlayback() === p;
  const classify = (text) => classifyEmotion(cached, text);
  try {
    const packs = new Map();
    for (const row of p.rows) {
      if (!packs.has(row.charId)) packs.set(row.charId, await expressionLabels(row.charId));
      row.runs = await expressionSegments(row.source, packs.get(row.charId), classify, current);
      if (!current()) return;
    }
    p.loading = false;
    reveal(p);
  } catch (e) {
    if (!current()) return;
    cancelExpressionPlayback();
    notify("expression-playback");
    toast(`Expression playback unavailable: ${e.message}`, true);
  }
}

export function handleExpressionPlaybackKey(event) {
  if (
    event.code !== "Space" ||
    event.repeat ||
    event.defaultPrevented ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    event.target?.closest?.(
      "input, textarea, select, button, a, [contenteditable]:not([contenteditable='false']), [role='textbox']",
    ) ||
    document.querySelector(".modal-overlay:not(.hidden)")
  )
    return;
  if (advanceExpressionPlayback()) event.preventDefault();
}
