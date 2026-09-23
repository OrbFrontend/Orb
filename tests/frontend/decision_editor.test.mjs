// The decision-fragment editor's write contract, driven through the real DOM.
//
// The rules under test are the ones the backend rejects a save over, and that
// an author cannot see going wrong: an explicit null is the only way to clear a
// decision column, and the outcome space is per type.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const key of ["document", "Node", "Element", "HTMLElement", "Event", "MouseEvent"]) {
  globalThis[key] = dom.window[key];
}

const { setDecisionConfig } = await import("../../frontend/decisions.js");
const {
  applyDecisionProblems,
  decisionDraftProblems,
  decisionSectionHtml,
  initDecisionDraft,
  readDecisionFields,
  repaintDecisionSection,
} = await import("../../frontend/library_decisions.js");

// The shape GET /api/decisions/config returns. Every control in the section is
// rendered from this, so the test supplies it rather than the module hardcoding
// anything the backend owns.
const CONFIG = {
  configured: true,
  decision_model: "typesafe/jev-1.13",
  resolved_url: "https://openrouter.ai/api/alpha/decisions",
  question_types: ["choice", "noul", "score"],
  resolution_policies: {
    noul: ["threshold", "roll"],
    choice: ["argmax", "weighted", "gated"],
    score: ["argmax", "weighted", "nearest"],
  },
  state_macros: ["last_message", "char"],
  text_macros: ["char", "user"],
  default_state_template: "Current request:\n{{last_message}}",
  max_questions_per_exchange: 128,
  choice: { min_options: 2, max_options: 255 },
  score: { min_levels: 2, max_levels: 10 },
};

const CHOICE_FRAGMENT = {
  field_type: "decision",
  decision_type: "choice",
  decision_placement: "before_director",
  decision_state_template: "Current request:\n{{last_message}}",
  decision_instructions: "Which of these best describes how she takes it?",
  decision_criteria: { win: "Wins.", lose: "Loses." },
  decision_outputs: { win: "", lose: "" },
  decision_resolution: "argmax",
  decision_threshold: null,
  decision_confidence_floor: null,
};

const NOUL_FRAGMENT = {
  field_type: "decision",
  decision_type: "noul",
  decision_placement: "before_director",
  decision_state_template: "Current request:\n{{last_message}}",
  decision_instructions: "The action described in the current request succeeds.",
  decision_criteria: { true: "It works.", false: "It does not." },
  decision_outputs: { true: "They pull it off.", false: "It fails." },
  decision_resolution: "threshold",
  decision_threshold: 0.5,
  decision_confidence_floor: null,
};

function mount(fragment) {
  setDecisionConfig(CONFIG);
  initDecisionDraft(fragment);
  document.body.innerHTML = decisionSectionHtml(fragment.field_type);
}

function click(selector) {
  document.querySelector(selector).dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
}

function setValue(selector, value) {
  const el = document.querySelector(selector);
  el.value = value;
  el.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
}

