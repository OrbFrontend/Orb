import { api } from "./api.js";
import { messageDisplaySource } from "./card_scripts.js";
import { expressionSegments } from "./expression_segments.js";
import { charactersView, localMlReady, notify, S } from "./state.js";
import { toast } from "./utils.js";

export const expressionPlaybackEnabled = () =>
  S.settings.expression_rendering === "expression" && localMlReady("emotion_classifier");

/** The character's uploaded expression labels; none without a pack or on failure. */
export async function expressionLabels(charId) {
  if (!charactersView().find((c) => c.id === charId)?.has_expressions) return [];
  try {
    return (await api.get(`/characters/${charId}/expressions`)).labels || [];
  } catch {
    return [];
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
  return { charId: row.charId, label: row.runs?.[p.runIndex]?.label ?? null };
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
  if (!expressionPlaybackEnabled() || !messages.length) return;
  const p = {
    convId: S.activeConvId,
    rows: messages.map((msg) => ({
      id: msg.id,
      content: msg.content,
      source: messageDisplaySource(msg),
      charId: S.groupCast
        ? S.groupCast.members.find((member) => member.id === msg.speaker_member_id)?.character_card_id
        : S.activeCharId,
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
  const classify = async (text) => (await api.post("/local-ml/classify-emotion", { text })).label;
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
