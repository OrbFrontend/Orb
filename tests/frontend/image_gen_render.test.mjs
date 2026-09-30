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

import {
  attachmentDetailsHtml,
  downloadButtonHtml,
  hasAttachment,
  messageButtonHtml,
  refineRun,
  refineTimelineHtml,
  viewToggleHtml,
} from "../../frontend/workflows/image_gen/render.js";

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
    MARKERS,
  );
  assert.equal(html.split(`«${HOSTILE}»`).length - 1, 5); // all five fields escaped
  assert.equal(html.split(`data-value="“${HOSTILE}”"`).length - 1, 2); // both sizing mirrors, as attributes
  // Nothing hostile survives outside a marker: strip the escaped occurrences and
  // the payload is gone entirely, so no field reached the HTML raw.
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").replaceAll(`“${HOSTILE}”`, "").includes("<script>"));
});

test("a missing attachment renders empty fields rather than throwing", () => {
  const html = attachmentDetailsHtml(undefined, MARKERS);
  assert.ok(html.includes("Render details"));
  assert.ok(!html.includes("undefined"));
});

test("the style label links back to its entry in the style editor", () => {
  // Generate → judge → edit the style → regenerate is the loop this feature lives
  // in; without the link every lap costs a hunt through settings.
  const linked = attachmentDetailsHtml({ consumption_metadata: { style_id: "anime", style_label: "Anime" } }, MARKERS);
  assert.match(linked, /data-wf-action="image_gen:editStyle"/);
  assert.match(linked, /data-style-id="“anime”"/);
  assert.ok(linked.includes("«Anime»"));

  // With no id there is nothing to open, so the label is plain text, not a dead link.
  const unlinked = attachmentDetailsHtml({ consumption_metadata: { style_label: "Anime" } }, MARKERS);
  assert.ok(!unlinked.includes("image_gen:editStyle"));
  assert.ok(unlinked.includes("«Anime»"));

  // A label-less style falls back to its id, and the backend to ComfyUI.
  const bare = attachmentDetailsHtml({ consumption_metadata: { style_id: "realistic" } }, MARKERS);
  assert.ok(bare.includes("«realistic»") && bare.includes("«External ComfyUI»"));
});

test("each prompt row is a readonly field its pencil unlocks, naming its attachment", () => {
  const html = attachmentDetailsHtml({ id: 7, consumption_metadata: {} }, MARKERS);
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
    { ...MARKERS, pending: { prompt: HOSTILE, negative_prompt: HOSTILE } },
  );
  assert.ok(!html.includes("«stored»"));
  assert.equal(html.split(`«${HOSTILE}»`).length - 1, 2);
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").replaceAll(`“${HOSTILE}”`, "").includes("<script>"));
  assert.match(html, /image-gen-pending/);
});

test("without a pending edit the stored metadata is shown unmarked", () => {
  const html = attachmentDetailsHtml({ consumption_metadata: { prompt: "stored" } }, MARKERS);
  assert.ok(html.includes("«stored»"));
  assert.ok(!html.includes("image-gen-pending"));
});

test("replay disclosure notes are shown and escaped", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { notes: [HOSTILE, "second note"] } },
    MARKERS,
  );
  assert.equal(html.split("<dt>Note</dt>").length - 1, 2);
  assert.ok(html.includes(`«${HOSTILE}»`));
  assert.ok(!html.replaceAll(`«${HOSTILE}»`, "").includes("<script>"));
});

test("the camera row names the viewpoint and the lever that chose it", () => {
  // A wrong camera is fixed in a different place depending on which lever chose it,
  // so the lever is named -- in one word, since the levers themselves are settings
  // the user set.
  const cam = (metadata) => attachmentDetailsHtml({ consumption_metadata: metadata }, MARKERS);

  const classified = cam({ pov: "first_person", pov_source: "classifier" });
  assert.ok(classified.includes("<dt>Camera</dt>"));
  assert.ok(classified.includes("«First-person»"));
  assert.ok(classified.includes("« — classifier»"));

  assert.ok(cam({ pov: "third_person", pov_source: "manual" }).includes("« — picker»"));
  // Both fallbacks read as "default": whether the classifier is installed is the
  // user's own setting, not something the image has to disclose.
  assert.ok(cam({ pov: "third_person", pov_source: "no_classifier" }).includes("« — default»"));
  assert.ok(cam({ pov: "third_person", pov_source: "default" }).includes("« — default»"));

  // An unrecognized camera falls back to its raw value, still escaped.
  const hostile = cam({ pov: HOSTILE, pov_source: HOSTILE });
  assert.ok(hostile.includes(`«${HOSTILE}»`));
  assert.ok(!hostile.replaceAll(`«${HOSTILE}»`, "").replaceAll(`« — ${HOSTILE}»`, "").includes("<script>"));

  // Absent on images generated before the camera was recorded: the row is omitted
  // rather than shown empty.
  assert.ok(!cam({ style_id: "anime" }).includes("<dt>Camera</dt>"));
});

