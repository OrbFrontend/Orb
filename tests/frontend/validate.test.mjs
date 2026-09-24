// Validator fixtures for frontend/validate.js. Pure functions, no DOM.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  validate,
  validateChatInput,
  validateConversationTitle,
  validateEditMessage,
} from "../../frontend/validate.js";

test("validateChatInput rejects empty / whitespace", () => {
  assert.equal(validateChatInput("").valid, false);
  assert.equal(validateChatInput("   ").valid, false);
});

test("validateChatInput accepts normal text", () => {
  assert.equal(validateChatInput("hello").valid, true);
});

test("validateChatInput rejects over-limit input", () => {
  assert.equal(validateChatInput("x".repeat(100001)).valid, false);
  assert.equal(validateChatInput("x".repeat(100000)).valid, true);
});

test("validateEditMessage is the exact same implementation as validateChatInput (alias)", () => {
  // The dedupe: one function, two names. Identity check guards against a future
  // divergent copy sneaking back in.
  assert.equal(validateEditMessage, validateChatInput);
  assert.equal(validate.validateEditMessage, validateChatInput);
});

test("validateConversationTitle rejects empty and over-limit", () => {
  assert.equal(validateConversationTitle("").valid, false);
  assert.equal(validateConversationTitle("A nice title").valid, true);
  assert.equal(validateConversationTitle("x".repeat(101)).valid, false);
});

test("the validate barrel exposes the domain validators", () => {
  for (const name of ["validateChatInput", "validateEditMessage", "validateCharacterName", "validateConversationTitle"]) {
    assert.equal(typeof validate[name], "function", `validate.${name} missing`);
  }
});

test("interactive fragments accept post-processing field type", () => {
  const result = validate.validateInteractiveFragment({
    id: "humanize_dialogue",
    label: "Humanize Dialogue",
    injection_label: "Humanize Dialogue",
    description: "Change dialogue only.",
    field_type: "post_processing",
  });
  assert.equal(result.valid, true);
});

test("state fragments take explicit settings, and the progressive and direction-note types are refused", () => {
  const base = { id: "threads", label: "Threads", injection_label: "Threads", description: "Open threads." };
  const state = { ...base, field_type: "state", state_mode: "entries", state_update: "manual", state_inject: "off" };
  assert.equal(validate.validateInteractiveFragment(state).valid, true);
  assert.equal(validate.validateInteractiveFragment({ ...state, state_mode: "list" }).valid, false);
  assert.equal(validate.validateInteractiveFragment({ ...state, state_update: "post_turn" }).valid, false);
  for (const legacy of ["progressive", "direction_note"]) {
    assert.equal(validate.validateInteractiveFragment({ ...base, field_type: legacy }).valid, false);
  }
});

test("fragment cooldowns must be whole turns between zero and fifty", () => {
  const mood = {
    id: "tense",
    label: "Tense",
    description: "Tension.",
    prompt_text: "Be tense.",
  };
  const interactive = {
    id: "pacing",
    label: "Pacing",
    injection_label: "Pacing",
    description: "Scene pace.",
    field_type: "string",
  };

  for (const cooldown_turns of [0, 3, 50]) {
    assert.equal(validate.validateMoodFragment({ ...mood, cooldown_turns }).valid, true);
    assert.equal(validate.validateInteractiveFragment({ ...interactive, cooldown_turns }).valid, true);
  }
  for (const cooldown_turns of [-1, 2.5, 51]) {
    assert.equal(validate.validateMoodFragment({ ...mood, cooldown_turns }).valid, false);
    assert.equal(validate.validateInteractiveFragment({ ...interactive, cooldown_turns }).valid, false);
  }
});
