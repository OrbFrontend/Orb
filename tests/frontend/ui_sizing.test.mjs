import assert from "node:assert/strict";
import { test } from "node:test";

import { localPx, panelWidth, parseSizing, shownScale, steppedScale } from "../../frontend/ui_sizing.js";

test("a region's scale steps with the wheel, stays in bounds and rests at exactly 1 near 1", () => {
  assert.ok(steppedScale(1, -100) > 1.09 && steppedScale(1, -100) < 1.12, "one notch up is about a tenth");
  assert.equal(steppedScale(1, -100), steppedScale(1, -5000), "a notch is capped however large its delta");
  assert.equal(steppedScale(2, -100), 2);
  assert.equal(steppedScale(0.75, 100), 0.75);

  // Pinch deltas are tiny: they must accumulate past the snap instead of being rounded away each step.
  let scale = 1;
  for (let i = 0; i < 20; i++) scale = steppedScale(scale, -2);
  assert.equal(shownScale(steppedScale(1, -2)), 1);
  assert.ok(shownScale(scale) > 1.05);

  assert.equal(shownScale(1.02), 1);
  assert.equal(shownScale(1.2345), 1.23);
  for (const bad of [undefined, Number.NaN, 0, 40]) assert.equal(shownScale(bad), 1);
});

test("a panel keeps its own width in unscaled pixels and cannot crowd out the chat", () => {
  assert.equal(panelWidth(450, 1.5, 2000), 300, "450px on screen at 150% is a 300px panel");
  assert.equal(panelWidth(50, 1, 2000), 200);
  assert.equal(panelWidth(5000, 1, 2000), 900);
  assert.equal(panelWidth(5000, 2, 2000), 450);
});

test("on-screen pixels convert to the units a scaled element scrolls in", () => {
  assert.equal(localPx({ currentCSSZoom: 1.5 }, 750), 500);
  assert.equal(localPx({}, 750), 750, "no zoom support means no scaling to undo");
});

test("stored sizing survives corruption", () => {
  const empty = { scale: {}, width: {}, box: {} };
  for (const bad of [null, "", "{", "[]", '"x"', '{"scale":"big"}']) assert.deepEqual(parseSizing(bad), empty);
  assert.deepEqual(parseSizing('{"scale":{"main":1.2,"panel":"x"},"box":{"reasoning":400,"raw":null}}'), {
    scale: { main: 1.2 },
    width: {},
    box: { reasoning: 400 },
  });
});

test("the assistant panel keeps a width and scale of its own, apart from the Inspector's", async () => {
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM("<!doctype html><body></body>");
  const saved = { document: globalThis.document, window: globalThis.window, localStorage: globalThis.localStorage };
  globalThis.document = dom.window.document;
  globalThis.window = { innerWidth: 2000 };
  globalThis.localStorage = {
    getItem: () => '{"width":{"assistant":520,"panel":300},"scale":{"assistant":1.2},"box":{"assistantLog":400}}',
    setItem() {},
  };
  try {
    const { initUiSizing } = await import("../../frontend/ui_sizing.js");
    initUiSizing();
    const root = document.documentElement.style;
    assert.equal(root.getPropertyValue("--assistant-width"), "520px");
    assert.equal(root.getPropertyValue("--assistant-scale"), "1.2");
    assert.equal(root.getPropertyValue("--inspector-width"), "300px");
    assert.equal(root.getPropertyValue("--panel-scale"), "", "the Inspector's scale is untouched");
    assert.equal(root.getPropertyValue("--assistant-log-height"), "", "the log has no height of its own");
  } finally {
    Object.assign(globalThis, saved);
  }
});
