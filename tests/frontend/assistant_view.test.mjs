import assert from "node:assert/strict";
import { test } from "node:test";
import { installEscapingDocument } from "./dom_fixture.mjs";

installEscapingDocument();
const {
  applyStreamEvent,
  applyVerdicts,
  diffHtml,
  handoffOutcome,
  isSendKey,
  parseTranscript,
  pendingIds,
  pendingProposalIds,
  serializeTranscript,
  settleRun,
  transcriptEntries,
  undoVerdicts,
  wireContext,
} = await import("../../frontend/assistant_view.js");

const chat = { mode: "chat", conversation_id: "c1", character_id: null, document_id: null, label: "Chat: Harbour" };
const doc = { mode: "document", conversation_id: null, character_id: null, document_id: "d1", label: "Document: <Notes>" };
const newRun = (items = []) => ({ items, note: "", status: "", terminal: null });
const NO_RESULT = "No result was received; check before asking again.";
const feed = (run, events) => {
  for (const [event, data] of events) applyStreamEvent(run, event, JSON.stringify(data), "The model failed.");
};

const transcriptHtml = (...args) =>
  transcriptEntries(...args)
    .map((row) => row.html)
    .join("");

test("a stored transcript survives corruption and can never restore live or actionable state", () => {
  for (const bad of [null, "", "{", '"x"', "{}", "[1, null, []]", '[{"kind":"confirm","text":"Apply?"}]']) {
    assert.deepEqual(parseTranscript(bad), []);
  }
  const forged = [
    { kind: "user", text: "Hi", context: { ...chat, approved: true }, proposal_id: "p1", requestId: "r1", editing: true },
    { kind: "user", text: "No view" },
    { kind: "tool", tool: "edit_message", text: "", ok: true, running: true, callId: "call1", actionable: true },
    { kind: "tool", tool: "lookup", text: "Found.", ok: "yes", ids: { character_id: "k1" } },
    { kind: "outcome", reason: "awaiting_apply", text: "Apply?" },
    { kind: "proposals", proposals: [{ tool: "edit_message", status: "applying", proposalId: "p1" }] },
    { kind: "assistant", text: "x".repeat(9000), final: true },
  ];
  assert.deepEqual(parseTranscript(JSON.stringify(forged)), [
    { kind: "user", text: "Hi", context: chat, requestId: "r1" },
    { kind: "tool", tool: "edit_message", text: NO_RESULT, ok: false },
    { kind: "tool", tool: "lookup", text: "Found.", ok: false, ids: { character_id: "k1" } },
    { kind: "assistant", text: "x".repeat(8000) },
  ]);
  const many = Array.from({ length: 130 }, (_, i) => ({ kind: "assistant", text: `reply ${i}` }));
  const kept = parseTranscript(serializeTranscript(many));
  assert.equal(kept.length, 100);
  assert.equal(kept.at(-1).text, "reply 129");
});

