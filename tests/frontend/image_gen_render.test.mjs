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

test("a missing attachment renders empty fields rather than throwing", () => {
  const html = attachmentDetailsHtml(undefined, "", MARKERS);
  assert.ok(html.includes("Render details"));
  assert.ok(!html.includes("undefined"));
});

test("the style label links back to its entry in the style editor", () => {
  // Generate → judge → edit the style → regenerate is the loop this feature lives
  // in; without the link every lap costs a hunt through settings.
  const linked = attachmentDetailsHtml({ consumption_metadata: { style_id: "anime", style_label: "Anime" } }, "", MARKERS);
  assert.match(linked, /data-wf-action="image_gen:editStyle"/);
  assert.match(linked, /data-style-id="“anime”"/);
  assert.ok(linked.includes("«Anime»"));

  // With no id there is nothing to open, so the label is plain text, not a dead link.
  const unlinked = attachmentDetailsHtml({ consumption_metadata: { style_label: "Anime" } }, "", MARKERS);
  assert.ok(!unlinked.includes("image_gen:editStyle"));
  assert.ok(unlinked.includes("«Anime»"));

  // A label-less style falls back to its id, and the source to ComfyUI.
  const bare = attachmentDetailsHtml({ consumption_metadata: { style_id: "realistic" } }, "", MARKERS);
  assert.ok(bare.includes("«realistic»") && bare.includes("«External ComfyUI»"));
});

test("each prompt row is a readonly field its pencil unlocks, naming its attachment", () => {
  const html = attachmentDetailsHtml({ id: 7, consumption_metadata: {} }, "", MARKERS);
  // `change` (not click) is what commits: it fires once on blur-after-edit.
  assert.equal(html.split('data-wf-action="image_gen:savePrompt"').length - 1, 2);
  assert.equal(html.split('data-wf-on="change"').length - 1, 2);
  // fields + their pencils each name the attachment, through escAttr
  assert.equal(html.split('data-att-id="“7”"').length - 1, 4);
  assert.match(html, /data-field="prompt"/);
  assert.match(html, /data-field="negative_prompt"/);
  assert.match(html, /aria-label="Negative prompt"/); // the <dt> is not a programmatic label
  assert.equal(html.split("readonly").length - 1, 2); // both fields locked
  assert.equal(html.split('data-wf-action="image_gen:editPrompt"').length - 1, 2);
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
  // A wrong camera is fixed in a different place depending on which lever chose it,
  // so the two ways of landing on the default must read differently.
  const cam = (metadata) => attachmentDetailsHtml({ consumption_metadata: metadata }, "", MARKERS);

  const classified = cam({ pov: "first_person", pov_source: "classifier" });
  assert.ok(classified.includes("<dt>Camera</dt>"));
  assert.ok(classified.includes("«First-person»"));
  assert.ok(classified.includes("« — from the classifier»"));

  assert.ok(cam({ pov: "third_person", pov_source: "no_classifier" }).includes("« — from the default (no POV classifier)»"));
  assert.ok(cam({ pov: "third_person", pov_source: "default" }).includes("ambiguous"));

  // An unrecognized camera falls back to its raw value, still escaped.
  const hostile = cam({ pov: HOSTILE, pov_source: HOSTILE });
  assert.ok(hostile.includes(`«${HOSTILE}»`));
  assert.ok(!hostile.replaceAll(`«${HOSTILE}»`, "").replaceAll(`« — from the ${HOSTILE}»`, "").includes("<script>"));

  // Absent on images generated before the camera was recorded: the row is omitted
  // rather than shown empty.
  assert.ok(!cam({ style_id: "anime" }).includes("<dt>Camera</dt>"));
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

// ── seedless backends and cost ───────────────────────────────────────────────

test("a normal attachment still prints its seed", () => {
  const html = attachmentDetailsHtml({ seed: "beef", consumption_metadata: {} }, "", MARKERS);
  assert.match(html, /<dt>Seed<\/dt><dd><code>«beef»<\/code>/);
});

test("a seedless backend says so instead of printing a meaningless hex", () => {
  // The seed is still minted and stored — rehydrate refuses a null one — so the
  // honest row is "recorded but unused", not a blank and not the hex.
  const html = attachmentDetailsHtml({ seed: "beef", consumption_metadata: { seed_honored: false } }, "", MARKERS);
  assert.match(html, /not used by this provider/);
  assert.ok(!html.includes("beef"));
});

test("cost is rendered only in the unit the payload names", () => {
  const cost = (value) => attachmentDetailsHtml({ consumption_metadata: { cost: value } }, "", MARKERS);

  // Never converted: nothing documents what a tick is worth, and picking a divisor
  // by omission prints a wrong number on a billing figure.
  const ticks = cost({ provider: "xai", unit: "usd_ticks", value: 1400 });
  assert.match(ticks, /1400 usd ticks/);
  assert.ok(!ticks.includes("$"));
  // An unrecognised unit still shows the value beside its own name.
  assert.match(cost({ provider: "acme", unit: "credits", value: 3 }), /3 credits/);

  // No row at all when the response reported none: a zero would read as "free".
  assert.ok(!attachmentDetailsHtml({ consumption_metadata: {} }, "", MARKERS).includes("<dt>Cost"));
  assert.ok(!cost({}).includes("<dt>Cost"));

  // And the whole cell is one escaped value, like every other field.
  const hostile = cost({ provider: HOSTILE, unit: HOSTILE, value: 1 });
  const cell = `«1 ${HOSTILE} (${HOSTILE})»`;
  assert.ok(hostile.includes(cell));
  assert.ok(!hostile.replaceAll(cell, "").includes("<script>"));
});
