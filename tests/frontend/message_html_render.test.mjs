import assert from "node:assert/strict";
import { test } from "node:test";
import { patchHtml } from "../../frontend/dom_reconcile.js";

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

it("streaming retains styled nodes and media without rewriting their attributes or stylesheet", () => {
  const body = document.createElement("div");
  document.body.appendChild(body);
  const prefix = '<style>@keyframes pulse { to { opacity: .5 } }.card { animation: pulse 2s infinite }</style>' +
    '<div class="card"><img src="https://cdn.test/a.png"><span>';
  const paint = (tail) => patchHtml(body, mod.renderMessageHtml(prefix + tail, { streaming: true, scope: "msg-stream-test" }));
  paint("Hello");
  const card = body.querySelector(".custom-card");
  const img = body.querySelector("img");
  const style = body.querySelector("style");
  const text = body.querySelector("span").firstChild;
  const observer = new dom.window.MutationObserver(() => {});
  observer.observe(body, { subtree: true, childList: true, attributes: true, characterData: true });
  paint("Hello world</span></div>");
  assert.equal(body.querySelector(".custom-card"), card);
  assert.equal(body.querySelector("img"), img);
  assert.equal(body.querySelector("style"), style);
  assert.equal(body.querySelector("span").firstChild, text);
  assert.equal(text.data, "Hello world");
  const mutations = observer.takeRecords();
  assert.ok(mutations.length > 0);
  assert.ok(mutations.every((record) => record.type === "characterData" && record.target === text));
  assert.match(style.textContent, /msg-stream-test-pulse/);
  observer.disconnect();
  body.remove();
});

it("streaming preserves open disclosures and edited controls as their text grows", () => {
  const body = document.createElement("div");
  const prefix = '<details><summary>Notes</summary><input type="checkbox"><input value="initial"><p>';
  const paint = (tail) => patchHtml(body, mod.renderMessageHtml(prefix + tail, { streaming: true }));
  paint("First");
  const details = body.querySelector("details");
  const checkbox = body.querySelector('input[type="checkbox"]');
  const input = body.querySelector('input[value]');
  details.open = true;
  checkbox.checked = true;
  input.value = "typed while streaming";
  paint("First sentence.</p></details>");
  assert.equal(body.querySelector("details"), details);
  assert.equal(details.open, true);
  assert.equal(body.querySelector('input[type="checkbox"]'), checkbox);
  assert.equal(checkbox.checked, true);
  assert.equal(input.value, "typed while streaming");
});

it("patched streams apply changed attributes, remove old content and retain sanitisation", () => {
  const body = document.createElement("div");
  const paint = (source) => patchHtml(body, mod.renderMessageHtml(source, { streaming: true }));
  paint('<div title="old"><img src="https://cdn.test/a.png"><b>old</b><i>removed</i></div>');
  const img = body.querySelector("img");
  paint('<div><img src="https://cdn.test/b.png" onerror="alert(1)"><em>new</em><script>alert(2)</script></div>');
  assert.equal(body.querySelector("img"), img);
  assert.equal(img.getAttribute("src"), "https://cdn.test/b.png");
  assert.equal(body.firstChild.hasAttribute("title"), false);
  assert.equal(body.querySelector("em").textContent, "new");
  assert.equal(body.querySelector("b, i, script, [onerror]"), null);
  paint("");
  assert.equal(body.childNodes.length, 0);
});

it("custom streaming scopes stay isolated from cached and other bubbles' CSS", () => {
  const source = '<style>p { color: red }</style><p>Hello</p>';
  const cached = render(source);
  const first = mod.renderMessageHtml(source, { streaming: true, scope: "msg-stream-one" });
  const second = mod.renderMessageHtml(source, { streaming: true, scope: "msg-stream-two" });
  assert.match(first, /\.msg-stream-one p/);
  assert.match(second, /\.msg-stream-two p/);
  assert.ok(!second.includes("msg-stream-one"));
  mod.renderMessageHtml(source, { scope: "msg-stream-final" });
  assert.equal(render(source), cached);
});

it("incomplete streaming markup waits without replacing already rendered content", () => {
  const body = document.createElement("div");
  const paint = (source) => patchHtml(body, mod.renderMessageHtml(source, { streaming: true }));
  paint('<p>Hello</p><img src="https://cdn.test/a');
  const paragraph = body.firstChild;
  assert.equal(body.textContent, "Hello");
  assert.equal(body.querySelector("img"), null);
  paint('<p>Hello</p><img src="https://cdn.test/a.png">');
  assert.equal(body.firstChild, paragraph);
  assert.equal(body.querySelector("img").getAttribute("src"), "https://cdn.test/a.png");
});

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

