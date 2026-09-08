// The CSS half of message rendering. A card ships a `<style>` block and `style`
// attributes; this module is the only thing between that CSS and the app's own
// stylesheet, so it reads the sheet as a token stream rather than as text.
//
// Why a tokenizer: every interesting attack on a CSS allowlist is a spelling
// attack. `\75 rl(...)` is `url(...)`, `back/**/ground` is not `background`, and
// a stray `}` ends a rule the scoper thought it owned. Reading the sheet the way
// an engine reads it -- escapes decoded, comments elided, strings and functions
// balanced -- is what lets the policy be permissive: card CSS keeps escapes, web
// fonts, remote images and `position: fixed`, because each is checked on the
// value the browser will actually see. Output is re-serialised from tokens, so
// nothing survives that this file did not deliberately write.
//
// Containment rests on three things, none of them a property ban:
//   * every selector is prefixed with `.msg-body .<scope>`, so a rule reaches
//     the one message that wrote it and no other;
//   * globally named things (keyframes, font families, registered properties,
//     counter styles, layers) are renamed into the message's scope;
//   * `.msg-css-scope` in chat.css takes paint containment, which makes the
//     wrapper the containing block for `position: fixed` and opens a stacking
//     context, so overlays and `z-index` stay inside the bubble.
//
// A remote `url()` is a beacon its author can watch, like the `<img src>` the
// sanitiser already allows. Card CSS can make that fetch conditional (`:hover`,
// `@media`) where `<img>` cannot; that is the deliberate cost of rendering the
// cards that use web fonts and remote art.

// ---- Tokenizer -------------------------------------------------------------
// CSS Syntax Level 3 section 4, minus the tokens a message body cannot contain.

const T = {
  WS: "ws",
  IDENT: "ident",
  FUNC: "func",
  URL: "url",
  STR: "str",
  HASH: "hash",
  AT: "at",
  NUM: "num",
  DELIM: "delim",
  COMMA: ",",
  COLON: ":",
  SEMI: ";",
  OPEN_P: "(",
  CLOSE_P: ")",
  OPEN_S: "[",
  CLOSE_S: "]",
  OPEN_B: "{",
  CLOSE_B: "}",
  BAD: "bad",
};

const REPLACEMENT = String.fromCharCode(0xfffd);

const isWs = (ch) => ch === " " || ch === "\t" || ch === "\n" || ch === "\r" || ch === "\f";
const isDigit = (ch) => ch >= "0" && ch <= "9";
const isHex = (ch) => !!ch && /[0-9a-fA-F]/.test(ch);
const isIdentStart = (ch) => !!ch && (/[a-zA-Z_]/.test(ch) || ch.charCodeAt(0) >= 0x80);
const isIdentChar = (ch) => !!ch && (/[a-zA-Z0-9_-]/.test(ch) || ch.charCodeAt(0) >= 0x80);

/** True when `css[at]` starts a valid escape sequence. */
function validEscape(css, at) {
  if (css[at] !== "\\") return false;
  const next = css[at + 1];
  return next !== undefined && next !== "\n" && next !== "\r" && next !== "\f";
}

function startsIdent(css, at) {
  const ch = css[at];
  if (ch === undefined) return false;
  if (ch === "-") {
    const next = css[at + 1];
    return next === "-" || isIdentStart(next) || validEscape(css, at + 1);
  }
  if (ch === "\\") return validEscape(css, at);
  return isIdentStart(ch);
}

function startsNumber(css, at) {
  const ch = css[at];
  if (isDigit(ch)) return true;
  if (ch === ".") return isDigit(css[at + 1]);
  if (ch === "+" || ch === "-") return isDigit(css[at + 1]) || (css[at + 1] === "." && isDigit(css[at + 2]));
  return false;
}

/** Decode one escape into the character it stands for, and say where it ends. */
function consumeEscape(css, at) {
  let i = at + 1;
  if (i >= css.length) return { value: REPLACEMENT, next: i };
  if (!isHex(css[i])) {
    const code = css.codePointAt(i);
    return { value: String.fromCodePoint(code), next: i + (code > 0xffff ? 2 : 1) };
  }
  let hex = "";
  while (i < css.length && hex.length < 6 && isHex(css[i])) hex += css[i++];
  if (css[i] === "\r" && css[i + 1] === "\n") i += 2;
  else if (isWs(css[i])) i++;
  let code = Number.parseInt(hex, 16);
  // A surrogate or an out-of-range codepoint reads as U+FFFD, as it does in an engine.
  if (!code || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) code = 0xfffd;
  return { value: String.fromCodePoint(code), next: i };
}

/** Read an identifier, decoding escapes as it goes. */
function consumeName(css, at) {
  let value = "";
  let i = at;
  while (i < css.length) {
    if (isIdentChar(css[i])) value += css[i++];
    else if (validEscape(css, i)) {
      const esc = consumeEscape(css, i);
      value += esc.value;
      i = esc.next;
    } else break;
  }
  return { value, next: i };
}

function consumeString(css, at) {
  const quote = css[at];
  let value = "";
  let i = at + 1;
  while (i < css.length) {
    const ch = css[i];
    if (ch === quote) return { value, next: i + 1, bad: false };
    // A raw newline ends a string badly; what surrounds it is not trustworthy.
    if (ch === "\n" || ch === "\r" || ch === "\f") return { value, next: i, bad: true };
    if (ch === "\\") {
      const after = css[i + 1];
      if (after === "\n" || after === "\f") {
        i += 2; // an escaped newline continues the string
        continue;
      }
      if (after === "\r") {
        i += css[i + 2] === "\n" ? 3 : 2;
        continue;
      }
      const esc = consumeEscape(css, i);
      value += esc.value;
      i = esc.next;
      continue;
    }
    value += ch;
    i++;
  }
  return { value, next: i, bad: true }; // unterminated at end of input
}

