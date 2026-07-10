// Pure editor model for Document mode — the invariant-heavy core, isolated so it
// stays reviewable and unit-testable. No S, no fetch, no DOM outside #doc-page's
// own children.
//
// Content model (load-bearing invariant): direct children of #doc-page are only
// text nodes and non-nested <span class="gen-text">; newlines are literal "\n"
// (the page is white-space: pre-wrap). Offsets are JS/UTF-16 string indices.

// Walk *pageEl*'s children into {content, spans}. The single source of truth for
// turning the DOM back into a plain string plus generated-span offsets. Defensive
// against browser quirks (<br>/<div>/<p> wrappers) so odd DOM degrades to
// normalized text on the next save rather than losing data. If *stopNode* is
// given, walking halts as soon as it is encountered (exclusive).
export function serializeEditor(pageEl, stopNode = null) {
  let content = "";
  const spans = [];
  let stopped = false;

  function walk(node) {
    for (const child of node.childNodes) {
      if (stopped) return;
      if (child === stopNode) {
        stopped = true;
        return;
      }
      if (child.nodeType === Node.TEXT_NODE) {
        content += child.data;
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const tag = child.tagName;
        if (tag === "BR") {
          content += "\n";
        } else if (child.classList?.contains("gen-text")) {
          const start = content.length;
          content += child.textContent; // spans are non-nested → textContent is exact
          spans.push({ start, end: content.length });
        } else if (tag === "DIV" || tag === "P") {
          // Browser wrapped some text in a block element: treat as a newline
          // boundary, then take its inner text/spans.
          if (content && !content.endsWith("\n")) content += "\n";
          walk(child);
        } else {
          content += child.textContent;
        }
      }
    }
  }

  walk(pageEl);
  return { content, spans };
}

// Sort/clamp/dedupe spans against a content length, dropping empties and clipping
// overlaps so rendering never nests or double-tints. Clamping is client-side only
// (the same JS string produced the offsets) — the backend never bounds-checks.
function normalizeSpans(spans, n) {
  if (!Array.isArray(spans)) return [];
  const cleaned = spans
    .map((s) => ({ start: Math.max(0, Math.min(n, s.start | 0)), end: Math.max(0, Math.min(n, s.end | 0)) }))
    .filter((s) => s.end > s.start)
    .sort((a, b) => a.start - b.start);
  const out = [];
  let lastEnd = -1;
  for (const s of cleaned) {
    if (s.start >= lastEnd) {
      out.push({ ...s });
      lastEnd = s.end;
    } else if (s.end > lastEnd) {
      out.push({ start: lastEnd, end: s.end }); // clip the overlapping head
      lastEnd = s.end;
    }
  }
  return out;
}

// Rebuild #doc-page's children from *content* + *spans*. Called only on doc open
// and at generation start (never per keystroke → no caret jumps / IME breakage).
// When *anchorOffset* is a number, an empty <span class="gen-text gen-active">
// streaming anchor is inserted at that offset (splitting any span straddling it)
// and returned; otherwise returns null.
export function renderEditor(pageEl, content, spans, anchorOffset = null) {
  pageEl.textContent = "";
  const n = content.length;
  const norm = normalizeSpans(spans, n);
  const anchor = anchorOffset == null ? null : Math.max(0, Math.min(n, anchorOffset));

  const cuts = new Set([0, n]);
  for (const s of norm) {
    cuts.add(s.start);
    cuts.add(s.end);
  }
  if (anchor != null) cuts.add(anchor);
  const points = [...cuts].sort((a, b) => a - b);

  const inSpan = (a) => norm.some((s) => a >= s.start && a < s.end);
  let anchorEl = null;

  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    if (anchor != null && p === anchor && anchorEl === null) {
      anchorEl = document.createElement("span");
      anchorEl.className = "gen-text gen-active";
      anchorEl.appendChild(document.createTextNode(""));
      pageEl.appendChild(anchorEl);
    }
    if (i === points.length - 1) break;
    const text = content.slice(p, points[i + 1]);
    if (!text) continue;
    if (inSpan(p)) {
      const span = document.createElement("span");
      span.className = "gen-text";
      span.textContent = text;
      pageEl.appendChild(span);
    } else {
      pageEl.appendChild(document.createTextNode(text));
    }
  }
  return anchorEl;
}

