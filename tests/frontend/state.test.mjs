// Selector + bus fixtures for frontend/state.js. state.js is DOM-free (it only
// imports workflow_registry.js, also DOM-free), so it loads under node --test.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  charactersView,
  localMlReady,
  notify,
  restingCooldowns,
  S,
  subscribe,
  upgradeLegacyFragment,
} from "../../frontend/state.js";

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

// A solo branch where `stormy` fires on reply 3 with a two-turn cooldown: it is
// held out of replies 5 and 7, and free again on 9. Each row carries the state
// its own turn leaves behind, which is why reading a turn's resting set off its
// own row would mark `stormy` on 3 (the turn it fired) and clear it on 7 (a turn
// it is still held out of).
const soloPath = [
  { id: 1, role: "assistant", fragment_cooldowns: {} },
  { id: 2, role: "user" },
  { id: 3, role: "assistant", fragment_cooldowns: { stormy: 2 } },
  { id: 4, role: "user" },
  { id: 5, role: "assistant", fragment_cooldowns: { stormy: 1 } },
  { id: 6, role: "user" },
  { id: 7, role: "assistant", fragment_cooldowns: {} },
  { id: 8, role: "user" },
  { id: 9, role: "assistant", fragment_cooldowns: {} },
];

test("restingCooldowns leaves the turn a fragment fires on free", () => {
  S.messages = soloPath;
  assert.deepEqual(restingCooldowns(3), {});
});

test("restingCooldowns rests a fragment for every turn its cooldown covers", () => {
  S.messages = soloPath;
  assert.deepEqual(restingCooldowns(5), { stormy: 2 });
  assert.deepEqual(restingCooldowns(7), { stormy: 1 });
});

test("restingCooldowns frees a fragment once its cooldown has run out", () => {
  S.messages = soloPath;
  assert.deepEqual(restingCooldowns(9), {});
});

test("restingCooldowns reads past a group exchange, not the previous speaker", () => {
  // One Director run covers the whole exchange, so every speaker row carries the
  // same state it leaves behind. Speaker two must not read speaker one's row.
  S.messages = [
    { id: 1, role: "assistant", exchange_id: "e1", fragment_cooldowns: { sulky: 2 } },
    { id: 2, role: "user" },
    { id: 3, role: "assistant", exchange_id: "e2", fragment_cooldowns: { flirty: 1 } },
    { id: 4, role: "assistant", exchange_id: "e2", fragment_cooldowns: { flirty: 1 } },
  ];
  assert.deepEqual(restingCooldowns(3), { sulky: 2 });
  assert.deepEqual(restingCooldowns(4), { sulky: 2 });
});

test("restingCooldowns rests nothing for a first reply or an unknown message", () => {
  S.messages = soloPath;
  assert.deepEqual(restingCooldowns(1), {});
  assert.deepEqual(restingCooldowns(404), {});
  assert.deepEqual(restingCooldowns(null), {});
});

test("legacy card fragment types read as explicit state fragments, like the backend's card boundary", () => {
  assert.deepEqual(upgradeLegacyFragment({ id: "trust", field_type: "progressive" }), {
    id: "trust",
    field_type: "state",
    state_mode: "value",
    state_update: "before_writer",
    state_inject: "both",
  });
  assert.deepEqual(upgradeLegacyFragment({ id: "plan", field_type: "direction_note", direction_note_timing: "pre_writer" }), {
    id: "plan",
    field_type: "state",
    state_mode: "entries",
    state_update: "before_writer",
    state_inject: "both",
  });
  assert.equal(upgradeLegacyFragment({ id: "arc", field_type: "direction_note" }).state_update, "after_reply");
  const plain = { id: "pace", field_type: "string" };
  assert.equal(upgradeLegacyFragment(plain), plain);
});
