import assert from "node:assert/strict";
import { test } from "node:test";

// The render boundary, driven end to end against a real DOM: escapeUnknownTags
// -> formatProse -> DOMPurify -> chrome rebuild -> CSS containment -> serialise
// -> re-parse. Every other suite here tests one pure pass; this is the only one
// that runs the pipeline the browser actually runs, and it is where the
// confused-deputy and mXSS questions can be asked at all.
//
// jsdom is a devDependency rather than a vendored file. Without `npm install`
// there is no DOM to drive, and the file skips loudly instead of passing quietly.

let dom = null;
let failure = "";
try {
  const { JSDOM } = await import("jsdom");
  dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "https://orb.invalid/" });
} catch (e) {
  failure = e?.message || String(e);
}

let mod = null;
let utils = null;
if (dom) {
  const w = dom.window;
  // DOMPurify binds to `window` at import time, so the globals go in before the
  // dynamic import below — which is also why this module cannot import
  // message_html.js statically.
  globalThis.window = w;
  for (const name of [
    "document",
    "Node",
    "NodeFilter",
    "Element",
    "DocumentFragment",
    "HTMLElement",
    "HTMLUnknownElement",
    "HTMLImageElement",
    "DOMParser",
    "MouseEvent",
  ]) {
    // `navigator` is deliberately not in this list: Node defines its own as a
    // getter-only global, and the one thing the pipeline asks of it —
    // navigator.clipboard — is absent there, which the copy handler tolerates.
    if (w[name] !== undefined) globalThis[name] = w[name];
  }
  mod = await import("../../frontend/message_html.js");
  utils = await import("../../frontend/utils.js");
} else {
  console.error(`SKIPPED tests/frontend/message_html_render.test.mjs — jsdom is unavailable (${failure}).`);
  console.error("Run `npm install` to exercise the sanitiser, the chrome rebuild and the serialise/re-parse round trip.");
}

const it = dom ? test : test.skip;
const render = (text) => mod.renderMessageHtml(text);

/** Re-parse rendered markup the way `innerHTML` does at the call sites. */
function reparse(html) {
  const holder = globalThis.document.createElement("div");
  holder.innerHTML = html;
  return holder;
}

it("script elements and inline handlers do not survive", () => {
  const html = render('<p onclick="alert(1)">hi</p><script>alert(2)</script>');
  assert.ok(!/onclick/i.test(html), html);
  assert.ok(!/<script/i.test(html), html);
  assert.match(html, />hi</);
});

it("no data attribute survives from message source", () => {
  // This is what makes the app's three global dispatchers unreachable from a
  // bubble: [data-chat-action] (app.js), [data-wf-action] (workflow_api.js) and
  // [data-orb-action] (message_html.js) can none of them be written by a model.
  const html = render(
    '<span data-wf-action="image_gen:generate" data-chat-action="inspector" data-orb-action="copy" data-msg-id="1">go</span>',
  );
  assert.ok(!/data-/i.test(html), html);
  assert.match(html, />go</);
});

it("a model-written button is dropped, and its label stays as text", () => {
  const html = render('<button data-wf-action="image_gen:generate">Generate an image</button>');
  assert.ok(!/<button/i.test(html), html);
  assert.match(html, /Generate an image/);
});

it("an id cannot collide with one the app looks up", () => {
  const html = render('<div id="chat-messages">x</div><a name="send-btn">y</a>');
  assert.ok(!/id="chat-messages"/.test(html), html);
  assert.match(html, /id="user-content-chat-messages"/);
  assert.match(html, /name="user-content-send-btn"/);
});

it("a class the app styles cannot be forged, but Orb's prose vocabulary passes", () => {
  const html = render('<span class="code-block-btn msg-body msg-toolbar quoted">x</span>');
  const tokens = [...reparse(html).querySelector("span").classList];
  // Token by token, not by substring: `custom-code-block-btn` contains the app's
  // class name and is exactly the rename that makes it harmless.
  assert.deepEqual(tokens.sort(), ["custom-code-block-btn", "custom-msg-body", "custom-msg-toolbar", "quoted"]);
});

it("the code-block toolbar is rebuilt after sanitising, so its actions exist", () => {
  const html = render("```js\nlet a = 1;\n```");
  assert.match(html, /class="code-block-bar"/);
  assert.match(html, /data-orb-action="wrap"/);
  assert.match(html, /data-orb-action="copy"/);
  assert.match(html, /<code class="language-js">/);
});

