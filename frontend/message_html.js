// Browser-side half of message rendering. `formatProse` emits model markup;
// `renderMessageHtml` is the only path that sanitises, scopes CSS and lays it out.
// Pipeline: escape unknown tags -> formatProse -> sanitise -> rebuild chrome ->
// contain styles -> block layout -> serialise.

import { CODE_COPY_ICON, CODE_WRAP_ICON } from "./icons.js";
import { compileCss, cssScope, emptyNames, filterDeclarations, scopeClassName } from "./message_css.js";
import { formatProse, formatProseWithDiff } from "./utils.js";
import DOMPurify from "./vendor/purify.js";

// ── Class vocabulary ────────────────────────────────────────────────────────
// Classes emitted by formatProse. Other model classes are prefixed with
// `custom-`; classes added after sanitising are intentionally not listed here.
export const ORB_CLASSES = new Set([
  "quoted",
  "code-block",
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
// Tags that already start a line, so adjacent newlines are redundant.
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
export const NON_PROSE_TAGS = new Set(["PRE", "CODE", "STYLE", "SCRIPT", "TEXTAREA", "TITLE", "SVG"]);

// ── Unknown-tag escaping ────────────────────────────────────────────────────
// Leave fenced code and style/SVG bodies alone; their contents are handled as
// data by formatProse or the browser.
const PASSTHROUGH_RE = /```[\s\S]*?```|```[\s\S]*$|<style\b[^>]*>[\s\S]*?<\/style\s*>|<svg\b[^>]*>[\s\S]*?<\/svg\s*>/gi;
const TAG_START_RE = /^<\/?([a-zA-Z][a-zA-Z0-9-]*)/;

/**
 * Escape every `<` that does not open a tag the browser actually knows.
 *
 * DOMPurify keeps the contents of dropped tags, so unknown tags must be escaped
 * first or ordinary prose can disappear. `isKnownTag` keeps this testable in Node.
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
    // Require a known, closed tag so prose such as `a < b` stays intact.
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
 * Drop an unfinished tag or style block during streaming. Open fences stay in
 * place because formatProse renders their body as escaped code.
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
// Keep DOMPurify's default tag set; narrow it with the forbids below.
const SANITIZE_CONFIG = {
  ADD_TAGS: ["custom-style"],
  // Controls, embedding, navigation and deferred parsing are not message content.
  FORBID_TAGS: [
    "form",
    "input",
    "select",
    "textarea",
    "button",
    "style",
    "template",
    "slot",
    "iframe",
    "object",
    "embed",
    "script",
    "base",
    "link",
    "meta",
  ],
  // Remove unsolicited fetch/noise and alternate URL surfaces.
  FORBID_ATTR: ["autoplay", "srcset", "sizes", "background", "ping", "nonce", "integrity"],
  // Delegated actions are restored only on Orb-built chrome after this pass.
  ALLOW_DATA_ATTR: false,
  // Prevent id/name collisions; scopeSelector mirrors the id rewrite in CSS.
  SANITIZE_NAMED_PROPS: true,
  RETURN_DOM_FRAGMENT: true,
};

// Sanitising is synchronous, so the hooks read this render's CSS context here:
// the message's scope, the sheet's rename table, and whether any inline style
// survived -- which decides whether the containment wrapper is needed.
let _css = { scope: "", names: emptyNames(), used: false };

let _hooksInstalled = false;

function installHooks() {
  if (_hooksInstalled) return;
  _hooksInstalled = true;

  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    const tag = node.tagName?.toUpperCase?.();
    if (tag === "A" || tag === "AREA") {
      // Links leave the app, so open them in a separate context.
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
    // Remote media is allowed, but should not send a referrer or preload.
    if (tag === "A" || tag === "AREA" || tag === "IMG") {
      node.setAttribute("referrerpolicy", "no-referrer");
    }
    if (tag === "IMG") {
      node.setAttribute("loading", "lazy");
      node.setAttribute("decoding", "async");
    } else if (tag === "AUDIO" || tag === "VIDEO") {
      node.setAttribute("preload", "none");
    }
  });

  DOMPurify.addHook("uponSanitizeAttribute", (_node, data) => {
    if (data.attrName === "class") {
      if (!data.attrValue) return;
      data.attrValue = data.attrValue.split(/\s+/).filter(Boolean).map(scopeClassAttr).join(" ");
      return;
    }
    if (data.attrName === "style") {
      data.attrValue = data.attrValue ? filterDeclarations(data.attrValue, _css.scope, _css.names) : "";
      if (data.attrValue) _css.used = true;
      else data.keepAttr = false;
    }
  });

  // Preserve the encoded CSS payload until the dedicated CSS pass.
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

// ── Card CSS ──────────────────────────────────────────────────────────────────
// message_css.js owns the policy; this half only has to give it the message's
// scope and hand the same rename table to the sheet and to the style attributes.

const CUSTOM_STYLE_RE = /<custom-style>([^<]*)<\/custom-style>/gi;

/** Pull the encoded sheets out before sanitising, so inline styles see those names. */
function compileMessageCss(html, scope) {
  // Most messages carry no sheet at all, and this runs on every streaming frame.
  if (!html.includes("<custom-style>")) return compileCss("", scope);
  let source = "";
  for (const match of html.matchAll(CUSTOM_STYLE_RE)) {
    try {
      source += `${decodeURIComponent(match[1])}\n`;
    } catch {
      // A payload formatProse did not write; there is nothing to recover from it.
    }
  }
  return compileCss(source, scope);
}

/** Replace the encoded custom styles with the one compiled sheet. */
function applyCustomStyles(root, css) {
  let placed = false;
  for (const node of Array.from(root.querySelectorAll("custom-style"))) {
    if (!css || placed) {
      node.remove();
      continue;
    }
    const style = document.createElement("style");
    style.textContent = css;
    node.replaceWith(style);
    placed = true;
  }
}

// ── Orb's own chrome ────────────────────────────────────────────────────────
// Build the code-block toolbar after sanitising so its delegated actions are
// never model-controlled.

function codeBlockButton(action, label, icon, pressed) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "code-block-btn";
  btn.dataset.orbAction = action;
  btn.title = label;
  btn.setAttribute("aria-label", label);
  if (pressed) btn.setAttribute("aria-pressed", "false");
  btn.innerHTML = icon;
  return btn;
}

function restoreCodeBlockChrome(root) {
  for (const block of Array.from(root.querySelectorAll(".code-block"))) {
    if (!block.querySelector("pre > code")) continue;
    const bar = document.createElement("div");
    bar.className = "code-block-bar";
    bar.appendChild(codeBlockButton("wrap", "Toggle word wrap", CODE_WRAP_ICON, true));
    bar.appendChild(codeBlockButton("copy", "Copy code", CODE_COPY_ICON, false));
    block.insertBefore(bar, block.firstChild);
  }
}

// ── Block layout ────────────────────────────────────────────────────────────

function isBlockBoundary(sibling, parent) {
  if (sibling) return sibling.nodeType === Node.ELEMENT_NODE && BLOCK_TAGS.has(sibling.tagName.toUpperCase());
  // At an edge, the parent determines whether a break is needed.
  return !parent || parent.nodeType !== Node.ELEMENT_NODE || BLOCK_TAGS.has(parent.tagName.toUpperCase());
}

function inNoBreakContext(node) {
  for (let el = node.parentNode; el && el.nodeType === Node.ELEMENT_NODE; el = el.parentNode) {
    if (NON_PROSE_TAGS.has(el.tagName.toUpperCase())) return true;
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

/** Turn prose newlines into DOM breaks without touching data elements. */
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

// LRU cache bounded by total characters and per-entry size.
const _renderCache = new Map();
const _RENDER_CACHE_MAX_CHARS = 4 * 1024 * 1024;
// Oversized messages do not evict the conversation cache.
const _RENDER_CACHE_MAX_ENTRY = 256 * 1024;
let _renderCacheChars = 0;

function cacheGet(source) {
  const hit = _renderCache.get(source);
  if (hit === undefined) return undefined;
  _renderCache.delete(source); // re-insert at the young end
  _renderCache.set(source, hit);
  return hit;
}

function cachePut(source, html) {
  const cost = source.length + html.length;
  if (cost > _RENDER_CACHE_MAX_ENTRY) return;
  const existing = _renderCache.get(source);
  if (existing !== undefined) {
    _renderCacheChars -= source.length + existing.length;
    _renderCache.delete(source);
  }
  _renderCache.set(source, html);
  _renderCacheChars += cost;
  for (const key of _renderCache.keys()) {
    if (_renderCacheChars <= _RENDER_CACHE_MAX_CHARS) break;
    if (key === source) continue; // never evict what this call just stored
    _renderCacheChars -= key.length + _renderCache.get(key).length;
    _renderCache.delete(key);
  }
}

function serialize(fragment) {
  const holder = document.createElement("div");
  holder.appendChild(fragment);
  return holder.innerHTML;
}

/** Keep wide tables inside the bubble. */
function wrapTables(root) {
  for (const table of Array.from(root.querySelectorAll("table"))) {
    const box = document.createElement("div");
    box.className = "msg-table-scroll";
    table.replaceWith(box);
    box.appendChild(table);
  }
}

function finish(html, scope) {
  installHooks();
  // The sheet is compiled first: its rename table has to be in hand before the
  // sanitiser reaches a `style` attribute that names one of the sheet's fonts.
  const sheet = compileMessageCss(html, scope);
  _css = { scope, names: sheet.names, used: false };
  let fragment;
  let inlineStyled = false;
  try {
    // Adopt the sanitised fragment before walking it.
    fragment = document.adoptNode(DOMPurify.sanitize(html, SANITIZE_CONFIG));
  } finally {
    inlineStyled = _css.used;
    _css = { scope: "", names: emptyNames(), used: false };
  }
  applyCustomStyles(fragment, sheet.css);
  restoreCodeBlockChrome(fragment);
  wrapTables(fragment);
  applyBlockLayout(fragment);
  if (sheet.css || inlineStyled) {
    // Card CSS is scoped below this wrapper, and contained by it: chat.css gives
    // `.msg-css-scope` paint containment, which is what keeps a card's
    // `position: fixed` and its `z-index` inside this one message.
    const box = document.createElement("div");
    box.className = `msg-css-scope ${scope}`;
    while (fragment.firstChild) box.appendChild(fragment.firstChild);
    fragment.appendChild(box);
  }
  return serialize(fragment);
}

/** Render model markup through the sanitise, layout and CSS-scope pipeline. */
export function renderMessageHtml(text, { streaming = false } = {}) {
  if (!text) return "";
  const source = streaming ? trimIncompleteMarkup(text) : text;
  if (!source) return "";
  const cached = cacheGet(source);
  if (cached !== undefined) return cached;
  const html = finish(formatProse(escapeUnknownTags(source, isKnownTag)), cssScope(source));
  // Streaming snapshots are transient and are not cached.
  if (!streaming) cachePut(source, html);
  return html;
}

/** Render an editor sentence diff through the same pipeline. */
export function renderMessageDiffHtml(ops) {
  const escaped = ops.map((op) => ({ ...op, text: escapeUnknownTags(op.text, isKnownTag) }));
  return finish(formatProseWithDiff(escaped), cssScope(ops.map((op) => op.text).join("\0")));
}

// ── Delegated actions ───────────────────────────────────────────────────────
// Delegate actions because sanitising removes handlers and data attributes from
// model markup; Orb adds its own code-block actions afterward.

const CODE_BLOCK_ACTIONS = new Set(["wrap", "copy"]);

export function initMessageHtmlActions() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest?.(".code-block-btn[data-orb-action]");
    if (!btn) return;
    const action = btn.dataset.orbAction;
    if (!CODE_BLOCK_ACTIONS.has(action)) return;
    const block = btn.closest(".code-block");
    if (!block) return;
    if (action === "wrap") {
      btn.setAttribute("aria-pressed", String(block.classList.toggle("wrap")));
    } else {
      const code = block.querySelector("code");
      navigator.clipboard?.writeText(code ? code.textContent : "").then(() => {
        btn.classList.add("copied");
        setTimeout(() => btn.classList.remove("copied"), 1200);
      });
    }
  });

  // Image errors do not bubble, so listen during capture.
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
