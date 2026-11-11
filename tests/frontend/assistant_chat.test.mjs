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
let writes = 0;
globalThis.localStorage = {
  getItem: (key) => (key === "orb-assistant-transcript" ? '{"not":"a list"' : key === "orb-assistant-open" ? "1" : null),
  setItem() {
    writes += 1;
    throw new Error("QuotaExceededError");
  },
};

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
    // The chat list as this page already has it: a refresh in place changes nothing a test relies on.
    if (request.url.endsWith("/api/conversations")) return Response.json(S.conversations);
    if (request.url.endsWith("/api/assistant/apply")) return new Promise((resolve) => (request.answer = resolve));
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

test("corrupt or failing storage leaves a working assistant in memory", () => {
  assert.deepEqual(S.assistant.items, []);
  assert.equal(S.assistant.open, true, "the remembered open state was restored on the wide layout");
  assert.ok($("assistant-panel").classList.contains("open"));
  assert.ok(writes > 0, "writes were attempted and their failure was absorbed");
});

const panelOpen = (id) => $(id).classList.contains("open");

test("the panel is exclusive with Tools and the Inspector, and every button that opens it says so", async () => {
  const { openUtilityPanel } = await import("../../frontend/panels.js");
  for (const id of ["assistant-btn", "doc-assistant-btn"]) assert.ok($(id).classList.contains("btn-active"));
  $("assistant-input").value = "a draft";
  openUtilityPanel("tools-panel");
  assert.equal(panelOpen("assistant-panel"), false);
  assert.equal(S.assistant.open, false, "opening another panel closes the assistant's state too");
  assert.equal($("assistant-btn").classList.contains("btn-active"), false);

  // The drawer's button opens it beside the lorebook, and the same action closes it.
  $("lorebook-drawer").insertAdjacentHTML(
    "beforeend",
    '<button id="lb-assistant-btn" class="asst-launch" data-wf-action="assistant:toggle"></button>',
  );
  $("lb-assistant-btn").click();
  await settled();
  assert.ok(panelOpen("assistant-panel"));
  assert.equal(panelOpen("tools-panel"), false);
  assert.equal(document.activeElement, $("assistant-input"), "the wide layout focuses the input");
  assert.equal($("assistant-input").value, "a draft");
  $("doc-assistant-mobile-btn").click();
  await settled();
  assert.equal(panelOpen("assistant-panel"), false);
  $("lb-assistant-btn").remove();
  $("assistant-input").value = "";
});

test("a run and its card outlive a closed panel, and the buttons show both", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  const app = $("app");
  const run = await ask("Fix the messages.");
  run.used = true;
  assert.ok(app.hasAttribute("data-asst-running"));
  assert.equal(panelOpen("assistant-panel"), false);
  for (const preview of ["p-shut", "p-shut-2"]) emit(run, "confirm", confirmed(preview, message(preview === "p-shut" ? 7 : 8)));
  emit(run, "assistant_message", { text: "Proposed.", final: true });
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
  assert.equal(app.hasAttribute("data-asst-running"), false);
  assert.ok(app.hasAttribute("data-asst-pending"));
  assert.equal(app.style.getPropertyValue("--asst-pending"), "2");

  // Reopened, the card is still the one to apply.
  $("assistant-btn").click();
  assert.ok(applyButton());
  document.querySelector('#assistant-log [data-wf-action="assistant:cancel"]').click();
  assert.equal(app.hasAttribute("data-asst-pending"), false);
  assert.equal(app.style.getPropertyValue("--asst-pending"), "");
  $("assistant-clear").click();
});

test("a suggested request fills the input for the view now open and sends nothing", async () => {
  S.conversations = [{ id: "chat-s", title: "Harbour", kind: "solo" }];
  S.activeConvId = null;
  $("assistant-panel").dispatchEvent(new window.Event("pointerenter"));
  const offered = () => [...document.querySelectorAll('#assistant-log [data-wf-action="assistant:suggest"]')];
  const library = offered().map((el) => el.dataset.text);
  S.activeConvId = "chat-s";
  $("assistant-panel").dispatchEvent(new window.Event("pointerenter"));
  assert.notDeepEqual(
    offered().map((el) => el.dataset.text),
    library,
    "the suggestions follow the view",
  );
  offered()[0].click();
  await settled();
  assert.equal($("assistant-input").value, offered()[0].dataset.text);
  assert.equal(requests.length, 0);
  $("assistant-input").value = "";
  S.activeConvId = null;
});

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
  assert.deepEqual(run.body.items, [{ kind: "user", text: "Who is here?", view: "Chat: Harbour" }]);
  assert.equal([...S.operations.values()].find((op) => op.kind === "assistant").target.conversation_id, "chat-a");

  // While it runs: Clear is off, the button is Stop, and a second Send starts nothing.
  assert.equal($("assistant-clear").disabled, true);
  assert.equal($("assistant-send").textContent, "Stop");
  S.activeConvId = "chat-b";
  emit(run, "tool_start", { call_id: "step1", tool: "get_context" });
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

  // The next request replays the earlier one under its own view, and a group names no character.
  const next = await ask("And now?");
  next.used = true;
  assert.deepEqual(next.body.context, { mode: "chat", conversation_id: "chat-b" });
  assert.deepEqual(next.body.items.at(-1), { kind: "user", text: "And now?", view: "Group chat: Campfire" });
  assert.equal(next.body.items[0].view, "Chat: Harbour");
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