test("a run records steps and results, and only a terminal event counts as an ending", () => {
  const run = newRun([{ kind: "user", text: "Who is Mara?", context: chat }]);
  feed(run, [
    ["start", {}],
    ["progress", { label: "Thinking…" }],
    ["assistant_message", { text: "Looking her up.", final: false }],
    ["tool_start", { call_id: "call1", tool: "read_character" }],
  ]);
  assert.equal(run.status, "Thinking…");
  assert.match(transcriptHtml(run.items, String), /asst-tool-running/);
  // Reloading mid-run stores the waiting tool as unanswered rather than as running.
  assert.equal(parseTranscript(serializeTranscript(run.items))[1].text, NO_RESULT);
  feed(run, [
    ["tool_result", { call_id: "call1", tool: "read_character", ok: true, summary: "Read character Mara.", ids: { character_id: "k1" } }],
    ["assistant_message", { text: "Mara is a fence.", final: true }],
  ]);
  assert.equal(run.terminal, null);
  feed(run, [["done", { reason: "reply", message: "" }]]);
  settleRun(run);
  assert.equal(run.terminal, "reply");
  assert.deepEqual(run.items.slice(1), [
    { kind: "tool", tool: "read_character", text: "Read character Mara.", ok: true, callId: "call1", note: "Looking her up.", ids: { character_id: "k1" } },
    { kind: "assistant", text: "Mara is a fence." },
  ]);

  // The stream closing by itself is not success, even after a final-looking message.
  const cut = newRun();
  feed(cut, [["tool_start", { call_id: "call1", tool: "read_messages" }]]);
  settleRun(cut);
  assert.deepEqual(cut.items, [
    { kind: "tool", tool: "read_messages", text: NO_RESULT, ok: false, callId: "call1" },
    { kind: "outcome", reason: "interrupted", text: "The connection ended before the assistant finished." },
  ]);

  const stopped = newRun();
  settleRun(stopped, { stopped: true });
  assert.deepEqual(stopped.items, [{ kind: "outcome", reason: "stopped", text: "Stopped." }]);
  const acknowledged = newRun();
  feed(acknowledged, [["done", { reason: "stopped", message: "" }]]);
  settleRun(acknowledged, { stopped: true });
  assert.equal(acknowledged.items.length, 1);

  const failed = newRun();
  feed(failed, [["error", { headline: "ignored here" }]]);
  settleRun(failed, { failure: "later failure" });
  assert.deepEqual(failed.items, [{ kind: "outcome", reason: "error", text: "The model failed." }]);
  const limited = newRun();
  feed(limited, [["done", { reason: "limit", message: "Reached 12 tool runs." }]]);
  assert.deepEqual(limited.items, [{ kind: "outcome", reason: "limit", text: "Reached 12 tool runs." }]);
});

