import assert from "node:assert/strict";
import { test } from "node:test";

// Word segmentation rewrites a rendered bubble in place, wrapping every word in
// a `<span class="seg">`. That is invisible to prose, and it is not invisible to
// a message that shipped its own stylesheet: the sheet's selectors were written
// against the elements the model wrote, and the spans are new elements sitting
// between them. This suite pins the line between those two cases.
//
// jsdom is a devDependency; without `npm install` there is no DOM to rewrite,
// and the file skips loudly instead of passing quietly.

let dom = null;
let failure = "";
try {
  const { JSDOM } = await import("jsdom");
  dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "https://orb.invalid/" });
} catch (e) {
  failure = e?.message || String(e);
}

let render = null;
let segmentBody = null;
if (dom) {
  const w = dom.window;
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
    if (w[name] !== undefined) globalThis[name] = w[name];
  }
  ({ renderMessageHtml: render } = await import("../../frontend/message_html.js"));
  ({ segmentBody } = await import("../../frontend/workflow_segmentation.js"));
} else {
  console.error(`SKIPPED tests/frontend/segmentation_card_css.test.mjs — jsdom is unavailable (${failure}).`);
  console.error("Run `npm install` to exercise segmentation against a rendered bubble.");
}

const it = dom ? test : test.skip;

/** Render `text` into a detached `.msg-body`, the way the chat list does. */
function body(text) {
  const el = globalThis.document.createElement("div");
  el.className = "msg-body";
  el.innerHTML = render(text);
  return el;
}

it("plain prose is segmented, because nothing in it can notice", () => {
  const el = body("one two three");
  segmentBody(el);
  assert.equal(el.querySelectorAll(".seg").length, 3);
  assert.equal(el.dataset.segApplied, "1");
});

it("a message that ships CSS keeps the DOM the model wrote", () => {
  // The card this comes from: a `::before` on `header span` that renders the
  // thread subject. One span in the markup, so it must fire once -- and it fired
  // once per word instead, because each word had become a span of its own.
  const el = body(
    "<style>#post-1 header span::before{content:'subject'}</style>" +
      "<div id='post-1'><header><span>Anonymous</span> 12/19/26(Fri)07:06:13 No.117980402</header></div>",
  );
  const header = () => el.querySelector("#user-content-post-1 header");
  assert.equal(header().querySelectorAll("span").length, 1);
  segmentBody(el);
  assert.equal(header().querySelectorAll("span").length, 1);
  assert.equal(el.querySelectorAll(".seg").length, 0);
});

it("an inline style attribute is enough to own the markup", () => {
  // `finish` wraps for a surviving inline style too, and for the same reason:
  // the model laid this out and the layout is load-bearing.
  const el = body("<div style='display:grid'><span>a</span> <span>b</span></div>");
  segmentBody(el);
  assert.equal(el.querySelectorAll(".seg").length, 0);
});

it("HTML without a stylesheet is still ordinary prose", () => {
  // The guard is about the sheet, not about markup: with no card CSS there are
  // no author selectors for the spans to disturb.
  const el = body("<p>one two</p>");
  segmentBody(el);
  assert.equal(el.querySelectorAll(".seg").length, 2);
});

it("segmentation stays idempotent, and survives a re-render that drops the spans", () => {
  const el = body("one two");
  segmentBody(el);
  segmentBody(el);
  assert.equal(el.querySelectorAll(".seg").length, 2);
  // A re-render replaces the body but leaves the flag behind.
  el.innerHTML = render("three four five");
  segmentBody(el);
  assert.equal(el.querySelectorAll(".seg").length, 3);
});
