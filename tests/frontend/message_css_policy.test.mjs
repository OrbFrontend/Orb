import assert from "node:assert/strict";
import { test } from "node:test";
import { compileCss, filterDeclarations, sanitizeCss } from "../../frontend/message_css.js";

// message_css_containment.test.mjs pins what card CSS may not do. This file pins
// the other half: what it *may* do, and why each of those is safe to allow.
//
// The policy is permissive because it reads the sheet as a token stream rather
// than as text -- escapes decoded, comments elided, functions balanced -- so
// every case below is really the same claim twice: the capability works, and the
// spelling that tries to smuggle something past it does not.

const SCOPE = "msg-sx";
const css = (text) => sanitizeCss(text, SCOPE);

test("an escaped property name is the property it spells", () => {
  // Legal CSS a card can write, and the reason the allowlist cannot be a
  // substring test: both of these reach it as a decoded name.
  assert.match(css(".a { \\62 ackground-color: red }"), /background-color: red/);
  assert.equal(css(".a { \\62 ehavior: url(x.htc) }"), "");
  assert.equal(css(".a { -moz-\\62 inding: url(https://evil.test/x) }"), "");
});

test("a vendor prefix is stripped, so one allowlist entry covers every spelling", () => {
  assert.match(css(".a { -webkit-mask-image: url(https://cdn.test/m.png) }"), /-webkit-mask-image: url\(/);
  assert.match(css(".a { -webkit-line-clamp: 3; -webkit-box-orient: vertical }"), /-webkit-line-clamp: 3/);
  // The same rule is what keeps the extension points out, under any prefix.
  for (const decl of ["-moz-binding: url(https://evil.test/x)", "-ms-behavior: url(x.htc)", "behavior: url(x.htc)"]) {
    assert.equal(css(`.a { ${decl} }`), "", decl);
  }
});

test("value functions are an allowlist, which is what makes url() safe to permit", () => {
  for (const value of [
    "color: color-mix(in srgb, red 40%, blue)",
    "width: clamp(1rem, 5vw, 3rem)",
    "background: linear-gradient(90deg, #000, #fff)",
    "transform: rotate(45deg) translateX(2px)",
    "filter: blur(2px) drop-shadow(0 0 2px #000)",
    "clip-path: polygon(0 0, 100% 0, 50% 100%)",
    "grid-template-columns: repeat(3, minmax(0, 1fr))",
  ]) {
    assert.notEqual(css(`.a { ${value} }`), "", value);
  }
  // Not on the list: a legacy script hatch, and one that paints another part of
  // the page into the bubble.
  for (const value of ["width: expression(alert(1))", "background: element(#hero)", "background: -moz-element(#hero)"]) {
    assert.equal(css(`.a { ${value} }`), "", value);
  }
});

test("a web font is renamed, and only the families this sheet declared are", () => {
  const out = css(
    '@font-face { font-family: "Card Sans"; src: local("Arial"), url(https://cdn.test/c.woff2) format("woff2") }' +
      '.a { font-family: "Card Sans", Georgia, serif }' +
      ".b { font: italic bold 12px/1.4 Card Sans }",
  );
  assert.match(out, /@font-face \{ font-family: "msg-sx-Card Sans"; src: local\("Arial"\), url\("https/);
  // The reference follows the rename, in the shorthand as well as the longhand.
  assert.match(out, /\.custom-a \{ font-family: "msg-sx-Card Sans", Georgia, serif \}/);
  assert.match(out, /\.custom-b \{ font: italic bold 12px\/1\.4 "msg-sx-Card Sans" \}/);
  // A family the sheet never declared is left alone, or `serif` would break.
  assert.match(css(".a { font-family: Georgia, serif }"), /font-family: Georgia, serif/);
});

test("a font may be inlined, but only as a font", () => {
  const face = (url) => `@font-face { font-family: F; src: url(${url}) }`;
  assert.match(css(face("data:font/woff2;base64,AA")), /@font-face/);
  assert.match(css(face("data:application/font-woff2;base64,AA")), /@font-face/);
  assert.equal(css(face("data:text/html,<script>alert(1)</script>")), "");
  assert.equal(css(face("javascript:alert(1)")), "");
});

test("a registered property is renamed, and its references with it", () => {
  const out = css('@property --tone { syntax: "<color>"; inherits: false; initial-value: red } .a { color: var(--tone) }');
  assert.match(out, /@property --msg-sx-tone \{/);
  assert.match(out, /color: var\(--msg-sx-tone\)/);
  // An unregistered name still reads the app's theme tokens, which cards rely on.
  assert.match(css(".a { color: var(--accent) }"), /var\(--accent\)/);
});

test("a counter style is renamed, and its references with it", () => {
  const out = css('@counter-style tick { system: cyclic; symbols: "x" } .a { list-style-type: tick }');
  assert.match(out, /@counter-style msg-sx-tick \{/);
  assert.match(out, /list-style-type: msg-sx-tick/);
  assert.match(css(".a { list-style-type: disc }"), /list-style-type: disc/);
});

test("a fragment url follows the id rewrite, so a card's own SVG filter still resolves", () => {
  // The sanitiser rewrites `id="glow"` to `id="user-content-glow"`; a filter
  // reference that still said `#glow` would point at nothing.
  assert.match(css(".a { filter: url(#glow) }"), /filter: url\("#user-content-glow"\)/);
  assert.match(css(".a { clip-path: url(#mask) }"), /url\("#user-content-mask"\)/);
  // Only a URL's own argument: a `#` in text is text.
  assert.match(css('.a::before { content: "#tag" }'), /content: "#tag"/);
});

test("escapes survive as characters in selectors and in values", () => {
  assert.match(css(".a\\ b { color: red }"), /\.custom-a\\20 b \{/);
  assert.match(css('.a { content: "\\201C" }'), /content: "\u201c"/);
  assert.match(css(".\\31 23 { color: red }"), /\.custom-123|\\/);
});

test("a sheet cannot close its own style element, however it spells the close", () => {
  const payloads = [
    '.a { content: "</style><img src=x onerror=alert(1)>" }',
    ".a { font-family: \\3c /style\\3e x }",
    ".a { background: url(https://cdn.test/</style>) }",
    "@media (min-width: 1px) { .a\\3c /style { color: red } }",
    ".a { content: '\\3c/style\\3e' }",
  ];
  for (const source of payloads) {
    const out = css(source);
    // Not just `</style`: no raw angle bracket survives at all, in a string or
    // in a name, so there is no tag for the re-parse to find. What is left is
    // inert CSS text -- `content: "\3c img ...\3e "` renders as characters.
    assert.ok(!/[<>]/.test(out), `${source} -> ${out}`);
  }
});

test("a malformed selector is dropped rather than emitted half-open", () => {
  // An unclosed `:is(` in the output would swallow the rules written after it,
  // since the browser reads on looking for a `)` this file never wrote.
  const out = css(".a:is( { color: red } .b { color: blue }");
  assert.ok(!out.includes(":is("), out);
  assert.match(out, /\.custom-b \{ color: blue \}/);
  assert.equal(css(".a) { color: red }"), "");
});

test("a sheet that costs more to scope than to write is cut off, not chased", () => {
  // Ten selector parts nested eight deep resolves to 10^8 characters of output
  // unless the budget stops it. What matters is that it returns, and quickly.
  let source = "";
  for (let i = 0; i < 9; i++) source += ".a,.b,.c,.d,.e,.f,.g,.h,.i,.j{color:red;";
  source += "}".repeat(9);
  const started = Date.now();
  const out = css(source);
  assert.ok(Date.now() - started < 2000, `took ${Date.now() - started}ms`);
  assert.ok(out.length < 512 * 1024, `emitted ${out.length} characters`);
  for (const line of out.trim().split("\n")) {
    assert.ok(line.startsWith(".msg-body .msg-sx ") || line.startsWith(":is(.msg-body .msg-sx "), line);
  }
});

test("an inline style carries the same policy, and the sheet's renames", () => {
  const { names } = compileCss('@font-face { font-family: "Card"; src: url(https://cdn.test/c.woff2) }', SCOPE);
  // The rename table has to reach the style attributes, or a card's own font
  // would work in its stylesheet and silently not in its markup.
  assert.match(filterDeclarations('font-family: "Card"', SCOPE, names), /font-family: "msg-sx-Card"/);
  assert.match(filterDeclarations("position: fixed; z-index: 99", SCOPE), /position: fixed; z-index: 99/);
  assert.equal(filterDeclarations("background: url(javascript:alert(1))", SCOPE), "");
  assert.equal(filterDeclarations("behavior: url(x.htc)", SCOPE), "");
  // A declaration cannot end early and start a rule of its own.
  assert.ok(!filterDeclarations("color: red } .evil { position: fixed", SCOPE).includes("evil"));
});

test("pathological input terminates", () => {
  for (const source of ["{".repeat(50000), "(".repeat(50000), "\\".repeat(50000), `${"a".repeat(200000)}{color:red}`]) {
    const started = Date.now();
    css(source);
    assert.ok(Date.now() - started < 2000, `${source.slice(0, 8)}... took ${Date.now() - started}ms`);
  }
});