/** Read the body of an unquoted `url(`. */
function consumeUrl(css, at) {
  let i = at;
  while (isWs(css[i])) i++;
  let value = "";
  let bad = false;
  while (i < css.length) {
    const ch = css[i];
    if (ch === ")") return { value, next: i + 1, bad };
    if (isWs(ch)) {
      while (isWs(css[i])) i++;
      if (css[i] === ")") return { value, next: i + 1, bad };
      bad = true; // whitespace inside an unquoted URL
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "(" || ch.charCodeAt(0) <= 0x1f) {
      bad = true;
      i++;
      continue;
    }
    if (ch === "\\") {
      if (!validEscape(css, i)) {
        bad = true;
        i++;
        continue;
      }
      const esc = consumeEscape(css, i);
      value += esc.value;
      i = esc.next;
      continue;
    }
    value += ch;
    i++;
  }
  return { value, next: i, bad: true };
}

function consumeNumeric(css, at) {
  let i = at;
  if (css[i] === "+" || css[i] === "-") i++;
  while (isDigit(css[i])) i++;
  if (css[i] === "." && isDigit(css[i + 1])) {
    i += 2;
    while (isDigit(css[i])) i++;
  }
  const expSign = css[i + 1] === "+" || css[i + 1] === "-";
  if ((css[i] === "e" || css[i] === "E") && (isDigit(css[i + 1]) || (expSign && isDigit(css[i + 2])))) {
    i += 2;
    while (isDigit(css[i])) i++;
  }
  const number = css.slice(at, i);
  if (css[i] === "%") return { v: number, u: "%", next: i + 1 };
  if (startsIdent(css, i)) {
    const unit = consumeName(css, i);
    return { v: number, u: unit.value, next: unit.next };
  }
  return { v: number, u: "", next: i };
}

const SINGLES = {
  "(": T.OPEN_P,
  ")": T.CLOSE_P,
  "[": T.OPEN_S,
  "]": T.CLOSE_S,
  "{": T.OPEN_B,
  "}": T.CLOSE_B,
  ",": T.COMMA,
  ":": T.COLON,
  ";": T.SEMI,
};

/** Tokenize a sheet, a declaration list or a selector. Never throws. */
export function tokenize(css) {
  const out = [];
  const n = css.length;
  // Comments and whitespace both separate tokens, which is what keeps
  // `back/**/ground` from spelling a property name past the allowlist.
  const space = () => {
    if (out.length && out[out.length - 1].t !== T.WS) out.push({ t: T.WS, v: " " });
  };
  let i = 0;
  while (i < n) {
    const ch = css[i];
    if (ch === "/" && css[i + 1] === "*") {
      const end = css.indexOf("*/", i + 2);
      i = end === -1 ? n : end + 2;
      space();
      continue;
    }
    if (isWs(ch)) {
      while (i < n && isWs(css[i])) i++;
      space();
      continue;
    }
    if (ch === '"' || ch === "'") {
      const str = consumeString(css, i);
      out.push(str.bad ? { t: T.BAD, v: "" } : { t: T.STR, u: str.value });
      i = str.next;
      continue;
    }
    if (ch === "#") {
      if (isIdentChar(css[i + 1]) || validEscape(css, i + 1)) {
        const name = consumeName(css, i + 1);
        out.push({ t: T.HASH, u: name.value });
        i = name.next;
        continue;
      }
      out.push({ t: T.DELIM, v: "#" });
      i++;
      continue;
    }
    if (ch === "@") {
      if (startsIdent(css, i + 1)) {
        const name = consumeName(css, i + 1);
        out.push({ t: T.AT, u: name.value });
        i = name.next;
        continue;
      }
      out.push({ t: T.DELIM, v: "@" });
      i++;
      continue;
    }
    if (startsNumber(css, i)) {
      const num = consumeNumeric(css, i);
      out.push({ t: T.NUM, v: num.v, u: num.u });
      i = num.next;
      continue;
    }
    if (startsIdent(css, i)) {
      const name = consumeName(css, i);
      if (css[name.next] !== "(") {
        out.push({ t: T.IDENT, u: name.value });
        i = name.next;
        continue;
      }
      if (unprefixed(name.value) === "url") {
        let j = name.next + 1;
        while (isWs(css[j])) j++;
        // `url("...")` is an ordinary function; only the unquoted form is a url-token.
        if (css[j] !== '"' && css[j] !== "'") {
          const url = consumeUrl(css, name.next + 1);
          out.push(url.bad ? { t: T.BAD, v: "" } : { t: T.URL, u: url.value });
          i = url.next;
          continue;
        }
      }
      out.push({ t: T.FUNC, u: name.value });
      i = name.next + 1;
      continue;
    }
    if (ch === "\\") {
      // A backslash that starts no valid escape is junk, not an identifier.
      out.push({ t: T.BAD, v: "" });
      i++;
      continue;
    }
    const single = SINGLES[ch];
    out.push(single ? { t: single, v: ch } : { t: T.DELIM, v: ch });
    i++;
  }
  return out;
}

// ---- Serialiser ------------------------------------------------------------
// Output is written from tokens, never copied from the source, so a rule can
// only contain characters this file chose to emit.

/** Escape a name so it re-parses as the same identifier. */
function escapeIdent(name) {
  const s = String(name);
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const code = s.codePointAt(i);
    const ch = String.fromCodePoint(code);
    if (code > 0xffff) i++;
    const alpha = (code >= 0x41 && code <= 0x5a) || (code >= 0x61 && code <= 0x7a) || code === 0x5f || code >= 0x80;
    const digit = code >= 0x30 && code <= 0x39;
    const dash = ch === "-" && (i > 0 || /^-(?:[^0-9]|$)/.test(s));
    // The trailing space closes the escape; CSS eats it, so the ident still joins up.
    out += alpha || (digit && i > 0) || dash ? ch : `\\${code.toString(16)} `;
  }
  return out;
}

/** A hash value may be any name, including one starting with a digit (`#0f0`). */
function escapeHash(name) {
  let out = "";
  for (const ch of String(name)) {
    out += /[a-zA-Z0-9_-]/.test(ch) || ch.charCodeAt(0) >= 0x80 ? ch : `\\${ch.codePointAt(0).toString(16)} `;
  }
  return out;
}

/**
 * Quote a string. `<`, `>` and `&` are escaped alongside the quote and the
 * backslash: the sheet is serialised back through innerHTML, where `</style` is
 * the one token that would end the element early.
 */
