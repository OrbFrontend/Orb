import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";
import { S } from "../../frontend/state.js";
import { currentDecisionsHtml } from "../../frontend/chat_decisions.js";

globalThis.document = new JSDOM("<!doctype html><body></body>").window.document;

const evaluations = [{ fragment_id: "outcome", outcome: "true", probability: 0.9, guidance: "Hold the door." }];

beforeEach(() => {
  S.inspectedMsgId = null;
  S.inspectedDirectorData = null;
  S.lastDecisions = { evaluations, skipped: [] };
});

test("live decision projections render without a stored-envelope version", () => {
  assert.match(currentDecisionsHtml(), /Hold the door/);
});

test("the inspector leaves future stored decision envelopes alone", () => {
  S.inspectedMsgId = 1;
  S.inspectedDirectorData = { decision_evaluations: { version: 3, evaluations, skipped: [] } };
  assert.equal(currentDecisionsHtml(), "");
});

test("a historical message awaiting its log never borrows live decisions", () => {
  S.inspectedMsgId = 1;
  assert.equal(currentDecisionsHtml(), "");
});

test("supported stored decisions render", () => {
  S.inspectedMsgId = 1;
  S.inspectedDirectorData = { decision_evaluations: { version: 2, evaluations, skipped: [] } };
  assert.match(currentDecisionsHtml(), /Hold the door/);
});
