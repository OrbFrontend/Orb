// The step ids are read out of the backend sources rather than restated here, so a
// new `step_start` without status text fails this test instead of leaving the bar
// showing the previous step.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { generationStepLabel } from "../../frontend/generation_status.js";

const pipeline = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "backend", "pipeline");

function backendSteps() {
  const steps = new Set();
  for (const file of readdirSync(pipeline, { recursive: true })) {
    if (!file.endsWith(".py")) continue;
    for (const m of readFileSync(join(pipeline, file), "utf8").matchAll(/"step": "([a-z_]+)"/g)) steps.add(m[1]);
  }
  return [...steps];
}

test("every backend step has its own status text", () => {
  const steps = backendSteps();
  assert.ok(steps.includes("writer"), "the step scan found nothing; did the emission shape change?");
  // `director_start` predates `step_start` and borrows the "director" entry.
  const labels = [...steps, "director"].map(generationStepLabel);
  for (const [i, label] of labels.entries()) assert.ok(label, `no status text for ${steps[i] ?? "director"}`);
  assert.equal(new Set(labels).size, labels.length);
});

test("an unknown step keeps the current text", () => {
  assert.equal(generationStepLabel("future_step"), "");
  assert.equal(generationStepLabel("toString"), "");
  assert.equal(generationStepLabel(undefined), "");
});