// The serialized string offset of a DOM position (*container*, *offset*) within
// *pageEl* — a position outside the editor resolves to end-of-doc. Measures by
// serializing a fragment cloned from doc-start to the position, so it stays
// consistent with serializeEditor (spans + newlines counted identically). Shared
// by the caret reader and the popup's caretPositionFromPoint hit-test.
export function offsetOfPosition(pageEl, container, offset) {
  if (!container || !pageEl.contains(container)) return serializeEditor(pageEl).content.length;
  const pre = document.createRange();
  pre.selectNodeContents(pageEl);
  pre.setEnd(container, offset);
  const tmp = document.createElement("div");
  tmp.appendChild(pre.cloneContents());
  return serializeEditor(tmp).content.length;
}

// The serialized string offset of the collapsed selection within *pageEl*. A
// selection outside the editor (e.g. focus on a button) resolves to end-of-doc.
export function computeCaretOffset(pageEl) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return serializeEditor(pageEl).content.length;
  const range = sel.getRangeAt(0);
  return offsetOfPosition(pageEl, range.startContainer, range.startOffset);
}

// A DOM Range spanning serialized offsets [start, end) — the inverse of
// serialize, used to position the alternatives popup over a token. Walks text
// nodes in document order (same pattern as setCaretOffset), so it is exact on the
// DOM renderEditor produces. Offsets past the end clamp to end-of-doc.
export function rangeForOffsets(pageEl, start, end) {
  const range = document.createRange();
  const walker = document.createTreeWalker(pageEl, NodeFilter.SHOW_TEXT);
  let pos = 0;
  let startSet = false;
  let lastNode = null;
  let node = walker.nextNode();
  while (node) {
    const len = node.data.length;
    if (!startSet && start <= pos + len) {
      range.setStart(node, start - pos);
      startSet = true;
    }
    if (startSet && end <= pos + len) {
      range.setEnd(node, end - pos);
      return range;
    }
    pos += len;
    lastNode = node;
    node = walker.nextNode();
  }
  // start and/or end fell past the last text node → clamp to end-of-doc.
  if (!startSet) range.selectNodeContents(pageEl);
  if (lastNode) range.setEndAfter(lastNode);
  else range.selectNodeContents(pageEl);
  return range;
}

// True when the collapsed caret sits inside a .gen-text span, i.e. native typing
// would absorb the keystroke into the highlight (the mikupad-mismatch bug).
function caretInGenText(pageEl) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return false;
  const n = sel.getRangeAt(0).startContainer;
  if (!pageEl.contains(n)) return false;
  const el = n.nodeType === Node.TEXT_NODE ? n.parentElement : n;
  return !!el?.closest?.(".gen-text");
}

// Insert *text* as plain (never-tinted) text at the selection, splitting/escaping
// any enclosing .gen-text span so user text is never highlighted like AI text.
// Manual DOM edits fire no input event, so we dispatch one → autosave/undo pick it
// up (mirrors why the old paste path used execCommand).
export function insertPlainText(pageEl, text) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  if (!pageEl.contains(range.startContainer)) return;
  if (!range.collapsed) range.deleteContents();

  const node = range.startContainer;
  const offset = range.startOffset;
  const plain = document.createTextNode(text);
  const span = node.nodeType === Node.TEXT_NODE ? node.parentElement?.closest(".gen-text") : null;

  if (span?.contains(node)) {
    // Split the (single-text-node) span so `plain` lands between the tinted halves.
    const T = node.data;
    const right = T.slice(offset);
    node.data = T.slice(0, offset); // may become ""
    const before = span.nextSibling;
    span.parentNode.insertBefore(plain, before);
    if (right) {
      const rspan = document.createElement("span");
      rspan.className = "gen-text";
      rspan.textContent = right;
      span.parentNode.insertBefore(rspan, before);
    }
    if (!node.data) span.remove(); // don't leave an empty highlighted span
  } else {
    range.insertNode(plain);
  }

  const r = document.createRange();
  r.setStartAfter(plain);
  r.collapse(true);
  sel.removeAllRanges();
  sel.addRange(r);
  pageEl.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
}

