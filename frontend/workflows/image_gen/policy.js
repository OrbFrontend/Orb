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

export function privacyDisclosure({ source, apiUrl, providerId, providerLabel, sendsImages }) {
  if (source === "cloud") {
    const who = providerLabel || providerId || "this provider";
    const key = `orb:image-gen-privacy-cloud${sendsImages ? "-images" : ""}:${providerId || "unknown"}`;
    return {
      key,
      message:
        `Your scene prompts will be sent to ${who}, a third-party commercial API. ` +
        `Each image is billed to your account there, and ${who} may retain what you send under its own ` +
        "retention policy. " +
        (sendsImages
          ? "Reference images are turned on, so images from your conversations and your character reference " +
            "photo are uploaded there too. "
          : "") +
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
    key: `orb:image-gen-privacy${sendsImages ? "-images" : ""}:${origin}`,
    message:
      "This ComfyUI server is not on this machine. Your scene prompts leave Orb, other clients may read queued " +
      "prompts, and generated files remain on that server. " +
      (sendsImages
        ? "A workflow you assigned uses reference images, so images from your conversations and your character " +
          "reference image are uploaded there too. "
        : "") +
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
    if (!hasContent(entry) && !linked.length && !pending.includes(id)) continue;
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

export function modelTakesReferences(preset, model) {
  if (!preset?.supports_references) return false;
  const allowed = Array.isArray(preset.reference_models) ? preset.reference_models : [];
  if (!allowed.length) return true;
  const chosen = String(model || preset.default_model || "").toLowerCase();
  return allowed.some((marker) => chosen.includes(marker));
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
      sendsImages:
        connection.source === "cloud"
          ? linked.some((style) => !!style.reference_source)
          : (external.user_graphs || []).some((graph) => (graph?.slots?.references || []).length > 0),
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