function escapeString(value) {
  let out = '"';
  for (const ch of String(value)) {
    const code = ch.codePointAt(0);
    if (ch === '"' || ch === "\\") out += `\\${ch}`;
    else if (code < 0x20 || code === 0x7f || ch === "<" || ch === ">" || ch === "&") out += `\\${code.toString(16)} `;
    else out += ch;
  }
  return `${out}"`;
}

function serializeRaw(tokens) {
  let out = "";
  for (const tk of tokens) {
    switch (tk.t) {
      case T.WS:
        out += " ";
        break;
      case T.IDENT:
        out += escapeIdent(tk.u);
        break;
      case T.FUNC:
        out += `${escapeIdent(tk.u)}(`;
        break;
      case T.URL:
        out += `url(${escapeString(tk.u)})`;
        break;
      case T.STR:
        out += escapeString(tk.u);
        break;
      case T.HASH:
        out += `#${escapeHash(tk.u)}`;
        break;
      case T.AT:
        out += `@${escapeIdent(tk.u)}`;
        break;
      case T.NUM:
        out += tk.v + (tk.u === "%" ? "%" : tk.u ? escapeIdent(tk.u) : "");
        break;
      default:
        out += tk.v ?? "";
        break;
    }
  }
  return out;
}

const serialize = (tokens) => serializeRaw(tokens).trim();

// ---- Token helpers ---------------------------------------------------------

const isSignificant = (tk) => tk.t !== T.WS;

function trimWs(tokens) {
  let start = 0;
  let end = tokens.length;
  while (start < end && tokens[start].t === T.WS) start++;
  while (end > start && tokens[end - 1].t === T.WS) end--;
  return tokens.slice(start, end);
}

/** Split on top-level commas, ignoring those inside `()` or `[]`. */
function splitOnComma(tokens) {
  const parts = [];
  let start = 0;
  let depth = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i].t;
    if (t === T.OPEN_P || t === T.OPEN_S) depth++;
    else if (t === T.CLOSE_P || t === T.CLOSE_S) depth = Math.max(0, depth - 1);
    else if (t === T.COMMA && depth === 0) {
      parts.push(tokens.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(tokens.slice(start));
  return parts;
}

/**
 * Split one nesting level into `{ prelude, block }` statements.
 *
 * An unclosed block runs to end of input rather than being discarded: a card cut
 * off mid-generation is the common case, and what it wrote so far is still
 * scoped and still allowlisted by the passes below.
 */
function splitStatements(tokens) {
  const out = [];
  let start = 0;
  let depth = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i].t;
    if (t === T.OPEN_P || t === T.OPEN_S) depth++;
    else if (t === T.CLOSE_P || t === T.CLOSE_S) depth = Math.max(0, depth - 1);
    else if (depth !== 0) continue;
    else if (t === T.SEMI) {
      const prelude = tokens.slice(start, i);
      if (prelude.some(isSignificant)) out.push({ prelude, block: null });
      start = i + 1;
    } else if (t === T.CLOSE_B) {
      start = i + 1; // a stray close: whatever preceded it is not a rule we can trust
    } else if (t === T.OPEN_B) {
      let inner = 0;
      let end = tokens.length;
      for (let j = i; j < tokens.length; j++) {
        if (tokens[j].t === T.OPEN_B) inner++;
        else if (tokens[j].t === T.CLOSE_B && --inner === 0) {
          end = j;
          break;
        }
      }
      out.push({ prelude: tokens.slice(start, i), block: tokens.slice(i + 1, end) });
      start = end + 1;
      i = end;
    }
  }
  const tail = tokens.slice(start);
  if (tail.some(isSignificant)) out.push({ prelude: tail, block: null });
  return out;
}

/** Strip a vendor prefix so one allowlist entry covers every spelling. */
function unprefixed(name) {
  return String(name)
    .toLowerCase()
    .replace(/^-(?:webkit|moz|ms|o|khtml|epub|apple)-/, "");
}

// ---- Policy ----------------------------------------------------------------

/**
 * Properties allowed verbatim, after vendor-prefix stripping. Longhands matter:
 * engines expand shorthands, and a shorthand is only as safe as its parts.
 */
const CSS_PROP_EXACT = new Set([
  "accent-color",
  "all",
  "animation",
  "appearance",
  "aspect-ratio",
  "backdrop-filter",
  "backface-visibility",
  "background",
  "baseline-shift",
  "block-size",
  "bottom",
  "box-decoration-break",
  "box-orient",
  "box-shadow",
  "box-sizing",
  "break-after",
  "break-before",
  "break-inside",
  "caption-side",
  "caret-color",
  "clear",
  "clip-path",
  "clip-rule",
  "color",
  "color-interpolation",
  "color-scheme",
  "columns",
  "contain",
  "content",
  "content-visibility",
  "counter-increment",
  "counter-reset",
  "counter-set",
  "cursor",
  "direction",
  "display",
  "dominant-baseline",
  "empty-cells",
  "fill",
  "fill-opacity",
  "fill-rule",
  "filter",
  "float",
  "font",
  "forced-color-adjust",
  "gap",
  "height",
  "hyphenate-character",
  "hyphens",
  "image-orientation",
  "image-rendering",
  "initial-letter",
  "inline-size",
  "inset",
  "isolation",
  "left",
  "letter-spacing",
  "line-break",
  "line-clamp",
  "line-height",
  "marker",
  "marker-end",
  "marker-mid",
  "marker-start",
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
  "orphans",
  "osx-font-smoothing",
  "paint-order",
  "perspective",
  "perspective-origin",
  "pointer-events",
  "position",
  "print-color-adjust",
  "quotes",
  "resize",
  "right",
  "rotate",
  "scale",
  "shape-image-threshold",
  "shape-margin",
  "shape-outside",
  "shape-rendering",
  "stop-color",
  "stop-opacity",
  "tab-size",
  "table-layout",
  "tap-highlight-color",
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
  "vector-effect",
  "vertical-align",
  "visibility",
  "white-space",
  "white-space-collapse",
  "widows",
  "width",
  "will-change",
  "word-break",
  "word-spacing",
  "writing-mode",
  "z-index",
  "zoom",
]);