// Enforce plain text in a contenteditable: paste/Enter land as plain text at the
// caret (never inside a gen-text span), and rich transforms / drops are blocked.
// The serializer tolerates anything that slips through, so this is
// belt-and-suspenders, not the only guard.
export function installPlainTextGuards(pageEl) {
  pageEl.addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData)?.getData("text/plain") ?? "";
    insertPlainText(pageEl, text);
  });
  pageEl.addEventListener("beforeinput", (e) => {
    const t = e.inputType || "";
    if (t === "insertParagraph" || t === "insertLineBreak") {
      e.preventDefault();
      insertPlainText(pageEl, "\n");
    } else if (t === "insertFromDrop" || t.startsWith("format")) {
      e.preventDefault();
    } else if (t === "insertText" && e.data != null && caretInGenText(pageEl)) {
      // Typing at the edge of / inside AI text: keep the keystroke un-tinted.
      e.preventDefault();
      insertPlainText(pageEl, e.data);
    }
  });
  // Mobile IMEs commit via composition (insertCompositionText), which beforeinput
  // can't cancel — the chars land tinted inside the gen-text span. Once the
  // composition commits, lift the just-typed run back out as plain text (this is
  // what desktop gets up-front from the beforeinput guard above).
  pageEl.addEventListener("compositionend", (e) => {
    const data = e.data;
    if (!data || !caretInGenText(pageEl)) return;
    const sel = window.getSelection();
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    const end = range.startOffset;
    const start = end - data.length;
    // ponytail: only the common case — the commit is a plain trailing run in one
    // text node. Anything fancier (multi-node, autocorrect replacement) bails.
    if (node.nodeType !== Node.TEXT_NODE || start < 0 || node.data.slice(start, end) !== data) return;
    range.setStart(node, start);
    range.deleteContents(); // drop the tinted copy; range collapses to `start`
    sel.removeAllRanges();
    sel.addRange(range);
    insertPlainText(pageEl, data); // re-insert plain + split span + fire input
  });
}

// Inverse of computeCaretOffset: place a collapsed caret at serialized string
// *offset*. Walks text nodes in document order, so it is exact on the DOM
// renderEditor produces (text nodes + spans, newlines literal); called only
// right after a render. Offsets past the end land at end-of-doc.
export function setCaretOffset(pageEl, offset) {
  const sel = window.getSelection();
  if (!sel) return;
  const range = document.createRange();
  let remaining = Math.max(0, offset);
  const walker = document.createTreeWalker(pageEl, NodeFilter.SHOW_TEXT);
  let placed = false;
  let node = walker.nextNode();
  while (node) {
    if (remaining <= node.data.length) {
      range.setStart(node, remaining);
      placed = true;
      break;
    }
    remaining -= node.data.length;
    node = walker.nextNode();
  }
  if (!placed) {
    range.selectNodeContents(pageEl);
    range.collapse(false);
  } else {
    range.collapse(true);
  }
  sel.removeAllRanges();
  sel.addRange(range);
}

// Place the caret immediately after *node* (used to drop the caret past the
// streaming anchor once generation finalizes).
export function caretAfter(node) {
  const sel = window.getSelection();
  if (!sel || !node) return;
  const range = document.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
}
