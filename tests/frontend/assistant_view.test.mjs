import assert from "node:assert/strict";
import { test } from "node:test";
import { installEscapingDocument } from "./dom_fixture.mjs";

installEscapingDocument();
const {
  applyStreamEvent,
  applyVerdicts,
  diffHtml,
  isSendKey,
  parseTranscript,
  pendingIds,
  runItems,
  serializeTranscript,
  settleRun,
  splitDiffHtml,
  transcriptHtml,
} = await import("../../frontend/assistant_view.js");

const chat = { mode: "chat", conversation_id: "c1", character_id: null, document_id: null, label: "Chat: Harbour" };
const doc = { mode: "document", conversation_id: null, character_id: null, document_id: "d1", label: "Document: <Notes>" };
const newRun = (items = []) => ({ items, note: "", status: "", terminal: null });
const NO_RESULT = "No result was received; check before asking again.";
const feed = (run, events) => {
  for (const [event, data] of events) applyStreamEvent(run, event, JSON.stringify(data), "The model failed.");
};

test("a stored transcript survives corruption and can never restore live or actionable state", () => {
  for (const bad of [null, "", "{", '"x"', "{}", "[1, null, []]", '[{"kind":"confirm","text":"Apply?"}]']) {
    assert.deepEqual(parseTranscript(bad), []);
  }
  const forged = [
    { kind: "user", text: "Hi", context: { ...chat, approved: true }, proposal_id: "p1" },
    { kind: "user", text: "No view" },
    { kind: "tool", tool: "edit_message", text: "", ok: true, running: true, callId: "call1", actionable: true },
    { kind: "tool", tool: "lookup", text: "Found.", ok: "yes", ids: { character_id: "k1" } },
    { kind: "outcome", reason: "awaiting_apply", text: "Apply?" },
    { kind: "proposals", proposals: [{ tool: "edit_message", status: "applying", proposalId: "p1" }] },
    { kind: "assistant", text: "x".repeat(9000), final: true },
  ];
  assert.deepEqual(parseTranscript(JSON.stringify(forged)), [
    { kind: "user", text: "Hi", context: chat },
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
    ["tool_start", { call_id: "call1", tool: "get_character" }],
  ]);
  assert.equal(run.status, "Thinking…");
  assert.match(transcriptHtml(run.items, String), /asst-tool-running/);
  // Reloading mid-run stores the waiting tool as unanswered rather than as running.
  assert.equal(parseTranscript(serializeTranscript(run.items))[1].text, NO_RESULT);
  feed(run, [
    ["tool_result", { call_id: "call1", tool: "get_character", ok: true, summary: "Read character Mara.", ids: { character_id: "k1" } }],
    ["assistant_message", { text: "Mara is a fence.", final: true }],
  ]);
  assert.equal(run.terminal, null);
  feed(run, [["done", { reason: "reply", message: "" }]]);
  settleRun(run);
  assert.equal(run.terminal, "reply");
  assert.deepEqual(run.items.slice(1), [
    { kind: "tool", tool: "get_character", text: "Read character Mara.", ok: true, callId: "call1", note: "Looking her up.", ids: { character_id: "k1" } },
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

test("replay sends tool and outcome entries as plain records with each request's own view label", () => {
  const items = [
    { kind: "user", text: "Who is Mara?", context: chat },
    { kind: "tool", tool: "get_character", text: "Read character Mara.", ok: true, ids: { character_id: "k1" } },
    { kind: "tool", tool: "lookup", text: "", ok: false, running: true },
    { kind: "outcome", reason: "stopped", text: "Stopped." },
    { kind: "assistant", text: "A fence." },
    { kind: "user", text: "y".repeat(8000), context: doc },
  ];
  assert.deepEqual(runItems(items), [
    { kind: "user", text: "Who is Mara?", view: "Chat: Harbour" },
    { kind: "tool", tool: "get_character", text: "ok: Read character Mara." },
    { kind: "outcome", text: "stopped: Stopped." },
    { kind: "assistant", text: "A fence." },
    { kind: "user", text: "y".repeat(8000), view: "Document: <Notes>" },
  ]);
  const long = Array.from({ length: 150 }, () => ({ kind: "assistant", text: "a" }));
  assert.equal(runItems([...long, items[0]]).length, 100);
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

test("an empty log suggests requests for the view in front of the reader, as escaped inert text", () => {
  const suggested = (html) => [...html.matchAll(/data-text="([^"]*)"/g)].map((match) => match[1]);
  const chatHtml = transcriptHtml([], String, { suggestFor: "chat" });
  assert.ok(suggested(chatHtml).length > 0);
  assert.equal(chatHtml.split('data-wf-action="assistant:suggest"').length - 1, suggested(chatHtml).length);
  assert.notDeepEqual(suggested(chatHtml), suggested(transcriptHtml([], String, { suggestFor: "document" })));
  assert.deepEqual(suggested(transcriptHtml([], String, { suggestFor: "unknown" })), suggested(transcriptHtml([], String)));
  assert.equal(transcriptHtml([{ kind: "assistant", text: "Hi" }], String).includes("asst-suggest"), false);
});

test("a wide diff puts what the field holds now beside what it will hold, each with only its own changes", () => {
  const split = splitDiffHtml("Keep this. Drop that. Keep the end.", "Keep this. Add this. Keep the end.");
  assert.ok(split.before.includes("<del") && !split.before.includes("<ins"));
  assert.ok(split.after.includes("<ins") && !split.after.includes("<del"));
  for (const side of [split.before, split.after]) assert.ok(side.includes("Keep this.") && side.includes("Keep the end."));
  // A field that is new or emptied has one side only.
  assert.deepEqual(splitDiffHtml("", "<b>new</b>"), { before: "", after: '<ins class="asst-ins">&lt;b&gt;new&lt;/b&gt;</ins>' });
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
  feed(run, [["tool_start", { call_id: "call1", tool: "create_lorebook_entry" }]]);
  const committed = applyStreamEvent(
    run,
    "mutation",
    JSON.stringify({ call_id: "call1", tool: "create_lorebook_entry", resources: [{ ...lore, entry_id: 7, extra: "x" }, { kind: "?" }] }),
  );
  assert.deepEqual(committed, [{ ...lore, entry_id: 7 }], "only the ids its kind carries");
  // A reload mid-tool finds a tool with no result, which says to check rather than redo; nothing names a target.
  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.deepEqual(reloaded[1], { kind: "tool", tool: "create_lorebook_entry", text: NO_RESULT, ok: false });
  assert.equal(runItems(reloaded)[1].text, `failed: ${NO_RESULT}`);
  assert.equal(run.failed, undefined);
  feed(run, [
    ["tool_result", { call_id: "call1", tool: "create_lorebook_entry", ok: true, summary: "Created.", ids: {} }],
    ["tool_start", { call_id: "call2", tool: "generate_image" }],
    ["tool_result", { call_id: "call2", tool: "generate_image", ok: false, summary: "May have been saved.", ids: {} }],
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
  const html = transcriptHtml(run.items, String, { active: card });
  assert.equal(html.includes("<img"), false);
  assert.match(html, /<del class="asst-del">Old line\. <\/del><ins class="asst-ins">New line\. <\/ins>/);
  assert.match(html, /2 proposed edits/);
  assert.match(html, /Description <span class="asst-diff-size">14 → 8 characters<\/span>/);
  assert.equal(html.split('data-wf-action="assistant:apply"').length - 1, 1, "one Apply for the whole card");
  // Grouped by what it edits: one heading per target.
  assert.equal(html.split('class="asst-proposal-target"').length - 1, 2);
  // The same card not held by this page, or while work runs, has no live control.
  assert.equal(transcriptHtml(run.items, String).includes("assistant:apply"), false);
  assert.match(transcriptHtml(run.items, String, { active: card, busy: true }), /assistant:apply"[^>]* disabled/);

  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.deepEqual(reloaded.at(-1), {
    kind: "proposals",
    proposals: [
      { tool: "edit_message", label: hostile, field: "Content", beforeChars: 54, afterChars: 54, status: "historical", target: { kind: "message", conversation_id: "c1", message_id: 7 } },
      { tool: "edit_message", label: "persona Wren", field: "Description", beforeChars: 14, afterChars: 8, status: "historical", target: persona },
    ],
    request: "Fix it",
  });
  const inert = transcriptHtml(reloaded, String, { active: reloaded.at(-1) });
  assert.equal(inert.includes("assistant:apply"), false);
  assert.match(inert, /data-wf-action="assistant:askAgain" data-index="3"/);
  assert.match(inert, /14 → 8 characters/, "the lengths outlive the texts");
  assert.match(runItems(reloaded).at(-1).text, /^proposed, never applied: edit to the description of persona Wren$/);

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
  const html = transcriptHtml([card], String, { active: card });
  assert.match(html, /Not applied: the text changed after this preview\./);
  assert.match(html, /assistant:apply/);
  assert.deepEqual(
    runItems([card]).map((item) => item.text.split(":")[0]),
    ["applied by the user", "not applied", "proposed, not applied"],
  );
  // The retry settles the last one; nothing else is touched.
  applyVerdicts(card, [{ proposal_id: "p3", verdict: "applied", resources: [] }]);
  assert.deepEqual(pendingIds(card), []);
  assert.equal(card.note, "");
});

test("a handoff names one reply and its run ends as a handoff", () => {
  const run = newRun();
  feed(run, [
    ["handoff", { call_id: "call2", tool: "regenerate_reply", handoff: { conversation_id: "c1", message_id: "7" } }],
    ["handoff", { call_id: "call2", tool: "rewrite_reply", handoff: { conversation_id: "c1", message_id: 7 } }],
    ["handoff", { call_id: "call2", tool: "edit_message", handoff: { conversation_id: "c1", message_id: 7 } }],
  ]);
  assert.equal(run.handoff, undefined, "a malformed handoff, a rewrite without direction or another tool is dropped");
  feed(run, [
    ["handoff", { call_id: "call2", tool: "rewrite_reply", handoff: { conversation_id: "c1", message_id: 7, direction: " Colder. " } }],
    ["done", { reason: "handoff", message: "Handed to the chat." }],
  ]);
  assert.deepEqual(run.handoff, { tool: "rewrite_reply", conversation_id: "c1", message_id: 7, direction: "Colder." });
  assert.equal(run.terminal, "handoff");
});
