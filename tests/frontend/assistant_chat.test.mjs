import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

// The panel against the real page markup: how it opens, what a request captures, and who owns Stop.
const page = readFileSync(new URL("../../frontend/index.html", import.meta.url), "utf8");
const dom = new JSDOM(page, { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const name of ["document", "Node", "NodeFilter", "Element", "DocumentFragment", "HTMLElement", "DOMParser", "MutationObserver"]) {
  globalThis[name] = dom.window[name];
}
// Storage that holds one corrupt transcript and then refuses every write, as a full or blocked store does.
globalThis.localStorage = {
  getItem: (key) => (key === "orb-assistant-transcript" ? '{"not":"a list"' : key === "orb-assistant-open" ? "1" : null),
  setItem() {
    throw new Error("QuotaExceededError");
  },
};

// The wide layout; the document editor reads its breakpoint as it loads.
window.matchMedia = () => ({ matches: false, addEventListener() {} });
// The chat list repaints after an applied message edit; jsdom lays nothing out and has no scrolling.
dom.window.HTMLElement.prototype.scrollTo ||= () => {};
const { S } = await import("../../frontend/state.js");
const { initAssistant } = await import("../../frontend/assistant_chat.js");
initAssistant();

const $ = (id) => document.getElementById(id);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const settled = async () => {
  for (let i = 0; i < 6; i++) await tick();
};
let requests;

beforeEach(() => {
  requests = [];
  globalThis.fetch = async (url, options = {}) => {
    const request = { url: String(url), body: options.body ? JSON.parse(options.body) : null };
    requests.push(request);
    // The server's view after a write: a toggled key, plus a key nothing asked for.
    if (request.url.endsWith("/api/settings"))
      return Response.json({ enabled_tools: { editor_apply_patch: false }, user_name: "from the server" });
    if (request.url.endsWith("/api/worlds") || request.url.endsWith("/api/user-personas")) return Response.json([]);
    if (request.url.endsWith("/api/fragments") || request.url.endsWith("/api/interactive-fragments")) return Response.json([]);
    // The chat list as this page already has it: a refresh in place changes nothing a test relies on.
    if (request.url.endsWith("/api/conversations")) return Response.json(S.conversations);
    if (/\/api\/assistant\/(apply|undo|revise)$/.test(request.url)) return new Promise((resolve) => (request.answer = resolve));
    if (request.url.includes("/messages") || request.url.endsWith("/api/characters")) return Response.json([]);
    if (!request.url.includes("/assistant/run")) return Response.json({ ok: true, active: true, settled: true });
    const body = new ReadableStream({
      start(controller) {
        request.writer = controller;
      },
    });
    options.signal?.addEventListener("abort", () => request.writer.error(new Error("aborted")));
    return { ok: true, body };
  };
});

function emit(request, event, data) {
  request.writer.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
}

async function ask(text) {
  $("assistant-input").value = text;
  $("assistant-send").click();
  await settled();
  return requests.find((request) => request.url.includes("/assistant/run") && request.writer && !request.used);
}

test("a request keeps the view captured at Send, and Stop names that operation alone", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "group" },
  ];
  S.activeConvId = "chat-a";
  const run = await ask("Who is here?");
  run.used = true;
  const operationId = new URL(run.url, "https://orb.invalid").searchParams.get("operation_id");
  assert.ok(operationId);
  assert.deepEqual(run.body.context, { mode: "chat", conversation_id: "chat-a" });
  // Only the request goes out; the server holds the conversation the session names.
  assert.equal(run.body.text, "Who is here?");
  assert.equal(run.body.session, S.assistant.session);
  assert.equal("items" in run.body, false);
  assert.equal([...S.operations.values()].find((op) => op.kind === "assistant").target.conversation_id, "chat-a");

  // While it runs: Clear is off, the button is Stop, and a second Send starts nothing.
  assert.equal($("assistant-clear").disabled, true);
  assert.equal($("assistant-send").dataset.state, "stop");
  S.activeConvId = "chat-b";
  emit(run, "tool_start", { call_id: "step1", tool: "read_context" });
  await settled();
  assert.equal($("assistant-scope").textContent, "Chat: Harbour", "the running request still names its own view");
  assert.match($("assistant-log").innerHTML, /asst-tool-running/);

  $("assistant-send").click();
  await settled();
  const stops = requests.filter((request) => request.url.includes("/assistant/stop"));
  assert.equal(stops.length, 1);
  assert.equal(new URL(stops[0].url, "https://orb.invalid").searchParams.get("operation_id"), operationId);
  emit(run, "done", { reason: "stopped", message: "" });
  run.writer.close();
  await settled();
  assert.equal(S.operations.size, 0);
  assert.deepEqual(
    S.assistant.items.map((item) => item.kind),
    ["user", "tool", "outcome"],
  );
  assert.equal(S.assistant.items[1].text, "No result was received; check before asking again.");
  assert.equal($("assistant-clear").disabled, false);
  assert.equal($("assistant-scope").textContent, "Group chat: Campfire", "the next request is about the view now open");

  // The next request goes out under its own view and the same session, and a group names no character.
  const next = await ask("And now?");
  next.used = true;
  assert.deepEqual(next.body.context, { mode: "chat", conversation_id: "chat-b" });
  assert.equal(next.body.text, "And now?");
  assert.equal(next.body.session, run.body.session);
  assert.equal(document.querySelectorAll("#assistant-log .asst-sep").length, 2);
  // A stream that just ends is reported as a failure, never shown as an answer.
  emit(next, "assistant_message", { text: "Half an answer", final: false });
  next.writer.close();
  await settled();
  assert.equal(S.assistant.items.at(-1).reason, "error");
  assert.match(S.assistant.items.at(-1).text, /ended before completion/);
  assert.equal(S.operations.size, 0);

  $("assistant-clear").click();
  assert.deepEqual(S.assistant.items, []);
});

