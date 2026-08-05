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

export const MAX_REFERENCE_SLOTS = 4;

// The image slots one imported graph declares, as normalization stored them. A graph
// that loads no image has no `references` key at all.
export function graphReferenceSlots(graphs, workflowId) {
  const declared = (graphs || []).find((g) => g.id === workflowId)?.slots?.references;
  return Array.isArray(declared) ? declared.slice(0, MAX_REFERENCE_SLOTS) : [];
}

// What a style will actually send, which is never simply what it stores: a style keeps
// both backends' answers across a relink, so `["", "character"]` under a cloud provider
// is one slot and it is off. Anything reading the stored list to decide what leaves the
// machine asks the user to approve an upload the panel shows as Off and no adapter makes.
export function effectiveReferenceSources(style, { graphs = [], source = "" } = {}) {
  const stored = Array.isArray(style?.reference_sources) ? style.reference_sources : [];
  return stored.slice(0, source === "cloud" ? 1 : graphReferenceSlots(graphs, style?.workflow).length);
}

// ── resolution ───────────────────────────────────────────────────────────────
// The fallback menu, offered to a cloud provider that publishes no size vocabulary
// of its own. Its two 16:9-ish rows are why it is not shared with ComfyUI: 1820 is
// not a multiple of 8, and a latent is the request divided by eight.
export const CLOUD_SIZES = ["1024x1024", "1024x1536", "1536x1024", "1024x1820", "1820x1024"];
// Multiples of 64, the grid the checkpoints Orb can drive are trained on -- SD 1.5
// at 512, SDXL at 1024 and its native portrait/landscape pair. A ComfyUI style is
// not limited to these: the backend takes any edge from 64 to 4096, which is why an
// off-menu size already stored is kept rather than snapped to one of these.
export const COMFY_SIZES = ["512x512", "768x768", "1024x1024", "832x1216", "1216x832", "1024x1536", "1536x1024"];

// Every edge on the provider's own pixel grid. `width_height` is the only mode that
// has one, and it is a hard contract rather than a rounding preference -- Together
// 400s on a non-multiple of 16.
function fitsGrid(preset, value) {
  const step = preset.dimension_step || 1;
  const low = preset.min_dimension || step;
  const high = preset.max_dimension || 0;
  return value
    .split("x")
    .map(Number)
    .every((edge) => edge >= low && (!high || edge <= high) && edge % step === 0);
}

/** Whether this exact pair reaches the renderer intact.
 *
 * A menu provider snaps to its own list and a `width_height` one to its grid, both
 * server-side and both disclosed only after the render is paid for. ComfyUI, an
 * `aspect_ratio` provider (where nothing but the ratio is ever sent) and a `size`
 * provider that declares no menu all take what they are given.
 */
export function sizeIsExact(preset, comfy, value) {
  if (comfy) return true;
  const declared = preset?.sizes || [];
  if (declared.length) return declared.includes(value);
  if (preset?.dimension_mode === "width_height") return fitsGrid(preset, value);
  return true;
}

/** What the resolution picker may offer for this target.
 *
 * A provider that names its own sizes gets exactly those: offering it anything else
 * is offering a row that renders as something its label does not say -- which is the
 * whole failure `size_for`'s disclosure exists to report afterwards, by which point
 * the bill has landed. The declared list is used verbatim rather than intersected
 * with `CLOUD_SIZES`, so a provider whose menu shares nothing with Orb's is still
 * fully offered instead of reduced to nothing.
 */
export function sizeChoices(preset, comfy) {
  if (comfy) return COMFY_SIZES;
  const declared = Array.isArray(preset?.sizes) ? preset.sizes : [];
  if (declared.length) return declared;
  if (preset?.dimension_mode === "width_height") return CLOUD_SIZES.filter((value) => fitsGrid(preset, value));
  return CLOUD_SIZES;
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
      // One rule for both backends now that a style owns its reference sources. The
      // ComfyUI half used to ask whether *any* imported graph mapped a slot, which
      // over-asked for a server no style pointed a reference at and — worse — went on
      // asking after every style had turned them off. Effective, not stored: what the
      // adapter will send is the question, and a style carries answers for slots its
      // current target does not have.
      sendsImages: linked.some((style) =>
        effectiveReferenceSources(style, { graphs: external.user_graphs, source: connection.source }).some(Boolean),
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
