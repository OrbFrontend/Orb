import assert from "node:assert/strict";
import test from "node:test";

import { graphFromApiJson, graphFromPng, slotCandidates, splitCandidate } from "../../frontend/workflows/image_gen/graph_import.js";

const GRAPH = {
  1: { class_type: "CLIPTextEncode", inputs: { text: "", clip: ["4", 1] }, _meta: { title: "Positive" } },
  2: { class_type: "KSampler", inputs: { seed: 1 } },
  3: { class_type: "SaveImage", inputs: { images: ["5", 0] } },
};

test("accepts API graph and rejects UI workflow", () => {
  assert.deepEqual(graphFromApiJson(JSON.stringify(GRAPH)), GRAPH);
  assert.throws(() => graphFromApiJson('{"nodes":[],"links":[]}'), /UI workflow/);
});

test("builds explicit slot candidates", () => {
  const candidates = slotCandidates(GRAPH);
  assert.equal(candidates.text[0].label, "Positive (#1) — text");
  assert.deepEqual(splitCandidate(candidates.seed[0].value), ["2", "seed"]);
  assert.equal(candidates.output[0].nodeId, "3");
});

test("extracts prompt tEXt metadata from PNG", () => {
  const encoder = new TextEncoder();
  const payload = encoder.encode(`prompt\0${JSON.stringify(GRAPH)}`);
  const bytes = new Uint8Array(8 + 12 + payload.length);
  bytes.set([137, 80, 78, 71, 13, 10, 26, 10]);
  const view = new DataView(bytes.buffer);
  view.setUint32(8, payload.length, false);
  bytes.set(encoder.encode("tEXt"), 12);
  bytes.set(payload, 16);
  assert.deepEqual(graphFromPng(bytes.buffer), GRAPH);
});

test("metadata-stripped PNG gets a clear error", () => {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  assert.throws(() => graphFromPng(bytes.buffer), /no embedded API workflow metadata/);
});
