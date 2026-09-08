import assert from "node:assert/strict";
import { test } from "node:test";
import { cssScope, sanitizeCss } from "../../frontend/message_html.js";

// Card CSS is the one grammar in a message body that the sanitiser cannot help
// with: DOMPurify sees a `<style>` element, not the sheet inside it. sanitizeCss
// is therefore the whole containment, and it is a pure string pass precisely so
// it can be pinned here rather than through a browser's CSSOM.

const SCOPE = "msg-stest";
const css = (text) => sanitizeCss(text, SCOPE);

test("every surviving selector is scoped to the one message that wrote it", () => {
  // Without the scope class, `.msg-body p` would restyle every bubble in the
  // conversation, and `*` would restyle every bubble's every node.
  for (const source of ["p { color: red }", "* { color: red }", ".a, .b { color: red }", "div > span { color: red }"]) {
    for (const rule of css(source).trim().split("\n")) {
      assert.ok(rule.startsWith(`.msg-body .${SCOPE} `), `unscoped rule from ${source}: ${rule}`);
    }
  }
});

test("a class in a selector is namespaced the same way the markup hook namespaces it", () => {
  assert.match(css(".card { color: red }"), /\.msg-body \.msg-stest \.custom-card \{/);
  // Orb's own vocabulary gets no exemption here: card CSS naming `.quoted` must
  // not reach the prose chrome formatProse emits.
  assert.match(css(".quoted { color: red }"), /\.custom-quoted \{/);
  assert.ok(!css(".quoted { color: red }").includes(" .quoted "));
});

test("an id selector follows the id the sanitiser actually wrote", () => {
  // SANITIZE_NAMED_PROPS rewrites `id="hero"` to `id="user-content-hero"`, so a
  // selector that still said `#hero` would silently stop matching.
  assert.match(css("#hero { color: gold }"), /#user-content-hero \{/);
});

test("nothing that reaches the network survives", () => {
  const leaks = [
    "@import url(https://evil.test/x.css);",
    "@import 'https://evil.test/x.css';",
    ".a { background: url(https://evil.test/pixel.png) }",
    ".a { background-image: image-set('https://evil.test/x.png' 1x) }",
    ".a { cursor: url(https://evil.test/c.cur), auto }",
    "@font-face { font-family: x; src: url(https://evil.test/f.woff) }",
    ".a { list-style-image: url(https://evil.test/b.png) }",
  ];
  for (const source of leaks) {
    const out = css(source);
    assert.ok(!/url\s*\(|image-set|@import|@font-face/i.test(out), `${source} -> ${out}`);
  }
});

test("a CSS escape cannot smuggle url() past the value check", () => {
  // `\75 rl(…)` is `url(…)` once the engine unescapes it, so a substring test on
  // the raw text is only sound because backslashes are rejected outright.
  assert.equal(css(".a { background-image: \\75 rl(https://evil.test/x.png) }"), "");
  assert.equal(css(".a { background: \\000075rl(https://evil.test/x.png) }"), "");
});

test("keyframes are renamed, so a card cannot drive the app's own animation", () => {
  // chat.css defines a global `pulse`; redefining it would re-time the director
  // badge and the streaming dots across the whole app.
  const out = css("@keyframes pulse { from { opacity: 0 } to { opacity: 1 } }");
  assert.match(out, /@keyframes msg-stest-pulse \{/);
  assert.ok(!/@keyframes pulse\b/.test(out));
});

test("an animation reference is renamed with it — including one the card never defined", () => {
  assert.match(css(".dot { animation: pulse 1.5s infinite }"), /animation: msg-stest-pulse 1\.5s infinite/);
  assert.match(css(".dot { animation-name: gen-pulse }"), /animation-name: msg-stest-gen-pulse/);
  // Keywords are not names and must survive, or the declaration stops working.
  assert.match(css(".dot { animation: 2s linear infinite alternate x }"), /2s linear infinite alternate msg-stest-x/);
});

test("global at-rules are dropped; conditional ones are kept and recursed into", () => {
  for (const source of [
    "@font-face { font-family: x }",
    "@page { margin: 0 }",
    "@property --x { syntax: '<color>' }",
    "@layer base { p { color: red } }",
    "@scope (.a) { p { color: red } }",
    "@counter-style c { system: cyclic }",
  ]) {
    assert.equal(css(source), "", `${source} should not survive`);
  }
  const media = css("@media (max-width: 600px) { .b { color: green } }");
  assert.match(media, /@media \(max-width: 600px\) \{/);
  assert.match(media, /\.msg-body \.msg-stest \.custom-b \{ color: green \}/);
  assert.match(css("@supports (display: grid) { .b { display: grid } }"), /@supports \(display: grid\)/);
});

test("declarations that would escape the bubble are dropped, not clamped silently", () => {
  assert.ok(!css(".a { position: fixed }").includes("position"));
  assert.ok(!css(".a { position: sticky }").includes("position"));
  assert.match(css(".a { position: absolute }"), /position: absolute/);
  assert.ok(!css(".a { z-index: 99999 }").includes("z-index"));
  assert.match(css(".a { z-index: 2 }"), /z-index: 2/);
});

test("only allowlisted properties survive", () => {
  assert.match(css(".a { color: red; font-size: 2em; border-radius: 4px }"), /color: red; font-size: 2em/);
  // Not on the list: behaviour hooks and anything that pulls in an external
  // resource or a browser extension point.
  for (const decl of ["behavior: url(x.htc)", "-moz-binding: url(x)", "src: url(x)", "unknown-prop: 1"]) {
    assert.equal(css(`.a { ${decl} }`), "", decl);
  }
});

test("custom properties are allowed, and carry the same value rules", () => {
  assert.match(css(".a { --tone: #fff; color: var(--tone) }"), /--tone: #fff; color: var\(--tone\)/);
  assert.equal(css(".a { --u: url(https://evil.test/x.png) }"), "");
});

test("a sheet cannot close its own style element on the way back through innerHTML", () => {
  // renderMessageHtml serialises the fragment to a string before it reaches
  // innerHTML, and `</style` is the one token that ends a style element there.
  const out = css(".a { color: red } </style><img src=x>");
  assert.ok(!/<\/style/i.test(out));
});

test("a truncated sheet still comes out contained, never half-scoped", () => {
  // A card cut off mid-generation is the common case, not the adversarial one:
  // the scanner closes the open blocks at end of input, so what it recovers is
  // still scoped and still allowlisted. What must never happen is a rule
  // escaping with its original selector.
  for (const source of ["@media (max-width: 600px) { .a { color: red }", ".a { color: red", ".a { color: red } b {"]) {
    for (const line of css(source).trim().split("\n")) {
      const rule = line.trim();
      if (!rule || rule === "}" || rule.startsWith("@")) continue;
      assert.ok(rule.startsWith(`.msg-body .${SCOPE} `), `${source} leaked: ${rule}`);
    }
  }
  // Nothing recoverable at all is dropped rather than guessed at.
  assert.equal(css("}}} .a"), "");
  assert.equal(css(""), "");
  assert.equal(sanitizeCss(".a { color: red }", ""), "");
});

test("a comment cannot hide a declaration from the allowlist", () => {
  assert.equal(css(".a { back/**/ground: url(https://evil.test/x.png) }"), "");
  assert.match(css(".a { /* note */ color: red }"), /color: red/);
});

test("a nested rule is dropped rather than emitted unscoped", () => {
  const out = css(".a { color: red; &:hover { color: blue } }");
  assert.ok(!out.includes("&"));
  assert.ok(!out.includes("blue"));
});

test("the scope is a function of the source, so equal messages render equal HTML", () => {
  // The render cache and the DOM reconciler both key on the produced markup; a
  // random scope per render would defeat each of them on every repaint.
  assert.equal(cssScope("hello"), cssScope("hello"));
  assert.notEqual(cssScope("hello"), cssScope("hello "));
  assert.match(cssScope("hello"), /^msg-s[0-9a-z]+$/);
});
