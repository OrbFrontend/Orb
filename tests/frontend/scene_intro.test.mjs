import assert from "node:assert/strict";
import { test } from "node:test";

// The Creator's Note and Scenario blocks that sit above the opening line.
//
// They are card metadata rendered into the message list, so they key like rows
// (dom_reconcile.js) but carry no message id: nothing that edits, regenerates,
// deletes or swipes a message can reach them.

let dom = null;
let failure = "";
try {
  const { JSDOM } = await import("jsdom");
  dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "https://orb.invalid/" });
} catch (e) {
  failure = e?.message || String(e);
}

let core = null;
let state = null;
if (dom) {
  const w = dom.window;
  globalThis.window = w;
  for (const name of ["document", "Node", "NodeFilter", "Element", "DocumentFragment", "HTMLElement", "DOMParser"]) {
    if (w[name] !== undefined) globalThis[name] = w[name];
  }
  core = await import("../../frontend/chat_core.js");
  state = await import("../../frontend/state.js");
} else {
  console.error(`SKIPPED tests/frontend/scene_intro.test.mjs — jsdom is unavailable (${failure}).`);
  console.error("Run `npm install` to exercise the scene intro against a real DOM.");
}

const it = dom ? test : test.skip;

const CONV = { id: "c1", kind: "solo", character_card_id: "card-1", character_name: "Vesna" };

function intro(fields) {
  state.S.conversations = [CONV];
  state.S.activeConvId = CONV.id;
  state.S.personas = [];
  state.S.settings = { user_name: "Ada" };
  state.S.sceneIntro = { convId: CONV.id, scenario: "", creatorNotes: "", ...fields };
  return core.sceneIntroEntries();
}

it("renders the Creator's Note above the Scenario", () => {
  const entries = intro({ scenario: "A cold harbour.", creatorNotes: "Slow burn." });
  assert.deepEqual(
    entries.map((e) => e.key),
    ["scene-notes", "scene-scenario"],
  );
  assert.match(entries[0].html, /Creator&#039;s Note|Creator's Note/);
  assert.match(entries[0].html, /Slow burn\./);
  assert.match(entries[1].html, /Scenario/);
  assert.match(entries[1].html, /A cold harbour\./);
});

it("carries no message id, so no message action can address the block", () => {
  for (const entry of intro({ scenario: "A cold harbour.", creatorNotes: "Slow burn." })) {
    assert.ok(!/data-msg-id/.test(entry.html), entry.html);
    assert.ok(!/msg-toolbar|swipe-nav|<button/.test(entry.html), entry.html);
  }
});

it("skips a field the card left empty", () => {
  assert.deepEqual(
    intro({ scenario: "   ", creatorNotes: "Slow burn." }).map((e) => e.key),
    ["scene-notes"],
  );
  assert.deepEqual(intro({}), []);
});

it("resolves the same placeholders the messages below it do", () => {
  const [scenario] = intro({ scenario: "{{char}} waits for {{user}}." });
  assert.match(scenario.html, /Vesna waits for Ada\./);
});

it("drops framing read for another conversation", () => {
  state.S.sceneIntro = { convId: "c0", scenario: "A cold harbour.", creatorNotes: "Slow burn." };
  assert.deepEqual(core.sceneIntroEntries(), []);
  state.S.sceneIntro = null;
  assert.deepEqual(core.sceneIntroEntries(), []);
});
