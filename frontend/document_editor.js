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
          if (!child.hasAttribute("data-filler")) content += "\n";
        } else if (child.classList?.contains("gen-text")) {
          const start = content.length;
          content += child.textContent;
          spans.push({ start, end: content.length });
        } else if (tag === "DIV" || tag === "P") {
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
      out.push({ start: lastEnd, end: s.end });
      lastEnd = s.end;
    }
  }
  return out;
}

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
  ensureTrailingFiller(pageEl);
  return anchorEl;
}

export function ensureTrailingFiller(pageEl) {
  let filler = null;
  let last = null;
  for (const c of pageEl.childNodes) {
    if (c.nodeType === Node.ELEMENT_NODE && c.tagName === "BR" && c.hasAttribute("data-filler")) filler = c;
    else if (c.nodeType !== Node.TEXT_NODE || c.data !== "") last = c;
  }
  const endsNL =
    (last?.nodeType === Node.TEXT_NODE && last.data.endsWith("\n")) ||
    (last?.nodeType === Node.ELEMENT_NODE && last.tagName === "BR");
  if (endsNL) {
    if (!filler) {
      filler = document.createElement("br");
      filler.setAttribute("data-filler", "");
    }
    if (pageEl.lastChild !== filler) pageEl.appendChild(filler);
  } else if (filler) {
    filler.remove();
  }
}

export function offsetOfPosition(pageEl, container, offset) {
  if (!container || !pageEl.contains(container)) return serializeEditor(pageEl).content.length;
  const pre = document.createRange();
  pre.selectNodeContents(pageEl);
  pre.setEnd(container, offset);
  const tmp = document.createElement("div");
  tmp.appendChild(pre.cloneContents());
  return serializeEditor(tmp).content.length;
}

export function computeCaretOffset(pageEl) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return serializeEditor(pageEl).content.length;
  const range = sel.getRangeAt(0);
  return offsetOfPosition(pageEl, range.startContainer, range.startOffset);
}

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
  if (!startSet) range.selectNodeContents(pageEl);
  if (lastNode) range.setEndAfter(lastNode);
  else range.selectNodeContents(pageEl);
  return range;
}

function caretInGenText(pageEl) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return false;
  const n = sel.getRangeAt(0).startContainer;
  if (!pageEl.contains(n)) return false;
  const el = n.nodeType === Node.TEXT_NODE ? n.parentElement : n;
  return !!el?.closest?.(".gen-text");
}

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
    const T = node.data;
    const right = T.slice(offset);
    node.data = T.slice(0, offset);
    const before = span.nextSibling;
    span.parentNode.insertBefore(plain, before);
    if (right) {
      const rspan = document.createElement("span");
      rspan.className = "gen-text";
      rspan.textContent = right;
      span.parentNode.insertBefore(rspan, before);
    }
    if (!node.data) span.remove();
  } else {
    range.insertNode(plain);
    if (plain.nextSibling?.nodeType === Node.TEXT_NODE && plain.nextSibling.data === "") plain.nextSibling.remove();
  }

  ensureTrailingFiller(pageEl);
  const next = plain.nextSibling;
  const r = document.createRange();
  r.setStartAfter(next?.nodeType === Node.ELEMENT_NODE && next.hasAttribute?.("data-filler") ? next : plain);
  r.collapse(true);
  sel.removeAllRanges();
  sel.addRange(r);
  pageEl.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
}

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
      e.preventDefault();
      insertPlainText(pageEl, e.data);
    }
  });
  pageEl.addEventListener("compositionend", (e) => {
    const data = e.data;
    if (!data || !caretInGenText(pageEl)) return;
    const sel = window.getSelection();
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    const end = range.startOffset;
    const start = end - data.length;
    if (node.nodeType !== Node.TEXT_NODE || start < 0 || node.data.slice(start, end) !== data) return;
    range.setStart(node, start);
    range.deleteContents();
    sel.removeAllRanges();
    sel.addRange(range);
    insertPlainText(pageEl, data);
  });
}

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

export function caretAfter(node) {
  const sel = window.getSelection();
  if (!sel || !node) return;
  const range = document.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
}
