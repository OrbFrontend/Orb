import assert from "node:assert/strict";
import { test } from "node:test";
import { escapeUnknownTags, trimIncompleteMarkup } from "../../frontend/message_html.js";

// Production asks the platform (`document.createElement(name) instanceof
// HTMLUnknownElement`); the predicate is injected so this suite can pin the
// behaviour without a DOM.
const KNOWN = new Set(["b", "i", "div", "span", "img", "style", "svg", "table", "tr", "td", "script", "p"]);
const isKnownTag = (name) => KNOWN.has(name.toLowerCase());

const survivesAsText = [
  // Only `<` is escaped: a bare `>` cannot open anything on its own.
  ["*<gasp>*", "*&lt;gasp>*"],
  ["<she trails off> ok", "&lt;she trails off> ok"],
  ["<thinking>", "&lt;thinking>"],
  ["a < b", "a &lt; b"],
  ["x<3", "x&lt;3"],
  ["a <b else", "a &lt;b else"], // known name, but no `>` — not a tag
  ["10 <= 20", "10 &lt;= 20"],
  // A comment the model never closed is not yet a comment.
  ["<!-- unfinished", "&lt;!-- unfinished"],
];

test("prose that only looks like markup survives as text", () => {
  for (const [input, expected] of survivesAsText) {
    assert.equal(escapeUnknownTags(input, isKnownTag), expected, `input: ${input}`);
  }
});

test("tags the browser knows pass through untouched", () => {
  for (const input of [
    "<b>x</b>",
    '<div class="c">y</div>',
    '<img src="x.png">',
    "<table><tr><td>a</td></tr></table>",
    "<p>one</p><p>two</p>",
  ]) {
    assert.equal(escapeUnknownTags(input, isKnownTag), input);
  }
});

test("svg, style and fenced code are pass-through regions", () => {
  // SVG children are unknown to document.createElement in the HTML namespace,
  // so the region carve-out is what keeps them from being escaped.
  const svg = '<svg viewBox="0 0 10 10"><circle cx="5" r="4"/></svg>';
  assert.equal(escapeUnknownTags(svg, isKnownTag), svg);
  // `<` is legal CSS inside a media range, and formatProse still has to find
  // the block whole in order to encode it.
  const style = "<style>@media (400px < width) { .a { color: red } }</style>";
  assert.equal(escapeUnknownTags(style, isKnownTag), style);
  // formatProse escapes fenced code itself; escaping here too would double it.
  const fence = "```\n<gasp> a < b\n```";
  assert.equal(escapeUnknownTags(fence, isKnownTag), fence);
});

// A comment is markup: escaping the `<` would turn a card's hidden instructions
// into visible prose, which is exactly what the sanitiser exists to prevent.
test("comments reach the sanitiser whole", () => {
  for (const input of [
    "<!-- note -->",
    "<!-- multi\nline -->",
    "<!---->",
    // The body is not parsed, so markup and prose inside it are left alone.
    "<!-- <gasp> a < b -->",
  ]) {
    assert.equal(escapeUnknownTags(input, isKnownTag), input);
  }
  // Only the comment is passed through; prose around it is still escaped.
  assert.equal(escapeUnknownTags("<!-- n --> <gasp>", isKnownTag), "<!-- n --> &lt;gasp>");
  assert.equal(escapeUnknownTags("<gasp> <!-- n -->", isKnownTag), "&lt;gasp> <!-- n -->");
});

test("escaping resumes after a pass-through region", () => {
  assert.equal(
    escapeUnknownTags("<svg></svg> then <gasp>", isKnownTag),
    "<svg></svg> then &lt;gasp>",
  );
});

test("empty input is handled", () => {
  assert.equal(escapeUnknownTags("", isKnownTag), "");
  assert.equal(escapeUnknownTags(null, isKnownTag), "");
});

test("streaming trims only a genuinely unfinished tag", () => {
  assert.equal(trimIncompleteMarkup('hello <div class="ca'), "hello ");
  assert.equal(trimIncompleteMarkup("hello </di"), "hello ");
  // The load-bearing case: a bare `<` in prose must not hide the rest.
  assert.equal(trimIncompleteMarkup("a < b and more"), "a < b and more");
  assert.equal(trimIncompleteMarkup("x<3 still here"), "x<3 still here");
  assert.equal(trimIncompleteMarkup("done <b>bold</b>"), "done <b>bold</b>");
  // An unterminated <style> would otherwise swallow the message.
  assert.equal(trimIncompleteMarkup("text <style>.a{color:red}"), "text ");
  assert.equal(trimIncompleteMarkup("<style>.a{}</style> after"), "<style>.a{}</style> after");
  // Half a comment would stream in as escaped prose, then vanish on `-->`.
  assert.equal(trimIncompleteMarkup("text <!-- hidden not"), "text ");
  assert.equal(trimIncompleteMarkup("text <!-- hidden --> after"), "text <!-- hidden --> after");
  assert.equal(trimIncompleteMarkup(""), "");
});
