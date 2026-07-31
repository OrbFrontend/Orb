// `segmentBody` is the only writer of `.seg` spans; it runs from the chat
// render path, never from workflow code. The integer `data-seg` (word) and
// `data-sent` (sentence) these spans carry are the single source of truth for
// unit numbering -- workflows address units only by index, never by
// re-tokenizing text. A word that `formatProse` splits across an inline tag
// (`wo<strong>rd</strong>`) keeps one `data-seg`, so one visual word is always
// one unit.

function _isWs(c) {
  return c === " " || c === "\t" || c === "\n" || c === "\r" || c === "\f" || c === "\v";
}

function _isTerminator(c) {
  return c === "." || c === "!" || c === "?";
}

function _isCloser(c) {
  return c === '"' || c === "'" || c === ")" || c === "]" || c === "\u201d" || c === "\u2019";
}

// A token ends a sentence when its last non-closer character is a terminator,
// so `"Hello."` and `(done!)` both count.
function _endsSentence(text, start, end) {
  let j = end - 1;
  while (j >= start && _isCloser(text[j])) j--;
  return j >= start && _isTerminator(text[j]);
}

// Pure tokenizer over one text run. `carry` threads state across runs so a word
// or sentence can span the inline-element boundaries `formatProse` introduces.
// `midWord`: the previous run ended mid-word (non-whitespace, no separating
// space), so the next run's first token continues it. `pendingTerminator`: a
// terminator is awaiting whitespace confirmation, because the closing quote of
// `"Hello."` ends one text node while the confirming space begins the next.
// `breakPending`: a confirmed sentence boundary, applied before the next word.
// `opts.lineBreakBefore` forces it for a `<br>`, which `formatProse` emits for
// every newline.
export function tokenizeRun(text, carry, opts) {
  let { wordIndex, sentIndex, midWord, pendingTerminator, breakPending } = carry;
  if (opts?.lineBreakBefore) {
    midWord = false;
    pendingTerminator = false;
    breakPending = true;
  }
  const words = [];
  const n = text.length;
  let i = 0;
  let firstToken = true;
  while (i < n) {
    const wsStart = i;
    while (i < n && _isWs(text[i])) i++;
    const hadWs = i > wsStart;
    if (hadWs) {
      midWord = false;
      if (pendingTerminator) {
        breakPending = true;
        pendingTerminator = false;
      }
    }
    if (i >= n) break;
    const start = i;
    while (i < n && !_isWs(text[i])) i++;
    const end = i;
    const continues = midWord && firstToken && !hadWs;
    if (!continues) {
      if (breakPending && wordIndex >= 0) sentIndex += 1;
      breakPending = false;
      wordIndex += 1;
    }
    words.push({ start, end, wordIndex, sentIndex });
    pendingTerminator = _endsSentence(text, start, end);
    midWord = end === n;
    firstToken = false;
  }
  return { words, carry: { wordIndex, sentIndex, midWord, pendingTerminator, breakPending } };
}

function _wrapTextNode(node, words) {
  if (!words.length) return;
  const text = node.data;
  const frag = document.createDocumentFragment();
  let pos = 0;
  for (const w of words) {
    if (w.start > pos) frag.appendChild(document.createTextNode(text.slice(pos, w.start)));
    const span = document.createElement("span");
    span.className = "seg";
    span.dataset.seg = String(w.wordIndex);
    span.dataset.sent = String(w.sentIndex);
    span.textContent = text.slice(w.start, w.end);
    frag.appendChild(span);
    pos = w.end;
  }
  if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
  node.parentNode.replaceChild(frag, node);
}

// Callers gate this off for diff-rendered bodies; the `<pre>`/`<code>` subtree
// skip keeps fenced and inline code opaque, and a `<br>` between runs is
// recorded as a hard sentence boundary the text-only walk would otherwise miss.
export function segmentBody(bodyEl) {
  if (!bodyEl || bodyEl.dataset.segApplied === "1") return;
  const walker = document.createTreeWalker(bodyEl, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    acceptNode(node) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.tagName === "BR") return NodeFilter.FILTER_ACCEPT;
        if (node.tagName === "PRE" || node.tagName === "CODE") return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_SKIP;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  // Collect before mutating; replacing a text node mid-walk invalidates the walker.
  const items = [];
  let breakBefore = false;
  let node = walker.nextNode();
  for (; node; node = walker.nextNode()) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      breakBefore = true;
      continue;
    }
    items.push({ node, lineBreakBefore: breakBefore });
    breakBefore = false;
  }
  let carry = { wordIndex: -1, sentIndex: 0, midWord: false, pendingTerminator: false, breakPending: false };
  for (const item of items) {
    const res = tokenizeRun(item.node.data, carry, { lineBreakBefore: item.lineBreakBefore });
    carry = res.carry;
    _wrapTextNode(item.node, res.words);
  }
  bodyEl.dataset.segApplied = "1";
}

// `word` and `sentenceText` are lazy getters: they coalesce the fragments of a
// split word / the spans of a sentence only when read, so the per-word
// affordance pass (one `segDescriptor` per `.seg` span) pays nothing unless a
// handler actually inspects text.
export function segDescriptor(spanEl, extra) {
  const wordIndex = Number(spanEl.dataset.seg);
  const sentIndex = Number(spanEl.dataset.sent);
  const body = spanEl.closest(".msg-body");
  let word;
  let sentence;
  const d = {
    wordIndex,
    sentIndex,
    get word() {
      if (word === undefined) {
        word = body
          ? Array.from(body.querySelectorAll(`.seg[data-seg="${wordIndex}"]`))
              .map((s) => s.textContent)
              .join("")
          : spanEl.textContent;
      }
      return word;
    },
    get sentenceText() {
      if (sentence === undefined) {
        sentence = body
          ? Array.from(body.querySelectorAll(`.seg[data-sent="${sentIndex}"]`))
              .map((s) => s.textContent)
              .join("")
          : spanEl.textContent;
      }
      return sentence;
    },
  };
  if (extra) Object.assign(d, extra);
  return d;
}

// One entry per visual word, with split-word fragments coalesced. A word-level
// effect aligns its timeline to these indices rather than re-tokenizing the
// message text, which would diverge from the DOM numbering on any message
// containing code (the `<pre>`/`<code>` subtrees `segmentBody` skips).
export function messageSegments(msgId) {
  const bodyEl = document.querySelector(`#chat-messages .message[data-msg-id="${msgId}"] .msg-body`);
  if (!bodyEl) return [];
  const out = [];
  let last = null;
  for (const span of bodyEl.querySelectorAll(".seg")) {
    const wordIndex = Number(span.dataset.seg);
    if (last && last.wordIndex === wordIndex) {
      last.word += span.textContent;
      continue;
    }
    last = { wordIndex, sentIndex: Number(span.dataset.sent), word: span.textContent };
    out.push(last);
  }
  return out;
}