/** Families allowed wholesale, longhands and shorthands alike. */
const CSS_PROP_PREFIXES = [
  "align-",
  "anchor-",
  "animation-",
  "background-",
  "border",
  "column-",
  "contain-intrinsic-",
  "container",
  "flex",
  "font-",
  "grid",
  "inset-",
  "justify-",
  "list-style",
  "margin",
  "mask",
  "offset",
  "outline",
  "overflow",
  "overscroll-",
  "padding",
  "place-",
  "position-",
  "row-gap",
  "ruby-",
  "scroll",
  "shape-",
  "stroke",
  "text-",
  "transition-",
];

/**
 * Extension points, not styling. Named even though nothing on the lists above
 * reaches them, because vendor-prefix stripping is a generic rule and these are
 * exactly the names it must not be allowed to normalise into one.
 */
const CSS_PROP_DENY = new Set(["behavior", "binding", "link", "link-source", "expression"]);

/** `animation` is the one property whose value is renamed wholesale (see below). */
const ANIMATION_PROPS = new Set(["animation", "animation-name"]);

/**
 * Functions allowed in a value, after vendor-prefix stripping. An allowlist
 * rather than a denylist: this is what makes `url()` safe to permit at all,
 * since anything the list does not name -- `expression()`, `element()`,
 * tomorrow's escape hatch -- drops the declaration.
 */
const CSS_FN_ALLOW = new Set([
  // math
  "abs",
  "acos",
  "asin",
  "atan",
  "atan2",
  "calc",
  "clamp",
  "cos",
  "exp",
  "hypot",
  "log",
  "max",
  "min",
  "mod",
  "pow",
  "rem",
  "round",
  "sign",
  "sin",
  "sqrt",
  "tan",
  // colour
  "color",
  "color-mix",
  "device-cmyk",
  "hsl",
  "hsla",
  "hwb",
  "lab",
  "lch",
  "light-dark",
  "oklab",
  "oklch",
  "rgb",
  "rgba",
  // images
  "conic-gradient",
  "cross-fade",
  "image",
  "image-set",
  "linear-gradient",
  "radial-gradient",
  "repeating-conic-gradient",
  "repeating-linear-gradient",
  "repeating-radial-gradient",
  "src",
  "url",
  // transforms
  "matrix",
  "matrix3d",
  "perspective",
  "rotate",
  "rotate3d",
  "rotatex",
  "rotatey",
  "rotatez",
  "scale",
  "scale3d",
  "scalex",
  "scaley",
  "scalez",
  "skew",
  "skewx",
  "skewy",
  "translate",
  "translate3d",
  "translatex",
  "translatey",
  "translatez",
  // filters
  "blur",
  "brightness",
  "contrast",
  "drop-shadow",
  "grayscale",
  "hue-rotate",
  "invert",
  "opacity",
  "saturate",
  "sepia",
  // shapes and paths
  "circle",
  "ellipse",
  "inset",
  "path",
  "polygon",
  "rect",
  "shape",
  "xywh",
  // layout
  "fit-content",
  "minmax",
  "repeat",
  // substitution and generated content
  "attr",
  "counter",
  "counters",
  "env",
  "symbols",
  "var",
  // easing
  "cubic-bezier",
  "linear",
  "steps",
  // fonts
  "format",
  "local",
  "tech",
  // anchor positioning
  "anchor",
  "anchor-size",
]);

/** Functions whose string argument is fetched as a URL rather than read as text. */
const URL_STRING_FNS = new Set(["url", "src", "image-set", "image"]);

/** Delimiters a value may contain. Everything else drops the declaration. */
const VALUE_DELIMS = new Set(["/", "+", "-", "*", "%", ".", "!"]);

/** Delimiters a selector may contain, `&` included for nesting. */
const SELECTOR_DELIMS = new Set([".", "*", ">", "+", "~", "|", "^", "$", "=", "&"]);

/** Delimiters an `@media`/`@supports`/`@container` prelude may contain. */
const CONDITION_DELIMS = new Set(["<", ">", "=", "/", "+", "-", "*", ".", "%"]);

/** Functions a conditional prelude may contain. None of them apply styling. */
const CONDITION_FNS = new Set(["selector", "style", "scroll-state", "font-tech", "font-format", "supports"]);

const CSS_AT_CONDITIONAL = new Set(["media", "supports", "container"]);

/** `@font-face` descriptors. `src` lives here rather than in the property list. */
const FONT_FACE_DESCRIPTORS = new Set([
  "ascent-override",
  "descent-override",
  "font-display",
  "font-family",
  "font-feature-settings",
  "font-language-override",
  "font-named-instance",
  "font-stretch",
  "font-style",
  "font-variant",
  "font-variation-settings",
  "font-weight",
  "line-gap-override",
  "size-adjust",
  "src",
  "unicode-range",
]);

const PROPERTY_DESCRIPTORS = new Set(["syntax", "inherits", "initial-value"]);

const COUNTER_STYLE_DESCRIPTORS = new Set([
  "additive-symbols",
  "fallback",
  "negative",
  "pad",
  "prefix",
  "range",
  "speak-as",
  "suffix",
  "symbols",
  "system",
]);

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
  "important",
  "infinite",
  "inherit",
  "initial",
  "linear",
  "none",
  "normal",
  "paused",
  "reverse",
  "revert",
  "revert-layer",
  "running",
  "step-end",
  "step-start",
  "unset",
]);

/**
 * `data:` payloads a card may point CSS at. Images and fonts only: both are
 * parsed in a context that cannot run script, and an SVG loaded as an image is
 * one of them.
 */
const DATA_URL_RE =
  /^data:(?:image\/[a-z0-9.+-]+|font\/[a-z0-9.+-]+|application\/(?:font-woff2?|x-font-[a-z0-9.+-]+|vnd\.ms-fontobject|octet-stream))[;,]/i;

const SCHEME_RE = /^([a-zA-Z][a-zA-Z0-9+.-]*):/;

/**
 * Decide whether a URL may be fetched from card CSS.
 *
 * The check runs on the decoded URL with whitespace and control characters
 * removed, because that is what a URL parser resolves: `\6a avascript:` and
 * `java\nscript:` are both `javascript:` by the time anything acts on them.
 */
