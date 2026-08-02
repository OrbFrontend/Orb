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

// What the `node_types` query action answers with: the typing verdict only,
// derived server-side from /object_info so the browser never receives that payload.
const NODE_TYPES = {
  CLIPTextEncode: { output_node: false, text_inputs: ["text"], seed_inputs: [], image_inputs: [] },
  KSampler: { output_node: false, text_inputs: [], seed_inputs: ["seed"], image_inputs: [] },
  SaveImage: { output_node: true, text_inputs: [], seed_inputs: [], image_inputs: [] },
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

// The picker must weigh a graph exactly as the normalizer does — UTF-8 bytes,
// taken after `is_changed` is stripped. Measuring UTF-16 code units let a CJK
// graph through at a third of its real size and the save came back one workflow
// short; measuring before the strip bounced graphs the backend would have stored.
test("measures the size cap the way the backend does", () => {
  const cjk = JSON.stringify({
    1: { class_type: "CLIPTextEncode", inputs: { text: "幻想的な風景、細部まで描写された".repeat(12000) } },
    2: { class_type: "KSampler", inputs: { seed: 1 } },
    3: { class_type: "SaveImage", inputs: { images: ["2", 0] } },
  });
  assert.ok(cjk.length < 512_000 && new TextEncoder().encode(cjk).length > 512_000, "precondition: under in UTF-16, over in bytes");
  assert.throws(() => graphFromApiJson(cjk), /too large/);

  const graph = { 1: { class_type: "CLIPTextEncode", inputs: { text: "" } }, 2: { class_type: "KSampler", inputs: { seed: 1 } } };
  for (let i = 3; i < 800; i++) graph[i] = { class_type: "LoadImage", inputs: { image: `${i}.png` }, is_changed: ["a".repeat(600)] };
  assert.ok(JSON.stringify(graph).length > 512_000, "precondition: over the cap only because of is_changed");
  // Accepted, and the caller still gets the graph it passed in — the strip is for
  // measurement only, and the backend repeats it on arrival.
  assert.deepEqual(graphFromApiJson(JSON.stringify(graph))[3].is_changed, graph[3].is_changed);
});

test("builds explicit slot candidates", () => {
  const candidates = slotCandidates(GRAPH, NODE_TYPES);
  assert.equal(candidates.text[0].label, "Positive (#1) — text");
  assert.deepEqual(splitCandidate(candidates.seed[0].value), ["2", "seed"]);
  assert.equal(candidates.output[0].nodeId, "3");
});

test("a save node's filename_prefix is not a prompt candidate", () => {
  // Krea/Flux export: SaveImage (#29) sorts before the encoder (#51), and real
  // /object_info types filename_prefix as STRING. Offering it made it the default
  // positive slot, pushing the real prompt onto the filename and the negative
  // onto the only encoder.
  const graph = {
    29: { class_type: "SaveImage", inputs: { filename_prefix: "Krea2", images: ["56", 0] } },
    51: { class_type: "CLIPTextEncode", inputs: { text: "1girl", clip: ["4", 1] } },
  };
  const typing = {
    SaveImage: { output_node: true, text_inputs: ["filename_prefix"], seed_inputs: [] },
    CLIPTextEncode: { output_node: false, text_inputs: ["text"], seed_inputs: [] },
  };
  const candidates = slotCandidates(graph, typing);
  assert.equal(candidates.text.length, 1);
  assert.deepEqual(splitCandidate(candidates.text[0].value), ["51", "text"]);
});

test("an output node's seed and model widgets sort last, but stay selectable", () => {
  const g = {
    1: { class_type: "SaveWithMeta", inputs: { seed: 1, ckpt_name: "logged.safetensors", images: ["3", 0] } },
    2: { class_type: "CheckpointLoaderSimple", inputs: { ckpt_name: "real.safetensors" } },
    3: { class_type: "KSampler", inputs: { seed: 1 } },
  };
  const typing = {
    SaveWithMeta: { output_node: true, text_inputs: [], seed_inputs: ["seed"] },
    CheckpointLoaderSimple: { output_node: false, text_inputs: [], seed_inputs: [] },
    KSampler: { output_node: false, text_inputs: [], seed_inputs: ["seed"] },
  };
  const c = slotCandidates(g, typing);
  assert.deepEqual(splitCandidate(c.seed[0].value), ["3", "seed"]);
  assert.deepEqual(splitCandidate(c.checkpoint[0].value), ["2", "ckpt_name"]);
  assert.equal(c.seed.length, 2);
  assert.equal(c.checkpoint.length, 2);

  // The all-in-one case: the only seed lives on the output node, so it must
  // still be offered or the graph becomes unimportable.
  const allInOne = {
    1: { class_type: "CLIPTextEncode", inputs: { text: "" } },
    2: { class_type: "KSamplerEfficient", inputs: { seed: 1 } },
  };
  const effTyping = {
    CLIPTextEncode: { output_node: false, text_inputs: ["text"], seed_inputs: [] },
    KSamplerEfficient: { output_node: true, text_inputs: [], seed_inputs: ["seed"] },
  };
  assert.deepEqual(missingRoles(slotCandidates(allInOne, effTyping)), []);
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

test("candidate values survive HTML-attribute parsing", () => {
  // The picker writes each value into an <option value> via innerHTML, and the
  // HTML tokenizer rewrites U+0000 in an attribute to U+FFFD. The original NUL
  // separator was mangled on that round-trip, so splitCandidate returned null and
  // the Confirm button silently did nothing. Simulate the rewrite here.
  const value = slotCandidates(GRAPH, NODE_TYPES).seed[0].value;
  assert.ok(!value.includes("\u0000"), "separator must not be NUL");
  const afterHtmlParse = value.replaceAll("\u0000", "\uFFFD");
  assert.deepEqual(splitCandidate(afterHtmlParse), ["2", "seed"]);
});

test("model-loader inputs are offered as model-override candidates", () => {
  // ckpt_name / unet_name become the "Model" slot, so the user's Orb checkpoint
  // overrides the filename an imported PNG pinned on another machine.
  const g = {
    1: { class_type: "UNETLoader", inputs: { unet_name: "foo.safetensors", weight_dtype: "default" }, _meta: { title: "Load Diffusion Model" } },
    2: { class_type: "CLIPTextEncode", inputs: { text: "" } },
    3: { class_type: "KSampler", inputs: { seed: 1 } },
    4: { class_type: "SaveImage", inputs: { images: ["3", 0] } },
  };
  const c = slotCandidates(g, {});
  assert.deepEqual(splitCandidate(c.checkpoint[0].value), ["1", "unet_name"]);
  // weight_dtype is a widget too, but not a model input.
  assert.equal(c.checkpoint.length, 1);
});

test("upload widgets become reference candidates from server typing", () => {
  // Qwen-Image-Edit shape: LoadImage (#103) feeds a scale node, which feeds both
  // encoders. The reference always enters through the LoadImage widget — the
  // encoder's image1 is a link and has no widget to patch.
  const edit = {
    103: { class_type: "LoadImage", inputs: { image: "woman-in-black.jpeg" }, _meta: { title: "Load Image" } },
    93: { class_type: "ImageScaleToTotalPixels", inputs: { image: ["103", 0], megapixels: 1 } },
    104: { class_type: "TextEncodeQwenImageEditPlus", inputs: { prompt: "", image1: ["93", 0] } },
    3: { class_type: "KSampler", inputs: { seed: 1 } },
    60: { class_type: "SaveImage", inputs: { images: ["8", 0] } },
  };
  const typing = {
    LoadImage: { output_node: false, text_inputs: [], seed_inputs: [], image_inputs: ["image"] },
    ImageScaleToTotalPixels: { output_node: false, text_inputs: [], seed_inputs: [], image_inputs: [] },
    TextEncodeQwenImageEditPlus: { output_node: false, text_inputs: ["prompt"], seed_inputs: [], image_inputs: [] },
    KSampler: { output_node: false, text_inputs: [], seed_inputs: ["seed"], image_inputs: [] },
    SaveImage: { output_node: true, text_inputs: [], seed_inputs: [], image_inputs: [] },
  };
  const c = slotCandidates(edit, typing);
  assert.equal(c.image.length, 1);
  assert.deepEqual(splitCandidate(c.image[0].value), ["103", "image"]);
  assert.equal(c.image[0].label, "Load Image (#103)");
  // The scale node's `image` input is a link, so it is never offered.
  assert.deepEqual(missingRoles(c), []);
});

test("size inputs are offered only where they are literally width and height", () => {
  // Both edges come back in one bucket; the picker filters it into two selects. The
  // point of the exactness is `grounding_px`: a bare INT that types identically and
  // is not an output size, so mapping it patches something else entirely.
  const sized = {
    5: { class_type: "EmptyLatentImage", inputs: { width: 1024, height: 1024, batch_size: 1 } },
    7: { class_type: "ImageScaleBy", inputs: { grounding_px: 768, upscale_method: "lanczos" } },
    2: { class_type: "KSampler", inputs: { seed: 1 } },
    3: { class_type: "SaveImage", inputs: { images: ["2", 0] } },
  };
  const typing = {
    EmptyLatentImage: { output_node: false, dimension_inputs: ["width", "height"] },
    // The server's rule already excluded it; this pins that the client agrees when
    // the server is the one answering.
    ImageScaleBy: { output_node: false, dimension_inputs: [] },
    KSampler: { output_node: false, seed_inputs: ["seed"] },
    SaveImage: { output_node: true },
  };
  assert.deepEqual(
    slotCandidates(sized, typing).dimension.map((i) => splitCandidate(i.value)),
    [
      ["5", "width"],
      ["5", "height"],
    ],
  );
  // With the server unreachable the name list stands in, and reaches the same
  // verdict on `grounding_px` -- a wrong guess here costs a broken render, so the
  // fallback is deliberately exact rather than seed's substring rule.
  assert.deepEqual(
    slotCandidates(sized, {}).dimension.map((i) => splitCandidate(i.value)),
    [
      ["5", "width"],
      ["5", "height"],
    ],
  );
  // A graph that maps neither is unaffected: dimensions are never a missing role.
  assert.deepEqual(slotCandidates(GRAPH, NODE_TYPES).dimension, []);
  assert.deepEqual(missingRoles(slotCandidates(GRAPH, NODE_TYPES)), []);
});

test("the unreachable-server fallback still finds stock loaders", () => {
  const edit = {
    72: { class_type: "LoadImage", inputs: { image: "scene.png" } },
    90: { class_type: "LoadImageMask", inputs: { image: "identity.png", channel: "red" } },
  };
  // `channel` is a widget on the same node, but not an upload input.
  assert.deepEqual(
    slotCandidates(edit, {}).image.map((i) => splitCandidate(i.value)),
    [
      ["72", "image"],
      ["90", "image"],
    ],
  );
  // And example.png's shape has nothing to map, typed or not, so a plain
  // text-to-image graph imports exactly the five slots it always did.
  assert.deepEqual(slotCandidates(GRAPH, NODE_TYPES).image, []);
  assert.deepEqual(slotCandidates(GRAPH, {}).image, []);
});
