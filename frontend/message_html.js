// Message-body HTML: the browser half of prose rendering.
//
// `formatProse` (utils.js) is a pure string pass so it can be tested on plain
// Node, and it deliberately emits markup the model wrote. Nothing it returns may
// reach `innerHTML` directly — it has to come through `renderMessageHtml` first,
// which is where DOMPurify, block layout and CSS containment live.
//
// Pipeline: escape unknown tags -> formatProse -> sanitise -> rebuild Orb's own
// chrome -> contain `<custom-style>` CSS -> block layout -> serialise.
//
// The threat model is a hostile message author. Everything a message carries is
// attacker-controlled, so the boundary is built to fail closed:
//
//   * no `data-*` survives sanitising, so model markup can never carry one of
//     the app's delegated action attributes;
//   * `id`/`name` are `user-content-`-prefixed, so they cannot collide with an
//     app id or clobber a named property;
//   * every class the model writes is `custom-`-prefixed, and Orb's interactive
//     chrome is *rebuilt after* sanitising, so forging its markup buys nothing;
//   * CSS is re-emitted from an allowlist, scoped to one message, and can name
//     no URL and no global at-rule.

import { CODE_COPY_ICON, CODE_WRAP_ICON } from "./icons.js";
import { formatProse, formatProseWithDiff } from "./utils.js";
import DOMPurify from "./vendor/purify.js";

// ── Class vocabulary ────────────────────────────────────────────────────────
// Every class `formatProse` emits. Anything else in a class attribute is model
// output and gets `custom-` prefixed, so card markup can never borrow an app
// class. tests/frontend/message_html_classes.test.mjs keeps this in sync with
// utils.js. Classes this module adds after sanitising — `pbreak`,
// `code-block-bar`, `code-block-btn`, `msg-table-scroll`, `msg-image-broken`,
// `msg-css-scope` — stay out: they never meet the hook, so listing them would
// only hand model markup a class it should not have.
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
// whose contents are CSS and SVG rather than prose. A fence that never closes
// counts too: formatProse reads it as code to the end of the message, so this
// has to leave its interior alone the same way.
const PASSTHROUGH_RE = /```[\s\S]*?```|```[\s\S]*$|<style\b[^>]*>[\s\S]*?<\/style\s*>|<svg\b[^>]*>[\s\S]*?<\/svg\s*>/gi;
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
 *
 * An unterminated ``` fence is deliberately *not* trimmed: formatProse renders
 * it as an open code block, which is both the CommonMark reading and the safe
 * one — its body is escaped rather than parsed, so a fence that never closes
 * cannot leave live markup on screen, mid-stream or after the turn ends.
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
// The tag defaults are load-bearing and deliberately kept: passing ALLOWED_TAGS
// *replaces* DEFAULT_ALLOWED_TAGS rather than intersecting with it, which would
// drop SVG and MathML and leave us hand-maintaining ~250 tag names. Narrowing
// is expressed through FORBID_TAGS, which is checked ahead of the allowlist.
const SANITIZE_CONFIG = {
  ADD_TAGS: ["custom-style"],
  // `style` is here because the only stylesheet allowed to reach the page is
  // the one applyCustomStyles builds *after* this pass. Anything the model
  // writes as a raw `<style>` — including a block formatProse could not fold
  // into `<custom-style>` because it never closed — is dropped, contents and
  // all (DOMPurify's FORBID_CONTENTS covers `style`).
  // `button` is the confused deputy's handle: a card has nothing to submit and
  // nothing to command, so the element goes and its label survives as text.
  // The rest are containers for input, embedding or deferred parsing — and
  // `template` can otherwise smuggle a subtree past the attribute hooks as
  // declarative shadow DOM.
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
  // `autoplay` starts a fetch and a noise nobody asked for; `srcset`/`sizes`
  // and `background` are URL surfaces with no prose use.
  FORBID_ATTR: ["autoplay", "srcset", "sizes", "background", "ping", "nonce", "integrity"],
  // Orb dispatches on `[data-wf-action]`, `[data-chat-action]` and
  // `[data-orb-action]`; with data attributes stripped, model markup cannot
  // carry one. Orb's own chrome inside a message body is rebuilt after this
  // pass (restoreCodeBlockChrome) precisely so it needs no exemption.
  ALLOW_DATA_ATTR: false,
  // `id`/`name` become `user-content-…`, so a message cannot answer a
  // getElementById() the app meant for its own node, nor clobber a named
  // property. scopeSelector rewrites `#id` in card CSS to match.
  SANITIZE_NAMED_PROPS: true,
  RETURN_DOM_FRAGMENT: true,
};

