// Per-token alternatives side-store for Document mode (mikupad-style token
// swapping). Session-only: token data lives here in a JS Map keyed by docId, NOT
// in the document schema — MB-scale logprob payloads must not ride the 1.5s
// autosave. S-free by construction (no imports from state.js); the popup half
// gets its context injected at init to avoid an import cycle with document.js.
//
// A "run" is one contiguous stretch of generated text with per-token records:
//   { start, end, tokens: [{ text, prob, top: [{ t, p }] }] }
// where [start, end) are UTF-16 offsets into the document's current content
// (kept aligned by remapRuns on every edit) and concatenating tokens' text
// exactly tiles content.slice(start, end). One generation can commit several
// runs: servers may send probs for only some tokens (llama.cpp speculative
// decoding skips draft-accepted ones), and each covered stretch is a run.
//
// document_editor.js is a pure leaf (it never imports this module), so importing
// its offset helpers here introduces no cycle — unlike document.js, whose context
// is injected at init instead.

import { offsetOfPosition, rangeForOffsets, serializeEditor } from "./document_editor.js";

// ── Pure helpers (plain data in/out; unit-testable, no DOM, no store) ─────────

// Longest common prefix length of two strings.
function _commonPrefix(a, b) {
  const m = Math.min(a.length, b.length);
  let i = 0;
  while (i < m && a[i] === b[i]) i++;
  return i;
}

// Longest common suffix length, capped so it never overlaps the given prefix.
function _commonSuffix(a, b, prefix) {
  const max = Math.min(a.length, b.length) - prefix;
  let i = 0;
  while (i < max && a[a.length - 1 - i] === b[b.length - 1 - i]) i++;
  return i;
}

// Remap runs from *oldContent* coordinates to *newContent* after an edit,
// localizing the change to a single prefix/suffix diff window (mikupad parity):
// runs entirely before the window keep, entirely after shift by the length
// delta, and any run overlapping the edited window is dropped (its token data is
// no longer trustworthy). Never mutates the inputs.
export function remapRuns(runs, oldContent, newContent) {
  if (oldContent === newContent) return runs.map((r) => ({ ...r }));
  const prefix = _commonPrefix(oldContent, newContent);
  const suffix = _commonSuffix(oldContent, newContent, prefix);
  const editStart = prefix; // first divergent index
  const oldEditEnd = oldContent.length - suffix; // one past last divergent (old)
  const delta = newContent.length - oldContent.length;
  const out = [];
  for (const run of runs) {
    if (run.end <= editStart) {
      out.push({ ...run }); // wholly before the edit
    } else if (run.start >= oldEditEnd) {
      out.push({ ...run, start: run.start + delta, end: run.end + delta }); // wholly after
    }
    // else: straddles the edit window → drop (touched tokens lose their data)
  }
  return out;
}

// Group position-carrying token records ({ text, start, … }) into contiguous
// segments that exactly tile *text* — probs coverage can have gaps (llama.cpp
// speculative decoding omits probs for draft-accepted tokens), so one generation
// may yield several covered stretches, each committed as its own run. A token
// whose text no longer matches *text* at its recorded start (chat-mode deltas
// that don't tile 1:1, abort truncation) is dropped and splits the segment.
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

// Which token of *run* covers absolute offset *offset*, by cumulative lengths.
// Returns { index, tokStart, tokEnd } (half-open [tokStart, tokEnd)) or null when
// the offset is at/after the run's last token boundary (a hover on the seam).
export function tokenAtOffset(run, offset) {
  let pos = run.start;
  for (let i = 0; i < run.tokens.length; i++) {
    const tokEnd = pos + run.tokens[i].text.length;
    if (offset < tokEnd) return { index: i, tokStart: pos, tokEnd };
    pos = tokEnd;
  }
  return null;
}

// Render whitespace-only tokens legibly in the popup: space→␣, tab→⇥, newline→↵.
export function visualizeWhitespace(text) {
  return text.replace(/ /g, "␣").replace(/\t/g, "⇥").replace(/\n/g, "↵");
}

// ── Session store ─────────────────────────────────────────────────────────────

const _store = new Map(); // docId -> { lastContent: string|null, runs: Run[] }
let _pending = null; // { docId, start, tokens } during an active generation
const RUNS_CAP = 50; // most-recent runs kept per doc

function _entry(docId) {
  let e = _store.get(docId);
  if (!e) {
    e = { lastContent: null, runs: [] };
    _store.set(docId, e);
  }
  return e;
}

// Start collecting the tokens of a new run at *startOffset* (the generation caret).
export function beginRun(docId, startOffset) {
  _pending = { docId, start: startOffset, tokens: [], text: "", chunkPos: 0 };
}

// Record one streamed content delta. The route emits a chunk's content before its
// probs records, so the delta's start is where that chunk's tokens begin — this
// is what positions probs records even when earlier chunks carried none.
export function addDelta(delta) {
  if (!_pending || typeof delta !== "string") return;
  _pending.chunkPos = _pending.text.length;
  _pending.text += delta;
}

