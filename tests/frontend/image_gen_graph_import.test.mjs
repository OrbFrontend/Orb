import assert from "node:assert/strict";
import test from "node:test";

import {
  classTypes,
  graphFromApiJson,
  graphFromPng,
  missingRoles,
  slotCandidates,
  splitCandidate,
} from "../../frontend/workflows/image_gen/graph_import.js";

const GRAPH = {
  1: { class_type: "CLIPTextEncode", inputs: { text: "", clip: ["4", 1] }, _meta: { title: "Positive" } },
  2: { class_type: "KSampler", inputs: { seed: 1 } },
  3: { class_type: "SaveImage", inputs: { images: ["5", 0] } },
};

// What POST /external/node-types answers with: the typing verdict only, derived
// server-side from /object_info so the browser never receives that payload.
const NODE_TYPES = {
  CLIPTextEncode: { output_node: false, text_inputs: ["text"], seed_inputs: [] },
  KSampler: { output_node: false, text_inputs: [], seed_inputs: ["seed"] },
  SaveImage: { output_node: true, text_inputs: [], seed_inputs: [] },
};

function pngWith(payload) {
  const encoder = new TextEncoder();
  const bytes = new Uint8Array(8 + 12 + payload.length);
  bytes.set([137, 80, 78, 71, 13, 10, 26, 10]);
  new DataView(bytes.buffer).setUint32(8, payload.length, false);
  bytes.set(encoder.encode("tEXt"), 12);
  bytes.set(payload, 16);
  return bytes.buffer;
}

test("accepts API graph and rejects UI workflow", () => {
  assert.deepEqual(graphFromApiJson(JSON.stringify(GRAPH)), GRAPH);
  assert.throws(() => graphFromApiJson('{"nodes":[],"links":[]}'), /UI workflow/);
});

test("refuses a graph too large for the config slot", () => {
  // Bounced at the file picker rather than dropped by normalization after save,
  // where the settings panel would keep listing a workflow nothing can run.
  const huge = {};
  for (let i = 0; i < 5000; i++) huge[i] = { class_type: "CLIPTextEncode", inputs: { text: "x".repeat(200) } };
  assert.throws(() => graphFromApiJson(JSON.stringify(huge)), /too large/);
});

test("builds explicit slot candidates", () => {
  const candidates = slotCandidates(GRAPH, NODE_TYPES);
  assert.equal(candidates.text[0].label, "Positive (#1) — text");
  assert.deepEqual(splitCandidate(candidates.seed[0].value), ["2", "seed"]);
  assert.equal(candidates.output[0].nodeId, "3");
});

test("server typing beats the class-name fallback", () => {
  // A graph whose save node is a custom class: the name heuristic finds no
  // output at all, while the server's output_node verdict does.
  const custom = {
    1: { class_type: "SomeTextNode", inputs: { prompt: "" } },
    2: { class_type: "SomeSampler", inputs: { noise: 1 } },
    3: { class_type: "ImageSaveWithMetadata", inputs: { images: ["2", 0] } },
  };
  const typing = {
    SomeTextNode: { output_node: false, text_inputs: ["prompt"], seed_inputs: [] },
    SomeSampler: { output_node: false, text_inputs: [], seed_inputs: ["noise"] },
    ImageSaveWithMetadata: { output_node: true, text_inputs: [], seed_inputs: [] },
  };
  assert.deepEqual(missingRoles(slotCandidates(custom, {})).length > 0, true);
  assert.deepEqual(missingRoles(slotCandidates(custom, typing)), []);
});

test("linked inputs are never slot candidates", () => {
  // A wired input carries [nodeId, slot], not a widget value; patching it would
  // replace the connection instead of the text.
  const linked = { 1: { class_type: "CLIPTextEncode", inputs: { text: ["9", 0] } } };
  assert.equal(slotCandidates(linked, NODE_TYPES).text.length, 0);
});

test("a one-encoder graph is importable — negative is optional", () => {
  const single = {
    1: { class_type: "CLIPTextEncode", inputs: { text: "" } },
    2: { class_type: "KSampler", inputs: { seed: 1 } },
    3: { class_type: "SaveImage", inputs: { images: ["2", 0] } },
  };
  assert.deepEqual(missingRoles(slotCandidates(single, NODE_TYPES)), []);
});

test("missing roles are named individually", () => {
  const noSeed = { 1: { class_type: "CLIPTextEncode", inputs: { text: "" } } };
  assert.deepEqual(missingRoles(slotCandidates(noSeed, NODE_TYPES)), [
    "a seed input",
    "an image output node",
  ]);
});

test("class types are collected once each for the typing request", () => {
  assert.deepEqual(classTypes(GRAPH).sort(), ["CLIPTextEncode", "KSampler", "SaveImage"]);
});

test("extracts prompt tEXt metadata from PNG", () => {
  const payload = new TextEncoder().encode(`prompt\0${JSON.stringify(GRAPH)}`);
  assert.deepEqual(graphFromPng(pngWith(payload)), GRAPH);
});

test("metadata-stripped PNG gets a clear error", () => {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  assert.throws(() => graphFromPng(bytes.buffer), /no embedded API workflow metadata/);
});
