import assert from "node:assert/strict";
import { test } from "node:test";

// After a stopped turn settles, the chat must show exactly what the server
// saved: this operation's own reply with its real id, never an earlier branch,
// never a cosmetic preview, and never an ID-less row claiming to be saved.

let dom = null;
let failure = "";
try {
  const { JSDOM } = await import("jsdom");
  dom = new JSDOM(
    `<!doctype html><html><body>
      <div id="chat-messages"></div>
      <button id="send-btn"></button><button id="stop-btn"></button>
      <div id="generation-status"><span class="gen-text"></span></div>
      <div id="inspector-content"></div><div id="inspector-workflow-content"></div>
      <div id="state-panel-content"></div>
    </body></html>`,
    { url: "https://orb.invalid/" },
  );
} catch (e) {
  failure = e?.message || String(e);
}

// The server the reconciliation reads back from.
let saved = [];
let offline = false;

let stream = null;
let settle = null;
let S = null;
if (dom) {
  const w = dom.window;
  globalThis.window = w;
  for (const name of ["document", "Node", "NodeFilter", "Element", "DocumentFragment", "HTMLElement", "DOMParser"]) {
    if (w[name] !== undefined) globalThis[name] = w[name];
  }
  // Layout APIs jsdom does not implement; scrolling is not under test.
  w.Element.prototype.scrollTo = () => {};
  w.Element.prototype.scrollIntoView = () => {};
  globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
  globalThis.fetch = async (url) => {
    if (offline) throw new Error("offline");
    const body = String(url).endsWith("/messages") ? saved : {};
    return { ok: true, status: 200, json: async () => structuredClone(body), text: async () => "" };
  };
  stream = await import("../../frontend/chat_stream.js");
  settle = await import("../../frontend/stream_settle.js");
  ({ S } = await import("../../frontend/state.js"));
} else {
  console.error(`SKIPPED tests/frontend/stream_reconcile.test.mjs — jsdom is unavailable (${failure}).`);
}

const it = dom ? test : test.skip;

const USER = { id: 1, role: "user", content: "hi", parent_id: null, branch_count: 1, branch_index: 0 };
const ORIGINAL = { id: 2, role: "assistant", content: "The original.", parent_id: 1, branch_count: 1, branch_index: 0 };

// A regeneration of ORIGINAL that streamed *streamed* before Stop.
function stoppedRegeneration({ streamed, preview = null }) {
  S.activeConvId = "c1";
  S.messages = [structuredClone(USER), structuredClone(ORIGINAL)];
  const op = settle.createStreamOperation({ convId: "c1", requestStop: async () => ({ active: true, settled: true }) });
  op.anchor = settle.streamAnchor(S.messages);
  S.streamOp = op;
  stream.setStreaming(true);
  document.getElementById("chat-messages").appendChild(stream.createStreamingDiv());
  S.streamingContent = streamed;
  S.editorDraftBaseline = preview === null ? null : streamed;
  S.pendingRefineDiff = preview === null ? null : { original: streamed, ops: [] };
  S.turnError = null;
  return op;
}

function bodyOf(id) {
  return document.querySelector(`.message[data-msg-id="${id}"] .msg-body`)?.textContent.trim();
}

it("the saved reply replaces the streamed bubble under its real id, and the preview diff is redrawn", async () => {
  saved = [USER, { id: 9, role: "assistant", content: "Kept prose.", parent_id: 1, branch_count: 2, branch_index: 1 }];
  offline = false;
  const op = stoppedRegeneration({ streamed: "Kept prose.", preview: "A preview the turn never kept." });

  await stream.afterStream(op, { settled: true });

  assert.deepEqual(
    S.messages.map((m) => m.id),
    [1, 9],
  );
  assert.equal(bodyOf(9), "Kept prose.");
  assert.equal(S.pendingRefineDiff, null, "no diff against a preview once the saved reply matches the draft");
  assert.equal(S.streamOp, null);
  assert.equal(S.isStreaming, false);
});

it("with nothing saved, the regenerated branch stays selected and no reply is invented", async () => {
  saved = [USER, ORIGINAL];
  offline = false;
  const op = stoppedRegeneration({ streamed: "" });

  await stream.afterStream(op, { settled: true });

  assert.deepEqual(
    S.messages.map((m) => m.id),
    [1, 2],
  );
  assert.equal(bodyOf(2), "The original.");
  assert.equal(document.querySelectorAll('.message.assistant:not([data-msg-id="2"])').length, 0);
});

it("text the server did not confirm stays on screen without an id", async () => {
  saved = [USER, ORIGINAL];
  offline = true;
  const op = stoppedRegeneration({ streamed: "Prose the refetch could not confirm." });

  await stream.afterStream(op, { settled: false });

  const last = S.messages.at(-1);
  assert.equal(last.content, "Prose the refetch could not confirm.");
  assert.equal(last.id, null, "an unconfirmed reply never gets an id to act on");
});

it("a late cleanup does not take the stop button from a newer operation", () => {
  const older = stream.beginStreamOperation("c1");
  const newer = stream.beginStreamOperation("c1");
  stream.endStreamOperation(older);
  assert.equal(S.streamOp, newer);
  stream.endStreamOperation(newer);
  assert.equal(S.streamOp, null);
});

it("EOF without a terminal event waits for settlement and keeps unconfirmed prose", async (t) => {
  saved = [USER];
  offline = false;
  S.activeConvId = "c1";
  S.messages = [structuredClone(USER)];
  S.streamingContent = null;
  let releaseStop;
  let stopReached;
  const stopping = new Promise((resolve) => {
    stopReached = resolve;
  });
  const fetch = globalThis.fetch;
  let refetched = false;
  t.mock.method(console, "error", () => {});
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (String(url).endsWith("/send")) {
      return new Response("event: token\ndata: Partial reply before EOF.\n\n");
    }
    if (String(url).endsWith("/stop")) {
      stopReached();
      return new Promise((resolve) => {
        releaseStop = () => resolve({ ok: true, json: async () => ({ active: true, settled: true }) });
      });
    }
    if (String(url).endsWith("/messages")) refetched = true;
    return fetch(url, options);
  });

  const run = stream.runStreamRequest("/conversations/c1/send", {});
  await stopping;
  assert.equal(S.isStreaming, true);
  assert.equal(refetched, false, "refetch must wait for the server's cleanup");
  releaseStop();
  await run;

  assert.equal(S.turnError.kind, "transport");
  assert.match(S.turnError.sentence, /ended before completion/);
  assert.equal(S.messages.at(-1).content, "Partial reply before EOF.");
  assert.equal(S.messages.at(-1).id, null);
  assert.equal(S.isStreaming, false);
  assert.equal(S.streamOp, null);
});

it("done and error are terminal, but speaker_done is not", async () => {
  const container = document.getElementById("chat-messages");
  for (const event of ["done", "error"]) {
    const response = new Response(`event: ${event}\ndata: Test result\n\n`);
    await stream.processSSEStream(response, container, { el: null });
    if (event === "error") assert.equal(S.turnError.headline, "Test result");
  }
  const response = new Response("event: speaker_done\ndata: {}\n\n");
  await assert.rejects(stream.processSSEStream(response, container, { el: null }), /ended before completion/);
});