test("another open tab blocks Send, and an IME Enter is not a Send", async () => {
  const input = $("assistant-input");
  input.value = "composed text";
  let reachedDocument = 0;
  document.addEventListener("keydown", () => reachedDocument++);
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true }));
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true }));
  await settled();
  assert.equal(requests.length, 0);
  assert.equal(reachedDocument, 0, "keys typed in the panel never reach the document's shortcuts");
  S.hasMultipleTabs = true;
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  await settled();
  S.hasMultipleTabs = false;
  assert.equal(requests.length, 0);
  assert.equal(input.value, "composed text");
});

test("a committed change is read again by id, and a run cut short reads everything again once and resends nothing", async () => {
  S.conversations = [{ id: "chat-long", title: "t".repeat(400), kind: "solo" }];
  S.activeConvId = "chat-long";
  S.settings = { ...S.settings, user_name: "local" };
  const run = await ask("Turn the Editor off, then add lore for the ferry.");
  run.used = true;
  // A long title is shortened at Send, so replaying it can never push a later request past the server's limit.
  assert.ok(run.body.items.at(-1).view.length < 200);

  const keys = { kind: "settings", keys: ["enabled_tools"] };
  emit(run, "tool_start", { call_id: "call1", tool: "set_pipeline_toggle" });
  emit(run, "mutation", { call_id: "call1", tool: "set_pipeline_toggle", resources: [keys] });
  await settled();
  assert.ok(requests.some((request) => request.url.endsWith("/api/settings")), "the toggle's keys are read again");
  assert.deepEqual(S.settings.enabled_tools, { editor_apply_patch: false });
  assert.equal(S.settings.user_name, "local", "only the keys the toggle wrote are taken from the row");
  emit(run, "tool_result", { call_id: "call1", tool: "set_pipeline_toggle", ok: true, summary: "Turned Editor off.", ids: {} });

  // The lore write starts, and the connection ends before anything says whether it committed.
  emit(run, "tool_start", { call_id: "call2", tool: "create_lorebook_entry" });
  await settled();
  const reads = (path) => requests.filter((request) => request.url.endsWith(path)).length;
  assert.equal(reads("/api/worlds"), 0);
  run.writer.close();
  await settled();
  const lore = S.assistant.items.findLast((item) => item.tool === "create_lorebook_entry");
  assert.equal(lore.text, "No result was received; check before asking again.");
  for (const path of ["/api/worlds", "/api/characters", "/api/user-personas", "/api/conversations", "/chat-long/messages"])
    assert.equal(reads(path), 1, `${path} is read again once the run has settled`);
  assert.equal(requests.filter((request) => request.url.includes("/assistant/run")).length, 1, "nothing is resent");
  assert.equal(S.operations.size, 0);
});

// A storage that keeps what is written, as a page that is about to be reloaded would find it.
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

test("a reload mid-tool stores it as unanswered, and the next request carries it only as a record", async () => {
  $("assistant-clear").click();
  const saved = globalThis.localStorage;
  globalThis.localStorage = keepingStorage();
  try {
    S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
    S.activeConvId = "chat-a";
    const run = await ask("Add lore entry Ferry");
    run.used = true;
    emit(run, "tool_start", { call_id: "call1", tool: "create_lorebook_entry" });
    await settled();
    // The page closes here: what it stored already says the tool got no result.
    const { parseTranscript, runItems } = await import("../../frontend/assistant_view.js");
    const reloaded = parseTranscript(globalThis.localStorage.kept.get("orb-assistant-transcript"));
    assert.equal(reloaded.at(-1).text, "No result was received; check before asking again.");
    const replay = runItems([...reloaded, { kind: "user", text: "Which worlds do I have?", context: reloaded[0].context }]);
    assert.deepEqual(replay.at(-1), { kind: "user", text: "Which worlds do I have?", view: "Chat: Harbour" });
    assert.match(replay.at(-2).text, /^failed: No result was received; check before asking again\.$/);
    run.writer.close();
    await settled();
  } finally {
    globalThis.localStorage = saved;
    $("assistant-clear").click();
  }
});

