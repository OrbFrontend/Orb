const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

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

function label(nodeId, node) {
  const title = node?._meta?.title;
  return `${typeof title === "string" && title ? title : node.class_type} (#${nodeId})`;
}

export function slotCandidates(graph) {
  const text = [];
  const seed = [];
  const output = [];
  for (const [nodeId, node] of Object.entries(graph || {})) {
    const inputs = node?.inputs || {};
    for (const name of Object.keys(inputs)) {
      const item = { value: `${nodeId}\0${name}`, nodeId, input: name, label: `${label(nodeId, node)} — ${name}` };
      if (name === "text") text.push(item);
      if (name === "seed" || name === "noise_seed") seed.push(item);
    }
    if (node.class_type === "SaveImage" || node.class_type === "PreviewImage") {
      output.push({ value: `${nodeId}\0images`, nodeId, input: "images", label: label(nodeId, node) });
    }
  }
  return { text, seed, output };
}

export function splitCandidate(value) {
  const [nodeId, input] = String(value || "").split("\0");
  return nodeId && input ? [nodeId, input] : null;
}