function type(selector, value) {
  const el = document.querySelector(selector);
  el.value = value;
  el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

beforeEach(() => {
  document.body.innerHTML = "";
});

test("every decision column is written, so a cleared one is cleared", () => {
  mount(NOUL_FRAGMENT);
  const fields = readDecisionFields();
  for (const column of [
    "decision_type",
    "decision_placement",
    "decision_state_template",
    "decision_instructions",
    "decision_criteria",
    "decision_outputs",
    "decision_resolution",
    "decision_threshold",
    "decision_confidence_floor",
  ]) {
    assert.ok(column in fields, `${column} must be written, not omitted`);
  }
});

test("switching to roll sends an explicit null threshold in the same request", () => {
  mount(NOUL_FRAGMENT);
  assert.equal(readDecisionFields().decision_threshold, 0.5);
  setValue('[data-dec="resolution"]', "roll");
  const fields = readDecisionFields();
  assert.equal(fields.decision_resolution, "roll");
  // Not `undefined`: omitting it leaves the stored threshold in the merged row
  // the backend validates, and the update is refused.
  assert.strictEqual(fields.decision_threshold, null);
});

test("a blank threshold sends the 0.5 its placeholder shows", () => {
  mount({ ...NOUL_FRAGMENT, decision_threshold: null });
  setValue('[data-dec="threshold"]', "");
  assert.equal(readDecisionFields().decision_threshold, 0.5);
});

test("a noul question never writes a confidence floor", () => {
  mount({ ...NOUL_FRAGMENT, decision_confidence_floor: 0.8 });
  assert.strictEqual(readDecisionFields().decision_confidence_floor, null);
});

test("choice keeps its authored key order; score is indexed from zero", () => {
  mount({
    ...NOUL_FRAGMENT,
    decision_type: "choice",
    decision_criteria: { stalemate: "Neither gives.", decisive: "One wins." },
    decision_outputs: { stalemate: "Hold.", decisive: "Break it." },
    decision_resolution: "argmax",
    decision_threshold: null,
  });
  // Authored order, not sorted: argmax breaks ties on it.
  assert.deepEqual(Object.keys(readDecisionFields().decision_criteria), ["stalemate", "decisive"]);

  mount({
    ...NOUL_FRAGMENT,
    decision_type: "score",
    decision_criteria: ["None", "Some", "A lot"],
    decision_outputs: { 0: "a", 1: "b", 2: "c" },
    decision_resolution: "nearest",
    decision_threshold: null,
  });
  const scored = readDecisionFields();
  assert.deepEqual(scored.decision_criteria, ["None", "Some", "A lot"]);
  assert.deepEqual(Object.keys(scored.decision_outputs), ["0", "1", "2"]);
});

test("adding a primary outcome appends an unnamed row, leaving the authored ones alone", () => {
  mount(CHOICE_FRAGMENT);
  click('[data-dec-act="add-option"]');

  // The authored outcomes survive. The new one is unnamed, because a choice key
  // is prompt text: a generated `option_3` would go to the Judge as the name of
  // an option and tell it nothing.
  const keys = [...document.querySelectorAll('[data-field="key"]')].map((el) => el.value);
  assert.deepEqual(keys, ["win", "lose", ""]);
  const fields = readDecisionFields();
  assert.equal(fields.decision_criteria.win, "Wins.");
});

test("deleting a primary outcome drops it and keeps criteria and guidance aligned", () => {
  mount({
    ...NOUL_FRAGMENT,
    decision_type: "choice",
    decision_criteria: { a: "A.", b: "B.", c: "C." },
    decision_outputs: { a: "", b: "", c: "" },
    decision_resolution: "argmax",
    decision_threshold: null,
  });
  click('[data-dec-act="del-option"][data-index="1"]');

  const fields = readDecisionFields();
  assert.deepEqual(Object.keys(fields.decision_criteria), ["a", "c"]);
  assert.deepEqual(fields.decision_outputs, { a: "", c: "" });
});

test("retyping the primary re-picks a policy its own type allows", () => {
  mount(NOUL_FRAGMENT);
  setValue('[data-dec="type"]', "score");
  const fields = readDecisionFields();
  assert.equal(fields.decision_type, "score");
  assert.ok(CONFIG.resolution_policies.score.includes(fields.decision_resolution));
  // `threshold` is noul-only and must be null for anything else.
  assert.strictEqual(fields.decision_threshold, null);
});

test("retyping rebuilds the outcome space for the new type", () => {
  mount(NOUL_FRAGMENT);
  setValue('[data-dec="type"]', "score");
  const fields = readDecisionFields();
  assert.deepEqual(Object.keys(fields.decision_outputs), ["0", "1"]);
  assert.ok(Array.isArray(fields.decision_criteria));
});

test("guidance mirrors an untouched criterion and never overwrites a written one", () => {
  mount({ ...NOUL_FRAGMENT, decision_outputs: { true: "", false: "Authored." } });
  type('[data-opt="0"][data-field="text"]', "They pull it off.");
  assert.equal(document.querySelector('[data-opt="0"][data-field="output"]').value, "They pull it off.");

  // The second row's guidance was authored, so it is the author's and stays.
  type('[data-opt="1"][data-field="text"]', "They do not.");
  assert.equal(document.querySelector('[data-opt="1"][data-field="output"]').value, "Authored.");
});

test("an empty guidance field stays empty: no implicit echo of the criterion", () => {
  mount({ ...NOUL_FRAGMENT, decision_outputs: { true: "", false: "" } });
  assert.deepEqual(readDecisionFields().decision_outputs, { true: "", false: "" });
});

test("validation problems land against the fields they name", () => {
  mount(NOUL_FRAGMENT);
  const placed = applyDecisionProblems(
    "decision_instructions must not be empty; decision_outputs must have exactly the keys true, false",
  );
  assert.equal(placed, true);
  const instructionField = document.querySelector('[data-dec="instructions"]').closest(".field");
  assert.match(instructionField.innerHTML, /must not be empty/);
});

test("a problem naming no rendered field is still shown, not swallowed", () => {
  mount(NOUL_FRAGMENT);
  assert.equal(applyDecisionProblems("decision_placement must be one of before_director"), true);
  assert.match(document.body.innerHTML, /decision_placement/);
});

test("the section renders its controls from the config payload, not from constants", () => {
  mount(NOUL_FRAGMENT);
  repaintDecisionSection();
  const types = [...document.querySelectorAll('[data-dec="type"] option')].map((o) => o.value);
  assert.deepEqual(types, CONFIG.question_types);
  const policies = [...document.querySelectorAll('[data-dec="resolution"] option')].map((o) => o.value);
  assert.deepEqual(policies, CONFIG.resolution_policies.noul);
});
