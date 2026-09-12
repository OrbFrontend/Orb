import assert from "node:assert/strict";
import { test } from "node:test";

// The message toolbar's regenerate button, against a real DOM.
//
// renderMessages reuses a bubble whose markup is byte-identical to the last
// pass (dom_reconcile.js), so anything baked into a row's markup outlives every
// repaint that does not change that row. A user row must therefore never name
// the reply under it: the reply can be deleted, or swiped to another branch,
// without the user row's own markup changing, and the button would go on
// pointing at a message the backend no longer has ("Invalid target message").

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
  console.error(`SKIPPED tests/frontend/msg_toolbar_regen.test.mjs — jsdom is unavailable (${failure}).`);
  console.error("Run `npm install` to exercise the message toolbar against a real DOM.");
}

const it = dom ? test : test.skip;

const USER = { id: 41, role: "user", content: "go on", parent_id: 7 };
const REPLY = { id: 42, role: "assistant", content: "...", parent_id: 41 };

function toolbarFor(msg, messages) {
  state.S.messages = messages;
  return core.buildMsgToolbar(msg);
}

it("a user row's regenerate button does not name the reply under it", () => {
  const withReply = toolbarFor(USER, [USER, REPLY]);
  assert.match(withReply, /onclick="regenerateFromUser\(41\)"/);
  assert.ok(!/regenerate\(42\)/.test(withReply), withReply);
});

it("the same user row renders identically with and without a reply", () => {
  // The invariant the reconciler depends on: deleting the reply changes no byte
  // of the user row, so reusing the node is safe and the button stays correct.
  assert.equal(toolbarFor(USER, [USER, REPLY]), toolbarFor(USER, [USER]));
});

it("an assistant row still regenerates itself", () => {
  const html = toolbarFor(REPLY, [USER, REPLY]);
  assert.match(html, /onclick="regenerate\(42\)"/);
});

it("a greeting has no regenerate button", () => {
  const greeting = { id: 7, role: "assistant", content: "hi", parent_id: null };
  const html = toolbarFor(greeting, [greeting]);
  assert.ok(!/Regenerate/.test(html), html);
});

it("an unsent user row's regenerate button is disabled", () => {
  const pending = { id: null, role: "user", content: "draft" };
  const html = toolbarFor(pending, [pending]);
  assert.match(html, /<button disabled>/);
  assert.ok(!/regenerateFromUser/.test(html), html);
});

// The general form of the rule above, and the tripwire for the next time a row
// builder reaches for a neighbour: a row's markup must be a pure function of
// its own message. Anything read from the rest of the conversation can change
// without changing this row's html, and the reconciler will then keep a node
// that says something no longer true.
it("row markup reads nothing but its own message", () => {
  const convo = [
    { id: 7, role: "assistant", content: "hi", parent_id: null },
    { id: 41, role: "user", content: "go on", parent_id: 7 },
    { id: 42, role: "assistant", content: "...", parent_id: 41, branch_count: 2, branch_index: 0, next_branch_id: 43 },
    { id: 44, role: "user", content: "and then", parent_id: 42 },
  ];
  for (const m of convo) {
    state.S.messages = convo;
    const inContext = core.buildMsgToolbar(m) + core.swipeNavHtml(m);
    state.S.messages = [m];
    const alone = core.buildMsgToolbar(m) + core.swipeNavHtml(m);
    assert.equal(alone, inContext, `row ${m.id} renders differently once its neighbours are gone`);
  }
});