test("a committed change is read again by id, and a run cut short reads everything again once and resends nothing", async () => {
  S.conversations = [{ id: "chat-long", title: "t".repeat(400), kind: "solo" }];
  S.activeConvId = "chat-long";
  S.settings = { ...S.settings, user_name: "local" };
  const run = await ask("Turn the Editor off, then add lore for the ferry.");
  run.used = true;

  const keys = { kind: "settings", keys: ["enabled_tools"] };
  emit(run, "tool_start", { call_id: "call1", tool: "set_pipeline_toggle" });
  emit(run, "mutation", { call_id: "call1", tool: "set_pipeline_toggle", resources: [keys] });
  await settled();
  assert.ok(requests.some((request) => request.url.endsWith("/api/settings")), "the toggle's keys are read again");
  assert.deepEqual(S.settings.enabled_tools, { editor_apply_patch: false });
  assert.equal(S.settings.user_name, "local", "only the keys the toggle wrote are taken from the row");
  emit(run, "tool_result", { call_id: "call1", tool: "set_pipeline_toggle", ok: true, summary: "Turned Editor off.", ids: {} });

  // The lore write starts, and the connection ends before anything says whether it committed.
  emit(run, "tool_start", { call_id: "call2", tool: "create_lore_entry" });
  await settled();
  const reads = (path) => requests.filter((request) => request.url.endsWith(path)).length;
  assert.equal(reads("/api/worlds"), 0);
  run.writer.close();
  await settled();
  const lore = S.assistant.items.findLast((item) => item.tool === "create_lore_entry");
  assert.equal(lore.text, "No result was received; check before asking again.");
  for (const path of ["/api/worlds", "/api/characters", "/api/user-personas", "/api/conversations", "/chat-long/messages"])
    assert.equal(reads(path), 1, `${path} is read again once the run has settled`);
  assert.equal(requests.filter((request) => request.url.includes("/assistant/run")).length, 1, "nothing is resent");
  assert.equal(S.operations.size, 0);
});

// A storage that keeps what is written, as a page that is about to be reloaded would find it.
test("a request about another character asks to start over first; a view with no character keeps the conversation", async () => {
  $("assistant-clear").click();
  const { notify } = await import("../../frontend/state.js");
  const answer = async (text) => {
    const run = await ask(text);
    run.used = true;
    emit(run, "assistant_message", { text: "Done.", final: true });
    emit(run, "done", { reason: "reply", message: "" });
    run.writer.close();
    await settled();
    return run;
  };
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo", character_card_id: "mara" },
    { id: "chat-a2", title: "Harbour, later", kind: "solo", character_card_id: "mara" },
    { id: "chat-b", title: "Forge", kind: "solo", character_card_id: "wren" },
  ];
  S.activeConvId = "chat-a";
  notify("conversations");
  const first = await answer("Who is Mara?");
  // Another chat with the same character, then a document: the conversation and its session go on.
  S.activeConvId = "chat-a2";
  notify("conversations");
  S.documentMode = true;
  notify("conversations");
  S.documentMode = false;
  const second = await answer("And her rival?");
  assert.equal(second.body.session, first.body.session);
  assert.equal(S.assistant.items.length, 4);
  // Another character: the log stays until a request is sent, which asks first; Cancel sends nothing and keeps the text.
  S.activeConvId = "chat-b";
  notify("conversations");
  assert.equal(S.assistant.items.length, 4);
  const runs = () => requests.filter((request) => request.url.includes("/assistant/run")).length;
  const before = runs();
  $("assistant-input").value = "Who is Wren?";
  $("assistant-send").click();
  await settled();
  const choice = (action) => $("modal-root").querySelector(`[data-confirm-action="${action}"]`);
  choice("cancel").click();
  assert.equal(runs(), before);
  assert.equal(S.assistant.items.length, 4);
  assert.equal($("assistant-input").value, "Who is Wren?");
  // Agreeing clears the log, and the request names a conversation the server has never seen.
  $("assistant-send").click();
  await settled();
  choice(0).click();
  await settled();
  const third = requests.find((request) => request.url.includes("/assistant/run") && request.writer && !request.used);
  third.used = true;
  emit(third, "assistant_message", { text: "Done.", final: true });
  emit(third, "done", { reason: "reply", message: "" });
  third.writer.close();
  await settled();
  assert.notEqual(third.body.session, first.body.session);
  assert.equal(S.assistant.items.length, 2);
  assert.equal($("assistant-input").value, "");
  S.activeConvId = null;
  $("assistant-clear").click();
});

