import {
  isHardLineBreak,
  isSentenceWhitespace,
  splitTightSentenceChunks,
  tokenEndsSentence,
} from "./text_segmentation.js";

// Word and sentence indices are shared by text effects and click handlers.

function _isWs(c) {
  return isSentenceWhitespace(c) || isHardLineBreak(c);
}

export function tokenizeRun(text, carry, opts) {
  let { wordIndex, sentIndex, midWord, breakPending } = carry;
  let pendingWord = carry.pendingWord || "";
  let separated = Boolean(carry.separated);
  if (opts?.lineBreakBefore) {
    midWord = false;
    pendingWord = "";
    separated = true;
    breakPending = true;
  }
  const words = [];
  const n = text.length;
  let i = 0;
  while (i < n) {
    const wsStart = i;
    while (i < n && _isWs(text[i])) i++;
    const hadWs = i > wsStart;
    if (hadWs) {
      midWord = false;
      separated = true;
      if ([...text.slice(wsStart, i)].some(isHardLineBreak)) {
        breakPending = true;
        pendingWord = "";
      }
    }
    if (i >= n) break;
    const start = i;
    while (i < n && !_isWs(text[i])) i++;
    const end = i;
    const raw = text.slice(start, end);
    const chunks = splitTightSentenceChunks(raw);
    let offset = start;
    for (let chunkIndex = 0; chunkIndex < chunks.length; chunkIndex++) {
      const chunk = chunks[chunkIndex];
      const chunkStart = offset;
      const chunkEnd = chunkStart + chunk.length;
      const continues = midWord && chunkIndex === 0 && !hadWs;
      if (continues) {
        pendingWord += chunk;
      } else {
        const endsPrevious = separated && pendingWord && tokenEndsSentence(pendingWord, chunk);
        if ((breakPending || endsPrevious) && wordIndex >= 0) sentIndex += 1;
        breakPending = false;
        separated = false;
        wordIndex += 1;
        pendingWord = chunk;
      }
      words.push({ start: chunkStart, end: chunkEnd, wordIndex, sentIndex });
      offset = chunkEnd;
      if (chunkIndex + 1 < chunks.length) {
        midWord = false;
        pendingWord = "";
        separated = true;
        breakPending = true;
      } else {
        midWord = end === n;
      }
    }
  }
  return { words, carry: { wordIndex, sentIndex, midWord, pendingWord, separated, breakPending } };
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
  let carry = {
    wordIndex: -1,
    sentIndex: 0,
    midWord: false,
    pendingWord: "",
    separated: false,
    breakPending: false,
  };
  for (const item of items) {
    const res = tokenizeRun(item.node.data, carry, { lineBreakBefore: item.lineBreakBefore });
    carry = res.carry;
    _wrapTextNode(item.node, res.words);
  }
  bodyEl.dataset.segApplied = "1";
}

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
