const TERMINATORS = new Set([...".!?…。！？؟۔｡．।॥"]);
const TIGHT_TERMINATORS = new Set([..."…。！？؟۔｡．।॥"]);
const TRAILING_MARKERS = new Set([..."'»›」』*_)]}>”’“‘\""]);

const ALWAYS_NONTERMINAL = new Set(["e.g.", "i.e.", "a.k.a.", "vs.", "v.", "cf."]);
const LOWERCASE_CONTINUATION = new Set([
  "a.m.",
  "p.m.",
  "approx.",
  "dept.",
  "est.",
  "misc.",
  "incl.",
  "esp.",
  "min.",
  "max.",
  "ref.",
  "sec.",
]);
const TITLES = new Set([
  "mr.",
  "mrs.",
  "ms.",
  "mx.",
  "dr.",
  "prof.",
  "rev.",
  "hon.",
  "pres.",
  "gov.",
  "sen.",
  "rep.",
  "sr.",
  "jr.",
  "st.",
  "mt.",
  "capt.",
  "cpt.",
  "lt.",
  "col.",
  "gen.",
  "sgt.",
  "adm.",
  "maj.",
]);
const NUMBER_ABBREVIATIONS = new Set([
  "no.",
  "fig.",
  "eq.",
  "ch.",
  "vol.",
  "pp.",
  "jan.",
  "feb.",
  "mar.",
  "apr.",
  "jun.",
  "jul.",
  "aug.",
  "sep.",
  "sept.",
  "oct.",
  "nov.",
  "dec.",
]);

const LETTER = /\p{L}/u;
const ALNUM = /[\p{L}\p{N}]/u;
const ABBREVIATION = /(?:\p{L}+\.)+$/u;
// Parity with backend/core/text_segmentation.py: a trailing-off ellipsis is
// only a sentence end when what follows starts one. `a bit... more still than
// usual` is one sentence, and splitting it manufactures a fragment.
const ELLIPSIS_RUN = /^(?:\.{2,}|…+)$/u;

export function isHardLineBreak(c) {
  return (
    c === "\n" ||
    c === "\v" ||
    c === "\f" ||
    c === "\r" ||
    c === "\x85" ||
    c === "\u2028" ||
    c === "\u2029" ||
    (c >= "\x1c" && c <= "\x1e")
  );
}

export function isSentenceWhitespace(c) {
  return c != null && /\s/u.test(c);
}

function _isUpper(c) {
  return Boolean(c && LETTER.test(c) && c === c.toUpperCase() && c !== c.toLowerCase());
}

function _isLower(c) {
  return Boolean(c && LETTER.test(c) && c === c.toLowerCase() && c !== c.toUpperCase());
}

function _periodIsNonterminal(text, period, nextIndex) {
  if (/\d/u.test(text[period - 1] || "") && /\d/u.test(text[period + 1] || "")) return true;
  const match = text.slice(0, period + 1).match(ABBREVIATION);
  if (!match) return false;
  const raw = match[0];
  const abbreviation = raw.toLowerCase();
  const next = text[nextIndex] || "";
  if (ALWAYS_NONTERMINAL.has(abbreviation)) return true;
  if (TITLES.has(abbreviation) && ALNUM.test(next)) return true;
  if (NUMBER_ABBREVIATIONS.has(abbreviation) && /\d/u.test(next)) return true;
  const letters = raw.replaceAll(".", "");
  if (
    ALNUM.test(next) &&
    (letters.length === 1 || ((abbreviation.match(/\./g) || []).length >= 2 && letters === letters.toUpperCase()))
  ) {
    return true;
  }
  return LOWERCASE_CONTINUATION.has(abbreviation) && next === next.toLowerCase() && LETTER.test(next);
}

export function sentenceBoundaryEnds(text) {
  const ends = [];
  let i = 0;
  while (i < text.length) {
    if (!TERMINATORS.has(text[i])) {
      i += 1;
      continue;
    }
    let terminalEnd = i + 1;
    while (terminalEnd < text.length && TERMINATORS.has(text[terminalEnd])) terminalEnd += 1;
    let markerEnd = terminalEnd;
    while (markerEnd < text.length && TRAILING_MARKERS.has(text[markerEnd])) markerEnd += 1;
    let boundaryEnd = markerEnd;
    while (
      boundaryEnd < text.length &&
      isSentenceWhitespace(text[boundaryEnd]) &&
      !isHardLineBreak(text[boundaryEnd])
    ) {
      boundaryEnd += 1;
    }
    const hasSeparator = boundaryEnd > markerEnd;
    const terminals = text.slice(i, terminalEnd);
    const allowsTight =
      [...terminals].some((c) => TIGHT_TERMINATORS.has(c)) ||
      (markerEnd < text.length && _isUpper(text[markerEnd]) && [...terminals].some((c) => ".!?".includes(c)));
    if (!hasSeparator && !(allowsTight && markerEnd < text.length)) {
      i = terminalEnd;
      continue;
    }
    // The abbreviation check below only ever guarded a SINGLE period, so a run
    // of terminators reached the push unconditionally.
    if (_isLower(text[boundaryEnd]) && ELLIPSIS_RUN.test(terminals)) {
      i = terminalEnd;
      continue;
    }
    const onePeriod = terminalEnd === i + 1 && text[i] === ".";
    if (!(onePeriod && _periodIsNonterminal(text, i, boundaryEnd))) {
      ends.push(boundaryEnd);
      i = boundaryEnd;
    } else {
      i = terminalEnd;
    }
  }
  return ends;
}

function _lineStream(line) {
  const out = [];
  let start = 0;
  for (const end of sentenceBoundaryEnds(line)) {
    if (line.slice(start, end).trim()) out.push({ kind: "sentence", text: line.slice(start, end) });
    else if (end > start) out.push({ kind: "spacing", text: line.slice(start, end) });
    start = end;
  }
  if (start < line.length) {
    out.push({ kind: line.slice(start).trim() ? "sentence" : "spacing", text: line.slice(start) });
  }
  return out;
}

export function sentenceStream(text) {
  const out = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (!isHardLineBreak(text[i])) continue;
    out.push(..._lineStream(text.slice(start, i)));
    let end = i + 1;
    if (text[i] === "\r" && text[end] === "\n") end += 1;
    out.push({ kind: "linebreak", text: text.slice(i, end) });
    i = end - 1;
    start = end;
  }
  out.push(..._lineStream(text.slice(start)));
  return out;
}

export function splitSentences(text) {
  return sentenceStream(text)
    .filter((unit) => unit.kind === "sentence")
    .map((unit) => unit.text.trim())
    .filter(Boolean);
}

export function endsWithSentenceTerminator(text) {
  let trimmed = text.trimEnd();
  while (trimmed && TRAILING_MARKERS.has(trimmed.at(-1))) trimmed = trimmed.slice(0, -1).trimEnd();
  return Boolean(trimmed && TERMINATORS.has(trimmed.at(-1)));
}

export function tokenEndsSentence(token, nextToken = "") {
  if (!token || !nextToken) return false;
  const probe = `${token} ${nextToken}`;
  const first = sentenceBoundaryEnds(probe)[0];
  return first != null && first <= token.length + 1;
}

export function splitTightSentenceChunks(token) {
  const out = [];
  let start = 0;
  for (const end of sentenceBoundaryEnds(token)) {
    if (end > start) out.push(token.slice(start, end));
    start = end;
  }
  if (start < token.length) out.push(token.slice(start));
  return out.length ? out : [token];
}
