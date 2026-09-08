// Message-body HTML: the browser half of prose rendering.
//
// `formatProse` (utils.js) is a pure string pass so it can be tested on plain
// Node, and it deliberately emits markup the model wrote. Nothing it returns may
// reach `innerHTML` directly — it has to come through `renderMessageHtml` first,
// which is where DOMPurify, block layout and `<style>` scoping live. Those three
// need a real `window`, which is exactly why they are not in utils.js.
//
// Pipeline: escape unknown tags -> formatProse -> sanitise -> block layout ->
// rescope `<custom-style>` -> serialise.

import { formatProse, formatProseWithDiff } from "./utils.js";
import DOMPurify from "./vendor/purify.js";

// ── Class vocabulary ────────────────────────────────────────────────────────
// Every class `formatProse` emits. Anything else in a class attribute is model
// output and gets `custom-` prefixed, so card markup can never borrow an app
// class. tests/frontend/message_html_classes.test.mjs keeps this in sync with
// utils.js. Classes this module adds after sanitising — `pbreak`,
// `msg-table-scroll`, `msg-image-broken` — stay out: they never meet the hook,
// so listing them would only hand model markup a class it should not have.
export const ORB_CLASSES = new Set([
  "quoted",
  "code-block",
  "code-block-bar",
  "code-block-btn",
  "inline-code",
  "md-h1",
  "md-h2",
  "md-h3",
  "md-h4",
  "md-h5",
  "md-h6",
  "msg-image-embed",
  "msg-image-label",
  "msg-image-link",
  "msg-image",
  "reasoning-summary-arrow",
  "diff-deleted",
  "diff-change",
  // formatProse interpolates icons.js's SVGs into the image embed's summary.
  "ui-icon",
]);

/** Class-name prefixes `formatProse` builds at runtime (```lang fences). */
export const ORB_CLASS_PREFIXES = ["language-"];

// ── Block layout ────────────────────────────────────────────────────────────
// Tags that already start a new line, so a newline touching one is redundant.
// A wrong guess here costs a stray line break, never deleted content, which is
// why a static set beats asking getComputedStyle per node.
export const BLOCK_TAGS = new Set([
  "ADDRESS",
  "ARTICLE",
  "ASIDE",
  "BLOCKQUOTE",
  "CAPTION",
  "CENTER",
  "COLGROUP",
  "DD",
  "DETAILS",
  "DIALOG",
  "DIV",
  "DL",
  "DT",
  "FIELDSET",
  "FIGCAPTION",
  "FIGURE",
  "FOOTER",
  "FORM",
  "H1",
  "H2",
  "H3",
  "H4",
  "H5",
  "H6",
  "HEADER",
  "HGROUP",
  "HR",
  "LI",
  "MAIN",
  "MENU",
  "NAV",
  "OL",
  "P",
  "PRE",
  "SECTION",
  "SUMMARY",
  "TABLE",
  "TBODY",
  "TD",
  "TFOOT",
  "TH",
  "THEAD",
  "TR",
  "UL",
]);

// Elements whose text is data, not prose: a `<br>` in here corrupts it.
const NO_BREAK_TAGS = new Set(["PRE", "CODE", "STYLE", "SCRIPT", "TEXTAREA", "TITLE", "SVG"]);

// ── Unknown-tag escaping ────────────────────────────────────────────────────
// Regions the escaper must not touch: fenced code (formatProse escapes it
// itself, so escaping here would double-escape), and `<style>`/`<svg>` bodies,
// whose contents are CSS and SVG rather than prose.
const PASSTHROUGH_RE = /```[\s\S]*?```|<style\b[^>]*>[\s\S]*?<\/style\s*>|<svg\b[^>]*>[\s\S]*?<\/svg\s*>/gi;
const TAG_START_RE = /^<\/?([a-zA-Z][a-zA-Z0-9-]*)/;

/**
 * Escape every `<` that does not open a tag the browser actually knows.
 *
 * DOMPurify keeps the contents of tags it drops, so without this `*<gasp>*`
 * renders as `**` and `<she trails off>` disappears entirely — ordinary
 * roleplay prose, silently deleted. `isKnownTag` is injected so the Node suite
 * can drive it without a DOM.
 */
export function escapeUnknownTags(text, isKnownTag) {
  if (!text) return text || "";
  let out = "";
  let cursor = 0;
  PASSTHROUGH_RE.lastIndex = 0;
  for (let region = PASSTHROUGH_RE.exec(text); region; region = PASSTHROUGH_RE.exec(text)) {
    out += _escapeSpan(text.slice(cursor, region.index), isKnownTag) + region[0];
    cursor = region.index + region[0].length;
  }
  return out + _escapeSpan(text.slice(cursor), isKnownTag);
}

