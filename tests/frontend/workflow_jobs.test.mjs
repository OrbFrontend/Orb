// A running workflow render's button is its Stop button, and a repaint mid-render
// rebuilds only rows whose markup changed. So a button can exist twice over a
// render's life: the one `show` turned over in place, and one a renderer drew
// from `stopButtonState`. `end` has to put both back, or a Stop that stops
// nothing is left on screen.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { startWorkflowJob, stopButtonState } = await import("../../frontend/workflow_jobs.js");

const button = (state) => `<button class="regen${state.cls}"${state.attrs}><svg></svg></button>`;

test("end turns back the in-place and the repainted Stop button alike", () => {
  const idle = button(stopButtonState(null, "Regenerate", "Regen label"));
  document.body.innerHTML = idle + idle;
  const [live, repainted] = document.body.querySelectorAll("button");
  const job = startWorkflowJob({ convId: "c", title: "Stop regenerating" });

  job.show(live);
  repainted.outerHTML = button(stopButtonState(job, "Regenerate", "Regen label"));
  for (const btn of document.body.querySelectorAll("button")) {
    assert.ok(btn.classList.contains("wf-running"));
    assert.equal(btn.title, "Stop regenerating");
    assert.equal(btn.getAttribute("aria-label"), "Stop regenerating");
  }

  job.end();
  assert.equal(document.body.innerHTML, idle + idle);
});

test("a stopping job repaints its button disabled", () => {
  const job = startWorkflowJob({ convId: "c", title: "Stop" });
  job.stopping = true;
  assert.match(button(stopButtonState(job, "Regenerate")), / disabled>/);
});
