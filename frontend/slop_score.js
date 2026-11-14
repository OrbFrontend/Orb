import { registerAction } from "./actions.js";
import { api } from "./api.js";
import { BLOCK_TAGS, NON_PROSE_TAGS } from "./message_html.js";
import { messageBody, toast } from "./utils.js";

const HIGHLIGHT = "slop-flag";
const _ranges = new WeakMap(); // body -> its painted ranges

function blockOf(node, body) {
  for (let el = node.parentElement; el && el !== body; el = el.parentElement) {
    if (BLOCK_TAGS.has(el.tagName.toUpperCase())) return el;
  }
  return body;
}

function cleanText(text) {
  return text
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}

// Paragraphs as the renderer draws them: a blank line becomes `.pbreak`, and each block element holds its own.
function paragraphs(body) {
  const out = [];
  let cur = null;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    acceptNode(node) {
      if (node.nodeType !== Node.ELEMENT_NODE) return NodeFilter.FILTER_ACCEPT;
      if (NON_PROSE_TAGS.has(node.tagName.toUpperCase()) || node.classList.contains("slop-badge")) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.tagName === "BR") {
        if (cur) cur.text += "\n";
      } else if (node.classList.contains("pbreak") || BLOCK_TAGS.has(node.tagName.toUpperCase())) cur = null;
      continue;
    }
    const block = blockOf(node, body);
    if (cur && cur.block !== block) cur = null;
    if (!cur) {
      if (!node.data.trim()) continue;
      cur = { block, text: "", nodes: [] };
      out.push(cur);
    }
    cur.text += node.data;
    cur.nodes.push(node);
  }
  return out.map((p) => ({ text: cleanText(p.text), nodes: p.nodes })).filter((p) => p.text);
}

function clearSlop(body) {
  const highlight = window.CSS?.highlights?.get(HIGHLIGHT);
  for (const range of _ranges.get(body) ?? []) highlight?.delete(range);
  _ranges.delete(body);
  for (const b of body.querySelectorAll(".slop-badge")) b.remove();
  delete body.dataset.slopScored;
  body.closest(".message")?.querySelector(".slop-chip")?.remove();
}

function paint(body, paras, { score, threshold, chunks }) {
  let highlight = window.CSS?.highlights?.get(HIGHLIGHT);
  if (!highlight && window.Highlight) {
    highlight = new Highlight();
    window.CSS.highlights.set(HIGHLIGHT, highlight);
  }
  const ranges = [];
  let flagged = 0;
  for (const chunk of chunks) {
    if (chunk.score < threshold) continue;
    flagged++;
    const nodes = chunk.paragraphs.flatMap((i) => paras[i]?.nodes ?? []);
    if (!nodes.length) continue;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const range = document.createRange();
    range.setStart(first, first.data.length - first.data.trimStart().length);
    range.setEnd(last, last.data.trimEnd().length);
    ranges.push(range);
    highlight?.add(range);
    const badge = document.createElement("sup");
    badge.className = "slop-badge";
    badge.textContent = `${Math.round(chunk.score * 100)}%`;
    last.after(badge);
  }
  _ranges.set(body, ranges);
  body.dataset.slopScored = "1";

  const chip = document.createElement("span");
  chip.className = "slop-chip";
  chip.textContent = `Slop ${Math.round(score * 100)}% · ${flagged}/${chunks.length} flagged`;
  const msg = body.closest(".message");
  const toolbar = msg?.querySelector(".msg-toolbar");
  if (toolbar) toolbar.after(chip);
  else msg?.appendChild(chip);
}

async function scoreSlop(msgId, btn) {
  const body = messageBody(msgId);
  if (!body) return;
  if (body.dataset.slopScored === "1") {
    clearSlop(body);
    return;
  }

  const paras = paragraphs(body);
  if (!paras.length) {
    toast("Nothing to score");
    return;
  }

  if (btn) btn.disabled = true;
  try {
    const grade = await api.post("/local-ml/slop-score", { paragraphs: paras.map((p) => p.text) });
    clearSlop(body); // in case a stale paint lingers
    if (grade.score != null) paint(body, paras, grade);
  } catch (e) {
    if (e.status === 503) toast("Enable & download the AI-Slop Classifier in Settings → Local ML");
    else {
      console.error("slop-score failed", e);
      toast("Slop scoring failed", true);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

registerAction("slop", "score", (el) => scoreSlop(Number(el.dataset.msgId), el));
