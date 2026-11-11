import assert from "node:assert/strict";
import { test } from "node:test";
import { installEscapingDocument } from "./dom_fixture.mjs";

installEscapingDocument();
const {
  affectedResources,
  applyRefusal,
  applyStreamEvent,
  diffHtml,
  isSendKey,
  parseTranscript,
  runItems,
  serializeTranscript,
  settleRun,
  transcriptHtml,
  UNKNOWN_OUTCOME,
} = await import("../../frontend/assistant_view.js");

const chat = { mode: "chat", conversation_id: "c1", character_id: null, document_id: null, label: "Chat: Harbour" };
const doc = { mode: "document", conversation_id: null, character_id: null, document_id: "d1", label: "Document: <Notes>" };
const newRun = (items = []) => ({ items, note: "", status: "", terminal: null });
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
    { kind: "assistant", text: "x".repeat(9000), final: true },
  ];
  assert.deepEqual(parseTranscript(JSON.stringify(forged)), [
    { kind: "user", text: "Hi", context: chat },
    { kind: "tool", tool: "edit_message", text: "No result was received.", ok: false },
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
  assert.equal(parseTranscript(serializeTranscript(run.items))[1].text, "No result was received.");
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
    { kind: "tool", tool: "read_messages", text: "No result was received.", ok: false, callId: "call1" },
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

test("Enter sends, but not with Shift and not while an IME composition is being committed", () => {
  assert.equal(isSendKey({ key: "Enter" }), true);
  assert.equal(isSendKey({ key: "Enter", shiftKey: true }), false);
  assert.equal(isSendKey({ key: "Enter", isComposing: true }), false);
  assert.equal(isSendKey({ key: "Enter", keyCode: 229 }), false);
  assert.equal(isSendKey({ key: "a" }), false);
});

test("a write keeps the ids it committed, and one never answered is an unknown outcome to check, not to resend", () => {
  const lore = { kind: "lorebook_entry", world_id: "w1" };
  const toggle = { kind: "settings", keys: ["enabled_tools", "length_guard_enabled"] };
  const run = newRun();
  feed(run, [
    ["tool_start", { call_id: "call1", tool: "create_lorebook_entry", write: true, target: lore }],
    ["mutation", { call_id: "call1", tool: "create_lorebook_entry", resources: [{ ...lore, entry_id: 7, extra: "x" }] }],
  ]);
  // Committed but cut off before its result: still shown, and stored, as done with the id it committed.
  assert.deepEqual(parseTranscript(serializeTranscript(run.items))[0], {
    kind: "tool",
    tool: "create_lorebook_entry",
    text: "Done; its result was not received.",
    ok: true,
    ids: { committed: [{ ...lore, entry_id: 7 }] },
  });
  feed(run, [["tool_start", { call_id: "call2", tool: "set_pipeline_toggle", write: true, target: toggle }]]);
  // A reload in the middle of a write cannot tell whether it committed.
  assert.equal(parseTranscript(serializeTranscript(run.items))[1].text, UNKNOWN_OUTCOME);
  settleRun(run, { stopped: true });
  assert.equal(run.items[1].text, UNKNOWN_OUTCOME);
  assert.equal(run.items[1].ok, false);
  assert.deepEqual(affectedResources(run), [{ ...lore, entry_id: 7 }, toggle]);
  // A write that answered with a failure committed nothing and has nothing to reconcile.
  const failed = newRun();
  feed(failed, [
    ["tool_start", { call_id: "call1", tool: "set_pipeline_toggle", write: true, target: toggle }],
    ["tool_result", { call_id: "call1", tool: "set_pipeline_toggle", ok: false, summary: "Invalid arguments.", ids: {} }],
  ]);
  settleRun(failed);
  assert.deepEqual(affectedResources(failed), []);
});

test("a request the page closed on reads as interrupted, and an unknown write is replayed as unknown with its target", () => {
  const lore = { kind: "lorebook_entry", world_id: "w1" };
  const run = newRun([{ kind: "user", text: "Add lore entry Ferry", context: chat }]);
  feed(run, [["tool_start", { call_id: "call1", tool: "create_lorebook_entry", write: true, target: lore }]]);
  // What a reload mid-write finds: the request, then a write whose outcome is unknown and names what to check.
  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.deepEqual(reloaded[1], { kind: "tool", tool: "create_lorebook_entry", text: UNKNOWN_OUTCOME, ok: false, unknown: true, target: lore });
  const replayed = runItems(reloaded)[1].text;
  assert.match(replayed, /^outcome unknown/);
  assert.ok(!replayed.startsWith("failed") && replayed.includes('"world_id":"w1"'));
  // Closed before even a tool started: the request is followed by an interrupted outcome, never left open.
  const bare = parseTranscript(JSON.stringify([{ kind: "user", text: "Add lore entry Ferry", context: chat }]));
  assert.deepEqual(bare.map((item) => item.reason || item.kind), ["user", "interrupted"]);
});

test("a proposed edit renders escaped diffs, acts only while held, and reloads as an inert card that offers Ask again", () => {
  const hostile = '<img src=x data-wf-action="assistant:clear">';
  const run = { ...newRun([{ kind: "user", text: "Fix it", context: chat }]), request: "Fix it", context: chat };
  feed(run, [
    [
      "confirm",
      {
        call_id: "call1",
        tool: "edit_message",
        proposal: {
          proposal_id: "p1",
          label: hostile,
          target: { kind: "message", conversation_id: "c1", message_id: 7 },
          fields: [{ field: "content", label: "Content", before: `Old line. ${hostile}`, after: `New line. ${hostile}` }],
        },
      },
    ],
    ["done", { reason: "awaiting_apply", message: "" }],
  ]);
  const card = run.proposal;
  assert.equal(run.terminal, "awaiting_apply");
  assert.equal(card.status, "pending");
  const html = transcriptHtml(run.items, String, { active: card });
  assert.equal(html.includes("<img"), false);
  assert.match(html, /<del class="asst-del">Old line\. <\/del><ins class="asst-ins">New line\. <\/ins>/);
  assert.match(html, /data-wf-action="assistant:apply" data-proposal-id="p1"/);
  // The same card not held by this page, or while work runs, has no live control.
  assert.equal(transcriptHtml(run.items, String).includes("assistant:apply"), false);
  assert.match(transcriptHtml(run.items, String, { active: card, busy: true }), /assistant:apply[^>]* disabled/);

  const reloaded = parseTranscript(serializeTranscript(run.items));
  assert.deepEqual(reloaded[1], {
    kind: "proposal",
    tool: "edit_message",
    label: hostile,
    status: "historical",
    fields: [{ label: "Content" }],
    request: "Fix it",
    target: { kind: "message", conversation_id: "c1", message_id: 7 },
  });
  const inert = transcriptHtml(reloaded, String, { active: reloaded[1] });
  assert.equal(inert.includes("assistant:apply"), false);
  assert.match(inert, /data-wf-action="assistant:askAgain" data-index="1"/);
  assert.match(runItems(reloaded)[1].text, /^proposed, never applied: edit to /);
  // An Apply that was in flight when the page closed is an unknown outcome, never pending.
  card.status = "applying";
  assert.equal(parseTranscript(serializeTranscript(run.items))[1].status, "unknown");

  // Long unchanged text around a change is shortened; the change itself is whole.
  const long = `${"a".repeat(500)}. Changed here. ${"b".repeat(500)}.`;
  const diff = diffHtml(long, long.replace("Changed here.", "Edited here."));
  assert.ok(diff.includes("Edited here.") && diff.includes("…") && diff.length < 700);
});

test("an Apply refusal leaves the card inert, except a busy chat, and anything unexplained is unconfirmed", () => {
  const refused = (status, detail) => applyRefusal({ status, body: JSON.stringify({ detail }) });
  assert.deepEqual(refused(409, { code: "stale", message: "x" }), { status: "stale", note: "" });
  assert.deepEqual(refused(409, { code: "busy", message: "Generating." }), { status: "pending", note: "Generating." });
  assert.equal(refused(404, { code: "missing" }).status, "missing");
  assert.equal(refused(410, { code: "expired" }).status, "expired");
  assert.equal(applyRefusal(new TypeError("Failed to fetch")).status, "unconfirmed");
  assert.equal(refused(500, "Internal error").status, "unconfirmed");
  // A retry of an Apply that was never answered: expired or stale may only mean the first one committed, so it is unknown.
  const retried = (status, detail) => applyRefusal({ status, body: JSON.stringify({ detail }) }, "unconfirmed");
  assert.deepEqual(retried(410, { code: "expired" }), { status: "unknown", note: "" });
  assert.deepEqual(retried(409, { code: "stale", message: "x" }), { status: "unknown", note: "" });
  assert.equal(retried(409, { code: "busy", message: "Generating." }).status, "pending");
  const [record] = runItems([{ kind: "proposal", tool: "edit_message", label: "message 7", status: "unknown" }]);
  assert.match(record.text, /outcome unknown/);
  assert.doesNotMatch(record.text, /not applied/);
});

test("a handoff names one reply and its run ends as a handoff; a create's target names only its kind", () => {
  const run = newRun();
  feed(run, [
    ["tool_start", { call_id: "call1", tool: "set_chat_world", write: true, target: { kind: "chat_worlds" } }],
    ["handoff", { call_id: "call2", tool: "regenerate_reply", handoff: { conversation_id: "c1", message_id: "7" } }],
    ["handoff", { call_id: "call2", tool: "rewrite_reply", handoff: { conversation_id: "c1", message_id: 7 } }],
    ["handoff", { call_id: "call2", tool: "edit_message", handoff: { conversation_id: "c1", message_id: 7 } }],
  ]);
  assert.equal(run.handoff, undefined, "a malformed handoff, a rewrite without direction or another tool is dropped");
  assert.equal(run.writes[0].target, null, "a chat's world choice names its chat and world");
  feed(run, [
    ["handoff", { call_id: "call2", tool: "rewrite_reply", handoff: { conversation_id: "c1", message_id: 7, direction: " Colder. " } }],
    ["done", { reason: "handoff", message: "Handed to the chat." }],
  ]);
  assert.deepEqual(run.handoff, { tool: "rewrite_reply", conversation_id: "c1", message_id: 7, direction: "Colder." });
  assert.equal(run.terminal, "handoff");
  assert.deepEqual(
    affectedResources({
      writes: [
        { text: UNKNOWN_OUTCOME, target: { kind: "character" } },
        { committed: [{ kind: "conversation", conversation_id: "c2" }] },
        { committed: [{ kind: "branch", conversation_id: "c1" }] },
      ],
    }),
    [{ kind: "character" }, { kind: "conversation", conversation_id: "c2" }, { kind: "branch", conversation_id: "c1" }],
  );
});