function isSafeUrl(url) {
  if (typeof url !== "string") return false;
  // biome-ignore lint/suspicious/noControlCharactersInRegex: matching what a URL parser strips is the point.
  const probe = url.replace(/[\u0000-\u0020\u007f-\u009f]/g, "");
  if (!probe) return false;
  if (probe.startsWith("#")) return true; // an in-document reference, resolved below
  const scheme = SCHEME_RE.exec(probe);
  if (!scheme) return true; // relative, so same origin as the app itself
  const name = scheme[1].toLowerCase();
  if (name === "http" || name === "https") return true;
  return name === "data" && DATA_URL_RE.test(probe);
}

function isAllowedProp(prop) {
  if (prop.startsWith("--")) return /^--[\w-]+$/.test(prop);
  if (!/^-?[a-z][a-z0-9-]*$/.test(prop)) return false;
  const base = unprefixed(prop);
  if (CSS_PROP_DENY.has(base)) return false;
  return CSS_PROP_EXACT.has(base) || CSS_PROP_PREFIXES.some((p) => base.startsWith(p));
}

// ---- Scoped names ----------------------------------------------------------
// A name declared at the top of a sheet is global to the document. Each one is
// renamed into the message's scope, so a card's `pulse` is its own and the app's
// stays the app's.

/** Every global name a sheet declares, collected before anything is emitted. */
export function emptyNames() {
  return { fonts: new Map(), props: new Map(), counters: new Map() };
}

function scopedName(ctx, name) {
  return `${ctx.scope}-${name}`;
}

function scopedProp(ctx, name) {
  return `--${ctx.scope}-${name.slice(2)}`;
}

/** Rewrite one class token in a class *attribute*, sparing Orb's own vocabulary. */
export function scopeClassName(token) {
  return token.startsWith("custom-") ? token : `custom-${token}`;
}

// ---- Values ----------------------------------------------------------------

/** Split `!important` off a value so the rewrites below never rename it. */
function splitImportant(tokens) {
  const value = trimWs(tokens);
  const last = value.length - 1;
  if (last < 1) return { value, important: false };
  if (value[last].t !== T.IDENT || value[last].u.toLowerCase() !== "important") return { value, important: false };
  const bang = value[last - 1].t === T.WS ? last - 2 : last - 1;
  if (bang < 0 || value[bang].t !== T.DELIM || value[bang].v !== "!") return { value, important: false };
  return { value: trimWs(value.slice(0, bang)), important: true };
}

/**
 * Walk a value and reject anything the policy does not name: an unknown
 * function, a URL that would reach somewhere it should not, a token that could
 * end the declaration early.
 */
function valueIsAllowed(tokens) {
  const open = [];
  for (const tk of tokens) {
    switch (tk.t) {
      case T.AT:
      case T.OPEN_B:
      case T.CLOSE_B:
      case T.SEMI:
      case T.COLON:
      case T.BAD:
        return false;
      case T.OPEN_P:
      case T.OPEN_S:
        open.push("");
        break;
      case T.CLOSE_P:
      case T.CLOSE_S:
        if (!open.length) return false;
        open.pop();
        break;
      case T.URL:
        if (!isSafeUrl(tk.u)) return false;
        break;
      case T.FUNC: {
        const name = unprefixed(tk.u);
        if (!CSS_FN_ALLOW.has(name)) return false;
        open.push(name);
        break;
      }
      case T.STR:
        // Inside these a bare string *is* a URL: `image-set("..." 1x)` fetches it,
        // and so does the quoted form of `url()`. Elsewhere a string is content.
        if (URL_STRING_FNS.has(open[open.length - 1]) && !isSafeUrl(tk.u)) return false;
        break;
      case T.DELIM:
        if (!VALUE_DELIMS.has(tk.v)) return false;
        break;
      default:
        break;
    }
  }
  return open.length === 0;
}

/**
 * Point a fragment URL at the id the sanitiser actually wrote, so a card's own
 * `filter: url(#glow)` still finds the `<filter>` it shipped.
 */
function rewriteUrls(tokens) {
  let inUrlFn = false;
  return tokens.map((tk) => {
    if (tk.t === T.WS) return tk;
    if (tk.t === T.URL) {
      inUrlFn = false;
      return tk.u.startsWith("#") ? { t: T.URL, u: `#user-content-${tk.u.slice(1)}` } : tk;
    }
    if (tk.t === T.FUNC) {
      const name = unprefixed(tk.u);
      inUrlFn = name === "url" || name === "src";
      return tk;
    }
    // Only a URL's own argument, so `content: "#tag"` stays the text it is.
    const rewrite = inUrlFn && tk.t === T.STR && tk.u.startsWith("#");
    inUrlFn = false;
    return rewrite ? { t: T.STR, u: `#user-content-${tk.u.slice(1)}` } : tk;
  });
}

/**
 * Rename the animation names in a value.
 *
 * Every top-level identifier that is not a keyword is renamed, including one the
 * card never defined -- an unknown animation name does nothing, but an unrenamed
 * `pulse` would drive the app's own keyframes.
 */
function rewriteAnimation(tokens, ctx) {
  let depth = 0;
  return tokens.map((tk) => {
    if (tk.t === T.OPEN_P || tk.t === T.FUNC) depth++;
    else if (tk.t === T.CLOSE_P) depth--;
    if (tk.t !== T.IDENT || depth > 0) return tk;
    if (tk.u.startsWith("--") || CSS_ANIMATION_KEYWORDS.has(tk.u.toLowerCase())) return tk;
    return { t: T.IDENT, u: scopedName(ctx, tk.u) };
  });
}

/**
 * Rename references to the names this sheet declared: registered properties and
 * counter styles. Only declared names are touched, so `var(--accent)` still
 * reads the app's theme token and `list-style-type: disc` still means `disc`.
 */
function rewriteDeclaredNames(tokens, ctx) {
  const { props, counters } = ctx.names;
  if (!props.size && !counters.size) return tokens;
  return tokens.map((tk) => {
    if (tk.t !== T.IDENT) return tk;
    if (tk.u.startsWith("--")) return props.has(tk.u) ? { t: T.IDENT, u: props.get(tk.u) } : tk;
    return counters.has(tk.u) ? { t: T.IDENT, u: counters.get(tk.u) } : tk;
  });
}