function _escapeSpan(span, isKnownTag) {
  let out = "";
  let cursor = 0;
  for (let at = span.indexOf("<"); at !== -1; at = span.indexOf("<", cursor)) {
    out += span.slice(cursor, at);
    cursor = at + 1;
    const rest = span.slice(at);
    const name = TAG_START_RE.exec(rest)?.[1];
    // A tag needs a name the platform knows and a `>` to close it; `a <b else`
    // has neither, and swallowing the rest of the message is not an option.
    if (name && isKnownTag(name) && rest.indexOf(">") !== -1) {
      const end = at + rest.indexOf(">") + 1;
      out += span.slice(at, end);
      cursor = end;
    } else {
      out += "&lt;";
    }
  }
  return out + span.slice(cursor);
}

const _knownTags = new Map();

function isKnownTag(name) {
  let known = _knownTags.get(name);
  if (known === undefined) {
    try {
      known = !(document.createElement(name) instanceof HTMLUnknownElement);
    } catch {
      known = false;
    }
    _knownTags.set(name, known);
  }
  return known;
}

// ── Streaming ───────────────────────────────────────────────────────────────

/**
 * Drop the trailing fragment of a tag that has not finished arriving, so a
 * half-written `<div class="ca` does not flicker and an unterminated `<style>`
 * does not swallow the rest of the bubble. Requires a tag-shaped start: a bare
 * `lastIndexOf("<")` would hide everything after a literal `a < b`.
 */
export function trimIncompleteMarkup(text) {
  if (!text) return text || "";
  const tail = /<\/?[a-zA-Z][^>]*$/.exec(text);
  let out = tail ? text.slice(0, tail.index) : text;
  const lower = out.toLowerCase();
  const open = lower.lastIndexOf("<style");
  if (open !== -1 && open > lower.lastIndexOf("</style>")) out = out.slice(0, open);
  return out;
}

// ── Sanitiser ───────────────────────────────────────────────────────────────
// The defaults are load-bearing and deliberately kept: passing ALLOWED_TAGS
// *replaces* DEFAULT_ALLOWED_TAGS rather than intersecting with it, which would
// drop SVG and MathML and leave us hand-maintaining ~250 tag names. Narrowing
// is expressed through FORBID_TAGS, which is checked ahead of the allowlist.
const SANITIZE_CONFIG = {
  ADD_TAGS: ["custom-style"],
  FORBID_TAGS: ["form", "input", "select", "textarea"],
  RETURN_DOM_FRAGMENT: true,
};

let _hooksInstalled = false;

function installHooks() {
  if (_hooksInstalled) return;
  _hooksInstalled = true;

  // Card links leave the app, so they leave it in a new tab. `target` is not in
  // DOMPurify's attribute allowlist, so this both restores Orb's own image-embed
  // link and forces the behaviour on model-written anchors.
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if ("target" in node) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener");
    }
  });

  // The load-bearing defence: a class the model wrote can never be a class Orb
  // styles or selects on. Card CSS gets the same rewrite (scopeSelector), so
  // `.foo` still matches `class="foo"` — just as `.custom-foo`.
  DOMPurify.addHook("uponSanitizeAttribute", (_node, data) => {
    if (data.attrName !== "class" || !data.attrValue) return;
    data.attrValue = data.attrValue.split(/\s+/).filter(Boolean).map(scopeClassAttr).join(" ");
  });

  // `<custom-style>` carries percent-encoded CSS across the sanitise boundary
  // and has to arrive byte-for-byte; nothing may rewrite it on the way through.
  DOMPurify.addHook("uponSanitizeElement", (_node, data) => {
    if (data.tagName === "custom-style") data.allowedTags["custom-style"] = true;
  });
}

/** Rewrite one class token in a class *attribute*, sparing Orb's own vocabulary. */
function scopeClassAttr(token) {
  if (ORB_CLASSES.has(token)) return token;
  if (ORB_CLASS_PREFIXES.some((prefix) => token.startsWith(prefix))) return token;
  return scopeClassName(token);
}

// Selectors get no such exemption. The markup hook has to spare `quoted` and
// `code-block` because formatProse emits them, but nothing makes card CSS
// naming `.quoted` legitimate — and unrewritten it would restyle Orb's own
// prose chrome across every message in the conversation.
function scopeClassName(token) {
  return token.startsWith("custom-") ? token : `custom-${token}`;
}