// Append one streamed token record ({token, prob, top}) to the pending run,
// anchored at the current chunk position. A record that doesn't match the
// streamed text there (reasoning tokens, provider drift) is dropped.
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

// Discard the pending run (nothing generated / aborted before any token).
export function clearPending() {
  _pending = null;
}

// Finalize the pending tokens against the text that actually landed in the
// editor: each contiguous covered stretch becomes its own run (probs coverage
// can be gappy — see segmentTokens); zero surviving tokens → nothing committed.
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
    // Fresh runs shouldn't overlap existing ones (syncContent shifted them
    // first), but drop any overlap defensively.
    e.runs = e.runs.filter((r) => r.end <= run.start || r.start >= run.end);
    e.runs.push(run);
  }
  e.runs.sort((a, b) => a.start - b.start);
  if (e.runs.length > RUNS_CAP) e.runs = e.runs.slice(e.runs.length - RUNS_CAP);
}

// Realign a doc's runs to *content* after it changed (edit, generation, undo).
// Must be called with the NEW content before adding a run for that same change,
// so a freshly committed run isn't shifted by its own edit's remap.
export function syncContent(docId, content) {
  const e = _entry(docId);
  if (e.lastContent != null && e.lastContent !== content && e.runs.length) {
    e.runs = remapRuns(e.runs, e.lastContent, content);
  }
  e.lastContent = content;
}

// Apply a token swap to the store (the mutation half of docSwapToken): replace
// run.tokens[index] with the chosen alternative — keeping the token's ORIGINAL
// top-N list so it can be swapped again — and discard everything after it: the
// run's own tail tokens AND all later runs, since the document is truncated at
// the swap point (mikupad semantics). Sets lastContent to *newContent* so the
// runs are already aligned and a following syncContent won't re-remap them.
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

// The run covering *offset*, or null. Validate-on-use: the run's tokens must
// still exactly tile the current content slice; a run that fails (content edited
// without a clean remap) is dropped and null returned, so stale data never shows.
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

// ── Popup: hover a generated token → alternatives, click to swap ──────────────
//
// No per-token DOM nodes — the content model stays text-nodes-and-spans. We
// hit-test the pointer to a caret position, resolve it to a token via the store,
// and float a fixed popup over the token measured with rangeForOffsets. Context
// (getDocId / isStreaming / requestSwap) is injected so this stays S-free.

const HOVER_DELAY = 300; // debounce; logprobs steering is deliberate, not twitchy
const HIDE_GRACE = 120; // let the pointer travel from token to popup without closing

let _ctx = null;
let _page = null;
let _popup = null;
let _hoverTimer = null;
let _hideTimer = null;
let _shownFor = null; // { run, index } — dedupe re-renders of the same token

// Wire the popup once (from initDocumentMode). *ctx* = { getDocId, isStreaming,
// requestSwap }. Silently a no-op when the popup element is absent or neither
// caret-from-point API exists (feature degrades to nothing).
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
    clearTimeout(_hideTimer); // back over generated text → cancel a pending hide
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

// Public: hide + reset (called on generation start and doc switch by document.js).
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

// Pointer → caret DOM position, across the Firefox/Chromium API split. Returns
// { node, offset } inside the editor, or null (also when neither API is present).
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
    clearTimeout(_hideTimer); // same token → keep it up, no rebuild
    return;
  }
  _render(run, at);
}

// Alternatives sorted desc by prob, with the sampled token guaranteed present
// (prepended when it fell outside the returned top-N).
function _sortedAlts(token) {
  const alts = (token.top || []).slice().sort((a, b) => b.p - a.p);
  if (!alts.some((a) => a.t === token.text)) alts.unshift({ t: token.text, p: token.prob });
  return alts;
}

function _render(run, at) {
  const token = run.tokens[at.index];
  _popup.textContent = ""; // token text is arbitrary → build via DOM, never innerHTML
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

// Float the popup above the token (fixed positioning → viewport coords), flipping
// below and clamping to the viewport when there isn't room.
function _position(at) {
  const rect = rangeForOffsets(_page, at.tokStart, at.tokEnd).getBoundingClientRect();
  _popup.style.visibility = "hidden";
  _popup.classList.remove("hidden"); // measure with layout applied
  const pw = _popup.offsetWidth;
  const ph = _popup.offsetHeight;
  let top = rect.top - ph - 6;
  if (top < 4) top = rect.bottom + 6; // no room above → below the token
  let left = rect.left;
  left = Math.max(4, Math.min(left, window.innerWidth - pw - 4));
  top = Math.max(4, Math.min(top, window.innerHeight - ph - 4));
  _popup.style.left = `${left}px`;
  _popup.style.top = `${top}px`;
  _popup.style.visibility = "";
}