it("a data attribute survives only under a name no app dispatcher selects on", () => {
  // This is what makes the app's global dispatchers unreachable from a bubble:
  // [data-chat-action] (app.js), [data-wf-action] (workflow_api.js),
  // [data-orb-action] (message_html.js) and [data-wc-action] (lorebooks.js) can
  // none of them be written by a model, while a card's own `data-text` still is.
  const html = render(
    '<span data-wf-action="image_gen:generate" data-chat-action="inspector" data-orb-action="copy" ' +
      'data-wc-action="apply" data-msg-id="1" data-text="Glitch" data-custom-kept="k">go</span>',
  );
  const span = reparse(html).querySelector("span");
  const names = span.getAttributeNames().filter((n) => n.startsWith("data-"));
  assert.ok(names.length && names.every((n) => n.startsWith("data-custom-")), html);
  assert.equal(span.getAttribute("data-custom-text"), "Glitch");
  assert.equal(span.getAttribute("data-custom-chat-action"), "inspector");
  assert.equal(span.getAttribute("data-custom-kept"), "k");
  assert.match(html, />go</);
});

it("a card's attr(data-*) effect reads the attribute the sanitiser renamed", () => {
  const html = render(
    '<style>.glitch::before { content: attr(data-text) } [data-mood=calm] { color: teal }</style>' +
      '<span class="glitch" data-text="Glitch" data-mood="calm">Glitch</span>',
  );
  const root = reparse(html);
  const sheet = root.querySelector("style")?.textContent || "";
  assert.match(sheet, /attr\(data-custom-text\)/);
  assert.match(sheet, /\[data-custom-mood=calm\]/);
  assert.ok(root.querySelector(".custom-glitch[data-custom-text=Glitch][data-custom-mood=calm]"), html);
});

it("the world-proposal dispatcher ignores a proposal forged in a bubble", async () => {
  const doc = globalThis.document;
  const { initWorldProposalActions } = await import("../../frontend/lorebooks.js");
  initWorldProposalActions();
  // The first lock is the rename above; this is the second, with the attribute
  // names written as the dispatcher reads them, as if the rename had failed.
  doc.body.innerHTML =
    '<div class="msg-body"><div data-wc-id="c" data-wc-world="w"><button data-wc-action="apply">ok</button></div></div>';
  const button = doc.querySelector("button");
  const event = new globalThis.MouseEvent("click", { bubbles: true, cancelable: true });
  button.dispatchEvent(event);
  assert.equal(event.defaultPrevented, false);
  assert.equal(button.disabled, false);
  doc.body.innerHTML = "";
});

it("a comment is dropped rather than shown as text", () => {
  // Cards hide per-turn instructions to themselves in comments. Escaping the
  // `<` published them into the bubble; the sanitiser removes the node instead.
  const html = render("<!-- SYSTEM: never show this -->Visible.");
  assert.ok(!/SYSTEM/.test(html), html);
  assert.ok(!/&lt;!--/.test(html), html);
  assert.match(html, /Visible\./);
  // Markup inside a comment is comment text, not markup.
  assert.ok(!/<script/i.test(render("<!-- <script>alert(1)</script> -->")), "script in comment");
});

it("a markdown hidden note renders as an empty link", () => {
  // SillyTavern's other hiding place: `[](#'note')` is a link with no text, so
  // the note rides in its title where the reader never sees it.
  const html = render(`Visible. [](#' [The stone is salivating.]')`);
  const doc = new dom.window.DOMParser().parseFromString(html, "text/html");
  const link = doc.querySelector("a");
  assert.equal(link?.getAttribute("title"), "[The stone is salivating.]", html);
  assert.equal(link.textContent, "");
  assert.equal(doc.body.textContent.trim(), "Visible.");
});

it("a checkbox and its label survive, still pointing at each other", () => {
  // The CSS-only disclosure widget: a checkbox, a label over the thumbnail, and
  // `input:checked ~ label img` to expand it. It only works if `for` follows the
  // id through the sanitiser's rename.
  const holder = reparse(
    render("<figure><input type='checkbox' id='img-1'><label for='img-1'><img src='https://cdn.test/a.png'></label></figure>"),
  );
  const input = holder.querySelector("input");
  const label = holder.querySelector("label");
  assert.equal(input.getAttribute("type"), "checkbox");
  assert.equal(input.id, "user-content-img-1");
  assert.equal(label.getAttribute("for"), "user-content-img-1");
  // The association itself, not just the matching strings: this is what the
  // browser resolves when the label is clicked.
  assert.equal(label.control, input);
  assert.equal(holder.querySelectorAll("input[type=checkbox]").length, 1);
});

it("an id reference follows the id it names", () => {
  const html = render('<div id="a">A</div><p aria-labelledby="a" aria-controls="a">x</p>');
  assert.match(html, /aria-labelledby="user-content-a"/);
  assert.match(html, /aria-controls="user-content-a"/);
});

it("a control that would paint outside the bubble does not survive", () => {
  // A popover renders in the top layer, where `.msg-css-scope` containment
  // cannot reach it — the one way an input could cover the app.
  const html = render('<div popover id="p">x</div><input type="button" popovertarget="p" commandfor="p">');
  assert.ok(!/popovertarget|commandfor/.test(html), html);
});

