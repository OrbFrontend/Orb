import assert from "node:assert/strict";
import { test } from "node:test";

import {
  generationPhaseIndex,
  generationPhaseView,
  renderGenerationPhase,
} from "../../frontend/generation_status.js";

function statusElement() {
  const text = { textContent: "" };
  const dot = { className: "" };
  const steps = Array.from({ length: 3 }, () => ({ dataset: {} }));
  return {
    dataset: {},
    querySelector(selector) {
      return { ".gen-text": text, ".gen-dot": dot }[selector] || null;
    },
    querySelectorAll(selector) {
      return selector === "[data-generation-step]" ? steps : [];
    },
    dot,
    steps,
    text,
  };
}

const CASES = [
  ["pending", "Preparing the request…", "", "upcoming,upcoming,upcoming", false],
  ["directing", "Reading context and planning the scene…", "director pass", "active,upcoming,upcoming", false],
  ["generating", "Drafting the response…", "writer pass", "complete,active,upcoming", false],
  ["refining", "Reviewing and polishing the response…", "editor pass", "complete,complete,active", true],
  ["finalizing", "Finishing the response…", "workflow hook", "complete,complete,active", true],
];

test("generation phases render in order from one metadata source", () => {
  assert.deepEqual(CASES.map(([phase]) => generationPhaseIndex(phase)), [0, 1, 2, 3, 4]);
  const el = statusElement();

  for (const [phase, label, stage, states, spins] of CASES) {
    renderGenerationPhase(el, phase);
    assert.equal(el.dataset.phase, phase);
    assert.equal(el.text.textContent, label);
    assert.equal(generationPhaseView(phase).stage, stage);
    assert.equal(el.steps.map((step) => step.dataset.state).join(), states);
    assert.equal(el.dot.className, `gen-dot${spins ? " spin" : ""}`);
  }

  renderGenerationPhase(el, "unknown");
  assert.equal(el.text.textContent, "Processing…");
  assert.equal(el.dot.className, "gen-dot");
});
