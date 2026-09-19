import assert from "node:assert/strict";
import { test } from "node:test";

import { generationStepLabel } from "../../frontend/generation_status.js";

// The ids the backend sends in `step_start`, plus "director" for `director_start`.
const BACKEND_STEPS = [
  "director",
  "lorebook",
  "direction_notes",
  "writer",
  "output_auditor",
  "length_guard",
  "post_processing",
  "feedback",
  "world_changes",
  "sheet_updates",
];

test("every backend step has its own status text", () => {
  const labels = BACKEND_STEPS.map(generationStepLabel);
  for (const [i, label] of labels.entries()) assert.ok(label, `no status text for ${BACKEND_STEPS[i]}`);
  assert.equal(new Set(labels).size, labels.length);
});

test("an unknown step keeps the current text", () => {
  assert.equal(generationStepLabel("future_step"), "");
  assert.equal(generationStepLabel("toString"), "");
  assert.equal(generationStepLabel(undefined), "");
});