it("a forged code-block gets Orb's toolbar, and the forged button gets nothing", () => {
  const html = render('<div class="code-block"><pre><code>x</code></pre><span class="code-block-btn">copy</span></div>');
  const root = reparse(html);
  // The forged button was renamed, so it is not a `.code-block-btn` at all.
  assert.equal(root.querySelectorAll(".code-block-btn:not([data-orb-action])").length, 0);
  assert.equal(root.querySelectorAll(".code-block-btn[data-orb-action]").length, 2);
  assert.equal(root.querySelectorAll(".custom-code-block-btn").length, 1);
});

it("card CSS arrives scoped, with nothing global left in it", () => {
  const html = render(
    '<style>@import url(https://evil.test/x.css); @keyframes pulse { to { opacity: 1 } }' +
      ".card { color: red; background: url(https://cdn.test/p.png) }</style>" +
      '<div class="card">hi</div>',
  );
  const root = reparse(html);
  const style = root.querySelector("style");
  assert.ok(style, `no <style> survived: ${html}`);
  const css = style.textContent;
  assert.ok(!/@import/i.test(css), css);
  assert.ok(!/@keyframes pulse\b/.test(css), css);
  // The card's own art is fetched; the sheet it tried to pull in is not.
  assert.match(css, /url\("https:\/\/cdn\.test\/p\.png"\)/);
  assert.match(css, /@keyframes msg-s[0-9a-z]+-pulse/);
  // Every rule reaches this message and no other.
  const scope = root.querySelector(".msg-css-scope");
  assert.ok(scope, `no scope wrapper: ${html}`);
  const scopeClass = [...scope.classList].find((c) => c.startsWith("msg-s"));
  assert.ok(scopeClass, [...scope.classList].join(" "));
  assert.match(css, new RegExp(`\\.msg-body \\.${scopeClass} \\.custom-card`));
  // The element the CSS names is inside the scope, or the rule matches nothing.
  assert.ok(scope.querySelector(".custom-card"), html);
});

it("two messages with different CSS get different scopes", () => {
  const a = reparse(render("<style>.card { color: red }</style><div class=\"card\">a</div>"));
  const b = reparse(render("<style>.card { color: blue }</style><div class=\"card\">b</div>"));
  const cls = (root) => [...root.querySelector(".msg-css-scope").classList].find((c) => c.startsWith("msg-s"));
  assert.notEqual(cls(a), cls(b));
});

it("a message with no CSS is not wrapped, so ordinary prose renders unchanged", () => {
  const html = render("just some prose");
  assert.ok(!html.includes("msg-css-scope"), html);
});

it("an inline style goes through the same allowlist as a stylesheet", () => {
  const html = render('<div style="color: red; background: url(javascript:alert(1)); font-weight: bold">x</div>');
  assert.match(html, /style="[^"]*color: red/);
  assert.match(html, /font-weight: bold/);
  assert.ok(!/url\s*\(/i.test(html), html);
});

it("an inline style alone is enough to earn the containment wrapper", () => {
  // `position: fixed` is only safe because .msg-css-scope contains it, and a
  // message can reach for it without ever writing a <style> block.
  const root = reparse(render('<div style="position: fixed; z-index: 9999">x</div>'));
  const scope = root.querySelector(".msg-css-scope");
  assert.ok(scope, root.innerHTML);
  assert.ok(scope.querySelector('[style*="position: fixed"]'), root.innerHTML);
});

it("a web font is scoped to the message that shipped it", () => {
  const root = reparse(
    render(
      '<style>@font-face { font-family: "Card"; src: url(https://cdn.test/c.woff2) format("woff2") }' +
        '.t { font-family: "Card", serif }</style><div class="t">hi</div>',
    ),
  );
  const css = root.querySelector("style").textContent;
  // The family is renamed, so a card cannot redefine a family the app uses.
  assert.match(css, /@font-face \{[^}]*font-family: "msg-s[0-9a-z]+-Card"/);
  assert.ok(!/font-family: "Card"/.test(css), css);
  assert.match(css, /\.custom-t \{ font-family: "msg-s[0-9a-z]+-Card", serif \}/);
});

