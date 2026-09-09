// Selector + bus fixtures for frontend/state.js. state.js is DOM-free (it only
// imports workflow_registry.js, also DOM-free), so it loads under node --test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { charactersView, localMlReady, notify, S, subscribe } from "../../frontend/state.js";

test("charactersView returns the full set when allCharacters is populated", () => {
  S.allCharacters = [{ id: 1 }, { id: 2 }];
  S.characters = [{ id: 2 }];
  assert.equal(charactersView().length, 2);
  assert.equal(charactersView(), S.allCharacters);
});

test("charactersView falls back to the recent set before allCharacters loads", () => {
  S.allCharacters = [];
  S.characters = [{ id: 7 }];
  assert.equal(charactersView(), S.characters);
});

test("charactersView is always an array (both empty)", () => {
  S.allCharacters = [];
  S.characters = [];
  assert.ok(Array.isArray(charactersView()));
  assert.equal(charactersView().length, 0);
});

test("subscribe/notify fans out synchronously and unsubscribes", () => {
  let seen = null;
  const off = subscribe("messages", (d) => {
    seen = d;
  });
  notify("messages", { n: 1 });
  assert.deepEqual(seen, { n: 1 });
  off();
  notify("messages", { n: 2 });
  assert.deepEqual(seen, { n: 1 }); // handler removed
});

test("a throwing subscriber does not starve the others", (t) => {
  t.mock.method(console, "error", () => {}); // the throw is logged on purpose; keep it out of test output
  let reached = false;
  const off1 = subscribe("settings", () => {
    throw new Error("boom");
  });
  const off2 = subscribe("settings", () => {
    reached = true;
  });
  notify("settings", {});
  assert.equal(reached, true);
  off1();
  off2();
});

test("notify/subscribe reject an unknown topic without throwing", (t) => {
  t.mock.method(console, "error", () => {}); // unknown-topic path logs on purpose; keep it quiet here
  assert.doesNotThrow(() => notify("not-a-topic", {}));
  const off = subscribe("not-a-topic", () => {});
  assert.equal(typeof off, "function");
  off();
});

test("localMlReady is false until a status lands", () => {
  S.localMlFeatures = {};
  assert.equal(localMlReady("pov_classifier"), false);
});

test("localMlReady needs the model downloaded, enabled and its deps installed", () => {
  const ready = { present: true, enabled: true, deps_ok: true };
  S.localMlFeatures = {
    pov_classifier: ready,
    not_downloaded: { ...ready, present: false },
    switched_off: { ...ready, enabled: false },
    no_deps: { ...ready, deps_ok: false },
  };
  assert.equal(localMlReady("pov_classifier"), true);
  assert.equal(localMlReady("not_downloaded"), false);
  assert.equal(localMlReady("switched_off"), false);
  assert.equal(localMlReady("no_deps"), false);
});

test("localMlReady only holds runtime_ok against a feature that reports one", () => {
  S.localMlFeatures = {
    in_process: { present: true, enabled: true, deps_ok: true },
    llama_server: { present: true, enabled: true, deps_ok: true, runtime_ok: false },
  };
  assert.equal(localMlReady("in_process"), true); // no runtime of its own to be missing
  assert.equal(localMlReady("llama_server"), false);
});
