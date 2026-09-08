import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { cssScope, sanitizeCss } from "../../frontend/message_css.js";

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
  // The hook skips a value that already carries the prefix, so the rewrite has
  // to skip it too rather than prefixing it twice.
  assert.match(css("#user-content-hero { color: gold }"), /#user-content-hero \{/);
});

test("the attribute form of an id selector follows the same rewrite", () => {
  // The bug this pins: a card laying its posts out with `[id^=post-]` had every
  // one of those rules drop on the floor, because only `#post-1` was rewritten.
  assert.match(css("[id^=post-] { display: grid }"), /\[id\^="user-content-post-"\]/);
  assert.match(css('[id="hero"] { color: gold }'), /\[id="user-content-hero"\]/);
  assert.match(css("[id|=post] { color: gold }"), /\[id\|="user-content-post"\]/);
  assert.match(css("[id~=hero] { color: gold }"), /\[id~="user-content-hero"\]/);
  // `name` gets the same prefix from the same hook.
  assert.match(css("[name^=field-] { color: gold }"), /\[name\^="user-content-field-"\]/);
  // A match that reads inside or off the end of the value is already correct,
  // and prefixing it would break the rule instead of fixing it.
  assert.match(css("[id$=-footer] { color: gold }"), /\[id\$=-footer\]/);
  assert.match(css("[id*=post] { color: gold }"), /\[id\*=post\]/);
  // A presence test needs no value, and other attributes are not rewritten.
  assert.match(css("[id] { color: gold }"), /\[id\]/);
  assert.match(css("[data-x=y] { color: gold }"), /\[data-x=y\]/);
  // Whitespace, flags and quoting are all spellings of the same selector.
  assert.match(css("[ id ^= post- i ] { color: gold }"), /\[ id \^= "user-content-post-" i \]/);
  assert.match(css("[id^='post-'] { color: gold }"), /\[id\^="user-content-post-"\]/);
  // The rewrite must not lose the rest of a compound selector.
  assert.match(css(".board [id^=post-] header { color: gold }"), /\.custom-board \[id\^="user-content-post-"\] header/);
});