it("an inline animation cannot reach one of the app's own keyframes", () => {
  const html = render('<div style="animation: pulse 1.5s infinite">x</div>');
  assert.ok(!/animation:\s*pulse\b/.test(html), html);
  assert.match(html, /animation: msg-s[0-9a-z]+-pulse/);
});

it("remote media is allowed but told not to name the page it was read on", () => {
  const html = render('<img src="https://example.test/a.png">');
  assert.match(html, /referrerpolicy="no-referrer"/);
  assert.match(html, /loading="lazy"/);
});

it("a link opens away from the app", () => {
  const html = render('<a href="https://example.test/">x</a>');
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

it("the serialised output re-parses into the same safe tree", () => {
  // The pipeline sanitises a DOM, serialises it, and the call sites re-parse it
  // through innerHTML. That round trip is where mXSS lives, so it is walked
  // here rather than trusted.
  const payloads = [
    '<math><mtext><table><mglyph><style><img src=x onerror="alert(1)">',
    '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">',
    "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
    '<form><math><mtext></form><form><mglyph><style></math><img src onerror="alert(1)">',
    "<style><style/><img src=x onerror=alert(1)>",
    '<![CDATA[><img src=x onerror="alert(1)">]]>',
    '<div style="background:url(javascript:alert(1))">x</div>',
    "```\n</code></pre><img src=x onerror=alert(1)>\n```",
  ];
  for (const payload of payloads) {
    const root = reparse(render(payload));
    assert.equal(root.querySelectorAll("script").length, 0, payload);
    assert.equal(root.querySelectorAll("iframe, object, embed, form").length, 0, payload);
    for (const el of root.querySelectorAll("*")) {
      for (const attr of el.attributes) {
        assert.ok(!attr.name.toLowerCase().startsWith("on"), `${payload} -> ${attr.name}`);
        assert.ok(!/javascript:/i.test(attr.value), `${payload} -> ${attr.name}=${attr.value}`);
      }
    }
    // Only stylesheets this module built may exist, and they are all scoped.
    for (const style of root.querySelectorAll("style")) {
      for (const line of style.textContent.split("\n")) {
        const rule = line.trim();
        if (!rule || rule === "}" || rule.startsWith("@") || rule.startsWith("  ")) continue;
        assert.ok(rule.startsWith(".msg-body .msg-s"), `${payload} -> ${rule}`);
      }
    }
  }
});

it("the delegated code-block action fires only on a button Orb built", () => {
  const doc = globalThis.document;
  mod.initMessageHtmlActions();
  const body = doc.createElement("div");
  body.className = "msg-body";
  body.innerHTML = render('<div class="code-block"><pre><code>x</code></pre><span class="code-block-btn">c</span></div>');
  doc.body.appendChild(body);

  const forged = body.querySelector(".custom-code-block-btn");
  forged.dispatchEvent(new globalThis.MouseEvent("click", { bubbles: true }));
  assert.ok(!body.querySelector(".code-block").classList.contains("wrap"));

  const real = body.querySelector('[data-orb-action="wrap"]');
  real.dispatchEvent(new globalThis.MouseEvent("click", { bubbles: true }));
  assert.ok(body.querySelector(".code-block").classList.contains("wrap"));
  assert.equal(real.getAttribute("aria-pressed"), "true");
  body.remove();
});

it("fromMessageBody tells the app's dispatchers where model markup begins", () => {
  const doc = globalThis.document;
  doc.body.innerHTML =
    '<div class="message"><div class="msg-body"><span id="inside">a</span></div>' +
    '<div class="msg-toolbar"><button id="outside">b</button></div></div>';
  assert.equal(utils.fromMessageBody(doc.getElementById("inside")), true);
  assert.equal(utils.fromMessageBody(doc.getElementById("outside")), false);
  assert.equal(utils.fromMessageBody(null), false);
  doc.body.innerHTML = "";
});

it("a message too large for the cache still renders, and renders the same way", () => {
  // The cache is bounded by characters rather than by entries — 2,000 entries of
  // 100k characters is hundreds of megabytes — and an entry past the per-entry
  // ceiling is simply not stored. What must not change is the output.
  const big = `${"x".repeat(300 * 1024)} tail`;
  const first = render(big);
  for (let i = 0; i < 50; i++) render(`message number ${i}`);
  assert.equal(render(big), first);
  // A small message takes the cached path and must round-trip identically too.
  const small = "*hello* `world`";
  assert.equal(render(small), render(small));
});
