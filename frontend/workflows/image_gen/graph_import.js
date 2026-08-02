// DOM-free parsing and slot-candidate derivation for user-imported ComfyUI
// graphs. Kept free of the plugin facade so it loads under `node --test`.
//
// Node typing comes from the server's /object_info via the compact
// `node_types` query action and is passed in as `nodeTypes`; this module owns
// only the shape rules. When typing is unavailable (server unreachable, unknown
// class) it falls back to the conventional input names, which is a degraded
// picker rather than a broken one.

const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

// Mirrors MAX_GRAPH_BYTES in backend/workflows/image_gen/config.py, so an oversized
// graph is refused at the picker where the user can still see which file they chose,
// rather than dropped by normalization and reported as a count that came back short.
// The measurement must match the backend's — UTF-8 bytes, taken *after* `is_changed`
// is stripped — or the two gates disagree in both directions.
const MAX_GRAPH_BYTES = 512_000;

const FALLBACK_TEXT_INPUTS = ["text"];
const FALLBACK_SEED_INPUTS = ["seed", "noise_seed"];
const FALLBACK_OUTPUT_CLASSES = ["SaveImage", "PreviewImage"];
// Upload widgets are typed server-side off /object_info's `image_upload` flag. With
// the server unreachable only the stock loaders are guessed, since a wrong guess
// would offer a reference slot that patches something else.
const FALLBACK_IMAGE_CLASSES = ["LoadImage", "LoadImageMask"];
const FALLBACK_IMAGE_INPUTS = ["image"];
// Widget inputs naming the diffusion model a loader reads, offered as a "Model" slot
// so Orb's checkpoint overrides the filename the PNG was exported with.
const MODEL_INPUTS = ["ckpt_name", "unet_name"];
// Deliberately exact, and deliberately the same list the server's `dimension_inputs`
// rule uses: a bare INT named `grounding_px` or `tile_size` is not an output size, and
// a wrong guess here patches something that is not one. Unlike the seed fallback,
// there is no near-miss spelling worth catching — an unoffered slot costs the user
// one unmapped picker, a wrong one costs a broken render.
const FALLBACK_DIMENSION_INPUTS = ["width", "height"];

// Mirrors `_strip_machine_local_state` in the backend normalizer. Measured on a copy:
// the caller stores the graph it passed in, and the backend strips again on arrival.
function graphByteLength(graph) {
  const stripped = Object.fromEntries(
    Object.entries(graph).map(([nodeId, node]) => {
      if (!node || typeof node !== "object" || Array.isArray(node)) return [nodeId, node];
      const { is_changed: _drop, ...rest } = node;
      return [nodeId, rest];
    }),
  );
  return new TextEncoder().encode(JSON.stringify(stripped)).length;
}

function parseGraphJson(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("The file does not contain valid JSON.");
  }
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("The API workflow must be an object.");
  if (Array.isArray(value.nodes) || Array.isArray(value.links)) {
    throw new Error("This is a UI workflow. In ComfyUI dev mode, use Workflow → Export (API). ");
  }
  const nodes = Object.entries(value);
  if (!nodes.length || nodes.some(([, node]) => !node || typeof node.class_type !== "string" || !node.inputs)) {
    throw new Error("The file is not a ComfyUI API workflow.");
  }
  if (graphByteLength(value) > MAX_GRAPH_BYTES) {
    throw new Error("This workflow is too large to store in Orb's settings.");
  }
  return value;
}

export function graphFromApiJson(text) {
  return parseGraphJson(text);
}

export function graphFromPng(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 8 || PNG_SIGNATURE.some((b, i) => bytes[i] !== b))
    throw new Error("The selected file is not a PNG.");
  const view = new DataView(buffer);
  const decoder = new TextDecoder();
  let offset = 8;
  while (offset + 12 <= bytes.length) {
    const length = view.getUint32(offset, false);
    const type = decoder.decode(bytes.slice(offset + 4, offset + 8));
    const start = offset + 8;
    const end = start + length;
    if (end + 4 > bytes.length) break;
    if (type === "tEXt") {
      const chunk = bytes.slice(start, end);
      const separator = chunk.indexOf(0);
      if (separator > 0 && decoder.decode(chunk.slice(0, separator)) === "prompt") {
        return parseGraphJson(decoder.decode(chunk.slice(separator + 1)));
      }
    }
    offset = end + 4;
  }
  throw new Error("This PNG has no embedded API workflow metadata.");
}

