export function isLoopbackUrl(apiUrl) {
  let parsed;
  try {
    parsed = new URL(apiUrl);
  } catch {
    return true;
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[/, "").replace(/\]$/, "");
  return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "0:0:0:0:0:0:0:1";
}

// Bump this only when a render can upload more image data.
const IMAGE_DISCLOSURE_VERSION = "-images-v2";

const SENDS_IMAGES =
  "A reference image is turned on, so images from your conversations are uploaded there too — " +
  "the character reference photo or card art for the character each picture is of, or the previous " +
  "image in the chat. ";

export function privacyDisclosure({ source, apiUrl, providerId, providerLabel, sendsImages }) {
  const scope = sendsImages ? IMAGE_DISCLOSURE_VERSION : "";
  if (source === "cloud") {
    const who = providerLabel || providerId || "this provider";
    const key = `orb:image-gen-privacy-cloud${scope}:${providerId || "unknown"}`;
    return {
      key,
      message:
        `Your scene prompts will be sent to ${who}, a third-party commercial API. ` +
        `Each image is billed to your account there, and ${who} may retain what you send under its own ` +
        "retention policy. " +
        (sendsImages ? SENDS_IMAGES : "") +
        "Save this connection?",
    };
  }
  if (isLoopbackUrl(apiUrl)) return null;
  let origin;
  try {
    origin = new URL(apiUrl).origin;
  } catch {
    return null;
  }
  return {
    key: `orb:image-gen-privacy${scope}:${origin}`,
    message:
      "This ComfyUI server is not on this machine. Your scene prompts leave Orb, other clients may read queued " +
      "prompts, and generated files remain on that server. " +
      (sendsImages ? SENDS_IMAGES : "") +
      "Save this connection?",
  };
}

export const COMFY_CONNECTION = "comfy";

export function connectionLabel(id, providers = []) {
  if (id === COMFY_CONNECTION) return "ComfyUI";
  if (!id) return "No connection";
  return providers.find((p) => p.id === id)?.label || id;
}

function hasContent(entry) {
  return !!(entry && (entry.api_key || entry.base_url));
}

function hostLabel(apiUrl) {
  try {
    return new URL(apiUrl).host;
  } catch {
    return apiUrl || "";
  }
}

function linkedLabel(count) {
  if (!count) return "No styles";
  return count === 1 ? "1 style" : `${count} styles`;
}

function readiness(connection, entry, preset) {
  if (connection.source !== "cloud") {
    return connection.detail ? { ready: true, detail: connection.detail } : { ready: false, detail: "No server URL" };
  }
  if (!preset) return { ready: false, detail: "Unknown provider" };
  if (preset.needs_base_url && !entry.base_url) return { ready: false, detail: "No API base URL" };
  if (!entry.api_key) return { ready: false, detail: "No API key" };
  return { ready: true, detail: connection.detail };
}

function stylesOn(config = {}, id) {
  const styles = Array.isArray(config.styles) ? config.styles : [];
  return styles.filter((style) => styleConnectionId(style, config) === id);
}

export function connectionList(config = {}, providers = [], pending = []) {
  const pendingIds = new Set(pending);
  const entries = config.cloud?.providers || {};
  const list = [
    {
      id: COMFY_CONNECTION,
      source: "external_comfy",
      label: connectionLabel(COMFY_CONNECTION),
      kind: "Local",
      removable: false,
      preset: null,
      detail: hostLabel(config.external_comfy?.api_url || ""),
    },
  ];
  for (const [id, entry] of Object.entries(entries)) {
    const linked = stylesOn(config, id);
    if (!hasContent(entry) && !linked.length && !pendingIds.has(id)) continue;
    list.push({
      id,
      source: "cloud",
      label: connectionLabel(id, providers),
      kind: "Cloud",
      removable: true,
      preset: providers.find((p) => p.id === id) || null,
      detail: linkedLabel(linked.length),
    });
  }
  return list.map((connection) => ({
    ...connection,
    ...readiness(connection, entries[connection.id] || {}, connection.preset),
  }));
}