// The CSS scope in force for the sanitise pass currently running. Sanitising is
// synchronous, so a module-level handoff to the attribute hook is safe, and it
// spares every call site an argument it has no other use for.
let _activeScope = "";

let _hooksInstalled = false;

function installHooks() {
  if (_hooksInstalled) return;
  _hooksInstalled = true;

  // Switches on the tag name rather than on `"target" in node`: which IDL
  // attributes a DOM reflects is an engine detail, and a containment rule that
  // quietly does nothing in one engine is worse than none.
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    const tag = node.tagName?.toUpperCase?.();
    if (tag === "A" || tag === "AREA") {
      // Card links leave the app, so they leave it in a new tab. `target` is not
      // in DOMPurify's attribute allowlist, so this both restores Orb's own
      // image-embed link and forces the behaviour on model-written anchors.
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
    // Remote media in a message does still reach the network — that is what the
    // image embed is for — but it goes without naming the page it was read on,
    // and without fetching before the bubble is on screen.
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
    // The load-bearing defence: a class the model wrote can never be a class Orb
    // styles or selects on. Card CSS gets the same rewrite (scopeSelector), so
    // `.foo` still matches `class="foo"` — just as `.custom-foo`.
    if (data.attrName === "class") {
      if (!data.attrValue) return;
      data.attrValue = data.attrValue.split(/\s+/).filter(Boolean).map(scopeClassAttr).join(" ");
      return;
    }
    // An inline style is a stylesheet with an implicit selector, so it goes
    // through the same declaration allowlist as `<style>`: no URLs, no escapes,
    // and no animation that could name one of the app's own keyframes.
    if (data.attrName === "style") {
      data.attrValue = data.attrValue ? filterDeclarations(data.attrValue, _activeScope) : "";
      if (!data.attrValue) data.keepAttr = false;
    }
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

// ── CSS containment ─────────────────────────────────────────────────────────
// Card CSS is re-emitted from scratch rather than filtered in place: every
// selector is rewritten to reach only the message that wrote it, every
// declaration has to name an allowlisted property, and anything the scanner
// cannot make sense of is dropped. The parse is a plain string scan rather than
// a CSSStyleSheet so containment does not vary with the engine — and so it can
// be tested directly, in Node, without a browser.

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

// Values that would reach the network, smuggle a second declaration, or hide
// either behind a CSS escape. `\` is rejected outright: `\75 rl(…)` is `url(…)`
// to the engine, so a substring check on raw text is only sound once the escape
// hatch is shut. `<`/`>`/`{`/`}` never need to survive into a `<style>` body
// either, so the sheet cannot be re-parsed into something else on the way back
// in through innerHTML.
const CSS_VALUE_DENY = /url\s*\(|image-set|cross-fade|element\s*\(|expression|behavior|[\\@<>{};]/i;

// Properties whose value range matters for containment rather than for looks:
// `fixed`/`sticky` would escape the bubble, and a large z-index would climb
// over the app's own chrome.
const CSS_VALUE_RULES = {
  position: /^(static|relative|absolute)$/i,
  "z-index": /^(auto|-?\d{1,2})$/,
};

const CSS_AT_CONDITIONAL = new Set(["media", "supports", "container"]);

// Keywords an `animation` shorthand may carry that are not the keyframes name.
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

/** Index just past the string literal starting at `at`. */
function _skipString(css, at) {
  const quote = css[at];
  for (let i = at + 1; i < css.length; i++) {
    if (css[i] === "\\") i++;
    else if (css[i] === quote) return i + 1;
  }
  return css.length;
}

/** Index just past the bracket group starting at `at`. */
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

/**
 * Split one nesting level into `{ prelude, block }` statements. A statement
 * that ends in `;` rather than a block gets `block: null`; so does a trailing
 * fragment, which is how a truncated sheet fails closed instead of inheriting
 * the braces of whatever came before it.
 */
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

/** Split on top-level `sep`, ignoring the inside of strings and bracket groups. */
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

const SELECTOR_TOKEN_RE = /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\.(-?[_a-zA-Z][\w-]*)|#(-?[_a-zA-Z][\w-]*)/g;

/**
 * Prefix a selector with this message's own scope and rewrite the names it can
 * reach: classes get `custom-`, ids get the `user-content-` the sanitiser gave
 * them. The scope class is what stops one message styling the next — and what
 * keeps a bare `*` from reaching every bubble in the conversation.
 */
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

/**
 * Point every keyframes reference at this message's own renamed copy. A name
 * the card never defined then resolves to nothing, which is exactly the point:
 * `animation: pulse 1s` must not reach the app's global `pulse`.
 */
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

/** Re-emit a declaration block, keeping only what the allowlist vouches for. */
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

// A conditional prelude is copied verbatim, so it may not contain anything that
// closes the rule, reaches the network, or hides behind an escape. Range syntax
// (`@media (400px <= width)`) means `<`/`>` cannot be banned here; they are
// inert in a prelude, and the `</style` guard covers serialisation.
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
      // Everything else — @import, @font-face, @page, @property, @layer,
      // @scope, @counter-style — is global by construction, and dropped.
      continue;
    }
    const selector = scopeSelector(prelude, scope);
    const decls = filterDeclarations(block, scope);
    if (selector && decls) out += `${selector} { ${decls} }\n`;
  }
  return out;
}

/**
 * Rewrite card CSS into something that can only reach the message that wrote
 * it. Exported for the Node suite: this is the containment, so it is tested
 * head-on rather than through a browser's CSSOM.
 */
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

/** A stable per-message scope: same source, same class, so the cache still hits. */
export function cssScope(source) {
  let h = 0x811c9dc5;
  for (let i = 0; i < source.length; i++) {
    h ^= source.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `msg-s${h.toString(36)}`;
}

/** Turn every surviving `<custom-style>` into one scoped `<style>`. */
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
// The code-block toolbar is built *after* sanitising, which is what lets
// `data-orb-action` be stripped from everything the model wrote and still exist
// on Orb's buttons. Forging `class="code-block"` therefore buys a card nothing
// but a copy button over its own code.

function codeBlockButton(action, label, icon, pressed) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "code-block-btn";
  btn.dataset.orbAction = action;
  btn.title = label;
  btn.setAttribute("aria-label", label);
  if (pressed) btn.setAttribute("aria-pressed", "false");
  btn.innerHTML = icon; // a module constant, never model text
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

// Bounded by characters, not by entries: one 100k-character message costs as
// much as fifty ordinary ones, and an entry cap cannot tell them apart. LRU
// rather than FIFO — re-rendering the message on screen is exactly the hit that
// should keep it alive.
const _renderCache = new Map();
const _RENDER_CACHE_MAX_CHARS = 4 * 1024 * 1024;
// Nothing bigger than this earns a slot: it would evict most of the
// conversation to hold one bubble.
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

function finish(html, scope) {
  installHooks();
  _activeScope = scope;
  let fragment;
  try {
    // The sanitiser parses in its own document; take ownership before the DOM
    // passes walk it.
    fragment = document.adoptNode(DOMPurify.sanitize(html, SANITIZE_CONFIG));
  } finally {
    _activeScope = "";
  }
  const styled = applyCustomStyles(fragment, scope);
  restoreCodeBlockChrome(fragment);
  wrapTables(fragment);
  applyBlockLayout(fragment);
  if (styled) {
    // Card CSS reaches `.msg-body .<scope> …`, so the content it styles has to
    // sit under a node carrying that scope — and only this message's does.
    const box = document.createElement("div");
    box.className = `msg-css-scope ${scope}`;
    while (fragment.firstChild) box.appendChild(fragment.firstChild);
    fragment.appendChild(box);
  }
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
  const cached = cacheGet(source);
  if (cached !== undefined) return cached;
  const html = finish(formatProse(escapeUnknownTags(source, isKnownTag)), cssScope(source));
  // Every token produces a string seen exactly once, so caching mid-stream buys
  // nothing and evicts the finished messages the cache exists for.
  if (!streaming) cachePut(source, html);
  return html;
}

/** The same pipeline over the editor's sentence diff. */
export function renderMessageDiffHtml(ops) {
  const escaped = ops.map((op) => ({ ...op, text: escapeUnknownTags(op.text, isKnownTag) }));
  return finish(formatProseWithDiff(escaped), cssScope(ops.map((op) => op.text).join(" ")));
}

// ── Delegated actions ───────────────────────────────────────────────────────
// The sanitiser strips `on*` and every `data-*`, including from Orb's own
// markup, so the code-block buttons and the broken-image fallback are
// delegated. A model cannot forge either: `data-orb-action` is written by
// restoreCodeBlockChrome, after sanitising, and never survives from source.

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