// ── `<style>` rescoping ─────────────────────────────────────────────────────

/** Split a selector list on its top-level commas, ignoring `:is(a, b)` and strings. */
function splitSelectorList(selector) {
  const parts = [];
  let depth = 0;
  let quote = null;
  let current = "";
  for (const ch of selector) {
    if (quote) {
      current += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === "(" || ch === "[") depth++;
    else if (ch === ")" || ch === "]") depth--;
    else if (ch === "," && depth === 0) {
      parts.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  parts.push(current);
  return parts;
}

const SELECTOR_CLASS_RE = /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\.(-?[_a-zA-Z][\w-]*)/g;

/** Prefix a selector with `.msg-body` and `custom-`-scope every class it names. */
function scopeSelector(selector) {
  return splitSelectorList(selector)
    .map((part) => {
      const scoped = part
        .trim()
        .replace(SELECTOR_CLASS_RE, (match, name) => (name === undefined ? match : `.${scopeClassName(name)}`));
      return scoped ? `.msg-body ${scoped}` : "";
    })
    .filter(Boolean)
    .join(", ");
}

function scopeRules(owner, rules) {
  for (let i = rules.length - 1; i >= 0; i--) {
    const rule = rules[i];
    // @import would pull in a stylesheet nothing here can rescope (and leak the
    // reader's IP to whoever the card names). replaceSync is specified to drop
    // it, but containment should not rest on the engine remembering to.
    if (/^@import\b/i.test(rule.cssText || "")) {
      owner.deleteRule(i);
      continue;
    }
    if (typeof rule.selectorText === "string") {
      try {
        rule.selectorText = scopeSelector(rule.selectorText);
      } catch {
        // A selector the engine will not take back.
      }
      // Assigning an unparseable selector is a silent no-op, which would leave
      // the card's original, unscoped selector in place. Drop the rule instead.
      if (!/^\.msg-body\b/.test(rule.selectorText)) {
        owner.deleteRule(i);
        continue;
      }
    }
    if (rule.cssRules) scopeRules(rule, rule.cssRules); // @media / @supports / @container
  }
}

/** Reparse card CSS through the engine, rescope it, and serialise it back. */
function scopeCss(cssText) {
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(cssText); // @import is dropped here, per spec
    scopeRules(sheet, sheet.cssRules);
    // The fragment is serialised before it reaches innerHTML, and `</style` is
    // the one token that ends a style element's raw text on the way back in.
    return Array.from(sheet.cssRules)
      .map((rule) => rule.cssText)
      .join("\n")
      .replace(/<\/style/gi, "\\3c /style");
  } catch {
    return "";
  }
}

function applyCustomStyles(root) {
  for (const node of Array.from(root.querySelectorAll("custom-style"))) {
    let css = "";
    try {
      css = scopeCss(decodeURIComponent(node.textContent || ""));
    } catch {
      css = "";
    }
    if (!css) {
      node.remove();
      continue;
    }
    const style = document.createElement("style");
    style.textContent = css;
    node.replaceWith(style);
  }
}

// ── Block layout ────────────────────────────────────────────────────────────

function isBlockBoundary(sibling, parent) {
  if (sibling) return sibling.nodeType === Node.ELEMENT_NODE && BLOCK_TAGS.has(sibling.tagName.toUpperCase());
  // No sibling on that side: the parent's own edge is a break if the parent is
  // a block (or the fragment root, whose children start on their own lines).
  return !parent || parent.nodeType !== Node.ELEMENT_NODE || BLOCK_TAGS.has(parent.tagName.toUpperCase());
}

function inNoBreakContext(node) {
  for (let el = node.parentNode; el && el.nodeType === Node.ELEMENT_NODE; el = el.parentNode) {
    if (NO_BREAK_TAGS.has(el.tagName.toUpperCase())) return true;
  }
  return false;
}

function lineBreakFragment(text) {
  const frag = document.createDocumentFragment();
  const runs = /\n+/g;
  let cursor = 0;
  for (let run = runs.exec(text); run; run = runs.exec(text)) {
    if (run.index > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, run.index)));
    frag.appendChild(document.createElement("br"));
    // A run of two or more newlines is a paragraph gap, not a second break.
    if (run[0].length >= 2) {
      const pbreak = document.createElement("span");
      pbreak.className = "pbreak";
      frag.appendChild(pbreak);
    }
    cursor = run.index + run[0].length;
  }
  if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
  return frag;
}