export function classTypes(graph) {
  return [
    ...new Set(
      Object.values(graph || {})
        .map((node) => node?.class_type)
        .filter((c) => typeof c === "string"),
    ),
  ];
}

function label(nodeId, node) {
  const title = node?._meta?.title;
  return `${typeof title === "string" && title ? title : node.class_type} (#${nodeId})`;
}

// A ComfyUI link is encoded as [nodeId, slot], so an array-valued input is wired
// from another node and has no widget to patch — never a slot candidate.
function isWidget(value) {
  return !Array.isArray(value);
}

export function slotCandidates(graph, nodeTypes = {}) {
  const text = [];
  const seed = [];
  const output = [];
  const checkpoint = [];
  const image = [];
  const dimension = [];
  const seedTail = [];
  const checkpointTail = [];
  for (const [nodeId, node] of Object.entries(graph || {})) {
    const inputs = node?.inputs || {};
    const typing = nodeTypes[node.class_type];
    const textInputs = typing ? typing.text_inputs || [] : FALLBACK_TEXT_INPUTS;
    const seedInputs = typing ? typing.seed_inputs || [] : FALLBACK_SEED_INPUTS;
    const dimensionInputs = typing ? typing.dimension_inputs || [] : FALLBACK_DIMENSION_INPUTS;
    const imageInputs = typing
      ? typing.image_inputs || []
      : FALLBACK_IMAGE_CLASSES.includes(node.class_type)
        ? FALLBACK_IMAGE_INPUTS
        : [];
    const isOutput = typing ? !!typing.output_node : FALLBACK_OUTPUT_CLASSES.includes(node.class_type);
    for (const name of Object.keys(inputs)) {
      if (!isWidget(inputs[name])) continue;
      const item = { value: `${nodeId}\u001f${name}`, nodeId, input: name, label: `${label(nodeId, node)} — ${name}` };
      // A save/preview node's STRING widget (filename_prefix) types as text but is
      // never conditioning: offering it makes it the default positive slot whenever
      // the output node sorts before the encoder, landing the prompt on the filename.
      if (!isOutput && textInputs.includes(name)) text.push(item);
      if (seedInputs.includes(name)) (isOutput ? seedTail : seed).push(item);
      if (MODEL_INPUTS.includes(name)) (isOutput ? checkpointTail : checkpoint).push(item);
      // One bucket for both edges rather than two, because the caller filters it by
      // `input` anyway — and a multi-stage graph legitimately offers the same node to
      // both pickers, so splitting here would only duplicate the filter.
      if (dimensionInputs.includes(name)) dimension.push(item);
      // Labelled by node, not input name: a two-reference graph asks which LoadImage
      // is the identity and which is the scene.
      if (imageInputs.includes(name)) image.push({ ...item, label: label(nodeId, node) });
    }
    if (isOutput) {
      output.push({ value: `${nodeId}\u001fimages`, nodeId, input: "images", label: label(nodeId, node) });
    }
  }
  return {
    text,
    seed: [...seed, ...seedTail],
    output,
    checkpoint: [...checkpoint, ...checkpointTail],
    image,
    dimension,
  };
}

// The three roles a graph cannot render without. `negative` and the two size slots
// are deliberately absent: a one-encoder prose graph has no negative conditioning and
// an img2img graph takes its size from its reference, and the backend slot map treats
// all three as optional for the same reason.
export function missingRoles({ text, seed, output }) {
  const missing = [];
  if (!text.length) missing.push("a prompt input");
  if (!seed.length) missing.push("a seed input");
  if (!output.length) missing.push("an image output node");
  return missing;
}

// U+001F, not NUL: these values land in an <option value> via innerHTML, and the
// HTML tokenizer rewrites U+0000 in an attribute to U+FFFD, so a NUL separator does
// not survive the round-trip and split() finds nothing.
export function splitCandidate(value) {
  const [nodeId, input] = String(value || "").split("\u001f");
  return nodeId && input ? [nodeId, input] : null;
}