test("a reference row names which kind of image was fed in", () => {
  // A wrong reference is the failure a user needs traced, and the useful half of
  // the origin is which *kind* of thing was fed in. The configured source policy
  // is not echoed back: that one is a setting the user picked.
  const html = attachmentDetailsHtml(
    {
      consumption_metadata: {
        references: [
          { slot: ["72", "image"], source: "previous_or_character", origin: "attachment:41" },
          { slot: ["90", "image"], source: "character", origin: "character:card-1" },
        ],
      },
    },
    MARKERS,
  );
  assert.equal(html.split("<dt>Reference</dt>").length - 1, 2); // one row per filled slot, in slot order
  assert.ok(html.includes("«previous image»"));
  assert.ok(html.includes("«character card»"));

  // A workflow with no reference slots records none, so the row is omitted.
  assert.ok(!attachmentDetailsHtml({ consumption_metadata: { style_id: "anime" } }, MARKERS).includes("<dt>Reference"));
});

test("a hostile recorded reference is escaped like every other field", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { references: [{ slot: [HOSTILE, "image"], source: HOSTILE, origin: HOSTILE }] } },
    MARKERS,
  );
  // Everything inside markers went through esc(); nothing hostile may survive outside them.
  assert.ok(!html.replaceAll(/«[^»]*»/gs, "").includes("<script>"));
});

test("selected composition skills are shown by label and escaped", () => {
  const html = attachmentDetailsHtml(
    { consumption_metadata: { composition_skills: [{ id: "hug", label: HOSTILE }, { id: "crop" }] } },
    MARKERS,
  );
  assert.ok(html.includes("<dt>Composition skills</dt>"));
  assert.ok(html.includes(`«${HOSTILE}, crop»`));
  assert.ok(!html.replaceAll(`«${HOSTILE}, crop»`, "").includes("<script>"));
  assert.ok(!attachmentDetailsHtml({ consumption_metadata: { composition_skills: [] } }, MARKERS).includes("Composition skills"));
});

// ── view toggle ─────────────────────────────────────────────────────────────

test("the info button is pressed while the details show", () => {
  const details = viewToggleHtml(false);
  assert.match(details, /data-wf-action="image_gen:toggleDetails"/);
  assert.match(details, /aria-pressed="true"/);
  assert.match(viewToggleHtml(true), /aria-pressed="false"/);
});

test("the download button names its attachment through escAttr", () => {
  const html = downloadButtonHtml({ id: 7 }, MARKERS);
  assert.match(html, /data-wf-action="image_gen:download"/);
  assert.match(html, /data-att-id="“7”"/);
  // Its own class: toggleDetails finds the info button by `.image-gen-view-btn`.
  assert.ok(!html.includes("image-gen-view-btn"));
});

// ── seedless backends and cost ───────────────────────────────────────────────

test("a normal attachment still prints its seed", () => {
  const html = attachmentDetailsHtml({ seed: "beef", consumption_metadata: {} }, MARKERS);
  assert.match(html, /<dt>Seed<\/dt><dd><code>«beef»<\/code>/);
});

test("a seedless backend says so instead of printing a meaningless hex", () => {
  // The seed is still minted and stored — rehydrate refuses a null one — so the
  // honest row is "recorded but unused", not a blank and not the hex.
  const html = attachmentDetailsHtml({ seed: "beef", consumption_metadata: { seed_honored: false } }, MARKERS);
  assert.match(html, /<dt>Seed<\/dt><dd>«not used»/);
  assert.ok(!html.includes("beef"));
});