function keepingStorage() {
  const kept = new Map();
  return { getItem: (key) => kept.get(key) ?? null, setItem: (key, value) => kept.set(key, String(value)), kept };
}

const message = (id) => ({ kind: "message", conversation_id: "chat-a", message_id: id });
const confirmed = (id, target = message(7), label = "message 7 in <Harbour>") => ({
  call_id: "call1",
  tool: "edit_message",
  proposal: {
    proposal_id: id,
    label,
    target,
    fields: [{ field: "content", label: "Content", before: "Old <b>line</b>.", after: "New <b>line</b>." }],
  },
});

/** A run that proposes *previews* (proposal ids, or whole `confirm` payloads) and replies. */
async function propose(...previews) {
  const run = await ask("Fix the messages.");
  run.used = true;
  for (const preview of previews) emit(run, "confirm", typeof preview === "string" ? confirmed(preview) : preview);
  emit(run, "assistant_message", { text: "Proposed.", final: true });
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
  return run;
}

const applyButton = () => document.querySelector('#assistant-log [data-wf-action="assistant:apply"]');

test("a reload mid-tool stores it as unanswered", async () => {
  $("assistant-clear").click();
  const saved = globalThis.localStorage;
  globalThis.localStorage = keepingStorage();
  try {
    S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
    S.activeConvId = "chat-a";
    const run = await ask("Add lore entry Ferry");
    run.used = true;
    emit(run, "tool_start", { call_id: "call1", tool: "create_lore_entry" });
    await settled();
    // The page closes here: what it stored already says the tool got no result.
    const { parseTranscript } = await import("../../frontend/assistant_view.js");
    const reloaded = parseTranscript(globalThis.localStorage.kept.get("orb-assistant-transcript"));
    assert.equal(reloaded.at(-1).text, "No result was received; check before asking again.");
    run.writer.close();
    await settled();
  } finally {
    globalThis.localStorage = saved;
    $("assistant-clear").click();
  }
});

const control = (action, id) =>
  document.querySelector(`#assistant-log [data-wf-action="assistant:${action}"]${id ? `[data-proposal="${id}"]` : ""}`);
const posted = (path) => requests.filter((request) => request.url.endsWith(path));

test("Apply all sends the card's ids alone, settles each change by its verdict, and a busy one is applied on its own", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  await propose("p1", confirmed("p2", message(8), "message 8"), confirmed("p3", { kind: "persona", persona_id: 3 }, "persona Wren"));
  assert.equal(document.querySelectorAll('#assistant-log [data-wf-action="assistant:apply"]').length, 3, "one Apply per change");
  assert.equal($("assistant-log").querySelector(".asst-diff b"), null, "the diff is escaped text");
  control("applyAll").click();
  await settled();
  let applies = posted("/api/assistant/apply");
  assert.equal(applies.length, 1);
  assert.deepEqual(applies[0].body, { proposal_ids: ["p1", "p2", "p3"] });
  assert.equal(control("applyAll").disabled, true, "no second Apply while the first is in flight");
  const ops = [...S.operations.values()].filter((item) => item.kind === "assistant-apply");
  assert.deepEqual(ops.map((op) => op.target.conversationId), ["chat-a"], "chat edits in flight are chat work");
  applies[0].answer(
    Response.json({
      results: [
        { proposal_id: "p1", verdict: "applied", summary: "Applied the edit to message 7.", resources: [message(7)] },
        { proposal_id: "p2", verdict: "busy", message: "That chat is generating right now." },
        { proposal_id: "p3", verdict: "stale", message: "Changed." },
      ],
    }),
  );
  await settled();
  assert.ok(requests.some((request) => request.url.endsWith("/api/conversations/chat-a/messages")), "the chat in view is read again");
  const card = S.assistant.items.find((item) => item.kind === "proposals");
  assert.deepEqual(card.proposals.map((entry) => entry.status), ["applied", "pending", "stale"]);
  assert.equal(S.assistant.items.at(-1).text, "Applied the edit to message 7.");
  assert.match($("assistant-log").innerHTML, /That chat is generating right now\./);
  assert.match($("assistant-log").innerHTML, /Applied 1; skipped 1: content of persona Wren\./);
  assert.ok(control("undo", "p1"), "an applied edit can be undone");

  // The busy one is still pending, and its own Apply sends it alone.
  assert.equal(control("applyAll"), null, "one change left needs no Apply all");
  control("apply", "p2").click();
  await settled();
  applies = posted("/api/assistant/apply");
  assert.deepEqual(applies[1].body, { proposal_ids: ["p2"] });
  applies[1].answer(Response.json({ results: [{ proposal_id: "p2", verdict: "applied", resources: [message(8)] }] }));
  await settled();
  assert.equal(card.proposals[1].status, "applied");
  assert.equal(control("apply"), null, "nothing is left to apply");
  assert.equal(requests.filter((request) => request.url.includes("/assistant/run")).length, 1, "nothing continues by itself");
  $("assistant-clear").click();
});