test("Apply sends the card's ids alone, settles each edit by its verdict, and sends only a busy one again", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  await propose("p1", confirmed("p2", message(8), "message 8"), confirmed("p3", { kind: "persona", persona_id: 3 }, "persona Wren"));
  assert.ok(applyButton(), "the held card can be applied");
  assert.equal(document.querySelectorAll('#assistant-log [data-wf-action="assistant:apply"]').length, 1, "one Apply");
  assert.equal($("assistant-log").querySelector(".asst-diff b"), null, "the diff is escaped text");
  applyButton().click();
  await settled();
  let applies = requests.filter((request) => request.url.endsWith("/api/assistant/apply"));
  assert.equal(applies.length, 1);
  assert.deepEqual(applies[0].body, { proposal_ids: ["p1", "p2", "p3"] });
  assert.equal(applyButton().disabled, true, "no second Apply while the first is in flight");
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

  // The busy one is still pending, so Apply stays and sends it alone.
  applyButton().click();
  await settled();
  applies = requests.filter((request) => request.url.endsWith("/api/assistant/apply"));
  assert.deepEqual(applies[1].body, { proposal_ids: ["p2"] });
  applies[1].answer(Response.json({ results: [{ proposal_id: "p2", verdict: "applied", resources: [message(8)] }] }));
  await settled();
  assert.equal(card.proposals[1].status, "applied");
  assert.equal(applyButton(), null, "nothing is left to apply");
  assert.equal(requests.filter((request) => request.url.includes("/assistant/run")).length, 1, "nothing continues by itself");
  $("assistant-clear").click();
});

test("a view change during Apply repaints nothing there, and a new request or Cancel closes the card", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "solo" },
  ];
  S.activeConvId = "chat-a";
  await propose("p-view");
  applyButton().click();
  await settled();
  S.activeConvId = "chat-b";
  const apply = requests.find((request) => request.url.endsWith("/api/assistant/apply"));
  apply.answer(Response.json({ results: [{ proposal_id: "p-view", verdict: "stale", message: "Changed." }] }));
  await settled();
  assert.equal(requests.filter((request) => request.url.includes("/messages")).length, 0, "the chat now in view is untouched");
  const card = S.assistant.items.find((item) => item.kind === "proposals");
  assert.equal(card.proposals[0].status, "stale");
  // Ask again puts the request back for the reader to send; nothing is sent by itself.
  document.querySelector('[data-wf-action="assistant:askAgain"]').click();
  assert.equal($("assistant-input").value, "Fix the messages.");
  $("assistant-input").value = "";

  // A new request closes a card still held.
  S.activeConvId = "chat-a";
  await propose("p-old");
  const next = await ask("Something else.");
  next.used = true;
  assert.equal(S.assistant.items.findLast((item) => item.kind === "proposals").proposals[0].status, "historical");
  assert.equal(applyButton(), null);
  emit(next, "done", { reason: "reply", message: "" });
  next.writer.close();
  await settled();

  // Cancel settles every pending edit on the card and sends nothing.
  await propose("p-cancel", confirmed("p-cancel-2", message(8)));
  const before = requests.length;
  document.querySelector('#assistant-log [data-wf-action="assistant:cancel"]').click();
  assert.deepEqual(
    S.assistant.items.findLast((item) => item.kind === "proposals").proposals.map((entry) => entry.status),
    ["cancelled", "cancelled"],
  );
  assert.equal(requests.length, before);
  $("assistant-clear").click();
});

