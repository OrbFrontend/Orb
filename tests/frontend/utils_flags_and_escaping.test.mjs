import assert from "node:assert/strict";
import { test } from "node:test";
import { boolFlag, escHandlerArg } from "../../frontend/utils.js";

test("boolFlag accepts SQLite and optimistic-update true values only", () => {
  assert.equal(boolFlag(true), true);
  assert.equal(boolFlag(1), true);
  assert.equal(boolFlag(false), false);
  assert.equal(boolFlag(0), false);
  assert.equal(boolFlag("1"), false);
  assert.equal(boolFlag("0"), false);
  assert.equal(boolFlag(null), false);
});

test("escHandlerArg preserves a single-quoted inline-handler argument", () => {
  assert.equal(escHandlerArg(`my'cast\\line\n"<&`), `my\\'cast\\\\line\\n&quot;&lt;&amp;`);
});
