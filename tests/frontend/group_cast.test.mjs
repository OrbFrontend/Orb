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
  cardDefTokens,
  castClickSpeaksNow,
  castRailHtml,
  contextMode,
  eligibleMembers,
  GROUP_LIMIT,
  groupFamilies,
  groupFamily,
  groupRootId,
  joinNames,
  overrideIsOneShot,
  recommendContextMode,
  sceneEmptyStateHtml,
  speakerLabel,
  speakingPlanHtml,
  TURN_MODES,
  visibleGroups,
} from "../../frontend/group_cast.js";
import { S } from "../../frontend/state.js";

const ARTUS = { id: "m1", display_name: "Artus", character_card_id: "c1" };
const ASSISTANT = { id: "m2", display_name: "Assistant", character_card_id: "c2" };
const NARRATOR = { id: "m3", display_name: "Narrator", member_kind: "narrator" };

function scene({
  mode = "director",
  members = [ARTUS, ASSISTANT],
  retired = [],
  pinned = null,
  plan = null,
  speaker = null,
} = {}) {
  // `members` is the active roster; `speakerNames` spans it plus everyone the
  // scene has retired, exactly as loadGroupCast derives the two from one fetch.
  S.groupCast = {
    members,
    speakerNames: new Map([...members, ...retired].map((member) => [member.id, member.display_name])),
    turn_mode: mode,
    max_speakers: 3,
  };
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

// Deliberately not asserted anywhere in this file: the *wording* of a label or a
// rail title. Those live in one table an import away, so restating them here proves
// only that someone typed them twice — and costs a failing suite every time the copy
// is improved. What is pinned instead is the shape the copy hangs on: which modes
// exist, that every field a modal renders is filled, and the data attributes and ARIA
// state a click actually reads.

test("the stored turn modes are the three the backend persists", () => {
  // The dropdown renders the table directly, so a mode missing here is a mode the
  // user cannot pick; a mode named differently is one the backend will reject.
  assert.deepEqual(Object.keys(TURN_MODES), ["director", "round_robin", "manual"]);
  assert.ok(Object.values(TURN_MODES).every((mode) => mode.label?.trim()));
});

test("an unknown or absent context mode falls back to the behaviour-preserving default", () => {
  // Reached with an undefined mode by any surface whose conversation row predates
  // the setting; it must land on a real entry rather than blanking the modal.
  assert.equal(contextMode("shared"), CONTEXT_MODES.shared);
  assert.equal(contextMode(undefined), CONTEXT_MODES.private);
  assert.equal(contextMode("everyone_sees_everything"), CONTEXT_MODES.private);
});

test("every context mode fills every field both modals render", () => {
  // The dropdown shows `label`; the "How character context works" disclosure shows
  // `detail` + `billing` for all three. A mode missing one renders an empty paragraph
  // where the privacy or cost consequence should be. Whether the sentence in it is a
  // *good* explanation is a review question, not a test one.
  for (const [value, mode] of Object.entries(CONTEXT_MODES)) {
    for (const field of ["label", "detail", "billing"]) {
      assert.ok(mode[field]?.trim(), `${value}.${field} is empty`);
    }
  }
});

// ── Context-mode recommendation ─────────────────────────────────────────────
// The rule was fitted against simulated 30-beat, three-pass group sessions
// rendered through the shipped prompt builders, on a server holding several
// prefix-cache lanes. These pin the boundary it landed on and, more
// importantly, the direction it is allowed to be wrong in.

// `def_chars` arrives from the library list, which is the only card payload
// creation ever holds.
const card = (defChars) => ({ id: `c${defChars}`, name: "x", def_chars: defChars });
// tokens → the `def_chars` a card of that weight would report (CHARS_PER_TOKEN=4).
const ofTokens = (tokens) => card(tokens * 4);

test("a card's weight counts only the fields the two modes disagree about", () => {
  assert.equal(cardDefTokens(card(4000)), 1000);
  assert.equal(cardDefTokens(card(0)), 0);
  // A card list fetched before `def_chars` existed, and a missing row from a
  // picker whose selection outran the character cache. Neither may throw, and
  // both have to read as weightless so the cast lands on the default.
  assert.equal(cardDefTokens({}), 0);
  assert.equal(cardDefTokens(undefined), 0);
  assert.equal(cardDefTokens({ def_chars: "not a number" }), 0);
});

test("no cast, no recommendation", () => {
  assert.equal(recommendContextMode([]), null);
  assert.equal(recommendContextMode(undefined), null);
  // A picker holding an id the character list no longer has resolves to
  // undefined; a cast of nothing but holes is still no cast.
  assert.equal(recommendContextMode([undefined, undefined]), null);
});

test("one character is not a cast, at any card weight", () => {
  // The threshold is 500 * (cast - 1), which at one member is zero — so every
  // card cleared it and an eight-token stub was told it was heavy enough to
  // cache. There is also nothing to weigh it against yet: the panel recomputes
  // per pick, so answering here means answering for a half-chosen cast.
  for (const tokens of [2, 8, 500, 2000]) {
    assert.equal(recommendContextMode([ofTokens(tokens)]), null, `1 x ${tokens} should stay silent`);
  }
  // The second pick is where advice starts.
  assert.ok(recommendContextMode([ofTokens(8), ofTokens(8)]));
});

test("the boundary is mean card weight against 500 tokens per member past the first", () => {
  // Measured crossovers: 2 members at ~500 tokens, 3 at ~1000.
  assert.equal(recommendContextMode([ofTokens(400), ofTokens(400)]).mode, "private");
  assert.equal(recommendContextMode([ofTokens(500), ofTokens(500)]).mode, "swap");
  assert.equal(recommendContextMode([ofTokens(800), ofTokens(800), ofTokens(800)]).mode, "private");
  assert.equal(recommendContextMode([ofTokens(1000), ofTokens(1000), ofTokens(1000)]).mode, "swap");
});

test("a wide cast is private at every card size — swap runs out of cache lanes, not tokens", () => {
  // Swap needs roughly 2.5 warm branches per member, so a fourth member is
  // where they stop fitting and its cost jumps 4-6x rather than drifting. No
  // card weight buys that back, so the cap is not a threshold.
  for (const tokens of [500, 1000, 1500, 2000, 8000]) {
    const wide = Array.from({ length: 4 }, () => ofTokens(tokens));
    assert.equal(recommendContextMode(wide).mode, "private", `4 x ${tokens} should stay private`);
    assert.equal(recommendContextMode([...wide, ofTokens(tokens)]).mode, "private");
  }
});

test("the mean is the statistic, because both modes bill per speaking turn", () => {
  // One heavy card and two light ones costs what three middling ones cost:
  // private re-sends whoever speaks, swap caches whoever speaks. Simulation put
  // these two casts within a token of each other.
  const lopsided = recommendContextMode([ofTokens(2000), ofTokens(500), ofTokens(500)]);
  const even = recommendContextMode([ofTokens(1000), ofTokens(1000), ofTokens(1000)]);
  assert.equal(lopsided.mode, even.mode);
  assert.equal(lopsided.meanTokens, even.meanTokens);
});

test("a cast with no card text lands on the default rather than on the cheaper mode", () => {
  // Narrator-shaped members have nothing worth caching. From two members up the
  // threshold is never below 500, so they fall to Private on the comparison
  // itself — no separate floor to keep in step with the boundary.
  assert.equal(recommendContextMode([card(0), card(0)]).mode, "private");
  assert.equal(recommendContextMode([{}, {}]).mode, "private");
  assert.equal(recommendContextMode([card(0), card(0), card(0)]).mode, "private");
  // One real card beside a cardless narrator is still weighed on the mean.
  assert.equal(recommendContextMode([ofTokens(2000), card(0)]).mode, "swap");
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
  // A muted member is inert to a click, which `disabled` is what actually enforces.
  assert.match(html, /data-cast-member-id="m2" aria-pressed="false" disabled/);
  assert.match(html, /data-cast-manage/);
});

test("a muted member stays in the scene but is not eligible to reply", () => {
  scene({ members: [ARTUS, { ...ASSISTANT, muted: true }, NARRATOR] });
  assert.deepEqual(
    eligibleMembers().map((m) => m.display_name),
    ["Artus", "Narrator"],
  );
});

test("the empty scene offers both starters", () => {
  scene({ members: [ARTUS, ASSISTANT] });
  const html = sceneEmptyStateHtml();
  assert.match(html, /data-scene-starter="describe"/);
  assert.match(html, /data-scene-starter="character"/);
  assert.doesNotMatch(html, /Convert to group/);
});

test("an all-muted scene offers no character opener, since nobody can take it", () => {
  scene({ members: [{ ...ARTUS, muted: true }] });
  assert.doesNotMatch(sceneEmptyStateHtml(), /data-scene-starter="character"/);
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

// ── Speaker labels ──────────────────────────────────────────────────────────
// The role line over every reply. This reads `speakerNames`, never `members`,
// and the distinction is the whole point: the active roster is what the rail
// paints, but the transcript outlives it.

test("a reply is labelled with its speaker's name", () => {
  scene();
  assert.equal(speakerLabel({ role: "assistant", speaker_member_id: "m1" }), "Artus");
});

test("the user is always 'You', in a group as in a solo chat", () => {
  scene();
  assert.equal(speakerLabel({ role: "user", speaker_member_id: null }), "You");
});

test("a removed member still labels the lines it wrote", () => {
  // The regression this pins: Manage cast tombstones a member rather than
  // deleting it, and its replies keep pointing at that id forever. Resolving
  // them through the active roster turned a roster edit into a silent rewrite
  // of the transcript — every one of that member's lines read "Unknown
  // speaker". The backend refuses the same shortcut in `get_speaker_names`.
  scene({ members: [ARTUS], retired: [ASSISTANT] });
  assert.equal(speakerLabel({ role: "assistant", speaker_member_id: "m2" }), "Assistant");
  // ...and it is still gone from every surface that asks who is in the scene.
  assert.deepEqual(
    eligibleMembers().map((member) => member.id),
    ["m1"],
  );
  assert.ok(!castRailHtml().includes("m2"));
});

test("a group reply with no speaker is the scene's own summary", () => {
  scene();
  assert.equal(speakerLabel({ role: "assistant", speaker_member_id: null }), "Summary");
});

test("an id no roster has ever held is named as unknown rather than blank", () => {
  // A blank role line merges the reply into the one above it, which is the
  // failure mode this fallback exists to prevent.
  scene();
  assert.equal(speakerLabel({ role: "assistant", speaker_member_id: "gone" }), "Unknown speaker");
});

// ── Group families ──────────────────────────────────────────────────────────
// A checkpoint of a group is a branch of that group. These cover the grouping
// the sidebar reads: what a fork belongs to, and which conversation in a family
// supplies the name and the click target.

// Conversations arrive newest-active first, which is the order the sidebar and
// the reopen-the-group click both depend on.
const ROOT = { id: "g1", kind: "group", title: "Campfire", group_root_id: null };
const FORK = { id: "g2", kind: "group", title: "Campfire (checkpoint)", group_root_id: "g1" };
const OTHER = { id: "g3", kind: "group", title: "Elsewhere", group_root_id: null };
const SOLO = { id: "s1", kind: "solo", title: "Ada", character_card_id: "c1" };

test("a root keys on itself and a fork keys on the group it branched from", () => {
  assert.equal(groupRootId(ROOT), "g1");
  assert.equal(groupRootId(FORK), "g1");
  assert.equal(groupRootId(null), null);
});

test("a family gathers every conversation of one group and nothing else", () => {
  const all = [FORK, OTHER, ROOT, SOLO];
  assert.deepEqual(
    groupFamily(all, "g1").map((c) => c.id),
    ["g2", "g1"],
  );
  assert.deepEqual(
    groupFamily(all, "g3").map((c) => c.id),
    ["g3"],
  );
});

test("each group collapses to one entry, ordered by its most recent conversation", () => {
  const families = groupFamilies([FORK, OTHER, ROOT, SOLO]);
  assert.deepEqual(
    families.map((f) => f.rootId),
    ["g1", "g3"],
  );
  assert.equal(families[0].members.length, 2);
  // The root names the group; with nothing of the family open, the newest
  // conversation is what the row shows and what a click opens.
  assert.equal(families[0].root.title, "Campfire");
  assert.equal(families[0].shown.id, "g2");
  assert.equal(families[0].open, false);
});

test("the open conversation is the one its group's row stands for", () => {
  // Selecting a checkpoint has to repaint the row with *that* conversation's
  // cast: the two forks' rosters have diverged, and the rail already shows the
  // open one's. Only the family holding it is marked open.
  const families = groupFamilies([FORK, OTHER, ROOT, SOLO], "g1");
  assert.equal(families[0].shown.id, "g1");
  assert.equal(families[0].open, true);
  assert.equal(families[0].root.title, "Campfire");
  assert.equal(families[1].shown.id, "g3");
  assert.equal(families[1].open, false);
});

test("a solo conversation on screen leaves every group row on its newest", () => {
  const families = groupFamilies([FORK, OTHER, ROOT, SOLO], "s1");
  assert.deepEqual(
    families.map((f) => [f.shown.id, f.open]),
    [
      ["g2", false],
      ["g3", false],
    ],
  );
});

test("a family whose root is missing still renders, led by its newest member", () => {
  const families = groupFamilies([FORK]);
  assert.equal(families.length, 1);
  assert.equal(families[0].root.id, "g2");
  assert.equal(families[0].shown.id, "g2");
});

// ── Sidebar cap ─────────────────────────────────────────────────────────────
// The Groups section is capped the way Worlds and Documents are: a recent slice
// by default, a search box and a "show all" behind it.

// n families in conversation order, the (openIndex)th marked open.
function families(n, openIndex = -1) {
  return Array.from({ length: n }, (_, i) => ({
    rootId: `r${i}`,
    root: { id: `r${i}`, title: `Group ${i}` },
    shown: { id: `r${i}`, group_member_names: [`Cast ${i}`] },
    open: i === openIndex,
    members: [],
  }));
}

test("a list inside the cap shows whole, with nothing hidden", () => {
  const { shown, hidden } = visibleGroups(families(GROUP_LIMIT));
  assert.equal(shown.length, GROUP_LIMIT);
  assert.equal(hidden, 0);
});

test("past the cap the newest are shown and the rest counted", () => {
  const { shown, hidden } = visibleGroups(families(GROUP_LIMIT + 4));
  assert.equal(shown.length, GROUP_LIMIT);
  assert.equal(shown[0].rootId, "r0");
  assert.equal(hidden, 4);
});

test("expanded shows every group", () => {
  const { shown, hidden } = visibleGroups(families(GROUP_LIMIT + 4), { expanded: true });
  assert.equal(shown.length, GROUP_LIMIT + 4);
  assert.equal(hidden, 0);
});

test("the open group keeps a row even when recency pushed it past the cut", () => {
  // Otherwise selecting a checkpoint of a quiet group would delete the row the
  // click came from. It takes the last slot, and the hidden count still counts
  // every group the list is not showing.
  const { shown, hidden } = visibleGroups(families(GROUP_LIMIT + 4, GROUP_LIMIT + 2));
  assert.equal(shown.length, GROUP_LIMIT);
  assert.equal(shown[shown.length - 1].rootId, `r${GROUP_LIMIT + 2}`);
  assert.equal(hidden, 4);
});

test("an open group already inside the cap is not moved", () => {
  const { shown } = visibleGroups(families(GROUP_LIMIT + 4, 1));
  assert.deepEqual(
    shown.map((f) => f.rootId),
    families(GROUP_LIMIT).map((f) => f.rootId),
  );
});

test("search matches the group name or its cast, and ignores the cap", () => {
  const all = families(GROUP_LIMIT + 4);
  assert.deepEqual(
    visibleGroups(all, { query: "Group 1" }).shown.map((f) => f.rootId),
    ["r1", "r10", "r11"],
  );
  assert.deepEqual(
    visibleGroups(all, { query: "cast 3" }).shown.map((f) => f.rootId),
    ["r3"],
  );
  // A search that matches nothing hides everything rather than falling back.
  assert.deepEqual(visibleGroups(all, { query: "nobody" }).shown, []);
});

test("search never reports a hidden remainder, so no “show all” is offered", () => {
  assert.equal(visibleGroups(families(GROUP_LIMIT + 4), { query: "Group" }).hidden, 0);
});