test("a settings refresh waits while the reader types in the Tools panel, then repaints", async () => {
  const list = $("tools-list");
  list.innerHTML = '<input id="lg-max-words" type="number" value="300">';
  const field = $("lg-max-words");
  field.focus();
  field.value = "77";
  const run = await ask("Turn the Director on.");
  run.used = true;
  const keys = { kind: "settings", keys: ["enabled_tools"] };
  emit(run, "tool_start", { call_id: "call1", tool: "set_pipeline_toggle" });
  emit(run, "mutation", { call_id: "call1", tool: "set_pipeline_toggle", resources: [keys] });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(requests.some((request) => request.url.endsWith("/api/settings")), "the refresh itself has run");
  assert.equal($("lg-max-words"), field, "the panel was not rewritten under the reader");
  assert.equal(field.value, "77");
  field.blur();
  // The held repaint runs on a timer, after the field's own change handler.
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.notEqual($("lg-max-words"), field, "the held repaint ran once focus left");
  // A checkbox there saves its own flag alone, so the server keeps a flag the assistant set that this tab has not seen.
  const box = document.querySelector('#tools-list [data-wf-action="settings:toolEnabled"]');
  box.checked = !box.checked;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  await settled();
  const save = requests.findLast((request) => request.url.endsWith("/api/settings") && request.body);
  assert.deepEqual(save.body, { enabled_tools: { [box.dataset.toolId]: box.checked } });
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
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

test("a media job's message is read again once it commits, and everything after a failed result or a Stop", async () => {
  S.conversations = [
    { id: "chat-a", title: "Harbour", kind: "solo" },
    { id: "chat-b", title: "Campfire", kind: "solo" },
  ];
  S.activeConvId = "chat-a";
  const reads = (path = "/api/conversations/chat-a/messages") => requests.filter((request) => request.url.endsWith(path)).length;
  const target = { kind: "media", conversation_id: "chat-a", message_id: 7 };
  let run = await ask("Make an image of the last reply.");
  run.used = true;
  emit(run, "tool_start", { call_id: "call1", tool: "generate_image" });
  emit(run, "progress", { label: "Rendering in ComfyUI..." });
  await settled();
  assert.equal($("assistant-status").textContent, "Rendering in ComfyUI...", "progress shows in the panel");
  emit(run, "mutation", { call_id: "call1", tool: "generate_image", resources: [{ ...target, attachment_ids: [3] }] });
  emit(run, "tool_result", { call_id: "call1", tool: "generate_image", ok: true, summary: "Saved an image.", ids: {} });
  await settled();
  assert.equal(reads(), 1, "the chat in view shows the saved image");
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
  assert.equal(reads(), 1, "a run that answered with every result ok reads nothing more");
  assert.ok(!JSON.stringify(S.assistant.items).includes("Rendering"), "progress is not kept in the transcript");

  // A failed result, then an ordinary answer: the job may have saved as it failed, so everything is read again.
  const worlds = reads("/api/worlds");
  run = await ask("Now speech for it.");
  run.used = true;
  emit(run, "tool_start", { call_id: "call1", tool: "generate_speech" });
  emit(run, "tool_result", { call_id: "call1", tool: "generate_speech", ok: false, summary: "May have been saved.", ids: {} });
  emit(run, "assistant_message", { text: "Check the message.", final: true });
  emit(run, "done", { reason: "reply", message: "" });
  run.writer.close();
  await settled();
  assert.equal(reads(), 2);
  assert.equal(reads("/api/worlds"), worlds + 1);

  // Stopped: once the run has settled, everything is read again.
  run = await ask("Once more.");
  run.used = true;
  emit(run, "tool_start", { call_id: "call1", tool: "generate_image" });
  await settled();
  $("assistant-send").click();
  emit(run, "done", { reason: "stopped", message: "" });
  run.writer.close();
  await settled();
  assert.equal(reads(), 3);
  assert.equal(S.operations.size, 0);

  // A chat no longer in view is not repainted.
  run = await ask("And again.");
  run.used = true;
  S.activeConvId = "chat-b";
  emit(run, "tool_start", { call_id: "call1", tool: "generate_image" });
  run.writer.close();
  await settled();
  assert.equal(reads(), 3);
  S.activeConvId = "chat-a";
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
  assert.ok(panelOpen("assistant-panel"), "the wide layout keeps the panel beside the chat");
});

test("on the narrow layout a handoff that starts closes the panel over the chat, and one that does not leaves it", async () => {
  S.conversations = [{ id: "chat-a", title: "Harbour", kind: "solo" }];
  S.activeConvId = "chat-a";
  window.matchMedia = () => ({ matches: true });
  const handOff = async () => {
    S.messages = [
      { id: 6, role: "user", content: "Hi", parent_id: null },
      { id: 7, role: "assistant", content: "Hello.", parent_id: 6 },
    ];
    const run = await ask("Regenerate the last reply.");
    run.used = true;
    emit(run, "handoff", { call_id: "call1", tool: "regenerate_reply", handoff: { conversation_id: "chat-a", message_id: 7 } });
    emit(run, "done", { reason: "handoff", message: "Handed to the chat." });
    run.writer.close();
    await settled();
  };
  try {
    S.activeConvId = "chat-b";
    await handOff();
    assert.ok(panelOpen("assistant-panel"), "nothing started, so the reader keeps the note");
    S.activeConvId = "chat-a";
    await handOff();
    assert.equal(panelOpen("assistant-panel"), false);
    assert.equal(S.assistant.open, false);
  } finally {
    delete window.matchMedia;
    $("assistant-btn").click();
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