test("a later request keeps a card live and names what it holds, and a change made again replaces its entry", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "solo" },
  ];
  S.activeConvId = "chat-a";
  await propose("p-view");
  control("apply", "p-view").click();
  await settled();
  S.activeConvId = "chat-b";
  posted("/api/assistant/apply")[0].answer(
    Response.json({ results: [{ proposal_id: "p-view", verdict: "stale", message: "Changed." }] }),
  );
  await settled();
  assert.equal(requests.filter((request) => request.url.includes("/messages")).length, 0, "the chat now in view is untouched");
  assert.equal(S.assistant.items.find((item) => item.kind === "proposals").proposals[0].status, "stale");
  // Ask again puts the request back for the reader to send; nothing is sent by itself.
  document.querySelector('[data-wf-action="assistant:askAgain"]').click();
  assert.equal($("assistant-input").value, "Fix the messages.");
  $("assistant-input").value = "";

  // A new request leaves a pending card live, and tells the server which of its changes still wait.
  S.activeConvId = "chat-a";
  await propose("p-old", confirmed("p-keep", message(8), "message 8"));
  const next = await ask("Make it shorter.");
  next.used = true;
  assert.deepEqual(next.body.pending, ["p-old", "p-keep"]);
  assert.ok(control("apply", "p-old"), "the earlier card can still be applied");
  // The run edits the same field again: its new proposal replaces the earlier card's entry.
  emit(next, "confirm", { ...confirmed("p-new"), proposal: { ...confirmed("p-new").proposal, replaces: "p-old" } });
  emit(next, "done", { reason: "reply", message: "" });
  next.writer.close();
  await settled();
  const [older, newer] = S.assistant.items.filter((item) => item.kind === "proposals").slice(-2);
  assert.deepEqual(older.proposals.map((entry) => entry.status), ["superseded", "pending"]);
  assert.equal(control("apply", "p-old"), null, "a replaced change has no control");
  assert.match($("assistant-log").innerHTML, /Replaced by a later proposal/);
  assert.deepEqual(newer.proposals.map((entry) => entry.proposalId), ["p-new"]);
  assert.deepEqual(
    [...S.assistant.items.filter((item) => item.kind === "proposals").flatMap((card) => card.proposals)]
      .filter((entry) => entry.status === "pending")
      .map((entry) => entry.proposalId),
    ["p-keep", "p-new"],
  );

  // Discard settles one change and Cancel all every pending one of its card; neither sends anything.
  const others = () => requests.filter((request) => !request.url.includes("/assistant/run")).length;
  const before = others();
  control("cancel", "p-keep").click();
  assert.equal(older.proposals[1].status, "cancelled");
  control("cancel", "p-new").click();
  await propose("p-cancel", confirmed("p-cancel-2", message(8)));
  control("cancelAll").click();
  assert.deepEqual(
    S.assistant.items.findLast((item) => item.kind === "proposals").proposals.map((entry) => entry.status),
    ["cancelled", "cancelled"],
  );
  assert.equal(others(), before, "nothing but the run was sent");
  $("assistant-clear").click();
});

test("an applied edit can be undone, and the reader's own text replaces what Apply writes", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  await propose("p-u");
  control("apply", "p-u").click();
  await settled();
  posted("/api/assistant/apply")[0].answer(
    Response.json({ results: [{ proposal_id: "p-u", verdict: "applied", resources: [message(7)] }] }),
  );
  await settled();
  control("undo", "p-u").click();
  await settled();
  const undo = posted("/api/assistant/undo")[0];
  assert.deepEqual(undo.body, { proposal_ids: ["p-u"] });
  undo.answer(
    Response.json({ results: [{ proposal_id: "p-u", verdict: "undone", summary: "Undid the edit.", resources: [message(7)] }] }),
  );
  await settled();
  const entry = S.assistant.items.find((item) => item.kind === "proposals").proposals[0];
  assert.equal(entry.status, "pending", "undone is pending again");
  assert.equal(S.assistant.items.at(-1).text, "Undid the edit.");
  assert.ok(control("apply", "p-u"));

  control("revise", "p-u").click();
  const box = $("assistant-log").querySelector("textarea.asst-revise");
  assert.equal(box.value, "New <b>line</b>.", "the box starts from the proposed text");
  box.value = "Mine.";
  box.dispatchEvent(new window.Event("input", { bubbles: true }));
  control("reviseSave", "p-u").click();
  await settled();
  const revise = posted("/api/assistant/revise")[0];
  assert.deepEqual(revise.body, { proposal_id: "p-u", text: "Mine." });
  revise.answer(
    Response.json({ proposal_id: "p-u", verdict: "revised", proposal: { fields: [{ field: "content", before: "Old <b>line</b>.", after: "Mine." }] } }),
  );
  await settled();
  assert.equal(entry.after, "Mine.");
  assert.equal($("assistant-log").querySelector("textarea.asst-revise"), null, "the box closes once saved");
  assert.match($("assistant-log").innerHTML, /<ins class="asst-ins">Mine\.<\/ins>/);
  $("assistant-clear").click();
});

