// The decision-fragment editor's write contract, driven through the real DOM.
//
// The rules under test are the ones the backend rejects a save over, and that
// an author cannot see going wrong: an explicit null is the only way to clear a
// decision column, a facet's instruction map must be keyed by the primary's
// outcomes exactly, and the outcome space is per type.
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
    choice: ["argmax", "weighted"],
    score: ["argmax", "weighted", "nearest"],
  },
  state_macros: ["last_message", "char"],
  text_macros: ["char", "user"],
  default_state_template: "Current request:\n{{last_message}}",
  max_questions_per_exchange: 128,
  choice: { min_options: 2, max_options: 255 },
  score: { min_levels: 2, max_levels: 10 },
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
  decision_facets: null,
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
    "decision_facets",
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

test("a noul question never writes a confidence floor", () => {
  mount({ ...NOUL_FRAGMENT, decision_confidence_floor: 0.8 });
  assert.strictEqual(readDecisionFields().decision_confidence_floor, null);
});

test("no facets writes null, not an empty array", () => {
  mount(NOUL_FRAGMENT);
  assert.strictEqual(readDecisionFields().decision_facets, null);
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

test("a facet's instructions are keyed by the primary's outcomes, exactly", () => {
  mount(NOUL_FRAGMENT);
  click('[data-dec-act="add-facet"]');
  const [facet] = readDecisionFields().decision_facets;
  assert.deepEqual(Object.keys(facet.instructions), ["true", "false"]);
});

test("adding a primary outcome adds an empty branch to every facet, never a generated one", () => {
  mount({
    ...NOUL_FRAGMENT,
    decision_type: "choice",
    decision_criteria: { win: "Wins.", lose: "Loses." },
    decision_outputs: { win: "", lose: "" },
    decision_resolution: "argmax",
    decision_threshold: null,
  });
  click('[data-dec-act="add-facet"]');
  document.querySelector('[data-facet="0"] [data-facet-branch="0"]').value = "Assume they win. How costly?";
  document.querySelector('[data-facet="0"] [data-facet-branch="1"]').value = "Assume they lose. How costly?";
  click('[data-dec-act="add-option"]');

  const [facet] = readDecisionFields().decision_facets;
  assert.equal(Object.keys(facet.instructions).length, 3);
  // The authored branches survive; the new one is empty and the author writes
  // it. Nothing is synthesised from the branch beside it.
  assert.equal(facet.instructions.win, "Assume they win. How costly?");
  assert.equal(facet.instructions.lose, "Assume they lose. How costly?");
  assert.equal(facet.instructions.option_3, "");
});

test("deleting a primary outcome drops that branch and keeps the rest aligned", () => {
  mount({
    ...NOUL_FRAGMENT,
    decision_type: "choice",
    decision_criteria: { a: "A.", b: "B.", c: "C." },
    decision_outputs: { a: "", b: "", c: "" },
    decision_resolution: "argmax",
    decision_threshold: null,
  });
  click('[data-dec-act="add-facet"]');
  const card = '[data-facet="0"]';
  document.querySelector(`${card} [data-facet-branch="0"]`).value = "branch a";
  document.querySelector(`${card} [data-facet-branch="1"]`).value = "branch b";
  document.querySelector(`${card} [data-facet-branch="2"]`).value = "branch c";
  click('[data-dec-act="del-option"][data-index="1"]');

  const fields = readDecisionFields();
  assert.deepEqual(Object.keys(fields.decision_criteria), ["a", "c"]);
  assert.deepEqual(fields.decision_facets[0].instructions, { a: "branch a", c: "branch c" });
});

test("an outcome-independent facet is written as one string, so it is asked once", () => {
  mount(NOUL_FRAGMENT);
  click('[data-dec-act="add-facet"]');
  document.querySelector('[data-facet="0"] [data-facet-single-instruction]');
  const toggle = document.querySelector('[data-dec-act="toggle-single"]');
  toggle.checked = true;
  toggle.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  type('[data-facet-single-instruction]', "How tense is this scene?");
  assert.equal(readDecisionFields().decision_facets[0].instructions, "How tense is this scene?");
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
  type('[data-dec-criterion="0"]', "They pull it off.");
  assert.equal(document.querySelector('[data-dec-output="0"]').value, "They pull it off.");

  // The second row's guidance was authored, so it is the author's and stays.
  type('[data-dec-criterion="1"]', "They do not.");
  assert.equal(document.querySelector('[data-dec-output="1"]').value, "Authored.");
});

test("an empty guidance field stays empty: no implicit echo of the criterion", () => {
  mount({ ...NOUL_FRAGMENT, decision_outputs: { true: "", false: "" } });
  assert.deepEqual(readDecisionFields().decision_outputs, { true: "", false: "" });
});

test("validation problems land against the fields they name", () => {
  mount(NOUL_FRAGMENT);
  const placed = applyDecisionProblems(
    "decision_instructions must not be empty; decision_facets[0].instructions must have exactly the keys true, false",
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

test("the fan-out readout counts one question per facet branch, plus the primary", () => {
  mount(NOUL_FRAGMENT);
  assert.match(document.querySelector(".decision-budget").textContent, /^\s*1 question/);

  // One noul primary (2 outcomes) and one per-branch facet: 1 + 2 = 3.
  click('[data-dec-act="add-facet"]');
  assert.match(document.querySelector(".decision-budget").textContent, /^\s*3 questions/);

  // An outcome-independent facet is asked once, not once per branch: 1 + 1 = 2.
  const toggle = document.querySelector('[data-dec-act="toggle-single"]');
  toggle.checked = true;
  toggle.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  assert.match(document.querySelector(".decision-budget").textContent, /^\s*2 questions/);
});

test("the section renders its controls from the config payload, not from constants", () => {
  mount(NOUL_FRAGMENT);
  repaintDecisionSection();
  const types = [...document.querySelectorAll('[data-dec="type"] option')].map((o) => o.value);
  assert.deepEqual(types, CONFIG.question_types);
  const policies = [...document.querySelectorAll('[data-dec="resolution"] option')].map((o) => o.value);
  assert.deepEqual(policies, CONFIG.resolution_policies.noul);
});