test("a URL is checked by scheme rather than banned outright", () => {
  // Remote CSS fetches are allowed for the same reason the sanitiser allows a
  // remote `<img src>`: a card's art and web fonts are the point of rendering it.
  // What the policy owes is that only a scheme that fetches an image or a font
  // gets through, and that it is judged on the decoded URL.
  for (const source of [
    ".a { background: url(https://cdn.test/pixel.png) }",
    ".a { cursor: url(https://cdn.test/c.cur), auto }",
    ".a { list-style-image: url(https://cdn.test/b.png) }",
    ".a { background: url(data:image/png;base64,iVBORw0KGgo=) }",
    ".a { background: url(/static/local.png) }",
  ]) {
    assert.match(css(source), /url\(/, source);
  }
  // A bare string is a URL inside `image-set()` and `image()`, and is held to
  // the same policy as one written as `url()`.
  assert.match(css(".a { background-image: image-set('https://cdn.test/x.png' 1x) }"), /image-set\("https/);
  assert.equal(css(".a { background-image: image-set('javascript:alert(1)' 1x) }"), "");
  for (const scheme of ["javascript:alert(1)", "vbscript:msgbox", "blob:https://x/y", "file:///etc/passwd", "about:blank"]) {
    assert.equal(css(`.a { background: url(${scheme}) }`), "", scheme);
    assert.equal(css(`.a { background: url("${scheme}") }`), "", `quoted ${scheme}`);
  }
  // A `data:` payload only ever decodes to an image or a font, never a document.
  assert.equal(css(".a { background: url(data:text/html,<script>alert(1)</script>) }"), "");
});

test("an @import still dies: it would pull in a sheet nothing here ever scopes", () => {
  for (const source of ["@import url(https://evil.test/x.css);", "@import 'https://evil.test/x.css';"]) {
    assert.equal(css(source), "", source);
  }
});

test("an escape is decoded before it is judged, not rejected for being one", () => {
  // `\75 rl(…)` is `url(…)` once the engine unescapes it. The tokenizer decodes
  // it the same way, so the URL policy sees the scheme the browser would see --
  // which is what lets escapes stay a capability instead of a ban.
  assert.match(css(".a { background-image: \\75 rl(https://cdn.test/x.png) }"), /url\("https:\/\/cdn\.test\/x\.png"\)/);
  assert.equal(css(".a { background: \\000075rl(javascript:alert(1)) }"), "");
  assert.equal(css(".a { background: url(\\6a avascript:alert(1)) }"), "");
  // An escape inside a value or a selector survives as the character it names.
  assert.match(css('.a { content: "\\201C" }'), /content: "\u201c"/);
  assert.match(css(".a\\.b { color: red }"), /\.custom-a\\2e b /);
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

test("a global name is renamed into the scope; what cannot be renamed is dropped", () => {
  // These at-rules all register a name in the document, so the containment is a
  // rename rather than a ban -- a card's `base` layer or `c` counter is its own.
  assert.match(css("@counter-style c { system: cyclic }"), /@counter-style msg-stest-c \{/);
  assert.match(css("@property --x { syntax: '<color>'; inherits: false }"), /@property --msg-stest-x \{/);
  assert.match(css("@layer base { p { color: red } }"), /@layer msg-stest-base \{/);
  assert.match(css("@layer a, b;"), /@layer msg-stest-a, msg-stest-b;/);
  for (const source of [
    // `@page` styles the printed page and `@scope` a subtree of it; neither has
    // a per-message spelling. `@font-face` with no `src` names a face that is
    // not one, which would leave a renamed family resolving to nothing.
    "@font-face { font-family: x }",
    "@page { margin: 0 }",
    "@scope (.a) { p { color: red } }",
  ]) {
    assert.equal(css(source), "", `${source} should not survive`);
  }
  const media = css("@media (max-width: 600px) { .b { color: green } }");
  assert.match(media, /@media \(max-width: 600px\) \{/);
  assert.match(media, /\.msg-body \.msg-stest \.custom-b \{ color: green \}/);
  assert.match(css("@supports (display: grid) { .b { display: grid } }"), /@supports \(display: grid\)/);
});

test("positioning is contained by the wrapper rather than clamped by the policy", () => {
  // `position: fixed` and a large `z-index` are allowed because .msg-css-scope
  // takes paint containment: the wrapper is the containing block for a fixed
  // descendant and opens a stacking context. The two halves are load-bearing
  // together, so the stylesheet half is asserted in the test below.
  for (const value of ["fixed", "sticky", "absolute", "relative", "static"]) {
    assert.match(css(`.a { position: ${value} }`), new RegExp(`position: ${value}`), value);
  }
  assert.match(css(".a { z-index: 99999 }"), /z-index: 99999/);
});

test("the containment the CSS policy leans on is actually in the stylesheet", () => {
  const sheet = readFileSync(fileURLToPath(new URL("../../frontend/css/chat.css", import.meta.url)), "utf8");
  const rule = /\.msg-css-scope \{([^}]*)\}/.exec(sheet);
  assert.ok(rule, "chat.css has no .msg-css-scope rule to contain card CSS with");
  // `!important` because card CSS can write `!important` too.
  assert.match(rule[1], /contain:[^;]*\bpaint\b[^;]*!important/);
  assert.match(rule[1], /isolation:\s*isolate\s*!important/);
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
  // A custom property is a token stream the engine substitutes verbatim, so it
  // is held to the same policy as the declaration it will end up in.
  assert.match(css(".a { --u: url(https://cdn.test/x.png) }"), /--u: url\("https:\/\/cdn\.test\/x\.png"\)/);
  assert.equal(css(".a { --u: url(javascript:alert(1)) }"), "");
  assert.equal(css(".a { --u: expression(alert(1)) }"), "");
  // An unregistered name still reads the app's theme tokens; that is deliberate.
  assert.match(css(".a { color: var(--accent) }"), /var\(--accent\)/);
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

test("a nested rule is resolved against its parent, never emitted unscoped", () => {
  // `&` in any position resolves to the parent, which is already anchored inside
  // the wrapper -- so the rule's subject is a descendant or a sibling of an
  // element in this message, and both of those are inside the wrapper too.
  const out = css(".a { color: red; &:hover { color: blue } .b { color: green } .c & { color: teal } }");
  assert.ok(!out.includes("&"), out);
  for (const line of out.trim().split("\n")) {
    assert.ok(line.includes(`.msg-body .${SCOPE} `), `nested rule escaped the scope: ${line}`);
  }
  assert.match(out, /^\.msg-body \.msg-stest \.custom-a \{ color: red \}/m);
  assert.match(out, /^:is\(\.msg-body \.msg-stest \.custom-a\):hover \{ color: blue \}/m);
  assert.match(out, /^:is\(\.msg-body \.msg-stest \.custom-a\) \.custom-b \{ color: green \}/m);
  // A trailing `&` makes the parent the subject; it is still inside the wrapper.
  assert.match(out, /^\.custom-c :is\(\.msg-body \.msg-stest \.custom-a\) \{ color: teal \}/m);
  // A `&` at the top of a sheet has no parent to mean, so the rule is dropped.
  assert.equal(css("& { color: red }"), "");
  assert.equal(css(".a:is(&) { color: red }"), "");
});

test("the scope is a function of the source, so equal messages render equal HTML", () => {
  // The render cache and the DOM reconciler both key on the produced markup; a
  // random scope per render would defeat each of them on every repaint.
  assert.equal(cssScope("hello"), cssScope("hello"));
  assert.notEqual(cssScope("hello"), cssScope("hello "));
  assert.match(cssScope("hello"), /^msg-s[0-9a-z]+$/);
});