test("an Apply that ends without verdicts leaves the card pending to try again, and reads its targets again", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  await propose("p-retry");
  applyButton().click();
  await settled();
  const first = requests.find((request) => request.url.endsWith("/api/assistant/apply"));
  const before = requests.length;
  first.answer(new Response("upstream gone", { status: 502 }));
  await settled();
  const card = S.assistant.items.find((item) => item.kind === "proposals");
  assert.equal(card.proposals[0].status, "pending");
  assert.match($("assistant-log").innerHTML, /Apply did not finish\. Try again/);
  assert.ok(
    requests.slice(before).some((request) => request.url.endsWith("/api/conversations/chat-a/messages")),
    "its target is read again",
  );
  applyButton().click();
  await settled();
  const retry = requests.filter((request) => request.url.endsWith("/api/assistant/apply")).at(-1);
  assert.notEqual(retry, first);
  assert.deepEqual(retry.body, { proposal_ids: ["p-retry"] });
  retry.answer(Response.json({ results: [{ proposal_id: "p-retry", verdict: "applied", repeated: true, resources: [] }] }));
  await settled();
  assert.equal(card.proposals[0].status, "applied");
  assert.equal(requests.filter((request) => request.url.includes("/assistant/run")).length, 1, "and nothing continues");
  $("assistant-clear").click();
});

test("markup in a reply names no action: a button the model wrote does nothing", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  const run = await ask("Say something.");
  run.used = true;
  emit(run, "assistant_message", {
    text: 'Done. <button data-wf-action="chat:regenerate" data-msg-id="7">Go</button><button data-wf-action="assistant:clear">x</button>',
    final: true,
  });
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
  const before = requests.length;
  for (const el of document.querySelectorAll("#assistant-log .msg-body *")) el.click();
  await settled();
  assert.equal(requests.length, before, "no request was made");
  assert.ok(S.assistant.items.length > 0, "and the transcript was not cleared");
  $("assistant-clear").click();
});

test("a lore entry open in the drawer takes an applied edit, or keeps unsaved edits with a notice", async () => {
  const { loadWorlds, refreshWorld } = await import("../../frontend/lorebooks.js");
  let content = "Paid at dusk.";
  const entry = () => ({ id: 5, world_id: "w1", name: "Tide tax", content, keywords: ["tax"], enabled: 1, priority: 100 });
  const base = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    const path = new URL(String(url), "https://orb.invalid").pathname;
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url: String(url), body, method: options.method || "GET" });
    if (path === "/api/worlds") return Response.json([{ id: "w1", name: "Harbour", is_global: 0, dynamic_enabled: 0 }]);
    if (path === "/api/worlds/w1/entries") return Response.json([entry()]);
    if (path === "/api/worlds/w1/entries/5") return Response.json({ ...entry(), ...body });
    if (path.startsWith("/api/worlds/w1/changesets")) return Response.json([]);
    return base(url, options);
  };
  try {
    await loadWorlds();
    const opener = document.createElement("button");
    opener.dataset.wfAction = "lorebook:open";
    opener.dataset.worldId = "w1";
    document.body.appendChild(opener);
    opener.click();
    await settled();
    document.querySelector('[data-wf-action="lorebook:selectEntry"][data-entry-id="5"]').click();
    await settled();
    assert.equal($("lb-content").value, "Paid at dusk.");

    // An applied edit, with nothing unsaved: the editor shows it, and its next Save sends it rather than the old text.
    content = "Paid at dawn.";
    await refreshWorld("w1");
    assert.equal($("lb-content").value, "Paid at dawn.");
    $("lb-priority").value = "7";
    $("lb-priority").dispatchEvent(new window.Event("input", { bubbles: true }));
    document.querySelector('[data-wf-action="lorebook:saveEntry"]').click();
    await settled();
    const save = requests.findLast((request) => request.method === "PUT");
    assert.equal(save.body.content, "Paid at dawn.");
    assert.equal(save.body.priority, 7);

    // Unsaved edits stay the reader's, and the editor says the entry changed elsewhere.
    $("lb-content").value = "My own wording.";
    $("lb-content").dispatchEvent(new window.Event("input", { bubbles: true }));
    content = "Changed by the assistant.";
    await refreshWorld("w1");
    assert.equal($("lb-content").value, "My own wording.");
    assert.ok(document.querySelector(".lb-changed-elsewhere"), "the notice is shown");
    document.querySelector('[data-wf-action="lorebook:discard"]').click();
    await settled();
    assert.equal($("lb-content").value, "Changed by the assistant.");
    assert.equal(document.querySelector(".lb-changed-elsewhere"), null);
    opener.remove();
  } finally {
    globalThis.fetch = base;
  }
});

