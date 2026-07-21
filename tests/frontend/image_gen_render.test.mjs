// Escaping + button-state fixtures for the image_gen message button and
// attachment details. Zero deps (node --test); no jsdom — render.js is DOM-free
// and takes its escapers as arguments precisely so it loads here.
//
// The escaping tests inject MARKERS rather than the real esc()/escAttr(): the
// assertion is that no interpolated value reaches the HTML unescaped, which is
// the property that actually matters and which entity-comparison would only
// check one character class of. A field added later without an escaper fails
// these tests instead of silently shipping an injection.
import assert from "node:assert/strict";
import { test } from "node:test";

import { attachmentDetailsHtml, hasAttachment, messageButtonHtml } from "../../frontend/workflows/image_gen/render.js";

const MARKERS = { esc: (v) => `«${v}»`, escAttr: (v) => `“${v}”` };
const ICON = "<svg></svg>";
const HOSTILE = '"><script>alert(1)</script>';

const assistant = (over = {}) => ({ id: 42, role: "assistant", ...over });

test("renders a Visualize button for a mutable assistant message", () => {
  const html = messageButtonHtml(assistant(), { mutable: true, icon: ICON, ...MARKERS });
  assert.match(html, /data-wf-action="image_gen:generate"/);
  assert.match(html, /data-msg-id="“42”"/); // the id goes through escAttr
  assert.ok(html.includes(ICON));
  assert.ok(!html.includes("disabled"));
});

test("another tab holding the lock yields a disabled button with no action", () => {
  const html = messageButtonHtml(assistant(), { mutable: false, icon: ICON, ...MARKERS });
  assert.match(html, /disabled/);
  assert.ok(!html.includes("data-wf-action"));
  assert.ok(!html.includes("data-msg-id"));
});

test("no button for user messages, id-less messages, or an existing image", () => {
  const cases = [
    assistant({ role: "user" }),
    assistant({ id: 0 }),
    assistant({ workflow_attachments: [{ workflow_id: "image_gen" }] }),
    null,
    undefined,
  ];
  for (const msg of cases) {
    assert.equal(messageButtonHtml(msg, { mutable: true, icon: ICON, ...MARKERS }), "");
  }
});

test("another workflow's attachment does not suppress the button", () => {
  const msg = assistant({ workflow_attachments: [{ workflow_id: "tts" }] });
  assert.equal(hasAttachment(msg), false);
  assert.match(messageButtonHtml(msg, { mutable: true, icon: ICON, ...MARKERS }), /image_gen:generate/);
});

test("render details route every metadata field through esc", () => {
  const html = attachmentDetailsHtml(
    {
      seed: HOSTILE,
      consumption_metadata: {
        style_label: HOSTILE,
        source: HOSTILE,
        prompt: HOSTILE,
        negative_prompt: HOSTILE,
      },
    },
    "<img>",
    MARKERS,
  );
  assert.ok(html.startsWith("<img>")); // defaultHtml is framework-produced, passed through
  assert.equal(html.split(`«${HOSTILE}»`).length - 1, 5); // all five fields escaped
  // Nothing hostile survives outside a marker: strip the escaped occurrences and
  // the payload is gone entirely, so no field reached the HTML raw.
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").includes("<script>"));
});

test("render details fall back to style_id and a default source", () => {
  const html = attachmentDetailsHtml({ consumption_metadata: { style_id: "realistic" } }, "", MARKERS);
  assert.ok(html.includes("«realistic»"));
  assert.ok(html.includes("«External ComfyUI»"));
});

test("a missing attachment renders empty fields rather than throwing", () => {
  const html = attachmentDetailsHtml(undefined, "", MARKERS);
  assert.ok(html.includes("Render details"));
  assert.ok(!html.includes("undefined"));
});

test("the style label links back to its entry in the style editor", () => {
  // Generate → judge → edit the style → regenerate is the loop this feature
  // lives in; without the link every lap costs a hunt through settings.
  const html = attachmentDetailsHtml({ consumption_metadata: { style_id: "anime", style_label: "Anime" } }, "", MARKERS);
  assert.match(html, /data-wf-action="image_gen:editStyle"/);
  assert.match(html, /data-style-id="“anime”"/);
  assert.ok(html.includes("«Anime»"));
});

test("a style id that no longer resolves renders as plain text, not a dead link", () => {
  const html = attachmentDetailsHtml({ consumption_metadata: { style_label: "Anime" } }, "", MARKERS);
  assert.ok(!html.includes("image_gen:editStyle"));
  assert.ok(html.includes("«Anime»"));
});

test("replay disclosure notes are shown and escaped", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { notes: [HOSTILE, "second note"] } },
    "",
    MARKERS,
  );
  assert.equal(html.split("<dt>Note</dt>").length - 1, 2);
  assert.ok(html.includes(`«${HOSTILE}»`));
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").includes("<script>"));
});