test("cost is rendered only in the unit the payload names", () => {
  const cost = (value) => attachmentDetailsHtml({ consumption_metadata: { cost: value } }, MARKERS);

  // Never converted: nothing documents what a tick is worth, and picking a divisor
  // by omission prints a wrong number on a billing figure.
  const ticks = cost({ provider: "xai", unit: "usd_ticks", value: 1400 });
  assert.match(ticks, /1400 usd ticks/);
  assert.ok(!ticks.includes("$"));
  // An unrecognised unit still shows the value beside its own name.
  assert.match(cost({ provider: "acme", unit: "credits", value: 3 }), /3 credits/);

  // No row at all when the response reported none: a zero would read as "free".
  assert.ok(!attachmentDetailsHtml({ consumption_metadata: {} }, MARKERS).includes("<dt>Cost"));
  assert.ok(!cost({}).includes("<dt>Cost"));

  // The provider is not appended: the Backend row above already names it.
  assert.ok(!cost({ provider: "xai", unit: "usd", value: 1 }).includes("xai"));

  // And the whole cell is one escaped value, like every other field.
  const hostile = cost({ provider: HOSTILE, unit: HOSTILE, value: 1 });
  const cell = `«1 ${HOSTILE}»`;
  assert.ok(hostile.includes(cell));
  assert.ok(!hostile.replaceAll(cell, "").includes("<script>"));
});

// ── refinement timeline ─────────────────────────────────────────────────────

const render = (id, refine, review) => ({
  id,
  consumption_metadata: { ...(refine ? { refine } : {}), ...(review ? { review } : {}) },
});
const RUN = { run: "r1", turns: 3 };
const at = (n, extra = {}) => ({ ...RUN, render: n, ...extra });
const rejected = (critique) => ({ critique, done: false });

test("a run is told apart from rerolls and other runs in the same group", () => {
  const first = render(1, at(1), rejected("hands"));
  const second = render(3, at(2));
  const reroll = render(2);
  const otherRun = render(4, { run: "r2", render: 1, turns: 1, ended: "accepted" });
  const model = refineRun([first, reroll, second, otherRun], second, null);
  assert.deepEqual(
    model.rows.map((row) => row.id),
    [1, 3],
  );
  // A reroll or a pre-timeline render on show draws no timeline.
  assert.equal(refineRun([first, reroll], reroll, null), null);
  assert.equal(refineRun([render(9)], render(9), null), null);
});

test("a deleted render leaves its number missing rather than renumbering", () => {
  const model = refineRun([render(1, at(1), rejected("a")), render(5, at(3))], render(5, at(3)), null);
  assert.deepEqual(
    model.rows.map((row) => row.render),
    [1, 3],
  );
});

test("every header state is derived from the rows and the live stage", () => {
  const one = render(1, at(1));
  const header = (siblings, live) => refineRun(siblings, siblings[0] || null, live)?.header;
  const live = (stage, n) => ({ run: "r1", stage, render: n, turns: 3 });

  assert.deepEqual(header([], live("composing", 1)), { state: "live", text: "Composing prompt…" });
  assert.equal(header([], live("rendering", 1)).text, "Rendering…");
  assert.equal(header([one], live("reviewing", 1)).text, "Reviewing render 1…");
  assert.equal(
    header([render(1, at(1), rejected("x")), render(2, at(2), rejected("y"))], live("rendering", 3)).text,
    "Rendering revision 2 of up to 3…",
  );
  assert.deepEqual(header([render(1, at(1, { ended: "accepted" }), { critique: "", done: true })], null), {
    state: "accepted",
    text: "Accepted at render 1",
  });
  assert.equal(
    header([render(4, at(4, { ended: "turns_used" }))], null).text,
    "Used all 3 revisions — render 4 not reviewed",
  );
  for (const ended of ["no_review", "no_prompt", "render_failed"])
    assert.equal(header([render(2, at(2, { ended }))], null).text, "Refinement stopped at render 2");
  // No job and no ending: the user stopped it.
  assert.deepEqual(header([one], null), { state: "stopped", text: "Stopped at render 1" });
  // An ending outranks a stage that has not been cleared yet.
  assert.equal(header([render(1, at(1, { ended: "accepted" }))], live("reviewing", 1)).state, "accepted");
});

test("a live revision adds one ghost row, and a review in flight is marked", () => {
  const first = render(1, at(1), rejected("hands"));
  const ghost = refineRun([first], first, { run: "r1", stage: "rendering", render: 2, turns: 3 });
  assert.deepEqual(ghost.rows.at(-1), {
    id: null,
    render: 2,
    review: null,
    ended: null,
    verdict: "rendering",
    current: false,
    ghost: true,
  });
  const second = render(2, at(2));
  const reviewing = refineRun([first, second], second, { run: "r1", stage: "reviewing", render: 2, turns: 3 });
  assert.equal(reviewing.rows.length, 2);
  assert.equal(reviewing.rows[1].verdict, "reviewing");
});

