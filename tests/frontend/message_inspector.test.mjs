import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";
import { S } from "../../frontend/state.js";

const dom = new JSDOM("<!doctype html><body></body>");
globalThis.document = dom.window.document;
globalThis.CSS ??= { escape: (value) => String(value) };

// The module listens on `document` as it loads, so it comes in after the DOM.
const { inlineInspectorHtml, inspectorBlockHtml, rememberInspection, renderLiveInspector, turnViewFromLog } =
  await import("../../frontend/message_inspector.js");

const EMPTY_LOG = {
  active_moods: [],
  mood_data_available: false,
  tool_calls: [],
  injection_block: "",
  agent_latency_ms: 0,
  reasoning_director: "",
  reasoning_writer: "",
  reasoning_editor: "",
  feedback: {},
  state: { changes: [], rejected: [], dropped: [] },
  decision_evaluations: {},
};

const reply = { id: 7, role: "assistant", content: "Hi." };

beforeEach(() => {
  S.activeConvId = "c1";
  S.messages = [{ id: 6, role: "user", content: "Hello" }, reply];
  S.inspectorInline = true;
  S.inlineInspectorOpen = true;
  S.reasoningOpen = true;
  S.moodFragments = [
    { id: "tense", label: "Tense" },
    { id: "grounded", label: "Grounded" },
  ];
  S.cardMoodFragments = [];
});

const view = (log) => turnViewFromLog(reply.id, { ...EMPTY_LOG, ...log });

test("a reply with nothing to show gets no block at all", () => {
  assert.equal(inspectorBlockHtml(view({})), "");
});

test("disabled reasoning and empty feedback leave no section behind", () => {
  const html = inspectorBlockHtml(view({ agent_latency_ms: 1500, feedback: { suggested_actions: "" } }));
  assert.match(html, /Agent Latency/);
  assert.doesNotMatch(html, /Reasoning/);
  assert.doesNotMatch(html, /Feedback/);
  assert.doesNotMatch(html, /Moods/);
});

test("the summary line shows active moods and decision values, not latency", () => {
  const html = inspectorBlockHtml(
    view({
      active_moods: ["grounded"],
      mood_data_available: true,
      agent_latency_ms: 15060,
      decision_evaluations: {
        version: 2,
        evaluations: [{ fragment_id: "outcome", fragment_label: "Outcome", outcome: "true", probability: 0.8 }],
        skipped: [],
      },
    }),
  );
  const summary = html.slice(html.indexOf("<summary"), html.indexOf("</summary>"));
  assert.match(summary, /style-tag active">Grounded</);
  assert.doesNotMatch(summary, /Tense/);
  // The chip shows the value alone; the hover names the decision.
  assert.match(summary, /class="msg-inspect-chip" title="Outcome: [^"]+">(?!Outcome)[^<]+</);
  assert.doesNotMatch(summary, /15\.1s|15060/);
});

test("only the passes that wrote reasoning get a tab, and the last one is shown", () => {
  const html = inspectorBlockHtml(view({ reasoning_director: "plan the scene", reasoning_editor: "tighten" }));
  assert.match(html, /data-inspect-pass="0"/);
  assert.match(html, /data-inspect-pass="2"/);
  assert.doesNotMatch(html, /data-inspect-pass="1"/);
  assert.match(html, /class="reasoning-box">tighten</);
  // A saved reply's box is not the streaming one.
  assert.doesNotMatch(html, /id="reasoning-box"/);
});

test("the shared open flags drive the block and its sections", () => {
  S.inlineInspectorOpen = false;
  S.reasoningOpen = false;
  const html = inspectorBlockHtml(view({ reasoning_writer: "draft" }));
  assert.match(html, /<details class="msg-inspect" data-inspect-section="inline">/);
  assert.match(html, /data-inspect-section="reasoning">/);
  S.inlineInspectorOpen = true;
  assert.match(inspectorBlockHtml(view({ reasoning_writer: "draft" })), /data-inspect-section="inline" open>/);
});

test("the chat renders a cached reply's block only while the setting is on", () => {
  rememberInspection(reply.id, { ...EMPTY_LOG, injection_block: "**Scene Guidance**" });
  assert.match(inlineInspectorHtml(reply), /Scene Guidance/);
  assert.equal(inlineInspectorHtml(S.messages[0]), "");
  S.inspectorInline = false;
  assert.equal(inlineInspectorHtml(reply), "");
});

test("a conversation switch drops the other conversation's cache", () => {
  rememberInspection(reply.id, { ...EMPTY_LOG, injection_block: "old" });
  S.activeConvId = "c2";
  assert.equal(inlineInspectorHtml(reply), "");
});

function streamingBubble() {
  document.body.innerHTML = `<div id="chat-messages"><div class="message assistant">
    <div class="msg-body" id="streaming-body"></div><div class="msg-inspect-live"></div><div class="msg-toolbar"></div>
  </div></div>`;
  S.streamingBodyEl = document.getElementById("streaming-body");
  return document.querySelector(".msg-inspect-live");
}

test("the streaming reply's block opens the running pass's box for the first delta", () => {
  const slot = streamingBubble();
  Object.assign(S, {
    isStreaming: true,
    lastDirectorData: null,
    lastDecisions: null,
    lastFeedback: null,
    lastState: null,
    reasoningDirector: "",
    reasoningWriter: "",
    reasoningEditor: "",
    reasoningPassActive: 0,
    reasoningPassSelected: 0,
  });
  S.reasoningEnabled = { director: true, writer: false, editor: false };
  assert.equal(renderLiveInspector(), true);
  assert.ok(slot.querySelector("#reasoning-box"));

  // With the pass off and nothing else to show yet, the slot stays empty.
  S.reasoningEnabled = { director: false, writer: false, editor: false };
  assert.equal(renderLiveInspector(), false);
  assert.equal(slot.innerHTML, "");

  S.lastDirectorData = { active_moods: ["tense"], agent_latency_ms: 900, injection_block: "", tool_calls: [] };
  assert.equal(renderLiveInspector(), true);
  assert.match(slot.innerHTML, /style-tag active">Tense</);
  S.isStreaming = false;
  S.streamingBodyEl = null;
});
