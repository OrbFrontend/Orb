// Browser-side half of message rendering. `formatProse` emits model markup;
// `renderMessageHtml` is the only path that sanitises, scopes CSS and lays it out.
// Pipeline: escape unknown tags -> formatProse -> sanitise -> rebuild chrome ->
// contain styles -> block layout -> serialise.

import { CODE_COPY_ICON, CODE_WRAP_ICON } from "./icons.js";
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

// Sanitising is synchronous, so hooks can read the current CSS scope here.
let _activeScope = "";

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
      data.attrValue = data.attrValue ? filterDeclarations(data.attrValue, _activeScope) : "";
      if (!data.attrValue) data.keepAttr = false;
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

// Selectors are always rewritten; card CSS must not target Orb's own classes.
function scopeClassName(token) {
  return token.startsWith("custom-") ? token : `custom-${token}`;
}

// ── CSS containment ─────────────────────────────────────────────────────────
// Re-emit only scoped, allowlisted CSS. This string parser is deterministic and
// testable without depending on a browser CSSOM.

/** Properties allowed verbatim. Longhands matter: engines expand shorthands. */
const CSS_PROP_EXACT = new Set([
  "animation",
  "aspect-ratio",
  "backdrop-filter",
  "background",
  "block-size",
  "bottom",
  "box-shadow",
  "box-sizing",
  "caret-color",
  "clear",
  "clip-path",
  "color",
  "color-scheme",
  "columns",
  "content",
  "counter-increment",
  "counter-reset",
  "cursor",
  "direction",
  "display",
  "empty-cells",
  "filter",
  "float",
  "font",
  "gap",
  "height",
  "hyphens",
  "inline-size",
  "inset",
  "isolation",
  "left",
  "letter-spacing",
  "line-break",
  "line-height",
  "max-block-size",
  "max-height",
  "max-inline-size",
  "max-width",
  "min-block-size",
  "min-height",
  "min-inline-size",
  "min-width",
  "mix-blend-mode",
  "object-fit",
  "object-position",
  "opacity",
  "order",
  "perspective",
  "perspective-origin",
  "pointer-events",
  "position",
  "quotes",
  "resize",
  "right",
  "rotate",
  "scale",
  "tab-size",
  "table-layout",
  "top",
  "touch-action",
  "transform",
  "transform-box",
  "transform-origin",
  "transform-style",
  "transition",
  "translate",
  "unicode-bidi",
  "user-select",
  "vertical-align",
  "visibility",
  "white-space",
  "width",
  "word-break",
  "word-spacing",
  "writing-mode",
  "z-index",
]);

/** Families allowed wholesale, longhands and shorthands alike. */
const CSS_PROP_PREFIXES = [
  "align-",
  "animation-",
  "background-",
  "border",
  "column-",
  "flex",
  "font-",
  "grid",
  "inset-",
  "justify-",
  "list-style",
  "margin",
  "mask",
  "outline",
  "overflow",
  "padding",
  "place-",
  "row-gap",
  "scroll-margin",
  "scroll-padding",
  "text-",
  "transition-",
];