test("Save after a change elsewhere keeps both: the applied rename and the reader's own typing", async () => {
  const { loadWorlds, refreshWorld } = await import("../../frontend/lorebooks.js");
  let stored = { id: 5, world_id: "w1", name: "Tide tax", content: "Paid at dusk.", keywords: ["tax"], enabled: 1, priority: 100 };
  const base = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    const path = new URL(String(url), "https://orb.invalid").pathname;
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url: String(url), body, method: options.method || "GET" });
    if (path === "/api/worlds") return Response.json([{ id: "w1", name: "Harbour", is_global: 0, dynamic_enabled: 0 }]);
    if (path === "/api/worlds/w1/entries") return Response.json([stored]);
    if (path === "/api/worlds/w1/entries/5") {
      stored = { ...stored, ...body };
      return Response.json(stored);
    }
    if (path.startsWith("/api/worlds/w1/changesets")) return Response.json([]);
    return base(url, options);
  };
  const opener = document.createElement("button");
  try {
    await loadWorlds();
    opener.dataset.wfAction = "lorebook:open";
    opener.dataset.worldId = "w1";
    document.body.appendChild(opener);
    opener.click();
    await settled();
    document.querySelector('[data-wf-action="lorebook:selectEntry"][data-entry-id="5"]').click();
    await settled();

    // The reader types into Content and starts a keyword; meanwhile an applied edit renames the entry.
    $("lb-content").focus();
    $("lb-content").value = "Paid at dusk, in copper.";
    $("lb-content").dispatchEvent(new window.Event("input", { bubbles: true }));
    $("lb-chip-text").value = "copp";
    stored = { ...stored, name: "Tide tithe" };
    await refreshWorld("w1");
    assert.equal($("lb-entry-name").value, "Tide tithe", "the field they left alone shows the new name");
    assert.equal($("lb-content").value, "Paid at dusk, in copper.", "their own typing stays");
    assert.equal($("lb-chip-text").value, "copp", "so does a keyword typed and not yet added");
    assert.equal(document.querySelector(".lb-changed-elsewhere"), null, "nothing they changed was changed elsewhere");
    document.querySelector('[data-wf-action="lorebook:saveEntry"]').click();
    await settled();
    assert.equal(stored.name, "Tide tithe", "Save does not revert the applied rename");
    assert.equal(stored.content, "Paid at dusk, in copper.");

    // A field changed in both places keeps the reader's version, and only that brings the notice.
    $("lb-content").value = "Mine.";
    $("lb-content").dispatchEvent(new window.Event("input", { bubbles: true }));
    stored = { ...stored, content: "Theirs.", priority: 5 };
    await refreshWorld("w1");
    assert.equal($("lb-content").value, "Mine.");
    assert.equal($("lb-priority").value, "5");
    assert.ok(document.querySelector(".lb-changed-elsewhere"));
    document.querySelector('[data-wf-action="lorebook:discard"]').click();
    await settled();
  } finally {
    opener.remove();
    globalThis.fetch = base;
  }
});

test("a chat action refreshes only the chat it changed, and only while that chat is the one in view", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "solo" },
  ];
  S.activeConvId = "chat-a";
  const run = await ask("Use the Harbour world in Campfire, then show the old branch here.");
  run.used = true;
  const reads = (path) => requests.filter((request) => request.url.endsWith(path)).length;
  emit(run, "tool_start", { call_id: "call1", tool: "set_chat_world" });
  emit(run, "mutation", {
    call_id: "call1",
    tool: "set_chat_world",
    resources: [{ kind: "chat_worlds", conversation_id: "chat-b", world_id: "w1" }],
  });
  emit(run, "tool_start", { call_id: "call2", tool: "checkpoint_chat" });
  emit(run, "mutation", {
    call_id: "call2",
    tool: "checkpoint_chat",
    resources: [{ kind: "conversation", conversation_id: "chat-c" }],
  });
  await settled();
  assert.equal(reads("/api/conversations/chat-b/worlds"), 0, "a chat not in view paints nothing");
  assert.equal(reads("/api/conversations"), 1, "the chat list is read again, in place");
  assert.equal(S.activeConvId, "chat-a", "and nothing opens the new chat");
  emit(run, "tool_start", { call_id: "call3", tool: "set_chat_world" });
  emit(run, "mutation", {
    call_id: "call3",
    tool: "set_chat_world",
    resources: [{ kind: "chat_worlds", conversation_id: "chat-a", world_id: "w1" }],
  });
  emit(run, "tool_start", { call_id: "call4", tool: "switch_branch" });
  emit(run, "mutation", { call_id: "call4", tool: "switch_branch", resources: [{ kind: "branch", conversation_id: "chat-a" }] });
  await settled();
  assert.equal(reads("/api/conversations/chat-a/worlds"), 1, "the chat in view shows its new worlds");
  assert.equal(reads("/api/conversations/chat-a/messages"), 1, "and the branch it now shows");
  emit(run, "assistant_message", { text: "Switched.", final: true });
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
  $("assistant-clear").click();
});

