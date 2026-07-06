// On-demand "slop score" for an assistant message. The button lives in the
// message toolbar (chat_core.js buildMsgToolbar); this module owns the click.
//
// We segment the rendered body ourselves (segmentBody, the same pass workflows
// use) so the sentence numbering that scores come back against is the exact
// numbering we colour -- no second splitter, no drift. The backend is a pure
// list-of-strings scorer; everything visual is ephemeral (a re-render clears it,
// re-click to re-score).

import { api } from "./api.js";
import { toast } from "./utils.js";
import { segmentBody } from "./workflow_segmentation.js";

const SLOP_THRESHOLD = 0.65; // matches the backend's intended cutoff; display-side only

function bodyEl(msgId) {
  return document.querySelector(`#chat-messages .message[data-msg-id="${msgId}"] .msg-body`);
}

// sentIndex -> [word spans], in document order.
function sentenceSpans(body) {
  const bySent = new Map();
  for (const span of body.querySelectorAll(".seg")) {
    const si = span.dataset.sent;
    if (!bySent.has(si)) bySent.set(si, []);
    bySent.get(si).push(span);
  }
  return bySent;
}

function clearSlop(body) {
  for (const sp of body.querySelectorAll(".seg.slop-flag")) {
    sp.classList.remove("slop-flag");
    sp.style.removeProperty("--slop-a");
  }
  for (const b of body.querySelectorAll(".slop-badge")) b.remove();
  delete body.dataset.slopScored;
  body.closest(".message")?.querySelector(".slop-chip")?.remove();
}

function paint(body, sentIndices, bySent, scores) {
  let flagged = 0;
  sentIndices.forEach((si, i) => {
    const score = Number(scores[i]) || 0;
    if (score < SLOP_THRESHOLD) return;
    flagged++;
    const spans = bySent.get(si);
    // score 0.65..1.0 -> background alpha 22%..55% (color-mix keeps it theme-aware).
    const alpha = Math.round(22 + Math.min(1, (score - SLOP_THRESHOLD) / (1 - SLOP_THRESHOLD)) * 33);
    for (const sp of spans) {
      sp.classList.add("slop-flag");
      sp.style.setProperty("--slop-a", alpha + "%");
    }
    const badge = document.createElement("sup");
    badge.className = "slop-badge";
    badge.textContent = Math.round(score * 100) + "%";
    spans[spans.length - 1].after(badge);
  });
  body.dataset.slopScored = "1";

  const total = sentIndices.length;
  const chip = document.createElement("span");
  chip.className = "slop-chip";
  chip.textContent = `Slop ${Math.round((flagged / total) * 100)}% · ${flagged}/${total} flagged`;
  // Sibling *after* the toolbar: the toolbar is opacity:0 unless hovered, so a
  // chip inside it would vanish; here it stays visible after scoring.
  const msg = body.closest(".message");
  const toolbar = msg?.querySelector(".msg-toolbar");
  if (toolbar) toolbar.after(chip);
  else msg?.appendChild(chip);
}

export async function scoreSlop(msgId, btn) {
  const body = bodyEl(msgId);
  if (!body) return;
  if (body.dataset.slopScored === "1") {
    clearSlop(body);
    return;
  }

  segmentBody(body); // idempotent; skips <pre>/<code>
  const bySent = sentenceSpans(body);
  const sentIndices = [...bySent.keys()];
  if (!sentIndices.length) {
    toast("Nothing to score");
    return;
  }
  // Word spans exclude whitespace, so join with a single space to rebuild the
  // sentence -- exact spacing doesn't matter to the classifier.
  const sentences = sentIndices.map((si) =>
    bySent
      .get(si)
      .map((s) => s.textContent)
      .join(" "),
  );

  if (btn) btn.disabled = true;
  try {
    const { scores } = await api.post("/local-ml/slop-score", { sentences });
    clearSlop(body); // in case a stale paint lingers
    paint(body, sentIndices, bySent, scores || []);
  } catch (e) {
    if (e.status === 503) toast("Enable & download the AI-Slop Classifier in Settings → Local ML");
    else {
      console.error("slop-score failed", e);
      toast("Slop scoring failed", true);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}
