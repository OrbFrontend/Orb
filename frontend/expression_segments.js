import { sentenceStream } from "./text_segmentation.js";

/** Exact source offsets, grouped by the image the reader will actually see. */
export async function expressionSegments(text, labels, classify, isCurrent = () => true) {
  const fallback = labels.includes("neutral") ? "neutral" : null;
  // Structured cards and code must stay whole: a sentence boundary inside a
  // tag, stylesheet or fenced block is not a safe place to pause rendering.
  if (!labels.length || /<\/?[a-z][^>]*>|```|~~~/i.test(text)) {
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
