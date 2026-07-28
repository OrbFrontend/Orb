import assert from "node:assert/strict";
import { test } from "node:test";

import {
  parseStructuredConfigValue,
  schemaConfigDefaults,
  schemaDefaultValue,
  setConfigDraftPath,
} from "../../frontend/fragment_type_config.js";

test("config defaults include required and explicit defaults but omit optional fields", () => {
  const schema = {
    type: "object",
    required: ["name", "mode", "nested"],
    properties: {
      name: { type: "string", minLength: 2 },
      mode: { type: "string", const: "fixed" },
      optional: { type: "number" },
      preferred: { type: "integer", default: 3 },
      nested: {
        type: "object",
        required: ["enabled"],
        properties: {
          enabled: { type: "boolean", default: true },
          omitted: { type: "string" },
        },
      },
    },
  };

  assert.deepEqual(schemaConfigDefaults(schema), {
    name: "xx",
    mode: "fixed",
    preferred: 3,
    nested: { enabled: true },
  });
});

test("array and numeric seeds respect declared lower bounds", () => {
  assert.equal(schemaDefaultValue({ type: "integer", minimum: 4 }), 4);
  assert.deepEqual(
    schemaDefaultValue({
      type: "array",
      minItems: 2,
      items: { type: "object", required: ["n"], properties: { n: { type: "number", minimum: 1 } } },
    }),
    [{ n: 1 }, { n: 1 }],
  );
});

test("structured input rejects malformed JSON without replacing the prior draft", () => {
  assert.deepEqual(parseStructuredConfigValue('{"ok":true}'), { ok: true, value: { ok: true } });
  assert.deepEqual(parseStructuredConfigValue('{"broken":'), { ok: false });
});

test("custom config views can update nested draft paths", () => {
  const draft = { options: { old: true } };
  setConfigDraftPath(draft, ["options", "threshold"], 7);
  setConfigDraftPath(draft, ["new", "deep", "value"], "x");
  assert.deepEqual(draft, {
    options: { old: true, threshold: 7 },
    new: { deep: { value: "x" } },
  });
});
