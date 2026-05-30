// Ordered speakable substrings of a message, one per synthesized audio block.
// This mirrors the backend dialogue extractor (regex_extractor.py): the block
// COUNT and ORDER must match `regex_extract` so block i here lines up with clip
// i in the attachment. Only the spoken substrings are produced -- emotion tags
// and pauses are backend-only and irrelevant to mapping rendered words to
// clips. Kept in sync with the Python extractor by hand; drift costs only
// click-to-block precision (a word may not be clickable), never playback.

// Parentheticals are inner monologue and are stripped before extraction.
const RE_PARENTHETICAL = /\([^)]+\)/g;
// Action beats in asterisks; only beats outside quoted spans split the text.
const RE_ASTERISK = /\*([^*]+)\*/g;
// Double-quoted dialogue, straight or curly quotes (U+201C open / U+201D close).
const RE_QUOTED = /[\u201c"]([^\u201d"]+)[\u201d"]/g;
// Em-dash (U+2014) dialogue, used only when a segment has no double-quoted dialogue.
const RE_EMDASH = /\u2014([^\u2014]+?)\u2014/g;

export function extractBlocks(content) {
  if (!content || !content.trim()) return [];
  const cleaned = content.replace(RE_PARENTHETICAL, "");

  // Asterisks inside quotes are emphasis, not action beats; mask the quoted
  // spans so they do not split the text into separate segments.
  const quotedSpans = [];
  for (const qm of cleaned.matchAll(RE_QUOTED)) {
    quotedSpans.push([qm.index, qm.index + qm[0].length]);
  }

  // Split the text at action beats that fall outside quoted spans; the beats
  // themselves carry no dialogue and are dropped.
  const segments = [];
  let pos = 0;
  for (const m of cleaned.matchAll(RE_ASTERISK)) {
    const start = m.index;
    const end = m.index + m[0].length;
    if (quotedSpans.some(([qs, qe]) => qs <= start && end <= qe)) continue;
    if (start > pos) segments.push(cleaned.slice(pos, start));
    pos = end;
  }
  if (pos < cleaned.length) segments.push(cleaned.slice(pos));

  const blocks = [];
  for (const seg of segments) {
    let matches = [...seg.matchAll(RE_QUOTED)];
    if (!matches.length) matches = [...seg.matchAll(RE_EMDASH)];
    for (const dm of matches) {
      const dialogue = dm[1].trim();
      if (dialogue) blocks.push(dialogue);
    }
  }
  return blocks;
}
