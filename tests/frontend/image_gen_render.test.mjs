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

test("each prompt row is an editable field naming its own attachment and key", () => {
  const html = attachmentDetailsHtml({ id: 7, consumption_metadata: {} }, "", MARKERS);
  // `change` (not click) is what commits: it fires once on blur-after-edit.
  assert.equal(html.split('data-wf-action="image_gen:savePrompt"').length - 1, 2);
  assert.equal(html.split('data-wf-on="change"').length - 1, 2);
  // fields + their pencils each name the attachment, through escAttr
  assert.equal(html.split('data-att-id="“7”"').length - 1, 4);
  assert.match(html, /data-field="prompt"/);
  assert.match(html, /data-field="negative_prompt"/);
  assert.match(html, /aria-label="Negative prompt"/); // the <dt> is not a programmatic label
});

test("prompt fields are readonly until their pencil unlocks them", () => {
  const html = attachmentDetailsHtml({ id: 7, consumption_metadata: {} }, "", MARKERS);
  assert.equal(html.split("<textarea").length - 1, 2);
  assert.equal(html.split("readonly").length - 1, 2); // both fields locked
  assert.equal(html.split('data-wf-action="image_gen:editPrompt"').length - 1, 2);
});

test("a prompt long enough to wrap gets more rows, clamped", () => {
  const rows = (prompt) => Number(/rows="(\d+)"/.exec(attachmentDetailsHtml({ consumption_metadata: { prompt } }, "", MARKERS))[1]);
  assert.equal(rows(""), 2); // the floor, not zero rows
  assert.equal(rows("x".repeat(300)), 7);
  assert.equal(rows("x".repeat(9000)), 10); // clamped, not a page-long field
});

test("a pending edit is shown through esc, marked, and beats the stored prompt", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { prompt: "stored", negative_prompt: "stored neg" } },
    "",
    { ...MARKERS, pending: { prompt: HOSTILE, negative_prompt: HOSTILE } },
  );
  assert.ok(!html.includes("«stored»"));
  assert.equal(html.split(`«${HOSTILE}»`).length - 1, 2);
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").includes("<script>"));
  assert.match(html, /image-gen-pending/);
});

test("without a pending edit the stored metadata is shown unmarked", () => {
  const html = attachmentDetailsHtml({ consumption_metadata: { prompt: "stored" } }, "", MARKERS);
  assert.ok(html.includes("«stored»"));
  assert.ok(!html.includes("image-gen-pending"));
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

test("the camera row names the viewpoint and the lever that chose it", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { pov: "first_person", pov_source: "classifier" } },
    "",
    MARKERS,
  );
  assert.ok(html.includes("<dt>Camera</dt>"));
  assert.ok(html.includes("«First-person»"));
  assert.ok(html.includes("« — from the classifier»"));
});

test("the default camera says which of the two ways it got there", () => {
  const off = attachmentDetailsHtml({ consumption_metadata: { pov: "third_person", pov_source: "no_classifier" } }, "", MARKERS);
  assert.ok(off.includes("« — from the default (no POV classifier)»"));
  const ambiguous = attachmentDetailsHtml({ consumption_metadata: { pov: "third_person", pov_source: "default" } }, "", MARKERS);
  assert.ok(ambiguous.includes("ambiguous"));
});

test("an unrecognized camera falls back to its raw value, still escaped", () => {
  const html = attachmentDetailsHtml({ consumption_metadata: { pov: HOSTILE, pov_source: HOSTILE } }, "", MARKERS);
  assert.ok(html.includes(`«${HOSTILE}»`));
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").replaceAll(`« — from the ${HOSTILE}»`, "").includes("<script>"));
});

test("an image generated before the camera was recorded shows no camera row", () => {
  const html = attachmentDetailsHtml({ consumption_metadata: { style_id: "anime" } }, "", MARKERS);
  assert.ok(!html.includes("<dt>Camera</dt>"));
});

test("a reference row names the slot, the source, and where the bytes came from", () => {
  // A wrong reference is the failure a user needs traced, and the useful half of
  // the origin is which *kind* of thing was fed in.
  const html = attachmentDetailsHtml(
    {
      consumption_metadata: {
        references: [
          { slot: ["72", "image"], source: "previous_or_character", origin: "attachment:41" },
          { slot: ["90", "image"], source: "character", origin: "character:card-1" },
        ],
      },
    },
    "",
    MARKERS,
  );
  assert.ok(html.includes("<dt>Reference« #72»</dt>"));
  assert.ok(html.includes("«previous image, else character reference — from a generated image in this chat»"));
  assert.ok(html.includes("<dt>Reference« #90»</dt>"));
  assert.ok(html.includes("«character reference — from the character»"));

  // A workflow with no reference slots records none, so the row is omitted.
  assert.ok(!attachmentDetailsHtml({ consumption_metadata: { style_id: "anime" } }, "", MARKERS).includes("<dt>Reference"));
});

test("a hostile recorded reference is escaped like every other field", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { references: [{ slot: [HOSTILE, "image"], source: HOSTILE, origin: HOSTILE }] } },
    "",
    MARKERS,
  );
  // Everything inside markers went through esc(); nothing hostile may survive outside them.
  assert.ok(!html.replaceAll(/«[^»]*»/gs, "").includes("<script>"));
});
