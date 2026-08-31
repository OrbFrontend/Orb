import { offsetOfPosition, rangeForOffsets, serializeEditor } from "./document_editor.js";

function _commonPrefix(a, b) {
  const m = Math.min(a.length, b.length);
  let i = 0;
  while (i < m && a[i] === b[i]) i++;
  return i;
}

function _commonSuffix(a, b, prefix) {
  const max = Math.min(a.length, b.length) - prefix;
  let i = 0;
  while (i < max && a[a.length - 1 - i] === b[b.length - 1 - i]) i++;
  return i;
}

export function remapRuns(runs, oldContent, newContent) {
  if (oldContent === newContent) return runs.map((r) => ({ ...r }));
  const prefix = _commonPrefix(oldContent, newContent);
  const suffix = _commonSuffix(oldContent, newContent, prefix);
  const editStart = prefix;
  const oldEditEnd = oldContent.length - suffix;
  const delta = newContent.length - oldContent.length;
  const out = [];
  for (const run of runs) {
    if (run.end <= editStart) {
      out.push({ ...run });
    } else if (run.start >= oldEditEnd) {
      out.push({ ...run, start: run.start + delta, end: run.end + delta });
    }
  }
  return out;
}

export function segmentTokens(tokens, text) {
  const segs = [];
  let cur = null;
  for (const tok of tokens) {
    if (text.slice(tok.start, tok.start + tok.text.length) !== tok.text) {
      cur = null;
      continue;
    }
    if (cur && tok.start === cur.start + cur.len) {
      cur.tokens.push(tok);
      cur.len += tok.text.length;
    } else {
      cur = { start: tok.start, len: tok.text.length, tokens: [tok] };
      segs.push(cur);
    }
  }
  return segs;
}

export function tokenAtOffset(run, offset) {
  let pos = run.start;
  for (let i = 0; i < run.tokens.length; i++) {
    const tokEnd = pos + run.tokens[i].text.length;
    if (offset < tokEnd) return { index: i, tokStart: pos, tokEnd };
    pos = tokEnd;
  }
  return null;
}

export function visualizeWhitespace(text) {
  return text.replace(/ /g, "␣").replace(/\t/g, "⇥").replace(/\n/g, "↵");
}

const _store = new Map(); // document id -> token runs
let _pending = null; // active generation
const RUNS_CAP = 50; // token runs kept per document

function _entry(docId) {
  let e = _store.get(docId);
  if (!e) {
    e = { lastContent: null, runs: [] };
    _store.set(docId, e);
  }
  return e;
}

export function beginRun(docId, startOffset) {
  _pending = { docId, start: startOffset, tokens: [], text: "", chunkPos: 0 };
}

export function addDelta(delta) {
  if (!_pending || typeof delta !== "string") return;
  _pending.chunkPos = _pending.text.length;
  _pending.text += delta;
}

export function addToken(rec) {
  if (!_pending || !rec || typeof rec.token !== "string") return;
  const start = _pending.chunkPos;
  if (_pending.text.slice(start, start + rec.token.length) !== rec.token) return;
  _pending.tokens.push({
    text: rec.token,
    prob: typeof rec.prob === "number" ? rec.prob : 0,
    top: Array.isArray(rec.top) ? rec.top : [],
    start,
  });
  _pending.chunkPos = start + rec.token.length;
}

export function clearPending() {
  _pending = null;
}

export function commitRun(docId, finalText) {
  const pending = _pending;
  _pending = null;
  if (!pending || pending.docId !== docId) return;
  const segs = segmentTokens(pending.tokens, finalText);
  if (!segs.length) return;
  const e = _entry(docId);
  for (const seg of segs) {
    const run = {
      start: pending.start + seg.start,
      end: pending.start + seg.start + seg.len,
      tokens: seg.tokens.map(({ text, prob, top }) => ({ text, prob, top })),
    };
    e.runs = e.runs.filter((r) => r.end <= run.start || r.start >= run.end);
    e.runs.push(run);
  }
  e.runs.sort((a, b) => a.start - b.start);
  if (e.runs.length > RUNS_CAP) e.runs = e.runs.slice(e.runs.length - RUNS_CAP);
}

export function syncContent(docId, content) {
  const e = _entry(docId);
  if (e.lastContent != null && e.lastContent !== content && e.runs.length) {
    e.runs = remapRuns(e.runs, e.lastContent, content);
  }
  e.lastContent = content;
}

export function swapRunToken(docId, run, index, alt, newContent) {
  const e = _store.get(docId);
  if (!e?.runs.includes(run) || index < 0 || index >= run.tokens.length) return;
  let tokStart = run.start;
  for (let i = 0; i < index; i++) tokStart += run.tokens[i].text.length;
  const origTop = run.tokens[index].top;
  run.tokens = [...run.tokens.slice(0, index), { text: alt.t, prob: alt.p, top: origTop }];
  run.end = tokStart + alt.t.length;
  e.runs = e.runs.filter((r) => r === run || r.end <= run.start);
  e.lastContent = newContent;
}

