import assert from "node:assert/strict";
import test from "node:test";

import { modelPickerState } from "../../frontend/workflows/image_gen/model_picker.js";

test("uses a select when model discovery returns one or more models", () => {
  assert.deepEqual(modelPickerState(["flux.safetensors"], "flux.safetensors"), {
    kind: "select",
    models: ["flux.safetensors"],
    current: "flux.safetensors",
  });
});

test("uses a text input when discovery is empty or failed", () => {
  assert.equal(modelPickerState([]).kind, "input");
  assert.equal(modelPickerState(undefined).kind, "input");
  assert.equal(modelPickerState({ error: "offline" }).kind, "input");
});

test("ignores invalid and duplicate discovery results without losing the current value", () => {
  assert.deepEqual(modelPickerState(["a.ckpt", null, "a.ckpt", "b.ckpt"], "old.ckpt"), {
    kind: "select",
    models: ["a.ckpt", "b.ckpt"],
    current: "old.ckpt",
  });
});
