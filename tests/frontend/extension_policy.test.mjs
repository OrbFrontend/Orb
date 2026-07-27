// The frontend trust gate: which manifest entries may become a dynamic
// import(). extension_policy.js is a DOM-free leaf precisely so this can be
// tested as behavior rather than as an assertion about workflow_loader.js's
// source text.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DECLARATIVE,
  isAvailable,
  isDeclarativeEntry,
  isTrustedModuleEntry,
  TRUSTED_MODULE,
} from "../../frontend/extension_policy.js";

const builtin = { id: "tts", source: "builtin", frontend_kind: TRUSTED_MODULE, load_status: "available" };
const community = {
  id: "scene-meter",
  source: "community",
  frontend_kind: DECLARATIVE,
  load_status: "available",
};

test("a shipped built-in is a trusted module", () => {
  assert.equal(isTrustedModuleEntry(builtin), true);
  assert.equal(isDeclarativeEntry(builtin), false);
});

test("a community package is never a trusted module", () => {
  assert.equal(isTrustedModuleEntry(community), false);
  assert.equal(isDeclarativeEntry(community), true);
});

test("an unavailable community package is still declarative, just not available", () => {
  const broken = { ...community, load_status: "incompatible" };
  assert.equal(isDeclarativeEntry(broken), true);
  assert.equal(isAvailable(broken), false);
  assert.equal(isAvailable(community), true);
});

// The gate is positive matching. Each of these would pass a `!== "declarative"`
// check and get imported — which is the whole failure this test exists to pin.
for (const [label, entry] of [
  ["a missing frontend_kind", { id: "x" }],
  ["a null frontend_kind", { id: "x", frontend_kind: null }],
  ["an unknown future kind", { id: "x", frontend_kind: "wasm_module" }],
  ["a truthy non-string kind", { id: "x", frontend_kind: 1 }],
  ["a lookalike kind", { id: "x", frontend_kind: "trusted_module " }],
  ["a case variant", { id: "x", frontend_kind: "Trusted_Module" }],
]) {
  test(`${label} fails closed`, () => {
    assert.equal(isTrustedModuleEntry(entry), false);
    assert.equal(isDeclarativeEntry(entry), false);
  });
}

for (const [label, entry] of [
  ["null", null],
  ["undefined", undefined],
  ["a string", "tts"],
  ["a number", 7],
  ["an entry with no id", { frontend_kind: TRUSTED_MODULE }],
  ["an entry with an empty id", { id: "", frontend_kind: TRUSTED_MODULE }],
  ["an entry with a non-string id", { id: 42, frontend_kind: TRUSTED_MODULE }],
]) {
  test(`${label} is not loadable`, () => {
    assert.equal(isTrustedModuleEntry(entry), false);
    assert.equal(isDeclarativeEntry(entry), false);
  });
}

test("a community entry cannot claim the trusted kind and be treated as both", () => {
  // Belt and braces: the backend derives frontend_kind from the record's tier,
  // so this shape should be unreachable. If it ever arrives anyway, the two
  // predicates must not both accept it — one entry, one path.
  const forged = { id: "evil", source: "community", frontend_kind: TRUSTED_MODULE };
  assert.notEqual(isTrustedModuleEntry(forged), isDeclarativeEntry(forged));
});
