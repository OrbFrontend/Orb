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

// The serialized string offset of the collapsed selection within *pageEl*. A
// selection outside the editor (e.g. focus on a button) resolves to end-of-doc.
// Measures by serializing a fragment cloned from doc-start to the caret, so it
// stays consistent with serializeEditor (spans + newlines counted identically).
export function computeCaretOffset(pageEl) {
  const full = serializeEditor(pageEl).content;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return full.length;
  const range = sel.getRangeAt(0);
  if (!pageEl.contains(range.startContainer)) return full.length;
  const pre = document.createRange();
  pre.selectNodeContents(pageEl);
  pre.setEnd(range.startContainer, range.startOffset);
  const tmp = document.createElement("div");
  tmp.appendChild(pre.cloneContents());
  return serializeEditor(tmp).content.length;
}

// Enforce plain text in a contenteditable: paste lands as text (preserving the
// native undo stack), Enter becomes a literal "\n", and rich transforms / drops
// are blocked. The serializer tolerates anything that slips through, so this is
// belt-and-suspenders, not the only guard.
export function installPlainTextGuards(pageEl) {
  pageEl.addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData)?.getData("text/plain") ?? "";
    document.execCommand("insertText", false, text);
  });
  pageEl.addEventListener("beforeinput", (e) => {
    const t = e.inputType || "";
    if (t === "insertParagraph" || t === "insertLineBreak") {
      e.preventDefault();
      document.execCommand("insertText", false, "\n");
    } else if (t === "insertFromDrop" || t.startsWith("format")) {
      e.preventDefault();
    }
  });
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
