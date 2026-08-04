// Workflow-private dialogue parser. It intentionally does not import the app's
// text utilities: TTS must remain portable as a plugin. The backend owns an
// independent implementation; shared adversarial fixtures pin their behavior.

const QUOTE_PAIRS = new Map([
  ["“", "”"],
  ["‘", "’"],
  ["«", "»"],
  ["‹", "›"],
  ["「", "」"],
  ["『", "』"],
  ["„", "“"],
  ["‚", "‘"],
]);
const OPEN_QUOTES = new Set(QUOTE_PAIRS.keys());
const CLOSE_QUOTES = new Set(QUOTE_PAIRS.values());
const HARD_BREAKS = new Set([..."\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029"]);
const ALNUM = /[\p{L}\p{N}]/u;

function escaped(text, index) {
  let slashes = 0;
  for (index -= 1; index >= 0 && text[index] === "\\"; index--) slashes += 1;
  return slashes % 2 === 1;
}

function overlaps(start, end, spans) {
  return spans.some((span) => start < span.end && span.start < end);
}

function isWorkflowWhitespace(char) {
  return char != null && (/\s/u.test(char) || HARD_BREAKS.has(char));
}

function findQuotedSpans(text) {
  const spans = [];
  const stack = [];
  let outerStart = 0;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (char === '"' && escaped(text, index)) continue;
    if (
      char === "’" &&
      index > 0 &&
      index + 1 < text.length &&
      ALNUM.test(text[index - 1]) &&
      ALNUM.test(text[index + 1])
    ) {
      continue;
    }
    if (char === '"' && !stack.length && index > 0 && /\d/u.test(text[index - 1])) continue;

    if (stack.length && char === stack.at(-1)) {
      stack.pop();
      if (!stack.length)
        spans.push({ start: outerStart, end: index + 1, contentStart: outerStart + 1, contentEnd: index });
      continue;
    }
    if (char === '"') {
      if (!stack.length) outerStart = index;
      stack.push(char);
    } else if (OPEN_QUOTES.has(char)) {
      if (!stack.length) outerStart = index;
      stack.push(QUOTE_PAIRS.get(char));
    } else if (CLOSE_QUOTES.has(char) && stack.length) {
      const found = stack.lastIndexOf(char);
      if (found >= 0) stack.splice(found);
      if (!stack.length)
        spans.push({ start: outerStart, end: index + 1, contentStart: outerStart + 1, contentEnd: index });
    }
  }
  return spans;
}

function findParentheticalSpans(text, quoted) {
  const spans = [];
  const stack = [];
  for (let index = 0; index < text.length; index++) {
    if (overlaps(index, index + 1, quoted)) continue;
    if (text[index] === "(") stack.push(index);
    else if (text[index] === ")" && stack.length) {
      const start = stack.pop();
      if (!stack.length) spans.push({ start, end: index + 1 });
    }
  }
  return spans;
}

function findBeatSpans(text, quoted, parentheticals) {
  const spans = [];
  let opener = null;
  const protectedSpans = [...quoted, ...parentheticals];
  for (let index = 0; index < text.length; index++) {
    if (text[index] !== "*" || escaped(text, index) || overlaps(index, index + 1, protectedSpans)) continue;
    if (text[index - 1] === "*" || text[index + 1] === "*") continue;
    if (opener == null) {
      if (index + 1 < text.length && !isWorkflowWhitespace(text[index + 1])) opener = index;
    } else if (index > opener + 1 && !isWorkflowWhitespace(text[index - 1])) {
      spans.push({ start: opener, end: index + 1, contentStart: opener + 1, contentEnd: index });
      opener = null;
    }
  }
  return spans;
}

function findEmdashSpans(text, protectedSpans) {
  const spans = [];
  let opener = null;
  for (let index = 0; index < text.length; index++) {
    if (text[index] !== "—" || overlaps(index, index + 1, protectedSpans)) continue;
    if (opener == null) opener = index;
    else {
      if (index > opener + 1)
        spans.push({ start: opener, end: index + 1, contentStart: opener + 1, contentEnd: index });
      opener = null;
    }
  }
  return spans;
}

function whitespaceTokens(text) {
  const tokens = [];
  let start = null;
  for (let index = 0; index <= text.length; index++) {
    const separator = index === text.length || isWorkflowWhitespace(text[index]);
    if (!separator && start == null) start = index;
    if (separator && start != null) {
      tokens.push(text.slice(start, index));
      start = null;
    }
  }
  return tokens;
}

function spokenText(text) {
  return whitespaceTokens(text).join(" ");
}

export function alignmentKey(token) {
  return token.toLowerCase().replace(/[^a-z0-9]/g, "");
}

export function alignableKeys(text) {
  return whitespaceTokens(text).map(alignmentKey).filter(Boolean);
}

export function extractBlocks(content) {
  if (!content?.trim()) return [];
  const quoted = findQuotedSpans(content);
  const parentheticals = findParentheticalSpans(content, quoted);
  const beats = findBeatSpans(content, quoted, parentheticals);
  const emdashes = findEmdashSpans(content, [...quoted, ...parentheticals, ...beats]);
  return [...quoted, ...emdashes]
    .filter((span) => !overlaps(span.start, span.end, parentheticals))
    .sort((left, right) => left.start - right.start)
    .map((span) => spokenText(content.slice(span.contentStart, span.contentEnd)))
    .filter(Boolean);
}
