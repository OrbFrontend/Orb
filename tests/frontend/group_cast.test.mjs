// The group chat's identity surface. group_cast.js is string-in/string-out over
// `S` (it imports only state.js and utils.js, both DOM-free beyond `esc`), so it
// loads under node --test.
//
// What matters here is what the scene *tells* the user: which strategy is in
// force, who is about to answer and whether that choice survives the turn, and
// that the speaking-plan rail never paints a row with nothing in it.
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
  castRailHtml,
  eligibleMembers,
  joinNames,
  overrideIsOneShot,
  replyBarHtml,
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
  // An unknown value must not blank the reply bar.
  assert.equal(turnMode("nonsense").label, "Auto");
});

test("an override is one-shot except in Choose mode, where picking is the strategy", () => {
  scene({ mode: "director" });
  assert.equal(overrideIsOneShot(), true);
  scene({ mode: "round_robin" });
  assert.equal(overrideIsOneShot(), true);
  scene({ mode: "manual" });
  assert.equal(overrideIsOneShot(), false);
});

test("with no override the reply bar states the strategy and offers no speak action", () => {
  scene({ mode: "director" });
  const html = replyBarHtml();
  assert.match(html, /Auto · Director chooses/);
  assert.doesNotMatch(html, /data-speak-now/);
  assert.doesNotMatch(html, /data-clear-override/);
});

test("Choose mode with nobody picked says so, since the composer is blocked until then", () => {
  scene({ mode: "manual" });
  assert.match(replyBarHtml(), /Pick who replies from the cast/);
});

test("an override names the member, offers to run it now, and says it is one-shot", () => {
  scene({ mode: "director", pinned: "m2" });
  const html = replyBarHtml();
  assert.match(html, /Assistant/);
  assert.match(html, /data-clear-override/);
  assert.match(html, /Let Assistant speak now/);
  assert.match(html, /next reply only/);
});

test("a Choose-mode pick is not advertised as one-shot", () => {
  scene({ mode: "manual", pinned: "m1" });
  const html = replyBarHtml();
  assert.match(html, /Let Artus speak now/);
  assert.doesNotMatch(html, /next reply only/);
});

test("the speak action is unavailable mid-stream", () => {
  scene({ mode: "director", pinned: "m1" });
  S.isStreaming = true;
  assert.match(replyBarHtml(), /data-speak-now disabled/);
});

test("a solo conversation renders no rail and no reply bar", () => {
  solo();
  assert.equal(replyBarHtml(), "");
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
  const html = castRailHtml();
  assert.match(html, /data-cast-member-id="m1" aria-pressed="true"/);
  assert.match(html, /data-cast-member-id="m2" aria-pressed="false" disabled/);
  assert.match(html, /Have Artus reply next|Artus replies next/);
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
