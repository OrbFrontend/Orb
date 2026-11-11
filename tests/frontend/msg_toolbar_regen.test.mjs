import { loadDom } from "./dom_fixture.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";

// The message toolbar's regenerate button, against a real DOM.
//
// renderMessages reuses a bubble whose markup is byte-identical to the last pass (dom_reconcile.js), so anything baked
// into a row's markup outlives every repaint that does not change that row. A user row must therefore never name the
// reply under it: the reply can be deleted, or swiped to another branch, without the user row's own markup changing,
// and the button would go on pointing at a message the backend no longer has ("Invalid target message").

const { dom, failure } = await loadDom();

let core = null;
let state = null;
if (dom) {
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
  assert.match(withReply, /data-wf-action="chat:regenerateFromUser" data-msg-id="41"/);
  assert.ok(!/data-msg-id="42"/.test(withReply), withReply);
});

it("the same user row renders identically with and without a reply", () => {
  // The invariant the reconciler depends on: deleting the reply changes no byte
  // of the user row, so reusing the node is safe and the button stays correct.
  assert.equal(toolbarFor(USER, [USER, REPLY]), toolbarFor(USER, [USER]));
});

it("an assistant row still regenerates itself", () => {
  const html = toolbarFor(REPLY, [USER, REPLY]);
  assert.match(html, /data-wf-action="chat:regenerate" data-msg-id="42"/);
});

it("an unsent user row's regenerate button is disabled", () => {
  const pending = { id: null, role: "user", content: "draft" };
  const html = toolbarFor(pending, [pending]);
  assert.match(html, /<button disabled>/);
  assert.ok(!/regenerateFromUser/.test(html), html);
});

// The general form of the rule above, and the tripwire for the next time a row builder reaches for a neighbour: a row's
// markup must be a pure function of its own message. Anything read from the rest of the conversation can change without
// changing this row's html, and the reconciler will then keep a node that says something no longer true.
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

// A switched-off workflow draws no message button, except one whose saved artifacts live in the toolbar (speech): those
// stay reachable while it is off, so a clip saved then still has its player.
it("a disabled workflow's button renders only for a message holding its toolbar-placed artifacts", () => {
  const saved = { ...REPLY, workflow_attachments: [{ id: 9, workflow_id: "speak" }] };
  const plain = { ...REPLY, id: 43 };
  const prior = {
    renderers: state.S.workflowMessageButtonRenderers,
    placements: state.S.workflowAttachmentPlacements,
    settings: state.S.settings,
  };
  state.S.workflowMessageButtonRenderers = [
    { workflowId: "speak", render: (m) => `<i data-speak="${m.id}"></i>` },
    { workflowId: "paint", render: (m) => `<i data-paint="${m.id}"></i>` },
  ];
  state.S.workflowAttachmentPlacements = { speak: { placement: "actions" }, paint: { placement: "artifact" } };
  try {
    state.S.settings = { workflow_enabled: { speak: false, paint: false } };
    const paintSaved = { ...saved, workflow_attachments: [{ id: 10, workflow_id: "paint" }] };
    assert.match(toolbarFor(saved, [saved]), /data-speak="42"/);
    assert.ok(!/data-speak/.test(toolbarFor(plain, [plain])), "no create button while it is off");
    assert.ok(!/data-paint/.test(toolbarFor(paintSaved, [paintSaved])), "an artifact-placed workflow stays hidden");
    state.S.settings = { workflow_enabled: { speak: true, paint: true }, workflows_globally_enabled: 0 };
    assert.match(toolbarFor(saved, [saved]), /data-speak="42"/, "the master switch off is the same as its own");
    state.S.settings = { workflow_enabled: { speak: true, paint: true } };
    assert.match(toolbarFor(plain, [plain]), /data-speak="43"/);
    assert.match(toolbarFor(plain, [plain]), /data-paint="43"/);
  } finally {
    state.S.workflowMessageButtonRenderers = prior.renderers;
    state.S.workflowAttachmentPlacements = prior.placements;
    state.S.settings = prior.settings;
  }
});