export function addableProviders(connections, providers = []) {
  const taken = new Set(connections.map((c) => c.id));
  return providers.filter((p) => !taken.has(p.id));
}

export function styleConnectionId(style, config = {}) {
  const pinned = style?.connection || "";
  if (pinned) return pinned;
  const cloud = config.cloud || {};
  return config.source === "cloud" ? String(cloud.provider || "") : COMFY_CONNECTION;
}

export function findConnection(connections, id) {
  return connections.find((c) => c.id === id) || null;
}

export const MAX_REFERENCE_SLOTS = 4;

export function graphReferenceSlots(graphs, workflowId) {
  const declared = (graphs || []).find((g) => g.id === workflowId)?.slots?.references;
  return Array.isArray(declared) ? declared.slice(0, MAX_REFERENCE_SLOTS) : [];
}

export function maxCloudReferences(preset) {
  const declared = Number(preset?.max_references);
  return Number.isFinite(declared) && declared >= 1 ? Math.min(declared, MAX_REFERENCE_SLOTS) : 1;
}

export function sendsReference(style, { graphs = [], source = "", preset = null } = {}) {
  if (!style?.reference_source) return false;
  return source === "cloud" ? providerTakesReferences(preset) : graphReferenceSlots(graphs, style?.workflow).length > 0;
}

export const CLOUD_SIZES = ["1024x1024", "1024x1536", "1536x1024", "1024x1820", "1820x1024"];
export const COMFY_SIZES = ["512x512", "768x768", "1024x1024", "832x1216", "1216x832", "1024x1536", "1536x1024"];

function fitsGrid(preset, value) {
  const step = preset.dimension_step || 1;
  const low = preset.min_dimension || step;
  const high = preset.max_dimension || 0;
  return value
    .split("x")
    .map(Number)
    .every((edge) => edge >= low && (!high || edge <= high) && edge % step === 0);
}

export function sizeIsExact(preset, comfy, value) {
  if (comfy) return true;
  const declared = preset?.sizes || [];
  if (declared.length) return declared.includes(value);
  if (preset?.dimension_mode === "width_height") return fitsGrid(preset, value);
  return true;
}

export function sizeChoices(preset, comfy) {
  if (comfy) return COMFY_SIZES;
  const declared = Array.isArray(preset?.sizes) ? preset.sizes : [];
  if (declared.length) return declared;
  if (preset?.dimension_mode === "width_height") return CLOUD_SIZES.filter((value) => fitsGrid(preset, value));
  return CLOUD_SIZES;
}

export function providerTakesReferences(preset) {
  return Boolean(preset?.supports_references);
}

export function pendingDisclosures(config = {}, connections = []) {
  const external = config.external_comfy || {};
  const notices = [];
  for (const connection of connections) {
    const linked = stylesOn(config, connection.id);
    if (!linked.length) continue;
    const notice = privacyDisclosure({
      source: connection.source,
      apiUrl: external.api_url || "",
      providerId: connection.id,
      providerLabel: connection.label,
      sendsImages: linked.some((style) =>
        sendsReference(style, {
          graphs: external.user_graphs,
          source: connection.source,
          preset: connection.preset,
        }),
      ),
    });
    if (notice) notices.push(notice);
  }
  return notices;
}

export const PROMPT_FORMATS = [
  ["tags", "Tags"],
  ["hybrid", "Hybrid"],
  ["prose", "Prose"],
];
export const DEFAULT_PROMPT_FORMAT = "hybrid";

export function normalizePromptFormat(value) {
  return PROMPT_FORMATS.some(([id]) => id === value) ? value : DEFAULT_PROMPT_FORMAT;
}

export function promptFormatLabel(value) {
  const id = normalizePromptFormat(value);
  return PROMPT_FORMATS.find(([f]) => f === id)[1];
}

export const POV_MODES = [
  ["auto", "Auto"],
  ["first", "First-person"],
  ["third", "Third-person"],
];

export function povChoices({ classifier, mode, fallback }) {
  if (classifier) return { modes: POV_MODES, selected: mode };
  return {
    modes: POV_MODES.filter(([id]) => id !== "auto"),
    selected: mode === "auto" ? fallback : mode,
  };
}