test("a request the server could not extend marks where its copy of the conversation starts", () => {
  const items = [
    { kind: "user", text: "Who is Mara?", context: chat },
    { kind: "assistant", text: "A fence." },
    { kind: "user", text: "And Wren?", context: chat },
  ];
  const run = newRun(items);
  feed(run, [["start", { continued: true }]]);
  assert.equal(run.items.length, 3);
  feed(run, [["start", { continued: false }]]);
  assert.deepEqual(run.items.map((item) => item.reason || item.kind), ["user", "assistant", "reset", "user"]);
  assert.match(transcriptHtml(run.items, String), /asst-sep" role="separator">The assistant no longer remembers/);
  assert.equal(parseTranscript(serializeTranscript(run.items))[2].reason, "reset");
  // A first request has nothing above it to mark.
  const first = newRun([{ kind: "user", text: "Hello", context: chat }]);
  feed(first, [["start", { continued: false }]]);
  assert.equal(first.items.length, 1);
});

test("only an assistant reply is rendered as markup, and a separator marks each change of view", () => {
  const hostile = '<img src=x data-wf-action="assistant:clear">';
  const items = [
    { kind: "user", text: hostile, context: chat },
    { kind: "tool", tool: hostile, text: hostile, ok: false, note: hostile },
    { kind: "user", text: "Again", context: { ...chat } },
    { kind: "outcome", reason: "error", text: hostile },
    { kind: "assistant", text: "**Done**" },
    { kind: "user", text: "And this?", context: doc },
  ];
  const html = transcriptHtml(items, (text) => `<b>${text}</b>`);
  assert.equal(html.includes("<img"), false);
  assert.equal(html.split("&lt;img").length - 1, 5);
  assert.equal(html.split('class="asst-sep"').length - 1, 2, "one separator per view, not per message");
  assert.ok(html.includes("Document: &lt;Notes&gt;"));
  assert.ok(html.includes('<div class="msg-body"><b>**Done**</b></div>'));
  assert.match(transcriptHtml([], String), /asst-empty/);
});

test("Enter sends, but not with Shift and not while an IME composition is being committed", () => {
  assert.equal(isSendKey({ key: "Enter" }), true);
  assert.equal(isSendKey({ key: "Enter", shiftKey: true }), false);
  assert.equal(isSendKey({ key: "Enter", isComposing: true }), false);
  assert.equal(isSendKey({ key: "Enter", keyCode: 229 }), false);
  assert.equal(isSendKey({ key: "a" }), false);
});

test("a mutation names what to read again, and a failed result marks the run for reading everything again", () => {
  const lore = { kind: "lorebook_entry", world_id: "w1" };
  const run = newRun([{ kind: "user", text: "Add lore entry Ferry", context: chat }]);
  feed(run, [["tool_start", { call_id: "call1", tool: "create_lore_entry" }]]);
  const committed = applyStreamEvent(
    run,
    "mutation",
    JSON.stringify({ call_id: "call1", tool: "create_lore_entry", resources: [{ ...lore, entry_id: 7 }] }),
  );
  assert.deepEqual(committed, [{ ...lore, entry_id: 7 }]);
  // A reload mid-tool finds a tool with no result, which says to check rather than redo; nothing names a target.
  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.deepEqual(reloaded[1], { kind: "tool", tool: "create_lore_entry", text: NO_RESULT, ok: false });
  assert.equal(run.failed, undefined);
  feed(run, [
    ["tool_result", { call_id: "call1", tool: "create_lore_entry", ok: true, summary: "Created.", ids: {} }],
    ["tool_start", { call_id: "call2", tool: "illustrate_reply" }],
    ["tool_result", { call_id: "call2", tool: "illustrate_reply", ok: false, summary: "May have been saved.", ids: {} }],
  ]);
  assert.equal(run.failed, true);
  // Closed before even a tool started: the request is followed by an interrupted outcome, never left open.
  const bare = parseTranscript(JSON.stringify([{ kind: "user", text: "Add lore entry Ferry", context: chat }]));
  assert.deepEqual(bare.map((item) => item.reason || item.kind), ["user", "interrupted"]);
});

const confirm = (id, label, before, after, field = "Content", target = { kind: "message", conversation_id: "c1", message_id: 7 }) => [
  "confirm",
  { call_id: "call1", tool: "edit_message", proposal: { proposal_id: id, label, target, fields: [{ field: "content", label: field, before, after }] } },
];

test("a run's proposed edits become one card at its end, however it ended, and a later preview replaces its entry", () => {
  const hostile = '<img src=x data-wf-action="assistant:clear">';
  const run = { ...newRun([{ kind: "user", text: "Fix it", context: chat }]), request: "Fix it" };
  const persona = { kind: "persona", persona_id: 3 };
  feed(run, [
    confirm("p1", hostile, `Old line. ${hostile}`, `Half line. ${hostile}`),
    confirm("p2", "persona Wren", "A ferry pilot.", "A pilot.", "Description", persona),
    confirm("p1", hostile, `Old line. ${hostile}`, `New line. ${hostile}`),
    ["assistant_message", { text: "Proposed both.", final: true }],
    ["done", { reason: "stopped", message: "" }],
  ]);
  assert.equal(run.items.length, 3, "nothing is shown until the run ends");
  settleRun(run, { stopped: true });
  const card = run.card;
  assert.equal(run.items.at(-1), card, "after the run's own ending");
  assert.deepEqual(pendingIds(card), ["p1", "p2"], "the second preview of p1 replaced its entry");
  const html = transcriptHtml(run.items, String);
  assert.equal(html.includes("<img"), false);
  assert.match(html, /<del class="asst-del">Old line\. <\/del><ins class="asst-ins">New line\. <\/ins>/);
  assert.match(html, /2 proposed edits/);
  assert.match(html, /Description <span class="asst-diff-size">14 → 8 characters<\/span>/);
  assert.equal(html.split('data-wf-action="assistant:apply"').length - 1, 2, "one Apply per change");
  assert.match(html, /data-wf-action="assistant:applyAll" data-index="3">Apply all 2</);
  // Grouped by what it edits: one heading per target.
  assert.equal(html.split('class="asst-proposal-target"').length - 1, 2);
  // While work runs its controls are disabled.
  assert.match(transcriptHtml(run.items, String, { busy: true }), /assistant:apply" data-proposal="p1" disabled/);

  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.deepEqual(reloaded.at(-1), {
    kind: "proposals",
    proposals: [
      { tool: "edit_message", label: hostile, field: "Content", beforeChars: 54, afterChars: 54, status: "historical", target: { kind: "message", conversation_id: "c1", message_id: 7 } },
      { tool: "edit_message", label: "persona Wren", field: "Description", beforeChars: 14, afterChars: 8, status: "historical", target: persona },
    ],
    request: "Fix it",
  });
  const inert = transcriptHtml(reloaded, String);
  assert.equal(inert.includes("assistant:apply"), false);
  assert.match(inert, /data-wf-action="assistant:askAgain" data-index="3"/);
  assert.match(inert, /14 → 8 characters/, "the lengths outlive the texts");

  // A run that proposed nothing adds no card.
  const plain = newRun();
  settleRun(plain);
  assert.equal(plain.card, undefined);

  // Long unchanged text around a change is shortened; the change itself is whole.
  const long = `${"a".repeat(500)}. Changed here. ${"b".repeat(500)}.`;
  const diff = diffHtml(long, long.replace("Changed here.", "Edited here."));
  assert.ok(diff.includes("Edited here.") && diff.includes("…") && diff.length < 700);
});

test("Apply's verdicts settle each edit on its own: busy stays pending, and a partly stale card says what it skipped", () => {
  const run = { ...newRun(), request: "Fix all three" };
  feed(run, [
    confirm("p1", "message 7", "a", "b"),
    confirm("p2", "message 8", "c", "d", "Content", { kind: "message", conversation_id: "c1", message_id: 8 }),
    confirm("p3", "message 9", "e", "f", "Content", { kind: "message", conversation_id: "c1", message_id: 9 }),
  ]);
  settleRun(run);
  const card = run.card;
  const message7 = { kind: "message", conversation_id: "c1", message_id: 7 };
  const applied = applyVerdicts(card, [
    { proposal_id: "p1", verdict: "applied", summary: "Applied the edit.", resources: [message7] },
    { proposal_id: "p2", verdict: "stale", message: "Changed." },
    { proposal_id: "p3", verdict: "busy", message: "That chat is generating right now." },
    { proposal_id: "p9", verdict: "applied" },
  ]);
  assert.deepEqual(applied, { resources: [message7], summaries: ["Applied the edit."] });
  assert.deepEqual(card.proposals.map((entry) => entry.status), ["applied", "stale", "pending"]);
  assert.equal(card.proposals[2].note, "That chat is generating right now.");
  assert.equal(card.note, "Applied 1; skipped 1: content of message 8.");
  assert.deepEqual(pendingIds(card), ["p3"], "only the busy one is sent again");
  const html = transcriptHtml([card], String);
  assert.match(html, /Not applied: the text changed after this preview\./);
  assert.match(html, /assistant:apply/);
  // The retry settles the last one; nothing else is touched.
  applyVerdicts(card, [{ proposal_id: "p3", verdict: "applied", resources: [] }]);
  assert.deepEqual(pendingIds(card), []);
  assert.equal(card.note, "");
});

test("a handoff names one reply and its run ends as a handoff", () => {
  const run = newRun();
  feed(run, [["handoff", { call_id: "call2", tool: "edit_message", handoff: { conversation_id: "c1", message_id: 7 } }]]);
  assert.equal(run.handoff, undefined, "a tool this page cannot start is dropped");
  feed(run, [
    ["handoff", { call_id: "call2", tool: "rewrite_reply", handoff: { conversation_id: "c1", message_id: 7, direction: "Colder." } }],
    ["done", { reason: "handoff", message: "Handed to the chat." }],
  ]);
  assert.deepEqual(run.handoff, { tool: "rewrite_reply", conversation_id: "c1", message_id: 7, direction: "Colder." });
  assert.equal(run.terminal, "handoff");
});

test("a continue names the message it follows and any speaker, and an opened chat reads as a view change", () => {
  const run = newRun();
  feed(run, [["handoff", { tool: "continue_chat", handoff: { conversation_id: "c1", message_id: 9, speaker_member_id: "m2" } }]]);
  assert.deepEqual(run.handoff, { tool: "continue_chat", conversation_id: "c1", message_id: 9, speaker_member_id: "m2" });
  feed(run, [["handoff", { tool: "open_chat", handoff: { conversation_id: "c2", title: "<Quay>" } }]]);
  assert.deepEqual(run.handoff, { tool: "open_chat", conversation_id: "c2", title: "<Quay>" });

  const opened = handoffOutcome(run.handoff, "");
  assert.equal(opened.reason, "opened");
  const html = transcriptHtml([opened], String);
  assert.match(html, /class="asst-sep"/);
  assert.ok(html.includes("&lt;Quay&gt;") && !html.includes("<Quay>"));
  assert.deepEqual(parseTranscript(serializeTranscript([opened])), [opened], "it is kept as an outcome");
  assert.match(handoffOutcome(run.handoff, "document").text, /^Not opened: Document mode is on/);
});

test("a list field's edit shows the items it removes and adds, escaped, and its counts outlive a reload", () => {
  const run = newRun([{ kind: "user", text: "Retag her.", context: chat }]);
  const fields = [{ field: "tags", label: "Tags", before: ["Noir", "<old>"], after: ["Noir", "Docks"] }];
  feed(run, [
    ["confirm", { tool: "edit_character", proposal: { proposal_id: "p-tags", label: "character Mara", fields } }],
    ["done", { reason: "reply", message: "" }],
  ]);
  settleRun(run);
  const html = transcriptHtml(run.items, String);
  assert.match(html, /<del class="asst-del">&lt;old&gt;<\/del>/);
  assert.match(html, /<ins class="asst-ins">Docks<\/ins>/);
  assert.match(html, /1 unchanged\./);
  assert.match(html, /2 → 2 items/);
  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.match(transcriptHtml(reloaded, String), /2 → 2 items/);
  assert.equal(JSON.stringify(reloaded).includes("Docks"), false, "a stored card keeps no list items");
});

test("a delete is a change of its own: no text to edit, no undo, and a warning that outlives a reload", () => {
  const run = { ...newRun([{ kind: "user", text: "Delete Ashfall.", context: chat }]), request: "Delete Ashfall." };
  const world = { kind: "world", world_id: "w1" };
  const fields = [{ field: "delete", label: "Delete", before: "Its 3 entries go with it.", after: "" }];
  feed(run, [
    ["confirm", { tool: "delete_world", proposal: { proposal_id: "d1", action: "delete", label: "world <Ashfall>", target: world, fields } }],
    ["done", { reason: "reply", message: "" }],
  ]);
  settleRun(run);
  const html = transcriptHtml(run.items, String);
  assert.match(html, /Proposed delete/);
  assert.match(html, /cannot be undone/);
  assert.match(html, /Its 3 entries go with it\./);
  assert.match(html, /data-wf-action="assistant:apply" data-proposal="d1">Delete</);
  assert.equal(html.includes("assistant:revise"), false, "a delete has no text to edit");
  assert.ok(html.includes("world &lt;Ashfall&gt;") && !html.includes("<Ashfall>"));
  applyVerdicts(run.card, [{ proposal_id: "d1", verdict: "applied", resources: [{ ...world, deleted: true }] }]);
  assert.equal(transcriptHtml(run.items, String).includes("assistant:undo"), false, "a delete has no undo");
  const reloaded = parseTranscript(serializeTranscript(run.items)).at(-1).proposals[0];
  assert.deepEqual(
    { action: reloaded.action, detail: reloaded.detail, status: reloaded.status },
    { action: "delete", detail: "Its 3 entries go with it.", status: "applied" },
  );
});

test("a change made again in a later request replaces the earlier card's entry, and Undo makes an edit pending again", () => {
  const run = newRun([{ kind: "user", text: "Fix it", context: chat }]);
  feed(run, [confirm("p1", "message 7", "a", "b"), confirm("p2", "message 8", "c", "d")]);
  settleRun(run);
  assert.deepEqual(pendingProposalIds(run.items), ["p1", "p2"]);
  const later = newRun(run.items);
  feed(later, [["confirm", { tool: "edit_message", proposal: { ...confirm("p3", "message 7", "a", "bb")[1].proposal, replaces: "p1" } }]]);
  settleRun(later);
  assert.equal(run.card.proposals[0].status, "superseded");
  assert.equal(run.card.proposals[0].proposalId, undefined, "a replaced entry is no longer live");
  assert.deepEqual(pendingProposalIds(run.items), ["p2", "p3"]);

  applyVerdicts(later.card, [{ proposal_id: "p3", verdict: "applied", resources: [] }]);
  assert.match(transcriptHtml(run.items, String), /assistant:undo" data-proposal="p3"/);
  const undone = undoVerdicts(later.card, [{ proposal_id: "p3", verdict: "undone", summary: "Undid it.", resources: [{ kind: "message" }] }]);
  assert.deepEqual(undone, { resources: [{ kind: "message" }], summaries: ["Undid it."] });
  assert.equal(later.card.proposals[0].status, "pending");
  applyVerdicts(later.card, [{ proposal_id: "p3", verdict: "applied", resources: [] }]);
  undoVerdicts(later.card, [{ proposal_id: "p3", verdict: "stale", message: "Changed." }]);
  assert.equal(later.card.proposals[0].status, "applied");
  assert.equal(later.card.proposals[0].proposalId, undefined, "a stale undo offers no second try");
  assert.match(transcriptHtml(run.items, String), /Not undone: the text changed after the edit was applied\./);
});

test("the lorebook drawer's world and entry ride with the view, and only when the drawer had them", () => {
  const lore = { ...chat, world_id: "w1", entry_id: 4, label: "Chat: Harbour · Lore entry: Tide tax" };
  assert.deepEqual(wireContext(lore), { mode: "chat", conversation_id: "c1", world_id: "w1", entry_id: 4 });
  const [stored] = parseTranscript(serializeTranscript([{ kind: "user", text: "Tighten it.", context: lore }]));
  assert.deepEqual(stored.context, lore);
  const forged = { ...chat, entry_id: 4 };
  const [plain] = parseTranscript(serializeTranscript([{ kind: "user", text: "Hi", context: forged }]));
  assert.deepEqual(plain.context, chat, "an entry without its world is dropped");
});

test("standalone image results render and survive reloads using only checked local ids", () => {
  const imageId = "a1".repeat(16);
  const run = newRun();
  feed(run, [
    ["tool_start", { tool: "create_image", call_id: "image-call" }],
    ["tool_result", { tool: "create_image", call_id: "image-call", ok: true, summary: "Created a picture.", ids: { image_id: imageId } }],
  ]);
  const restored = parseTranscript(serializeTranscript(run.items));
  assert.equal(restored[0].ids.image_id, imageId);
  const html = transcriptHtml(restored, (s) => s);
  assert.match(html, new RegExp(`src="/api/assistant/images/${imageId}"`));
  assert.match(html, /data-wf-action="assistant:zoomImage"/);
  assert.match(html, /\?download=true" download/);
  assert.match(html, /expired or the server restarted/);
  for (const bad of ["../../settings", "https://evil.invalid/a.png", '"><script>', "a".repeat(33), 12]) {
    restored[0].ids.image_id = bad;
    assert.doesNotMatch(transcriptHtml(restored, (s) => s), /<img/);
  }
  restored[0].ids.image_id = imageId;
  restored[0].ok = false;
  assert.doesNotMatch(transcriptHtml(restored, (s) => s), /<img/);
  restored[0].ok = true;
  restored[0].tool = "illustrate_reply";
  assert.doesNotMatch(transcriptHtml(restored, (s) => s), /<img/);
});