test("the collapsed body is the shown render's review, else the newest", () => {
  const first = render(1, at(1), rejected("first"));
  const second = render(2, at(2), rejected("second"));
  const third = render(3, at(3));
  assert.equal(refineRun([first, second, third], first, null).focusRowId, 1);
  assert.equal(refineRun([first, second, third], third, null).focusRowId, 2);
});

const TIMELINE = { ...MARKERS, msgId: 7, rootId: 1 };

test("critique text is escaped and emitted in full at any length", () => {
  const long = `${"word ".repeat(399)}ends!`;
  assert.equal(long.length, 2000);
  const rows = [render(1, at(1), rejected(HOSTILE)), render(2, at(2, { ended: "accepted" }), { critique: long, done: true })];
  for (const open of [false, true]) {
    const html = refineTimelineHtml(refineRun(rows, rows[1], null), { ...TIMELINE, open });
    assert.ok(html.includes(`«${long}»`), "no text is cut on the client");
    assert.ok(!html.replaceAll(/«[^»]*»/gs, "").replaceAll(/“[^”]*”/gs, "").includes("<script>"));
  }
  const list = refineTimelineHtml(refineRun(rows, rows[1], null), { ...TIMELINE, open: true });
  assert.ok(list.includes(`«${HOSTILE}»`));
});

test("an empty critique reads as its verdict", () => {
  const rows = [
    render(1, at(1), { critique: "", done: false }),
    render(2, at(2, { ended: "accepted" }), { critique: "  ", done: true }),
  ];
  const html = refineTimelineHtml(refineRun(rows, rows[1], null), { ...TIMELINE, open: true });
  assert.ok(html.includes("«Asked for another render»"));
  assert.ok(html.includes("«Accepted»"));
});

test("only a review that asked for a new seed is tagged with one", () => {
  const rows = [
    render(1, at(1), { critique: "mangled", done: false, reseed: true }),
    render(2, at(2), { critique: "hands", done: false }),
    render(3, at(3, { ended: "accepted" }), { critique: "", done: true, reseed: true }),
  ];
  const html = refineTimelineHtml(refineRun(rows, rows[2], null), { ...TIMELINE, open: true });
  assert.equal(html.split('class="ig-refine-reseed"').length - 1, 1);
  assert.match(html, /class="ig-refine-reseed"[^>]*>New seed<\/span> «mangled»/);
});

test("the strip carries its roles, current row, and expansion state", () => {
  const rows = [render(1, at(1), rejected("a")), render(2, at(2), rejected("b"))];
  const model = refineRun(rows, rows[0], { run: "r1", stage: "rendering", render: 3, turns: 3 });
  const html = refineTimelineHtml(model, {
    ...TIMELINE,
    open: true,
    openRows: new Set([2]),
    stop: { jobId: "j1", stopping: false },
  });
  assert.match(html, /role="status" aria-live="polite"/);
  assert.equal(html.split('aria-current="true"').length - 1, 1);
  assert.match(html, /<li class="ig-refine-row" aria-current="true"><button [^>]*data-att-id="“1”"/);
  assert.match(html, /aria-label="Show render “1”"/);
  assert.match(html, /class="ig-refine-toggle"[^>]*aria-expanded="true"/);
  // Row 2 is open, row 1 is not; each names the critique it controls.
  assert.match(html, /data-att-id="“2”" aria-expanded="true" aria-controls="“ig-refine-1-c2”">Less/);
  assert.match(html, /data-att-id="“1”" aria-expanded="false" aria-controls="“ig-refine-1-c1”">More/);
  assert.match(html, /data-wf-action="image_gen:refineStop"[^>]*data-wf-job="“j1”"/);
  assert.match(html, /is-ghost/);

  const collapsed = refineTimelineHtml(model, { ...TIMELINE, stop: { jobId: "j1", stopping: true } });
  assert.match(collapsed, /class="ig-refine-toggle"[^>]*aria-expanded="false"/);
  assert.match(collapsed, /Stop here/);
  assert.match(collapsed, /refineStop"[^>]*disabled>/);
  assert.ok(!collapsed.includes("<ol"));
});