/**
 * Turn newlines into `<br>` in the DOM rather than by regex, so real markup
 * survives: no `<br>` lands inside a `<style>` or `<pre>`, and a newline that
 * merely separates two block elements does not become a blank line.
 */
function applyBlockLayout(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.data.includes("\n") && !inNoBreakContext(node)) nodes.push(node);
  }
  for (const node of nodes) {
    const parent = node.parentNode;
    if (!parent) continue;
    let data = node.data;
    if (isBlockBoundary(node.previousSibling, parent)) data = data.replace(/^[^\S\n]*\n/, "");
    if (isBlockBoundary(node.nextSibling, parent)) data = data.replace(/\n[^\S\n]*$/, "");
    if (!data.trim() && (isBlockBoundary(node.previousSibling, parent) || isBlockBoundary(node.nextSibling, parent))) {
      node.remove();
      continue;
    }
    if (!data.includes("\n")) {
      node.data = data;
      continue;
    }
    parent.replaceChild(lineBreakFragment(data), node);
  }
}

// ── Render ──────────────────────────────────────────────────────────────────

const _renderCache = new Map();
const _RENDER_CACHE_MAX = 2000;

function serialize(fragment) {
  const holder = document.createElement("div");
  holder.appendChild(fragment);
  return holder.innerHTML;
}

/** Give a table its own scroll box so a wide one cannot widen the bubble. */
function wrapTables(root) {
  for (const table of Array.from(root.querySelectorAll("table"))) {
    if (table.parentElement?.classList.contains("msg-table-scroll")) continue;
    const box = document.createElement("div");
    box.className = "msg-table-scroll";
    table.replaceWith(box);
    box.appendChild(table);
  }
}

function finish(html) {
  installHooks();
  // The sanitiser parses in its own document; take ownership before the DOM
  // passes walk it.
  const fragment = document.adoptNode(DOMPurify.sanitize(html, SANITIZE_CONFIG));
  applyCustomStyles(fragment);
  wrapTables(fragment);
  applyBlockLayout(fragment);
  return serialize(fragment);
}

/**
 * Render a message body: model markup included, sanitised, laid out and style
 * scoped. This is the only safe way to get `formatProse` output onto the page.
 */
export function renderMessageHtml(text, { streaming = false } = {}) {
  if (!text) return "";
  const source = streaming ? trimIncompleteMarkup(text) : text;
  if (!source) return "";
  const cached = _renderCache.get(source);
  if (cached !== undefined) return cached;
  const html = finish(formatProse(escapeUnknownTags(source, isKnownTag)));
  // Every token produces a string seen exactly once, so caching mid-stream buys
  // nothing and evicts the finished messages the cache exists for.
  if (!streaming) {
    if (_renderCache.size >= _RENDER_CACHE_MAX) {
      _renderCache.delete(_renderCache.keys().next().value);
    }
    _renderCache.set(source, html);
  }
  return html;
}

/** The same pipeline over the editor's sentence diff. */
export function renderMessageDiffHtml(ops) {
  const escaped = ops.map((op) => ({ ...op, text: escapeUnknownTags(op.text, isKnownTag) }));
  return finish(formatProseWithDiff(escaped));
}

// ── Delegated actions ───────────────────────────────────────────────────────
// The sanitiser strips `on*`, including from Orb's own markup, so the code-block
// buttons and the broken-image fallback are delegated. A model cannot forge
// either: `class="code-block-btn"` sanitises to `custom-code-block-btn`.

export function initMessageHtmlActions() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest?.(".code-block-btn");
    if (!btn) return;
    const block = btn.closest(".code-block");
    if (!block) return;
    if (btn.dataset.orbAction === "wrap") {
      btn.setAttribute("aria-pressed", String(block.classList.toggle("wrap")));
    } else if (btn.dataset.orbAction === "copy") {
      const code = block.querySelector("code");
      navigator.clipboard?.writeText(code ? code.textContent : "").then(() => {
        btn.classList.add("copied");
        setTimeout(() => btn.classList.remove("copied"), 1200);
      });
    }
  });

  // `error` does not bubble, so the fallback has to listen on the way down.
  document.addEventListener(
    "error",
    (e) => {
      const img = e.target;
      if (!(img instanceof HTMLImageElement) || !img.classList.contains("msg-image")) return;
      const broken = document.createElement("span");
      broken.className = "msg-image-broken";
      broken.textContent = img.src;
      img.replaceWith(broken);
    },
    true,
  );
}
