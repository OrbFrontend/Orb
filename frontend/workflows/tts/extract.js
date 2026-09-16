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

const OPENERS = new Set([...OPEN_QUOTES, '"', "＂", "″", "—", "‟", "❝", "〝"]);
const CLOSERS = new Set([...CLOSE_QUOTES, '"', "＂", "″", "—", "❞", "〞"]);

function quoteAffinity(firstWord, lastWord) {
  const lead = /^[^\p{L}\p{N}]*/u.exec(firstWord || "")[0];
  const trail = /[^\p{L}\p{N}]*$/u.exec(lastWord || "")[0];
  return ([...lead].some((c) => OPENERS.has(c)) ? 1 : 0) + ([...trail].some((c) => CLOSERS.has(c)) ? 1 : 0);
}

function runMatches(words, tokens, at) {
  for (let k = 0; k < tokens.length; k++) {
    if (words[at + k].t !== tokens[k]) return false;
  }
  return true;
}

function firstRun(words, tokens, from, until) {
  for (let at = from; at + tokens.length <= until; at++) {
    if (runMatches(words, tokens, at)) return at;
  }
  return -1;
}

function lastRun(words, tokens, from, until) {
  for (let at = until - tokens.length; at >= from; at--) {
    if (runMatches(words, tokens, at)) return at;
  }
  return -1;
}

function bestRun(words, tokens, from, to) {
  let best = -1;
  let bestScore = -1;
  for (let at = from; at <= to; at++) {
    if (!runMatches(words, tokens, at)) continue;
    const score = quoteAffinity(words[at].raw, words[at + tokens.length - 1].raw);
    if (score > bestScore) {
      best = at;
      bestScore = score;
      if (score === 2) break;
    }
  }
  return best;
}

// Locate each block's on-screen words. `words` are the message's rendered tokens
// as `{t, raw}`, `blockTokens` each block's keys from `alignableKeys`; the result
// holds one start index per block, or -1 for a block the message no longer says.
//
// Taking the first run that matches gives a one-word block like `"Right,"` away
// to any bare `right` earlier in the narration, which steals the click target and
// the karaoke highlight and leaves the real line unmapped. Preferring the
// best-delimited run fixes that but, chosen per block against the whole rest of
// the message, lets an undelimited block reach forward and take the run a later
// block needed. So bound the choice first: the leftmost chain gives every block
// its earliest feasible run and the rightmost chain its latest, and a block can
// only move inside that window, where by construction no other block is
// displaced. Ties keep the leftmost run, as does a message that delimits nothing.
export function alignBlocks(words, blockTokens) {
  const count = blockTokens.length;
  const earliest = new Array(count).fill(-1);
  const latest = new Array(count).fill(-1);
  const starts = new Array(count).fill(-1);

  let cursor = 0;
  for (let i = 0; i < count; i++) {
    const tokens = blockTokens[i];
    if (!tokens.length) continue;
    const at = firstRun(words, tokens, cursor, words.length);
    if (at < 0) continue;
    earliest[i] = at;
    cursor = at + tokens.length;
  }

  let until = words.length;
  for (let i = count - 1; i >= 0; i--) {
    if (earliest[i] < 0) continue;
    latest[i] = lastRun(words, blockTokens[i], earliest[i], until);
    until = latest[i];
  }

  cursor = 0;
  for (let i = 0; i < count; i++) {
    if (earliest[i] < 0) continue;
    const tokens = blockTokens[i];
    const at = bestRun(words, tokens, Math.max(cursor, earliest[i]), latest[i]);
    if (at < 0) continue;
    starts[i] = at;
    cursor = at + tokens.length;
  }
  return starts;
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

// New attachments carry the exact speech chosen by the backend classifier.
// Keep the old scanner above only for attachments without this additive field.
export function attachmentBlocks(content, blocks) {
  if (Array.isArray(blocks) && blocks.every((block) => typeof block.spoken_text === "string")) {
    return blocks.map((block) => block.spoken_text);
  }
  return extractBlocks(content);
}