test("after the assistant switches a branch, the Inspector leaves a message the chat no longer shows", async () => {
  const { showBranchChange } = await import("../../frontend/chat_messages.js");
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  let branch = [
    { id: 6, role: "user", content: "Hi", parent_id: null },
    { id: 8, role: "assistant", content: "The other branch.", parent_id: 6 },
  ];
  const base = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    const path = new URL(String(url), "https://orb.invalid").pathname;
    requests.push({ url: String(url) });
    if (path === "/api/conversations/chat-a/messages") return Response.json(branch);
    if (path.endsWith("/director-log")) return Response.json({ reasoning_writer: `log for ${path}` });
    return base(url, options);
  };
  try {
    // The Inspector shows message 7, which the switch has left behind: it moves to the reply the chat now shows.
    S.inspectedMsgId = 7;
    await showBranchChange("chat-a");
    await settled();
    assert.equal(S.inspectedMsgId, 8);
    assert.ok(requests.some((request) => request.url.endsWith("/api/conversations/chat-a/messages/8/director-log")));

    // A message still on the branch stays inspected; with no reply left, nothing is.
    await showBranchChange("chat-a");
    await settled();
    assert.equal(S.inspectedMsgId, 8);
    branch = [{ id: 6, role: "user", content: "Hi", parent_id: null }];
    await showBranchChange("chat-a");
    await settled();
    assert.equal(S.inspectedMsgId, null);
  } finally {
    globalThis.fetch = base;
  }
});

test("a handoff starts only in the chat it names, once the assistant has settled, and never switches the view", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "solo" },
  ];
  S.activeConvId = "chat-a";
  S.messages = [
    { id: 6, role: "user", content: "Hi", parent_id: null },
    { id: 7, role: "assistant", content: "Hello.", parent_id: 6 },
  ];
  const regenerations = () => requests.filter((request) => request.url.includes("/messages/7/regenerate"));

  // The reader moves to another chat while the run is going: the handoff is a note, and nothing starts or switches.
  let run = await ask("Regenerate the last reply.");
  run.used = true;
  S.activeConvId = "chat-b";
  emit(run, "handoff", { call_id: "call1", tool: "regenerate_reply", handoff: { conversation_id: "chat-a", message_id: 7 } });
  emit(run, "done", { reason: "handoff", message: "Handed to the chat." });
  run.writer.close();
  await settled();
  assert.equal(regenerations().length, 0);
  assert.equal(S.activeConvId, "chat-b");
  assert.match(S.assistant.items.at(-1).text, /^Not started: the chat for regenerating message 7 is no longer the one open/);

  // In the chat it names: the chat's own regeneration starts after the assistant's stream has settled.
  S.activeConvId = "chat-a";
  S.messages = [
    { id: 6, role: "user", content: "Hi", parent_id: null },
    { id: 7, role: "assistant", content: "Hello.", parent_id: 6 },
  ];
  run = await ask("Regenerate the last reply.");
  run.used = true;
  emit(run, "handoff", { call_id: "call1", tool: "regenerate_reply", handoff: { conversation_id: "chat-a", message_id: 7 } });
  await settled();
  assert.equal(regenerations().length, 0, "not while the assistant's own stream is open");
  emit(run, "done", { reason: "handoff", message: "Handed to the chat." });
  run.writer.close();
  await settled();
  assert.equal(regenerations().length, 1);
  assert.match(new URL(regenerations()[0].url, "https://orb.invalid").pathname, /^\/api\/conversations\/chat-a\/messages\/7\/regenerate$/);
  assert.deepEqual(regenerations()[0].body, { enable_agent: S.agentEnabled }, "through the chat's own Regenerate");
  assert.match(S.assistant.items.at(-1).text, /^Started regenerating message 7 in the chat/);
});

test("an opened chat opens only from the view the request came from, and a continue starts the chat's own", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "solo" },
  ];
  const handOff = async (tool, handoff, moveTo = null) => {
    const run = await ask("Do it.");
    run.used = true;
    if (moveTo) S.activeConvId = moveTo;
    emit(run, "handoff", { call_id: "call1", tool, handoff });
    emit(run, "done", { reason: "handoff", message: "Handed off." });
    run.writer.close();
    await settled();
  };

  const saved = globalThis.localStorage;
  globalThis.localStorage = keepingStorage();
  try {
  // The reader moved to another chat meanwhile: nothing pulls them back.
  S.activeConvId = "chat-a";
  await handOff("open_chat", { conversation_id: "chat-b", title: "Campfire" }, "chat-a-elsewhere");
  assert.equal(S.activeConvId, "chat-a-elsewhere");
  assert.match(S.assistant.items.at(-1).text, /^Not opened: the view changed/);
  S.activeConvId = "chat-a";
  await handOff("open_chat", { conversation_id: "chat-b", title: "Campfire" });
  assert.equal(S.activeConvId, "chat-b");
  assert.equal(S.assistant.items.at(-1).reason, "opened");

  S.messages = [
    { id: 6, role: "assistant", content: "Hello.", parent_id: null },
    { id: 7, role: "user", content: "And then?", parent_id: 6 },
  ];
  const continues = () => requests.filter((request) => request.url.includes("/conversations/chat-b/continue"));
  await handOff("continue_chat", { conversation_id: "chat-b", message_id: 6 });
  assert.equal(continues().length, 0, "the chat has a newer last message than the one checked");
  assert.match(S.assistant.items.at(-1).text, /^Not started: the chat has a newer message/);
  await handOff("continue_chat", { conversation_id: "chat-b", message_id: 7 });
  assert.equal(continues().length, 1);
  assert.match(S.assistant.items.at(-1).text, /^Started the next reply in the chat/);

  const sends = () => requests.filter((request) => request.url.includes("/conversations/chat-b/send"));
  $("chat-input").value = "my draft";
  await handOff("send_as_user", { conversation_id: "chat-b", message_id: 7, content: "Fine." });
  assert.equal(sends().length, 0, "the last message is the user's, so Send takes no second one");
  S.messages = [{ id: 6, role: "assistant", content: "Hello.", parent_id: null }];
  await handOff("send_as_user", { conversation_id: "chat-b", message_id: 6, content: "Fine." });
  assert.equal(sends().length, 1);
  assert.equal(sends()[0].body.content, "Fine.");
  assert.equal($("chat-input").value, "my draft", "the box keeps the user's own draft");
  } finally {
    globalThis.localStorage = saved;
    $("assistant-clear").click();
  }
});