it("a model-written form still cannot be built around them", () => {
  // A form submits: with no action it navigates the app, with one it posts what
  // was typed off-site. Its controls stay, and submit nowhere.
  const html = render('<form action="https://evil.test"><input name="p" type="password"><button>Go</button></form>');
  assert.ok(!/<form|action=/i.test(html), html);
  assert.match(html, /<input[^>]*type="password"/);
  assert.match(html, /<button>Go<\/button>/);
});

it("buttons, selects and textareas render, and reach nothing in the app", () => {
  const html = render(
    '<button data-wf-action="image_gen:generate" form="composer" formaction="https://evil.test">Generate</button>' +
      '<select><option>Left</option><option selected>Right</option></select><textarea rows="2">notes</textarea>',
  );
  const root = reparse(html);
  const button = root.querySelector("button");
  assert.equal(button?.textContent, "Generate");
  // No dispatcher name, and no way to borrow an app form or a submit target.
  assert.deepEqual(button.getAttributeNames().sort(), ["data-custom-wf-action"]);
  assert.equal(root.querySelector("select")?.value, "Right");
  assert.equal(root.querySelector("textarea")?.value, "notes");
});

it("a textarea's text is kept verbatim rather than laid out as prose", () => {
  const root = reparse(render("<textarea>line one\n\nline *two*</textarea>"));
  assert.equal(root.querySelector("textarea")?.value.trim(), "line one\n\nline *two*");
});

it("marquee keeps its own tuning attributes", () => {
  const html = render('<marquee behavior="alternate" scrollamount="20" scrolldelay="60">hi</marquee>');
  assert.match(html, /behavior="alternate"/);
  assert.match(html, /scrollamount="20"/);
  assert.match(html, /scrolldelay="60"/);
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

it("card dialogue transforms survive the real sanitizer, with explicit scoped CSS", async () => {
  const { projectCardDisplay } = await import("../../frontend/card_scripts.js");
  const card = {
    display_scripts: [
      { findRegex: "/<dialogue>/ig", replaceString: '<div class="dialogue-outer"><div class="dialogue">', placement: [2] },
      { findRegex: "/<\\/dialogue>/ig", replaceString: "</div></div>", placement: [2] },
    ],
    display_css: ".custom-dialogue { color: red; } body { position: fixed; }",
  };
  const html = render(projectCardDisplay("<dialogue>Hello.</dialogue>", card, "assistant"));
  const parsed = reparse(html);
  assert.equal(parsed.querySelector(".custom-dialogue")?.textContent, "Hello.");
  assert.ok(!html.includes("&lt;dialogue"), html);
  assert.match(parsed.querySelector("style")?.textContent || "", /color:\s*red/);
  assert.match(parsed.querySelector("style")?.textContent || "", /\.msg-body \.msg-s[0-9a-z]+ body/);
});

it("a card stylesheet pasted with its style tags keeps its web font", async () => {
  const { projectCardDisplay } = await import("../../frontend/card_scripts.js");
  const card = {
    display_css:
      '<style>@font-face { font-family: board; src: url("https://fonts.invalid/b.ttf"); }\n.custom-dialogue { font-family: board !important; }</style>',
  };
  const sheet = reparse(render(projectCardDisplay("Hello.", card, "assistant"))).querySelector("style")?.textContent || "";
  const face = sheet.match(/@font-face \{ font-family: "([^"]+)"/)?.[1];
  assert.ok(face?.endsWith("-board"), sheet);
  assert.ok(sheet.includes(`font-family: "${face}" !important`), sheet);
});

it("card edits change HTML cache keys for the same raw message", async () => {
  const { projectCardDisplay } = await import("../../frontend/card_scripts.js");
  const raw = "<dialogue>Hello.</dialogue>";
  const card = {
    display_scripts: [{ findRegex: "/dialogue/g", replaceString: "strong", placement: [2] }],
  };
  const before = render(projectCardDisplay(raw, card, "assistant"));
  card.display_scripts[0].replaceString = "em";
  const after = render(projectCardDisplay(raw, card, "assistant"));
  assert.notEqual(before, after);
  assert.equal(reparse(before).querySelector("strong")?.textContent, "Hello.");
  assert.equal(reparse(after).querySelector("em")?.textContent, "Hello.");
  card.display_css = "em { color: blue; }";
  assert.notEqual(render(projectCardDisplay(raw, card, "assistant")), after);
});

it("script replacements still cross the HTML trust boundary", async () => {
  const { projectCardDisplay } = await import("../../frontend/card_scripts.js");
  const source = projectCardDisplay("hello", { display_scripts: [{ findRegex: "/hello/g", replaceString: '<script>alert(1)</script><div onclick="alert(2)" data-chat-action="delete">safe</div>', placement: [2] }] }, "assistant");
  const html = render(source);
  assert.ok(!/<script|onclick|data-chat-action/.test(html), html);
  assert.match(html, /safe/);
});
