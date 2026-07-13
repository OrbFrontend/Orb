// Selector + bus fixtures for frontend/state.js. state.js is DOM-free (it only
// imports workflow_registry.js, also DOM-free), so it loads under node --test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { charactersView, notify, S, subscribe } from "../../frontend/state.js";

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

test("a throwing subscriber does not starve the others", () => {
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

test("notify/subscribe reject an unknown topic without throwing", () => {
  assert.doesNotThrow(() => notify("not-a-topic", {}));
  const off = subscribe("not-a-topic", () => {});
  assert.equal(typeof off, "function");
  off();
});