/**
 * The family name of one comma group is its trailing run of words, which is
 * true of `font-family: My Font` and of the `font` shorthand's tail alike.
 */
function familyRun(group) {
  const tokens = trimWs(group);
  let start = tokens.length;
  for (let i = tokens.length - 1; i >= 0; i--) {
    const t = tokens[i].t;
    if (t === T.IDENT || t === T.STR || t === T.WS) start = i;
    else break;
  }
  const run = trimWs(tokens.slice(start));
  if (!run.length) return null;
  const name = run
    .filter(isSignificant)
    .map((tk) => tk.u)
    .join(" ");
  return { start, name };
}

/** Point a family reference at the `@font-face` this sheet renamed. */
function rewriteFontFamily(tokens, ctx) {
  if (!ctx.names.fonts.size) return tokens;
  const groups = splitOnComma(tokens).map((group) => {
    const run = familyRun(group);
    if (!run) return group;
    const renamed = ctx.names.fonts.get(run.name);
    if (!renamed) return group;
    const head = trimWs(group).slice(0, run.start);
    return head.length ? [...head, { t: T.WS, v: " " }, { t: T.STR, u: renamed }] : [{ t: T.STR, u: renamed }];
  });
  const out = [];
  for (const group of groups) {
    if (out.length) out.push({ t: T.COMMA, v: "," }, { t: T.WS, v: " " });
    out.push(...trimWs(group));
  }
  return out;
}

/** Emit one declaration, or "" when the policy does not allow it. */
function emitDeclaration(tokens, ctx, allowProp) {
  let depth = 0;
  let colon = -1;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i].t;
    if (t === T.OPEN_P || t === T.OPEN_S) depth++;
    else if (t === T.CLOSE_P || t === T.CLOSE_S) depth--;
    else if (t === T.COLON && depth === 0) {
      colon = i;
      break;
    }
  }
  if (colon < 0) return "";
  const head = trimWs(tokens.slice(0, colon));
  // Exactly one identifier: a comment or a stray token in here is not a property.
  if (head.length !== 1 || head[0].t !== T.IDENT) return "";
  const custom = head[0].u.startsWith("--");
  const prop = custom ? head[0].u : head[0].u.toLowerCase();
  if (!allowProp(prop)) return "";

  const { value, important } = splitImportant(tokens.slice(colon + 1));
  if (!value.length || !valueIsAllowed(value)) return "";

  let out = rewriteDeclaredNames(rewriteUrls(value), ctx);
  const base = custom ? prop : unprefixed(prop);
  if (ANIMATION_PROPS.has(base)) out = rewriteAnimation(out, ctx);
  if (base === "font" || base === "font-family") out = rewriteFontFamily(out, ctx);

  const name = custom && ctx.names.props.has(prop) ? ctx.names.props.get(prop) : prop;
  const text = serialize(out);
  if (!text) return "";
  return `${escapeIdent(name)}: ${text}${important ? " !important" : ""}`;
}

/** Re-emit only the declarations the policy allows. Used for `style` attributes. */
export function filterDeclarations(text, scope, names) {
  if (!text) return "";
  const ctx = { scope: scope || "", names: names || emptyNames(), emitted: 0 };
  const kept = [];
  for (const statement of splitStatements(tokenize(text))) {
    if (statement.block !== null) continue;
    const decl = emitDeclaration(statement.prelude, ctx, isAllowedProp);
    if (decl) kept.push(decl);
  }
  return kept.join("; ");
}

// ---- Selectors -------------------------------------------------------------

const isCombinator = (tk) => tk.t === T.DELIM && (tk.v === ">" || tk.v === "+" || tk.v === "~");

/** A selector may not carry anything that could end the rule or reach the network. */
function selectorIsAllowed(tokens) {
  let depth = 0;
  for (const tk of tokens) {
    if (tk.t === T.AT || tk.t === T.OPEN_B || tk.t === T.CLOSE_B || tk.t === T.SEMI || tk.t === T.BAD) return false;
    if (tk.t === T.URL) return false;
    if (tk.t === T.FUNC && unprefixed(tk.u) === "url") return false;
    if (tk.t === T.DELIM && !SELECTOR_DELIMS.has(tk.v)) return false;
    if (tk.t === T.OPEN_P || tk.t === T.OPEN_S || tk.t === T.FUNC) depth++;
    else if (tk.t === T.CLOSE_P || tk.t === T.CLOSE_S) depth--;
    if (depth < 0) return false;
  }
  // An unclosed `:is(` would swallow the rules written after it, since the
  // browser reads on looking for the `)` this file never emitted.
  return depth === 0;
}

/**
 * Namespace the classes and ids a selector names.
 *
 * Orb's own vocabulary gets no exemption: card CSS naming `.quoted` must not
 * reach the prose chrome formatProse emits. Ids follow the rewrite DOMPurify's
 * SANITIZE_NAMED_PROPS performs, or the selector would silently stop matching.
 */
function rewriteSelectorTokens(tokens) {
  const out = [];
  for (let i = 0; i < tokens.length; i++) {
    const tk = tokens[i];
    if (tk.t === T.HASH) {
      out.push({ t: T.HASH, u: `user-content-${tk.u}` });
      continue;
    }
    if (tk.t === T.DELIM && tk.v === "." && tokens[i + 1]?.t === T.IDENT) {
      out.push(tk, { t: T.IDENT, u: scopeClassName(tokens[i + 1].u) });
      i++;
      continue;
    }
    out.push(tk);
  }
  return out;
}

function serializeWithParent(tokens, parent) {
  let out = "";
  for (const tk of tokens) {
    out += tk.t === T.DELIM && tk.v === "&" ? parent : serializeRaw([tk]);
  }
  return out.trim();
}

/**
 * Prefix a selector with this message's scope.
 *
 * The prefix ends in a descendant combinator, so every rule's subject is inside
 * the message's own wrapper -- which is also why the wrapper itself can never be
 * targeted, and why its containment cannot be styled away.
 */
