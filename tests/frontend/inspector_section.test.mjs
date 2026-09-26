import assert from "node:assert/strict";
import { test } from "node:test";
import { sectionHtml } from "../../frontend/inspector_section.js";

test("a static section keeps an empty chevron slot and its value in the heading row", () => {
  const html = sectionHtml({ title: "Agent Latency", meta: "900 ms", className: "inspector-latency" });
  assert.match(html, /^<div class="inspector-block inspector-latency">/);
  assert.match(html, /<span class="inspector-head-slot"><\/span><h4>Agent Latency<\/h4><span class="inspector-meta">900 ms<\/span>/);
  assert.doesNotMatch(html, /inspector-body|data-inspect-section/);
});

test("a keyed section collapses under its open flag and indents its body", () => {
  const closed = sectionHtml({ key: "tool_calls", title: "Tool Calls", body: "x" });
  assert.match(closed, /^<details class="inspector-block" data-inspect-section="tool_calls">/);
  assert.match(closed, /<summary class="inspector-head"><span class="reasoning-summary-arrow">/);
  assert.match(closed, /<div class="inspector-body">x<\/div>/);
  const open = sectionHtml({ key: "tool_calls", open: true, id: "tc", title: "Tool Calls" });
  assert.match(open, /^<details class="inspector-block" id="tc" data-inspect-section="tool_calls" open>/);
});
