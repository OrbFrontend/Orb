import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";
import { S } from "../../frontend/state.js";

const dom = new JSDOM("<!doctype html><body></body>");
globalThis.document = dom.window.document;
globalThis.CSS ??= { escape: (value) => String(value) };

// The module listens on `document` as it loads, so it comes in after the DOM.
const {
  inlineInspectorHtml,
  inlineReasoningHtml,
  inspectorBlockHtml,
  reasoningBlockHtml,
  rememberInspection,
  renderLiveInspector,
  turnViewFromLog,
} = await import("../../frontend/message_inspector.js");

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
  S.inlineReasoningOpen = true;
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
  assert.equal(reasoningBlockHtml(view({})), "");
});

test("disabled reasoning and empty feedback leave no section behind", () => {
  const html = inspectorBlockHtml(view({ agent_latency_ms: 1500, feedback: { suggested_actions: "" } }));
  assert.match(html, /Agent Latency/);
  assert.doesNotMatch(html, /Reasoning/);
  assert.equal(reasoningBlockHtml(view({ agent_latency_ms: 1500 })), "");
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

test("reasoning gets its own block, not a section of the Inspector's", () => {
  const log = view({ reasoning_writer: "draft", agent_latency_ms: 900 });
  assert.doesNotMatch(inspectorBlockHtml(log), /Reasoning|reasoning-box/);
  const html = reasoningBlockHtml(log);
  assert.match(html, /^<details class="msg-inspect msg-reasoning" data-inspect-section="inline_reasoning" open>/);
  assert.match(html, /class="msg-inspect-title">Reasoning</);
  assert.match(html, /class="reasoning-box">draft</);
});

test("only the passes that wrote reasoning get a tab, and the last one is shown", () => {
  const html = reasoningBlockHtml(view({ reasoning_director: "plan the scene", reasoning_editor: "tighten" }));
  assert.match(html, /data-inspect-pass="0"/);
  assert.match(html, /data-inspect-pass="2"/);
  assert.doesNotMatch(html, /data-inspect-pass="1"/);
  assert.match(html, /class="reasoning-box">tighten</);
  // A saved reply's box is not the streaming one.
  assert.doesNotMatch(html, /id="reasoning-box"/);
});

test("the shared open flags drive the block and its sections", () => {
  S.inlineInspectorOpen = false;
  S.inlineReasoningOpen = false;
  const log = view({ reasoning_writer: "draft", injection_block: "guidance" });
  assert.match(inspectorBlockHtml(log), /<details class="msg-inspect" data-inspect-section="inline">/);
  assert.match(inspectorBlockHtml(log), /data-inspect-section="injection_block">/);
  assert.match(reasoningBlockHtml(log), /data-inspect-section="inline_reasoning">/);
  S.inlineInspectorOpen = true;
  S.inlineReasoningOpen = true;
  assert.match(inspectorBlockHtml(log), /data-inspect-section="inline" open>/);
  assert.match(reasoningBlockHtml(log), /data-inspect-section="inline_reasoning" open>/);
});

test("the chat's Reasoning block opens apart from the panel's Reasoning section", () => {
  const log = view({ reasoning_writer: "draft" });
  S.reasoningOpen = false;
  assert.match(reasoningBlockHtml(log), /data-inspect-section="inline_reasoning" open>/);
  S.reasoningOpen = true;
  S.inlineReasoningOpen = false;
  assert.match(reasoningBlockHtml(log), /data-inspect-section="inline_reasoning">/);
});

test("the chat renders a cached reply's block only while the setting is on", () => {
  rememberInspection(reply.id, { ...EMPTY_LOG, injection_block: "**Scene Guidance**", reasoning_writer: "draft" });
  assert.match(inlineInspectorHtml(reply), /Scene Guidance/);
  assert.match(inlineReasoningHtml(reply), /draft/);
  assert.equal(inlineInspectorHtml(S.messages[0]), "");
  assert.equal(inlineReasoningHtml(S.messages[0]), "");
  S.inspectorInline = false;
  assert.equal(inlineInspectorHtml(reply), "");
  assert.equal(inlineReasoningHtml(reply), "");
});

test("a conversation switch drops the other conversation's cache", () => {
  rememberInspection(reply.id, { ...EMPTY_LOG, injection_block: "old" });
  S.activeConvId = "c2";
  assert.equal(inlineInspectorHtml(reply), "");
});

function streamingBubble() {
  document.body.innerHTML = `<div id="chat-messages"><div class="message assistant">
    <div class="msg-reasoning-live"></div><div class="msg-inspect-live"></div>
    <div class="msg-body" id="streaming-body"></div><div class="msg-toolbar"></div>
  </div></div>`;
  S.streamingBodyEl = document.getElementById("streaming-body");
  return {
    reasoning: document.querySelector(".msg-reasoning-live"),
    inspect: document.querySelector(".msg-inspect-live"),
  };
}

test("the streaming reply's Reasoning block opens the running pass's box for the first delta", () => {
  const slots = streamingBubble();
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
  assert.ok(slots.reasoning.querySelector("#reasoning-box"));
  assert.equal(slots.inspect.innerHTML, "");

  // With the pass off and nothing else to show yet, both slots stay empty.
  S.reasoningEnabled = { director: false, writer: false, editor: false };
  assert.equal(renderLiveInspector(), false);
  assert.equal(slots.reasoning.innerHTML, "");
  assert.equal(slots.inspect.innerHTML, "");

  // The Inspector block fills in; no reasoning box holds text, so it reports none.
  S.lastDirectorData = { active_moods: ["tense"], agent_latency_ms: 900, injection_block: "", tool_calls: [] };
  assert.equal(renderLiveInspector(), false);
  assert.match(slots.inspect.innerHTML, /style-tag active">Tense</);
  assert.equal(slots.reasoning.innerHTML, "");
  S.isStreaming = false;
  S.streamingBodyEl = null;
});