function scopeSelector(prelude, ctx) {
  const parts = [];
  for (const raw of splitOnComma(prelude)) {
    const part = trimWs(raw);
    if (!part.length) continue;
    if (!selectorIsAllowed(part)) return "";
    // `~ .x` after the prefix would reach the wrapper's siblings, not its children.
    if (isCombinator(part[0])) continue;
    // At the top of a sheet there is no parent for `&` to mean, and leaving one
    // in the output would emit a selector no engine can read.
    if (part.some((tk) => tk.t === T.DELIM && tk.v === "&")) continue;
    const text = serialize(rewriteSelectorTokens(part));
    if (text) parts.push(`.msg-body .${ctx.scope} ${text}`);
  }
  return parts.join(", ");
}

/** Resolve a nested selector against the parent rule it was written inside. */
function nestSelector(prelude, parent) {
  const wrapped = `:is(${parent})`;
  const parts = [];
  for (const raw of splitOnComma(prelude)) {
    const part = trimWs(raw);
    if (!part.length) continue;
    if (!selectorIsAllowed(part)) return "";
    const rewritten = rewriteSelectorTokens(part);
    const relative = rewritten.some((tk) => tk.t === T.DELIM && tk.v === "&");
    const text = serializeWithParent(rewritten, wrapped);
    if (text) parts.push(relative ? text : `${wrapped} ${text}`);
  }
  return parts.join(", ");
}

// ---- Rules -----------------------------------------------------------------

const MAX_DEPTH = 8;

/**
 * Two budgets, both about a sheet that costs more to scope than to write.
 * Nesting comma lists multiplies the resolved selector at every level -- ten
 * parts eight deep is 10^8 characters of output from a few hundred of input --
 * so a resolved selector past the cap drops its rule, and the sheet as a whole
 * stops once it has produced more than a message could plausibly want.
 */
const MAX_SELECTOR_CHARS = 4096;
const MAX_OUTPUT_CHARS = 256 * 1024;

/**
 * Emit a block: its own declarations, then the rules nested inside it.
 *
 * `parent` is the already-scoped selector the block belongs to, or "" at the top
 * of a sheet -- where a bare declaration is not CSS and is dropped.
 */
function emitBody(tokens, ctx, parent, depth) {
  if (depth > MAX_DEPTH) return { decls: "", rules: "" };
  const decls = [];
  let rules = "";
  for (const statement of splitStatements(tokens)) {
    if (ctx.emitted > MAX_OUTPUT_CHARS) break;
    const head = statement.prelude.find(isSignificant);
    if (head?.t === T.AT) {
      rules += emitAtRule(statement, ctx, parent, depth);
      continue;
    }
    if (statement.block === null) {
      if (!parent) continue;
      const decl = emitDeclaration(statement.prelude, ctx, isAllowedProp);
      if (decl) decls.push(decl);
      continue;
    }
    const selector = parent ? nestSelector(statement.prelude, parent) : scopeSelector(statement.prelude, ctx);
    if (!selector || selector.length > MAX_SELECTOR_CHARS) continue;
    const inner = emitBody(statement.block, ctx, selector, depth + 1);
    if (inner.decls) rules += `${selector} { ${inner.decls} }\n`;
    rules += inner.rules;
    ctx.emitted += rules.length;
  }
  return { decls: decls.join("; "), rules };
}

/** Emit a declaration-only block: a keyframe stop, or an at-rule's descriptors. */
function emitDeclarationList(tokens, ctx, allowProp) {
  const kept = [];
  for (const statement of splitStatements(tokens)) {
    if (statement.block !== null) continue;
    const decl = emitDeclaration(statement.prelude, ctx, allowProp);
    if (decl) kept.push(decl);
  }
  return kept;
}

function emitDescriptors(tokens, ctx, allowed) {
  return emitDeclarationList(tokens, ctx, (prop) => allowed.has(unprefixed(prop)));
}

/** Copy a conditional prelude, which may test the page but never style it. */
function emitCondition(tokens) {
  const prelude = trimWs(tokens);
  if (!prelude.length) return "";
  let depth = 0;
  for (const tk of prelude) {
    switch (tk.t) {
      case T.AT:
      case T.OPEN_B:
      case T.CLOSE_B:
      case T.SEMI:
      case T.URL:
      case T.BAD:
        return "";
      case T.OPEN_P:
        depth++;
        break;
      case T.CLOSE_P:
        depth--;
        break;
      case T.FUNC:
        if (!CONDITION_FNS.has(unprefixed(tk.u))) return "";
        depth++;
        break;
      case T.DELIM:
        if (!CONDITION_DELIMS.has(tk.v)) return "";
        break;
      default:
        break;
    }
  }
  return depth === 0 ? serialize(prelude) : "";
}

const KEYFRAME_SELECTOR_RE = /^(from|to)$/i;

function emitKeyframes(prelude, block, ctx) {
  const name = trimWs(prelude);
  if (name.length !== 1 || (name[0].t !== T.IDENT && name[0].t !== T.STR)) return "";
  let body = "";
  for (const statement of splitStatements(block)) {
    if (statement.block === null) continue;
    const stops = splitOnComma(statement.prelude).map((group) => trimWs(group));
    const ok = stops.every(
      (stop) =>
        stop.length === 1 &&
        ((stop[0].t === T.IDENT && KEYFRAME_SELECTOR_RE.test(stop[0].u)) || (stop[0].t === T.NUM && stop[0].u === "%")),
    );
    if (!ok) continue;
    const decls = emitDeclarationList(statement.block, ctx, isAllowedProp).join("; ");
    if (decls) body += `  ${stops.map((stop) => serialize(stop)).join(", ")} { ${decls} }\n`;
  }
  return body ? `@keyframes ${escapeIdent(scopedName(ctx, name[0].u))} {\n${body}}\n` : "";
}

function emitFontFace(block, ctx) {
  const decls = emitDescriptors(block, ctx, FONT_FACE_DESCRIPTORS);
  // A face with no family and no source is not a face; dropping it keeps the
  // renamed family in the sheet from resolving to nothing.
  if (!decls.some((d) => d.startsWith("font-family:")) || !decls.some((d) => d.startsWith("src:"))) return "";
  return `@font-face { ${decls.join("; ")} }\n`;
}

function emitProperty(prelude, block, ctx) {
  const name = trimWs(prelude);
  if (name.length !== 1 || name[0].t !== T.IDENT || !ctx.names.props.has(name[0].u)) return "";
  const decls = emitDescriptors(block, ctx, PROPERTY_DESCRIPTORS);
  if (!decls.length) return "";
  return `@property ${escapeIdent(ctx.names.props.get(name[0].u))} { ${decls.join("; ")} }\n`;
}

