// The group chat's identity surface. group_cast.js is string-in/string-out over
// `S` (it imports only state.js and utils.js, both DOM-free beyond `esc`), so it
// loads under node --test.
//
// What matters here is what the scene *tells* the user: who is about to answer
// and whether that choice survives the turn, what a click on a cast chip will
// actually do, and that the speaking-plan rail never paints a row with nothing
// in it.
import assert from "node:assert/strict";
import { test } from "node:test";

// `esc` escapes through a detached DOM node; the same minimal stand-in the other
// string-rendering tests install (see world_proposals.test.mjs for why it is a
// module-scope statement rather than a before() hook).
globalThis.document = {
  createElement() {
    return {
      innerHTML: "",
      set textContent(value) {
        this.innerHTML = String(value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;");
      },
    };
  },
};

import {
  CONTEXT_MODES,
  castClickSpeaksNow,
  castRailHtml,
  contextMode,
  eligibleMembers,
  joinNames,
  overrideIsOneShot,
  sceneEmptyStateHtml,
  speakingPlanHtml,
  turnMode,
} from "../../frontend/group_cast.js";
import { S } from "../../frontend/state.js";

const ARTUS = { id: "m1", display_name: "Artus", character_card_id: "c1" };
const ASSISTANT = { id: "m2", display_name: "Assistant", character_card_id: "c2" };
const NARRATOR = { id: "m3", display_name: "Narrator", member_kind: "narrator" };

function scene({ mode = "director", members = [ARTUS, ASSISTANT], pinned = null, plan = null, speaker = null } = {}) {
  S.groupCast = { members, turn_mode: mode, max_speakers: 3 };
  S.pinnedSpeakerId = pinned;
  S.speakingPlan = plan;
  S.currentSpeaker = speaker;
  S.isStreaming = false;
}

function solo() {
  S.groupCast = null;
  S.pinnedSpeakerId = null;
  S.speakingPlan = null;
  S.currentSpeaker = null;
}

test("the three stored turn modes carry user-language names", () => {
  assert.equal(turnMode("director").label, "Auto");
  assert.equal(turnMode("round_robin").label, "Rotate");
  assert.equal(turnMode("manual").label, "Choose");
  // An unknown value must not blank the settings dropdown.
  assert.equal(turnMode("nonsense").label, "Auto");
});

test("the three stored context modes carry user-language names", () => {
  assert.equal(contextMode("private").label, "Private perspective");
  assert.equal(contextMode("shared").label, "Shared dossier");
  assert.equal(contextMode("swap").label, "Classic card swap");
  // Reached with an undefined mode by any surface whose conversation row
  // predates the setting; it must fall back to the behaviour-preserving
  // default rather than blanking the modal's explanation.
  assert.equal(contextMode(undefined).label, "Private perspective");
  assert.equal(contextMode("everyone_sees_everything").label, "Private perspective");
});

test("every context mode carries the copy both modals render", () => {
  // The dropdown shows `label`; the "How character context works" disclosure
  // shows `detail` + `billing` for all three. A mode missing one renders an
  // empty paragraph where the privacy or cost consequence should be.
  for (const [value, mode] of Object.entries(CONTEXT_MODES)) {
    for (const field of ["label", "detail", "billing"]) {
      assert.ok(mode[field]?.trim(), `${value}.${field} is empty`);
    }
  }
  // The two modes that widen what a speaker can see have to say so outright —
  // this is the only place the user is told before flipping the setting.
  assert.match(CONTEXT_MODES.shared.detail, /read one another's card details/);
  assert.match(CONTEXT_MODES.swap.detail, /names alone/);
});

test("an override is one-shot except in Choose mode, where picking is the strategy", () => {
  scene({ mode: "director" });
  assert.equal(overrideIsOneShot(), true);
  scene({ mode: "round_robin" });
  assert.equal(overrideIsOneShot(), true);
  scene({ mode: "manual" });
  assert.equal(overrideIsOneShot(), false);
});

test("a chip speaks only on a resting scene — a live beat or a draft makes it queue", () => {
  scene({ mode: "director" });
  assert.equal(castClickSpeaksNow(false), true, "nothing running, nothing typed");
  assert.equal(castClickSpeaksNow(true), false, "an unsent draft is waiting for an answer");
  S.isStreaming = true;
  assert.equal(castClickSpeaksNow(false), false, "a beat is already running");
  assert.equal(castClickSpeaksNow(true), false);
});

test("a chip's title states which of the two things this click will do", () => {
  scene({ mode: "director" });
  assert.match(castRailHtml(), /Give Artus the floor now/);
  assert.match(castRailHtml({ hasDraft: true }), /Queue Artus to reply next/);
  S.isStreaming = true;
  assert.match(castRailHtml(), /Queue Artus to reply next/);
});

test("only a queueing click can be taken back — a resting scene has no toggle", () => {
  scene({ mode: "director", pinned: "m2" });
  // Drafted: the pick is still pending, so the pressed chip offers to undo it
  // and the rest offer to take the queue over.
  const queueing = castRailHtml({ hasDraft: true });
  assert.match(queueing, /Assistant is up next — click to clear/);
  assert.match(queueing, /Queue Artus to reply next/);
  // Resting: the same click resolves the pick by using it, for every chip.
  assert.match(castRailHtml(), /Give Assistant the floor now/);
  assert.match(castRailHtml(), /Give Artus the floor now/);
});

test("a solo conversation renders no rail", () => {
  solo();
  assert.equal(castRailHtml(), "");
  assert.equal(speakingPlanHtml(), "");
});

test("the plan rail paints only for a genuinely multi-speaker beat", () => {
  scene({ plan: null });
  assert.equal(speakingPlanHtml(), "", "no plan yet");
  scene({ plan: [] });
  assert.equal(speakingPlanHtml(), "", "the scene rests — reported as a toast, not a strip");
  scene({ plan: [{ member_id: "m1", name: "Artus" }] });
  assert.equal(speakingPlanHtml(), "", "one speaker is already announced by its cast chip");
  const html = (scene({ plan: [{ member_id: "m1", name: "Artus" }, { member_id: "m2", name: "Assistant" }] }),
  speakingPlanHtml());
  assert.equal(html.match(/plan-pill/g).length, 2);
});

test("the plan marks the speaker in flight and the ones already done", () => {
  scene({
    plan: [
      { member_id: "m1", name: "Artus" },
      { member_id: "m2", name: "Assistant" },
    ],
    speaker: { member_id: "m2", index: 1 },
  });
  const html = speakingPlanHtml();
  assert.match(html, /plan-pill done">Artus/);
  assert.match(html, /plan-pill active">Assistant/);
});

test("the cast rail marks the next speaker, disables muted members, and offers manage", () => {
  scene({ members: [ARTUS, { ...ASSISTANT, muted: true }], pinned: "m1" });
  const html = castRailHtml({ hasDraft: true });
  assert.match(html, /data-cast-member-id="m1" aria-pressed="true"/);
  assert.match(html, /data-cast-member-id="m2" aria-pressed="false" disabled/);
  assert.match(html, /Artus is up next/);
  // A muted member says why it is inert rather than advertising a click.
  assert.match(html, /Assistant — not replying in this scene/);
  assert.match(html, /data-cast-manage/);
});

test("a muted member stays in the scene but is not eligible to reply", () => {
  scene({ members: [ARTUS, { ...ASSISTANT, muted: true }, NARRATOR] });
  assert.deepEqual(
    eligibleMembers().map((m) => m.display_name),
    ["Artus", "Narrator"],
  );
});

test("the empty scene names its cast and offers two starters", () => {
  scene({ members: [ARTUS, ASSISTANT] });
  const html = sceneEmptyStateHtml();
  assert.match(html, /Set the scene for Artus and Assistant\./);
  assert.match(html, /data-scene-starter="describe"/);
  assert.match(html, /data-scene-starter="character"/);
  assert.doesNotMatch(html, /Convert to group/);
});

test("an all-muted scene asks for a cast instead of offering a character opener", () => {
  scene({ members: [{ ...ARTUS, muted: true }] });
  const html = sceneEmptyStateHtml();
  assert.match(html, /Add a cast member/);
  assert.doesNotMatch(html, /data-scene-starter="character"/);
});

test("cast names read as a sentence at every size", () => {
  assert.equal(joinNames([]), "");
  assert.equal(joinNames(["Artus"]), "Artus");
  assert.equal(joinNames(["Artus", "Assistant"]), "Artus and Assistant");
  assert.equal(joinNames(["Artus", "Assistant", "Vela"]), "Artus, Assistant, and Vela");
});

test("a display name cannot inject markup into the rail or the empty state", () => {
  scene({ members: [{ id: "m9", display_name: '<img src=x onerror="boom()">' }] });
  assert.doesNotMatch(castRailHtml(), /<img src=x/);
  assert.doesNotMatch(sceneEmptyStateHtml(), /<img src=x/);
});
