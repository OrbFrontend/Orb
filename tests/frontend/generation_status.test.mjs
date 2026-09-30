// Read step ids from the backend so new steps cannot silently lose their label.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

import { generationStepLabel, syncGenerationStatusMarquee } from "../../frontend/generation_status.js";

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

test("unknown steps have no label", () => {
  assert.equal(generationStepLabel("future_step"), "");
  assert.equal(generationStepLabel("toString"), "");
  assert.equal(generationStepLabel(undefined), "");
});

test("long status text scrolls automatically and responds to resizing and visibility", (t) => {
  const dom = new JSDOM(`<div class="gen-status">
    <div class="gen-status-content"><div class="gen-status-track">Rendering: a long review reason</div></div>
  </div>`);
  t.after(() => dom.window.close());
  const bar = dom.window.document.querySelector(".gen-status");
  const viewport = bar.querySelector(".gen-status-content");
  const track = bar.querySelector(".gen-status-track");
  let viewportWidth = 300;
  let trackWidth = 600;
  Object.defineProperty(viewport, "clientWidth", { get: () => viewportWidth });
  Object.defineProperty(track, "offsetWidth", { get: () => trackWidth });
  let resize;
  const observed = [];
  const originalResizeObserver = globalThis.ResizeObserver;
  t.after(() => {
    if (originalResizeObserver === undefined) delete globalThis.ResizeObserver;
    else globalThis.ResizeObserver = originalResizeObserver;
  });
  globalThis.ResizeObserver = class {
    constructor(callback) {
      resize = callback;
    }
    observe(element) {
      observed.push(element);
    }
  };

  syncGenerationStatusMarquee(bar);
  assert.deepEqual(observed, [viewport, track]);
  assert.ok(viewport.classList.contains("is-scrolling"), "overflow must start without user interaction");
  const duration = viewport.style.getPropertyValue("--gen-scroll-duration");

  trackWidth = 800;
  resize();
  assert.ok(parseFloat(viewport.style.getPropertyValue("--gen-scroll-duration")) > parseFloat(duration));

  viewportWidth = 900;
  resize();
  assert.ok(!viewport.classList.contains("is-scrolling"), "fitting text should stay centered");

  viewportWidth = 200;
  resize();
  assert.ok(viewport.classList.contains("is-scrolling"));
  bar.classList.add("hidden");
  syncGenerationStatusMarquee(bar);
  assert.ok(!viewport.classList.contains("is-scrolling"), "a hidden bar must stop animating");
  bar.classList.remove("hidden");
  syncGenerationStatusMarquee(bar);
  assert.ok(viewport.classList.contains("is-scrolling"), "workflow-only status must restart too");
  assert.equal(observed.length, 2, "status updates must reuse the resize observer");
});