test("a Stop between the handoff's done and the run's settlement starts nothing, and says so", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  S.messages = [
    { id: 6, role: "user", content: "Hi", parent_id: null },
    { id: 7, role: "assistant", content: "Hello.", parent_id: 6 },
  ];
  const before = requests.filter((request) => request.url.includes("/regenerate")).length;
  const run = await ask("Regenerate the last reply.");
  run.used = true;
  emit(run, "handoff", { call_id: "call1", tool: "regenerate_reply", handoff: { conversation_id: "chat-a", message_id: 7 } });
  emit(run, "done", { reason: "handoff", message: "Handed to the chat." });
  await settled();
  $("assistant-send").click();
  run.writer.close();
  await settled();
  assert.equal(requests.filter((request) => request.url.includes("/regenerate")).length, before);
  assert.match(S.assistant.items.at(-1).text, /^Not started: the assistant was stopped before the chat began regenerating/);
  $("assistant-clear").click();
});

test("an edited request replaces itself and everything below it, under the name it was first sent with", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo", character_card_id: "mara" }];
  S.activeConvId = "chat-a";
  const answer = async (run) => {
    run.used = true;
    emit(run, "confirm", confirmed(`p-${run.body.request_id}`));
    emit(run, "assistant_message", { text: "Done.", final: true });
    emit(run, "done", { reason: "reply", message: "" });
    run.writer.close();
    await settled();
    return run;
  };
  const first = await answer(await ask("Who is Mara?"));
  const second = await answer(await ask("And her rival?"));
  assert.equal(first.body.edit, false);
  assert.notEqual(second.body.request_id, first.body.request_id);
  const edits = () => document.querySelectorAll('#assistant-log [data-wf-action="assistant:editRequest"]');
  assert.equal(edits().length, 2);

  // Escape puts the request back; nothing was sent and nothing removed.
  edits()[0].click();
  let box = $("assistant-log").querySelector("textarea.asst-request-box");
  assert.match($("assistant-log").textContent, /The 2 changes still waiting for Apply are discarded/);
  box.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal($("assistant-log").querySelector("textarea.asst-request-box"), null);
  assert.equal(S.assistant.items.length, 6);

  edits()[0].click();
  box = $("assistant-log").querySelector("textarea.asst-request-box");
  assert.equal(box.value, "Who is Mara?");
  box.value = "Who is Wren?";
  box.dispatchEvent(new window.Event("input", { bubbles: true }));
  box.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await settled();
  const edited = requests.find((request) => request.url.includes("/assistant/run") && request.writer && !request.used);
  assert.equal(edited.body.text, "Who is Wren?");
  assert.deepEqual([edited.body.edit, edited.body.request_id], [true, first.body.request_id]);
  assert.deepEqual(edited.body.pending, [], "the cards below went with their entries");
  assert.deepEqual(
    S.assistant.items.map((item) => item.text),
    ["Who is Wren?"],
  );
  await answer(edited);
  $("assistant-clear").click();
  S.activeConvId = null;
});

test("a standalone image stays in the assistant panel and uses the shared lightbox", async () => {
  const imageId = "b2".repeat(16);
  const run = await ask("Create a standalone portrait.");
  emit(run, "tool_start", { tool: "create_image", call_id: "image-call" });
  emit(run, "tool_result", { tool: "create_image", call_id: "image-call", ok: true, summary: "Created a picture.", ids: { image_id: imageId } });
  emit(run, "done", { reason: "reply" });
  run.writer.close();
  await settled();
  const img = $("assistant-log").querySelector(".asst-image img.workflow-artifact-image");
  assert.equal(img.getAttribute("src"), `/api/assistant/images/${imageId}`);
  assert.equal(requests.filter((r) => r.url.includes("/messages")).length, 0);
  assert.equal(img.closest(".asst-image").querySelector("a[download], [data-wf-action]"), null);
});
