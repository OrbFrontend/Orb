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
      <div id="chat-messages"></div><div id="char-list"></div>
      <button id="send-btn"></button><button id="stop-btn"></button>
      <div id="generation-status"><span class="gen-text"></span></div>
      <div id="inspector-content"></div><div id="inspector-workflow-content"></div>
      <div id="state-panel-content"></div>
      <div id="avatar-popup" class="hidden"><img id="avatar-popup-image"></div>
    </body></html>`,
    { url: "https://orb.invalid/" },
  );
} catch (e) {
  failure = e?.message || String(e);
}

// The server the reconciliation reads back from.
let saved = [];
let offline = false;
let classify = async () => "neutral";

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
  globalThis.fetch = async (url, options) => {
    if (offline) throw new Error("offline");
    const body = String(url).endsWith("/messages") ? saved
      : String(url).endsWith("/expressions") ? { labels: ["joy", "anger", "neutral"] }
      : String(url).endsWith("/classify-emotion") ? { label: await classify(JSON.parse(options.body).text) } : {};
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

it("Expression Playback keeps saved Editor prose buffered through settlement and reveals expression runs", async () => {
  const playback = await import("../../frontend/expression_playback.js");
  const { registerAction } = await import("../../frontend/workflow_api.js");
  registerAction("expression-playback", "advance", playback.advanceExpressionPlayback);
  const labels = ["joy", "joy", "anger"];
  let release;
  classify = () => new Promise((resolve) => { release = () => resolve(labels.shift()); });
  S.settings.expression_rendering = "expression";
  S.localMlFeatures.emotion_classifier = { present: true, enabled: true, deps_ok: true };
  S.activeCharId = 7;
  S.allCharacters = [{ id: 7, has_expressions: 1 }];
  saved = [USER, { id: 19, role: "assistant", content: "Hello. Welcome! Leave.", parent_id: 1 }];
  offline = false;
  const op = stoppedRegeneration({ streamed: "Writer text.", preview: "Preview never saved." });
  S.expressionBuffering = true;
  await stream.afterStream(op);
  assert.equal(bodyOf(19), "", "settlement must not flash the full saved text");
  for (let i = 0; i < 3; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    release();
  }
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(bodyOf(19), "Hello. Welcome!");
  assert.equal(playback.expressionPlaybackCue().label, "joy");
  assert.equal(S.messages.at(-1).content, "Hello. Welcome! Leave.", "storage and context retain the complete reply");
  playback.handleExpressionPlaybackKey({ code: "Space", target: document.body, preventDefault() { this.prevented = true; } });
  assert.equal(bodyOf(19), "Hello. Welcome! Leave.");
  assert.equal(playback.expressionPlaybackCue().label, "anger");
  assert.equal(document.querySelector('[data-wf-action="expression-playback:advance"]'), null);
  playback.cancelExpressionPlayback();
  S.settings.expression_rendering = "classic";
});

it("group playback follows the revealed speaker and respects typing, repeat keys and modals", async () => {
  const playback = await import("../../frontend/expression_playback.js");
  S.activeConvId = "group";
  S.settings.expression_rendering = "expression";
  S.localMlFeatures.emotion_classifier = { present: true, enabled: true, deps_ok: true };
  S.groupCast = { members: [{ id: 1, character_card_id: 7 }, { id: 2, character_card_id: 8 }] };
  S.allCharacters = [{ id: 7, has_expressions: 1 }, { id: 8, has_expressions: 1 }];
  S.messages = [{ id: 21, role: "assistant", content: "Hello.", speaker_member_id: 1 }, { id: 22, role: "assistant", content: "Goodbye.", speaker_member_id: 2 }];
  classify = async () => "joy";
  await playback.startExpressionPlayback(S.messages);
  assert.equal(playback.expressionPlaybackCue().charId, 7);
  assert.equal(document.querySelector('[data-msg-id="22"]'), null);
  const textarea = document.createElement("textarea");
  const event = { code: "Space", target: textarea, preventDefault() { assert.fail("typing must not advance"); } };
  playback.handleExpressionPlaybackKey(event);
  playback.handleExpressionPlaybackKey({ ...event, target: document.body, repeat: true });
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  document.body.appendChild(modal);
  playback.handleExpressionPlaybackKey({ ...event, target: document.body });
  modal.remove();
  assert.equal(playback.expressionPlaybackCue().charId, 7);
  document.getElementById("avatar-popup").classList.remove("hidden");
  document.querySelector('[data-wf-action="expression-playback:advance"]').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(playback.expressionPlaybackCue().charId, 8);
  assert.equal(bodyOf(22), "Goodbye.");
  assert.equal(document.getElementById("avatar-popup-image").getAttribute("src"), "/api/characters/8/expressions/joy");
  document.getElementById("avatar-popup").classList.add("hidden");
  playback.cancelExpressionPlayback();
  S.groupCast = null;
  S.settings.expression_rendering = "classic";
});

it("sentences classified while the reply streams are not classified again at settlement", async () => {
  const playback = await import("../../frontend/expression_playback.js");
  S.activeConvId = "c1";
  S.settings.expression_rendering = "expression";
  S.localMlFeatures.emotion_classifier = { present: true, enabled: true, deps_ok: true };
  S.activeCharId = 7;
  S.allCharacters = [{ id: 7, has_expressions: 1 }];
  const seen = [];
  classify = async (text) => {
    seen.push(text.trim());
    return "joy";
  };
  S.expressionBuffering = true;
  playback.beginExpressionPrewarm();
  playback.prewarmExpressionLabels("Hello. Welcome! Le", undefined);
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.deepEqual(seen, ["Hello.", "Welcome!"], "only settled sentences warm, never the growing tail");
  S.expressionBuffering = false;
  // The Editor rewrote the second sentence before the reply was saved.
  S.messages = [{ id: 24, role: "assistant", content: "Hello. Welcome back! Leave." }];
  await playback.startExpressionPlayback(S.messages);
  assert.deepEqual(seen, ["Hello.", "Welcome!", "Welcome back!", "Leave."]);
  assert.equal(playback.expressionPlaybackCue().label, "joy");
  playback.cancelExpressionPlayback();
  S.settings.expression_rendering = "classic";
});

it("late expression classification cannot restore playback after a conversation switch", async () => {
  const playback = await import("../../frontend/expression_playback.js");
  S.activeConvId = "c1";
  S.settings.expression_rendering = "expression";
  S.activeCharId = 7;
  S.messages = [{ id: 23, role: "assistant", content: "Hello. Goodbye." }];
  let release;
  classify = () => new Promise((resolve) => { release = resolve; });
  const loading = playback.startExpressionPlayback(S.messages);
  await new Promise((resolve) => setTimeout(resolve, 0));
  S.activeConvId = "c2";
  release("joy");
  await loading;
  assert.equal(S.expressionPlayback, null);
  S.settings.expression_rendering = "classic";
});

it("settings keep the expression model beside its rendering mode and save the selected mode", async () => {
  const { renderSettings } = await import("../../frontend/settings.js");
  const form = document.createElement("div");
  form.id = "settings-form";
  document.body.appendChild(form);
  const originalFetch = globalThis.fetch;
  const info = { present: false, enabled: false, deps_ok: true, size_mb: 50 };
  S.localMlFeatures = { emotion_classifier: info };
  S.settings = { expression_rendering: "classic" };
  globalThis.fetch = async (url, options) => {
    const result = String(url).endsWith("/local-ml/status")
      ? { deps_ok: true, features: { emotion_classifier: info } }
      : { ...S.settings, ...JSON.parse(options.body) };
    return { ok: true, json: async () => result };
  };
  try {
    renderSettings();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(form.querySelector('#expression-playback-settings [data-ml-feature="emotion_classifier"][data-ml-act="download"]'));
    assert.equal(form.querySelector('#local-ml-section [data-ml-feature="emotion_classifier"]'), null);
    assert.equal(form.querySelector("[data-expression-rendering]").disabled, true);
    info.present = true;
    info.enabled = true;
    renderSettings();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const select = form.querySelector("[data-expression-rendering]");
    assert.equal(select.disabled, false);
    select.value = "expression";
    select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(S.settings.expression_rendering, "expression");
    // Expression-based rendering always waits for the saved reply; the stored choice survives.
    const baked = form.querySelector('[data-setting-toggle="hideUntilBaked"]');
    assert.equal(baked.disabled, true);
    assert.equal(baked.checked, true);
    select.value = "classic";
    select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    assert.equal(baked.disabled, false);
    assert.equal(baked.checked, S.hideUntilBaked);
  } finally {
    globalThis.fetch = originalFetch;
    S.settings.expression_rendering = "classic";
    form.remove();
  }
});
