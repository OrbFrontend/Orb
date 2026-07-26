// DOM-free parsing and slot-candidate derivation for user-imported ComfyUI
// graphs. Kept free of the plugin facade so it loads under `node --test`.
//
// Node typing comes from the server's /object_info via the compact
// `node_types` query action and is passed in as `nodeTypes`; this module owns
// only the shape rules. When typing is unavailable (server unreachable, unknown
// class) it falls back to the conventional input names, which is a degraded
// picker rather than a broken one.

const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

// Mirrors MAX_GRAPH_BYTES in backend/workflows/image_gen/config.py. Checked here
// too so an oversized graph is refused at the file picker, where the user can
// still see which file they chose, instead of being silently dropped by
// normalization after save.
export const MAX_GRAPH_BYTES = 512_000;

const FALLBACK_TEXT_INPUTS = ["text"];
const FALLBACK_SEED_INPUTS = ["seed", "noise_seed"];
const FALLBACK_OUTPUT_CLASSES = ["SaveImage", "PreviewImage"];
// Widget inputs that name the diffusion model a loader reads. Offered as a
// "Model" slot so an imported graph's model can be overridden by the checkpoint
// the user picked in Orb, instead of staying pinned to a filename from whatever
// machine exported the PNG.
const MODEL_INPUTS = ["ckpt_name", "unet_name"];

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
  if (JSON.stringify(value).length > MAX_GRAPH_BYTES) {
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
  for (const [nodeId, node] of Object.entries(graph || {})) {
    const inputs = node?.inputs || {};
    const typing = nodeTypes[node.class_type];
    const textInputs = typing ? typing.text_inputs || [] : FALLBACK_TEXT_INPUTS;
    const seedInputs = typing ? typing.seed_inputs || [] : FALLBACK_SEED_INPUTS;
    const isOutput = typing ? !!typing.output_node : FALLBACK_OUTPUT_CLASSES.includes(node.class_type);
    for (const name of Object.keys(inputs)) {
      if (!isWidget(inputs[name])) continue;
      const item = { value: `${nodeId}\u001f${name}`, nodeId, input: name, label: `${label(nodeId, node)} — ${name}` };
      // A save/preview node's STRING widget (e.g. filename_prefix) types as text
      // but is never conditioning. Offering it made it the default positive slot
      // whenever the output node sorts before the encoder, so the real prompt
      // landed on the filename (later clobbered) and the negative on the encoder.
      if (!isOutput && textInputs.includes(name)) text.push(item);
      if (seedInputs.includes(name)) seed.push(item);
      if (MODEL_INPUTS.includes(name)) checkpoint.push(item);
    }
    if (isOutput) {
      output.push({ value: `${nodeId}\u001fimages`, nodeId, input: "images", label: label(nodeId, node) });
    }
  }
  return { text, seed, output, checkpoint };
}

// The three roles a graph cannot render without. `negative` is deliberately
// absent: a one-encoder prose graph has no negative conditioning, and the
// backend slot map treats it as optional for the same reason.
export function missingRoles({ text, seed, output }) {
  const missing = [];
  if (!text.length) missing.push("a prompt input");
  if (!seed.length) missing.push("a seed input");
  if (!output.length) missing.push("an image output node");
  return missing;
}

// Separator is U+001F, not NUL: these values land in an <option value> via
// innerHTML, and the HTML tokenizer rewrites U+0000 in an attribute to U+FFFD.
// A NUL separator never survived the round-trip, so split() found nothing and
// addPendingGraph() silently dropped every slot.
export function splitCandidate(value) {
  const [nodeId, input] = String(value || "").split("\u001f");
  return nodeId && input ? [nodeId, input] : null;
}