export function runAt(docId, offset, content) {
  const e = _store.get(docId);
  if (!e) return null;
  for (const run of e.runs) {
    if (offset >= run.start && offset < run.end) {
      let concat = "";
      for (const t of run.tokens) concat += t.text;
      if (content.slice(run.start, run.end) === concat) return run;
      e.runs = e.runs.filter((r) => r !== run);
      return null;
    }
  }
  return null;
}

const HOVER_DELAY = 300; // hover delay in ms
const HIDE_GRACE = 120; // popup grace period in ms

let _ctx = null;
let _page = null;
let _popup = null;
let _hoverTimer = null;
let _hideTimer = null;
let _shownFor = null; // last shown token

export function initDocProbs(page, ctx) {
  _page = page;
  _ctx = ctx;
  _popup = document.getElementById("doc-prob-popup");
  if (!_popup) return;
  const canHover = window.matchMedia("(hover: hover)");

  page.addEventListener("mousemove", (e) => {
    if (!_ctx || _ctx.isStreaming() || !canHover.matches) return;
    if (!e.target.closest?.(".gen-text")) {
      scheduleHide();
      return;
    }
    clearTimeout(_hideTimer);
    const x = e.clientX;
    const y = e.clientY;
    clearTimeout(_hoverTimer);
    _hoverTimer = setTimeout(() => _tryShow(x, y), HOVER_DELAY);
  });
  page.addEventListener("mouseleave", scheduleHide);
  _popup.addEventListener("mouseenter", () => clearTimeout(_hideTimer));
  _popup.addEventListener("mouseleave", scheduleHide);

  document.getElementById("doc-editor-scroll")?.addEventListener("scroll", hideProbPopup, { passive: true });
  document.addEventListener("mousedown", (e) => {
    if (_popup && !_popup.classList.contains("hidden") && !_popup.contains(e.target)) hideProbPopup();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideProbPopup();
  });
}

export function hideProbPopup() {
  clearTimeout(_hoverTimer);
  clearTimeout(_hideTimer);
  if (_popup) _popup.classList.add("hidden");
  _shownFor = null;
}

function scheduleHide() {
  clearTimeout(_hoverTimer);
  clearTimeout(_hideTimer);
  _hideTimer = setTimeout(hideProbPopup, HIDE_GRACE);
}

function _hitTest(x, y) {
  let node = null;
  let offset = 0;
  if (document.caretPositionFromPoint) {
    const pos = document.caretPositionFromPoint(x, y);
    if (!pos) return null;
    node = pos.offsetNode;
    offset = pos.offset;
  } else if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y);
    if (!r) return null;
    node = r.startContainer;
    offset = r.startOffset;
  } else {
    return null;
  }
  if (!node || !_page.contains(node)) return null;
  return { node, offset };
}

function _tryShow(x, y) {
  if (!_ctx || _ctx.isStreaming()) return;
  const docId = _ctx.getDocId();
  if (!docId) return;
  const hit = _hitTest(x, y);
  if (!hit) {
    hideProbPopup();
    return;
  }
  const content = serializeEditor(_page).content;
  const offset = offsetOfPosition(_page, hit.node, hit.offset);
  const run = runAt(docId, offset, content);
  if (!run) {
    hideProbPopup();
    return;
  }
  const at = tokenAtOffset(run, offset);
  if (!at) {
    hideProbPopup();
    return;
  }
  if (_shownFor && _shownFor.run === run && _shownFor.index === at.index) {
    clearTimeout(_hideTimer);
    return;
  }
  _render(run, at);
}

function _sortedAlts(token) {
  const alts = (token.top || []).slice().sort((a, b) => b.p - a.p);
  if (!alts.some((a) => a.t === token.text)) alts.unshift({ t: token.text, p: token.prob });
  return alts;
}

function _render(run, at) {
  const token = run.tokens[at.index];
  _popup.textContent = "";
  let currentMarked = false;
  for (const alt of _sortedAlts(token)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "prob-alt";
    if (!currentMarked && alt.t === token.text) {
      btn.classList.add("current");
      currentMarked = true;
    }
    const tokSpan = document.createElement("span");
    tokSpan.className = "prob-tok";
    tokSpan.textContent = visualizeWhitespace(alt.t);
    const pctSpan = document.createElement("span");
    pctSpan.className = "prob-pct";
    pctSpan.textContent = `${(alt.p * 100).toFixed(2)}%`;
    btn.append(tokSpan, pctSpan);
    btn.addEventListener("click", () => _ctx.requestSwap?.(run, at.index, alt));
    _popup.appendChild(btn);
  }
  _position(at);
  _shownFor = { run, index: at.index };
}

function _position(at) {
  const rect = rangeForOffsets(_page, at.tokStart, at.tokEnd).getBoundingClientRect();
  _popup.style.visibility = "hidden";
  _popup.classList.remove("hidden");
  const pw = _popup.offsetWidth;
  const ph = _popup.offsetHeight;
  let top = rect.top - ph - 6;
  if (top < 4) top = rect.bottom + 6;
  let left = rect.left;
  left = Math.max(4, Math.min(left, window.innerWidth - pw - 4));
  top = Math.max(4, Math.min(top, window.innerHeight - ph - 4));
  _popup.style.left = `${left}px`;
  _popup.style.top = `${top}px`;
  _popup.style.visibility = "";
}
