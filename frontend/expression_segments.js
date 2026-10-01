import { sentenceStream } from "./text_segmentation.js";

// Structured cards and code must stay whole: a sentence boundary inside a
// tag, stylesheet or fenced block is not a safe place to pause rendering.
const STRUCTURED = /<\/?[a-z][^>]*>|```|~~~/i;

/** Exact source offsets, grouped by the image the reader will actually see. */
export async function expressionSegments(text, labels, classify, isCurrent = () => true) {
  const fallback = labels.includes("neutral") ? "neutral" : null;
  if (!labels.length || STRUCTURED.test(text)) {
    return [{ end: text.length, label: fallback }];
  }
  const runs = [];
  let end = 0;
  for (const unit of sentenceStream(text)) {
    if (!isCurrent()) return [];
    end += unit.text.length;
    if (unit.kind !== "sentence") {
      if (runs.length) runs.at(-1).end = end;
      continue;
    }
    const detected = await classify(unit.text);
    if (!isCurrent()) return [];
    const label = labels.includes(detected) ? detected : fallback;
    if (runs.length && runs.at(-1).label === label) runs.at(-1).end = end;
    else runs.push({ end, label });
  }
  if (!runs.length) runs.push({ end: text.length, label: fallback });
  runs.at(-1).end = text.length;
  return runs;
}

/** Sentences of a still-growing reply that later text can no longer change. */
export function settledSentences(text) {
  if (STRUCTURED.test(text)) return [];
  // The last unit may still grow, and a boundary depends on the character after it.
  return sentenceStream(text)
    .slice(0, -1)
    .filter((unit) => unit.kind === "sentence")
    .map((unit) => unit.text);
}