function emitCounterStyle(prelude, block, ctx) {
  const name = trimWs(prelude);
  if (name.length !== 1 || name[0].t !== T.IDENT || !ctx.names.counters.has(name[0].u)) return "";
  const decls = emitDescriptors(block, ctx, COUNTER_STYLE_DESCRIPTORS);
  if (!decls.length) return "";
  return `@counter-style ${escapeIdent(ctx.names.counters.get(name[0].u))} { ${decls.join("; ")} }\n`;
}

/** Rename a layer name so a card's `base` cannot re-order the app's own layers. */
function emitLayerNames(prelude, ctx) {
  const names = [];
  for (const group of splitOnComma(prelude)) {
    const part = trimWs(group);
    if (!part.length || part[0].t !== T.IDENT) return null;
    const rest = part.slice(1);
    // A dotted sub-layer keeps its tail; only the root name needs scoping.
    if (rest.some((tk) => !(tk.t === T.IDENT || (tk.t === T.DELIM && tk.v === ".")))) return null;
    names.push(escapeIdent(scopedName(ctx, part[0].u)) + serialize(rest));
  }
  return names;
}

function emitAtRule({ prelude, block }, ctx, parent, depth) {
  const head = prelude.find(isSignificant);
  const name = unprefixed(head.u);
  const rest = prelude.slice(prelude.indexOf(head) + 1);

  if (CSS_AT_CONDITIONAL.has(name)) {
    if (block === null) return "";
    const condition = emitCondition(rest);
    if (!condition) return "";
    const inner = emitBody(block, ctx, parent, depth + 1);
    // Declarations directly inside a nested conditional belong to the rule around it.
    const body = (inner.decls && parent ? `${parent} { ${inner.decls} }\n` : "") + inner.rules;
    return body ? `@${escapeIdent(head.u)} ${condition} {\n${body}}\n` : "";
  }
  if (block === null && name !== "layer") return "";
  switch (name) {
    case "keyframes":
      return emitKeyframes(rest, block, ctx);
    case "font-face":
      return emitFontFace(block, ctx);
    case "property":
      return emitProperty(rest, block, ctx);
    case "counter-style":
      return emitCounterStyle(rest, block, ctx);
    case "layer": {
      const names = trimWs(rest).length ? emitLayerNames(rest, ctx) : [];
      if (names === null) return "";
      if (block === null) return names.length ? `@layer ${names.join(", ")};\n` : "";
      if (names.length > 1) return "";
      const inner = emitBody(block, ctx, parent, depth + 1);
      const body = (inner.decls && parent ? `${parent} { ${inner.decls} }\n` : "") + inner.rules;
      return body ? `@layer ${names.join("")} {\n${body}}\n` : "";
    }
    default:
      // `@import` pulls in a sheet nothing here ever sees; `@page` and `@scope`
      // style things a per-message scope cannot express. All are dropped.
      return "";
  }
}

// ---- Name collection -------------------------------------------------------

/**
 * Collect the global names a sheet declares, before any of it is emitted: a
 * `font-family` may reference an `@font-face` written further down.
 */
function collectNames(tokens, ctx, depth) {
  if (depth > MAX_DEPTH) return;
  for (const statement of splitStatements(tokens)) {
    const head = statement.prelude.find(isSignificant);
    if (head?.t !== T.AT) {
      if (statement.block) collectNames(statement.block, ctx, depth + 1);
      continue;
    }
    const name = unprefixed(head.u);
    const rest = trimWs(statement.prelude.slice(statement.prelude.indexOf(head) + 1));
    if (CSS_AT_CONDITIONAL.has(name) || name === "layer") {
      if (statement.block) collectNames(statement.block, ctx, depth + 1);
      continue;
    }
    if (name === "font-face" && statement.block) {
      for (const decl of splitStatements(statement.block)) {
        const family = declaredFamily(decl.prelude);
        if (family) ctx.names.fonts.set(family, scopedName(ctx, family));
      }
      continue;
    }
    if (rest.length !== 1 || rest[0].t !== T.IDENT) continue;
    if (name === "property" && rest[0].u.startsWith("--")) ctx.names.props.set(rest[0].u, scopedProp(ctx, rest[0].u));
    else if (name === "counter-style") ctx.names.counters.set(rest[0].u, scopedName(ctx, rest[0].u));
  }
}

/** The family name a `font-family` descriptor declares, quoted or not. */
function declaredFamily(tokens) {
  let colon = -1;
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].t === T.COLON) {
      colon = i;
      break;
    }
  }
  if (colon < 0) return "";
  const head = trimWs(tokens.slice(0, colon));
  if (head.length !== 1 || head[0].t !== T.IDENT || head[0].u.toLowerCase() !== "font-family") return "";
  const value = trimWs(tokens.slice(colon + 1)).filter(isSignificant);
  if (!value.length || !value.every((tk) => tk.t === T.IDENT || tk.t === T.STR)) return "";
  return value.map((tk) => tk.u).join(" ");
}

// ---- Entry points ----------------------------------------------------------

/**
 * Compile card CSS into rules scoped to one message.
 *
 * The returned `names` is the sheet's rename table; the same table has to reach
 * the `style` attributes in that message, which are filtered separately.
 */
export function compileCss(cssText, scope) {
  if (!cssText || !scope) return { css: "", names: emptyNames() };
  const ctx = { scope, names: emptyNames(), emitted: 0 };
  try {
    const tokens = tokenize(cssText);
    collectNames(tokens, ctx, 0);
    const css = emitBody(tokens, ctx, "", 0).rules;
    // Belt and braces on top of the string escaping: the fragment is serialised
    // before it reaches innerHTML, and `</style` is the one token that ends a
    // style element's raw text on the way back in.
    return { css: css.replace(/<\/style/gi, "\\3c /style"), names: ctx.names };
  } catch {
    return { css: "", names: ctx.names };
  }
}

/** Return card CSS scoped to one message. */
export function sanitizeCss(cssText, scope) {
  return compileCss(cssText, scope).css;
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