// Block network loads, escapes and values that can break the declaration body.
const CSS_VALUE_DENY = /url\s*\(|image-set|cross-fade|element\s*\(|expression|behavior|[\\@<>{};]/i;

// Keep positioning inside the bubble and below the app's chrome.
const CSS_VALUE_RULES = {
  position: /^(static|relative|absolute)$/i,
  "z-index": /^(auto|-?\d{1,2})$/,
};

const CSS_AT_CONDITIONAL = new Set(["media", "supports", "container"]);

// Animation shorthand keywords are not keyframe names.
const CSS_ANIMATION_KEYWORDS = new Set([
  "alternate",
  "alternate-reverse",
  "backwards",
  "both",
  "ease",
  "ease-in",
  "ease-in-out",
  "ease-out",
  "forwards",
  "infinite",
  "inherit",
  "initial",
  "linear",
  "none",
  "normal",
  "paused",
  "reverse",
  "revert",
  "running",
  "step-end",
  "step-start",
  "unset",
]);

/** Return the index after a quoted string. */
function _skipString(css, at) {
  const quote = css[at];
  for (let i = at + 1; i < css.length; i++) {
    if (css[i] === "\\") i++;
    else if (css[i] === quote) return i + 1;
  }
  return css.length;
}

/** Return the index after a parenthesized or bracketed group. */
function _skipGroup(css, at) {
  const close = css[at] === "(" ? ")" : "]";
  let depth = 0;
  for (let i = at; i < css.length; i++) {
    const ch = css[i];
    if (ch === '"' || ch === "'") i = _skipString(css, i) - 1;
    else if (ch === css[at]) depth++;
    else if (ch === close && --depth === 0) return i + 1;
  }
  return css.length;
}

function _stripCssComments(css) {
  let out = "";
  for (let i = 0; i < css.length; ) {
    if (css[i] === "/" && css[i + 1] === "*") {
      const end = css.indexOf("*/", i + 2);
      i = end === -1 ? css.length : end + 2;
      out += " ";
    } else if (css[i] === '"' || css[i] === "'") {
      const end = _skipString(css, i);
      out += css.slice(i, end);
      i = end;
    } else {
      out += css[i++];
    }
  }
  return out;
}

/** Split one nesting level into `{ prelude, block }` statements. */
function parseCssBlocks(css) {
  const out = [];
  let start = 0;
  for (let i = 0; i < css.length; ) {
    const ch = css[i];
    if (ch === '"' || ch === "'") {
      i = _skipString(css, i);
    } else if (ch === "(" || ch === "[") {
      i = _skipGroup(css, i);
    } else if (ch === ";") {
      const prelude = css.slice(start, i).trim();
      if (prelude) out.push({ prelude, block: null });
      start = ++i;
    } else if (ch === "{") {
      let depth = 0;
      let end = css.length;
      for (let j = i; j < css.length; j++) {
        const c = css[j];
        if (c === '"' || c === "'") j = _skipString(css, j) - 1;
        else if (c === "(" || c === "[") j = _skipGroup(css, j) - 1;
        else if (c === "{") depth++;
        else if (c === "}" && --depth === 0) {
          end = j;
          break;
        }
      }
      out.push({ prelude: css.slice(start, i).trim(), block: css.slice(i + 1, end) });
      start = end + 1;
      i = start;
    } else if (ch === "}") {
      start = ++i; // a stray close: whatever preceded it is not a rule we can trust
    } else {
      i++;
    }
  }
  const tail = css.slice(start).trim();
  if (tail) out.push({ prelude: tail, block: null });
  return out;
}

/** Split on `sep` outside strings and bracket groups. */
function splitTopLevel(text, sep) {
  const parts = [];
  let start = 0;
  for (let i = 0; i < text.length; ) {
    const ch = text[i];
    if (ch === '"' || ch === "'") i = _skipString(text, i);
    else if (ch === "(" || ch === "[") i = _skipGroup(text, i);
    else if (ch === sep) {
      parts.push(text.slice(start, i));
      start = ++i;
    } else i++;
  }
  parts.push(text.slice(start));
  return parts;
}

/** Split a selector list on top-level commas. */
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

const SELECTOR_TOKEN_RE = /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\.(-?[_a-zA-Z][\w-]*)|#(-?[_a-zA-Z][\w-]*)/g;

/** Prefix a selector with this message's scope and rewrite classes and ids. */
function scopeSelector(selector, scope) {
  return splitSelectorList(selector)
    .map((part) => {
      const trimmed = part.trim();
      if (!trimmed || /[{}@;]/.test(trimmed)) return "";
      const scoped = trimmed.replace(SELECTOR_TOKEN_RE, (match, cls, id) => {
        if (cls !== undefined) return `.${scopeClassName(cls)}`;
        if (id !== undefined) return `#user-content-${id}`;
        return match;
      });
      return `.msg-body .${scope} ${scoped}`;
    })
    .filter(Boolean)
    .join(", ");
}

function isAllowedCssProp(prop) {
  if (prop.startsWith("--")) return /^--[\w-]+$/.test(prop);
  if (!/^-?[a-z][a-z0-9-]*$/.test(prop)) return false;
  return CSS_PROP_EXACT.has(prop) || CSS_PROP_PREFIXES.some((p) => prop.startsWith(p));
}

/** Point animation references at this message's renamed keyframes. */
function rewriteAnimationValue(value, scope) {
  return splitTopLevel(value, ",")
    .map((group) =>
      group.replace(/[-\w]+/g, (token) => {
        if (!/^-?[_a-zA-Z][\w-]*$/.test(token)) return token;
        if (CSS_ANIMATION_KEYWORDS.has(token.toLowerCase())) return token;
        return `${scope}-${token}`;
      }),
    )
    .join(",");
}

/** Re-emit only declarations allowed by the CSS policy. */
function filterDeclarations(block, scope) {
  const kept = [];
  for (const raw of splitTopLevel(_stripCssComments(block), ";")) {
    const decl = raw.trim();
    // A `{` here is a nested rule, not a declaration. Nothing rewrites selectors
    // at this depth, so it goes rather than escaping the scope unrewritten.
    if (!decl || decl.includes("{") || decl.includes("}")) continue;
    const colon = decl.indexOf(":");
    if (colon <= 0) continue;
    const prop = decl.slice(0, colon).trim().toLowerCase();
    let value = decl.slice(colon + 1).trim();
    if (!value || !isAllowedCssProp(prop)) continue;
    if (CSS_VALUE_DENY.test(value)) continue;
    const bare = value.replace(/\s*!important$/i, "").trim();
    const rule = CSS_VALUE_RULES[prop];
    if (rule && !rule.test(bare)) continue;
    if (scope && (prop === "animation" || prop === "animation-name")) {
      value = rewriteAnimationValue(value, scope);
    }
    kept.push(`${prop}: ${value}`);
  }
  return kept.join("; ");
}

const KEYFRAMES_NAME_RE = /^@(?:-webkit-)?keyframes\s+(-?[_a-zA-Z][\w-]*)$/i;
const KEYFRAME_SELECTOR_RE = /^(from|to|[+-]?\d+(?:\.\d+)?%)$/i;

function renderKeyframes(prelude, block, scope) {
  const name = KEYFRAMES_NAME_RE.exec(prelude.trim())?.[1];
  if (!name) return "";
  let body = "";
  for (const { prelude: stop, block: decls } of parseCssBlocks(block)) {
    if (decls === null) continue;
    const stops = splitTopLevel(stop, ",").map((s) => s.trim());
    if (!stops.every((s) => KEYFRAME_SELECTOR_RE.test(s))) continue;
    const kept = filterDeclarations(decls, scope);
    if (kept) body += `  ${stops.join(", ")} { ${kept} }\n`;
  }
  // Renamed, so a card's `pulse` is its own and the app's stays the app's.
  return body ? `@keyframes ${scope}-${name} {\n${body}}\n` : "";
}

// Conditional preludes are copied only when they cannot escape the rule.
const AT_PRELUDE_DENY = /url\s*\(|[\\{};]|@[-\w]+[\s\S]*@/i;

function renderRules(css, scope, depth) {
  if (depth > 8) return "";
  let out = "";
  for (const { prelude, block } of parseCssBlocks(css)) {
    if (block === null) continue; // a bare `@import`/`@charset`, or a truncated tail
    if (prelude.startsWith("@")) {
      const name = /^@(?:-webkit-)?([-\w]+)/.exec(prelude)?.[1]?.toLowerCase();
      if (name === "keyframes") {
        out += renderKeyframes(prelude, block, scope);
      } else if (CSS_AT_CONDITIONAL.has(name) && !AT_PRELUDE_DENY.test(prelude)) {
        const inner = renderRules(block, scope, depth + 1);
        if (inner) out += `${prelude} {\n${inner}}\n`;
      }
      // Other at-rules are global by construction and are dropped.
      continue;
    }
    const selector = scopeSelector(prelude, scope);
    const decls = filterDeclarations(block, scope);
    if (selector && decls) out += `${selector} { ${decls} }\n`;
  }
  return out;
}

/** Return card CSS scoped to one message. */
export function sanitizeCss(cssText, scope) {
  if (!cssText || !scope) return "";
  try {
    const css = renderRules(_stripCssComments(cssText), scope, 0);
    // The fragment is serialised before it reaches innerHTML, and `</style` is
    // the one token that ends a style element's raw text on the way back in.
    return css.replace(/<\/style/gi, "\\3c /style");
  } catch {
    return "";
  }
}

/** Return a stable scope for a message source. */
export function cssScope(source) {
  let h = 0x811c9dc5;
  for (let i = 0; i < source.length; i++) {
    h ^= source.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `msg-s${h.toString(36)}`;
}

/** Replace encoded custom styles with scoped styles. */
function applyCustomStyles(root, scope) {
  let applied = false;
  for (const node of Array.from(root.querySelectorAll("custom-style"))) {
    let css = "";
    try {
      css = sanitizeCss(decodeURIComponent(node.textContent || ""), scope);
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
    applied = true;
  }
  return applied;
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
  _activeScope = scope;
  let fragment;
  try {
    // Adopt the sanitised fragment before walking it.
    fragment = document.adoptNode(DOMPurify.sanitize(html, SANITIZE_CONFIG));
  } finally {
    _activeScope = "";
  }
  const styled = applyCustomStyles(fragment, scope);
  restoreCodeBlockChrome(fragment);
  wrapTables(fragment);
  applyBlockLayout(fragment);
  if (styled) {
    // Card CSS is scoped below this message-specific wrapper.
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
