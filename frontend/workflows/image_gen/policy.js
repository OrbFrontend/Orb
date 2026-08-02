// DOM-free, facade-free policy predicates shared by the settings panel.
//
// Separate from config_panel.js for the same reason render.js is separate from
// widget.js: anything importing the plugin facade pulls in the chat spine and
// touches the DOM at load, so it cannot be exercised under `node --test`. These
// rules decide what the user is warned about, which is worth a test.

export function isLoopbackUrl(apiUrl) {
  let parsed;
  try {
    parsed = new URL(apiUrl);
  } catch {
    // The backend normalizer rejects an unparseable URL before it can reach a
    // server, so there is no remote boundary to warn about.
    return true;
  }
  // URL.hostname keeps the brackets on an IPv6 literal, so `[::1]` is what arrives
  // here and a bare `::1` comparison never matches.
  const host = parsed.hostname.toLowerCase().replace(/^\[/, "").replace(/\]$/, "");
  return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "0:0:0:0:0:0:0:1";
}

// What the user must be told before their prompts leave this machine, and under
// which acknowledgement key. Null when no boundary is crossed, else `{key, message}`.
//
// A panel-side version could only ask about the ComfyUI URL — correct while ComfyUI
// was the only source, and a silent hole the moment cloud is selectable: with cloud
// active and the ComfyUI URL still at its loopback default, `isLoopbackUrl` reads
// true and no cloud warning ever fires.
export function privacyDisclosure({ source, apiUrl, providerId, providerLabel, sendsImages }) {
  if (source === "cloud") {
    // Always non-null: there is no such thing as a loopback commercial API, and the
    // disclosure is materially larger than ComfyUI's, because this one bills.
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
  // Loopback ComfyUI gets no banner: none of the warning's claims describe a
  // boundary being crossed when the server is this machine, and a warning shown on
  // every configuration is one users learn to click through.
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

// ── connections ──────────────────────────────────────────────────────────────
//
// A connection is one place an image can be rendered: the ComfyUI server, or one
// configured cloud provider. Styles link to one by id, which is what puts a
// hand-tuned anime checkpoint and a commercial API one dropdown apart in the same
// conversation rather than a global mode switch away.
//
// The list is **derived, not stored**: ComfyUI is implied by `external_comfy`, and
// a cloud connection by a `cloud.providers` entry that holds something or is linked
// by a style. That is what keeps "this connection exists" and "this connection has
// credentials" from becoming two facts that can disagree.
//
// The ComfyUI id is reserved -- the backend's id pattern accepts it as a provider
// id too, so nothing may ever ship a cloud preset called `comfy`.
export const COMFY_CONNECTION = "comfy";

function hasContent(entry) {
  return !!(entry && (entry.api_key || entry.model || entry.base_url));
}

function hostLabel(apiUrl) {
  try {
    return new URL(apiUrl).host;
  } catch {
    return apiUrl || "";
  }
}

// Whether this connection could render right now, mirroring the backend
// `readiness()` implementations — but only their *connection-level* clauses. A
// ComfyUI style missing a workflow is a style problem, reported on the style row.
function readiness(connection, entry, preset) {
  if (connection.source !== "cloud") {
    return connection.detail ? { ready: true, detail: connection.detail } : { ready: false, detail: "No server URL" };
  }
  if (!preset) return { ready: false, detail: "Unknown provider" };
  if (preset.needs_base_url && !entry.base_url) return { ready: false, detail: "No API base URL" };
  if (!entry.api_key) return { ready: false, detail: "No API key" };
  if (!connection.detail) return { ready: false, detail: "No model" };
  return { ready: true, detail: connection.detail };
}

// Every connection the settings form should list, ComfyUI first.
//
// `providers` is the backend's preset catalogue. A stored entry whose provider it
// no longer knows is still listed with `preset: null` -- the backend retains such
// rows so a rename does not erase a key, and hiding the row would make that
// credential unreachable. `pending` is the ids just added and not yet filled in,
// which "counts because it holds something" would drop between the click and the
// first keystroke.
export function connectionList(config = {}, providers = [], pending = []) {
  const comfy = config.external_comfy || {};
  const cloud = config.cloud || {};
  const entries = cloud.providers || {};
  const styles = Array.isArray(config.styles) ? config.styles : [];
  const linked = new Set([...styles.map((s) => s?.connection).filter(Boolean), ...pending]);

  const list = [
    {
      id: COMFY_CONNECTION,
      source: "external_comfy",
      label: "ComfyUI",
      kind: "Local",
      removable: false,
      preset: null,
      detail: hostLabel(comfy.api_url || ""),
    },
  ];
  for (const [id, entry] of Object.entries(entries)) {
    // The shipped config carries one empty `xai` row so the preset-schema walker
    // sees the `api_key` leaf. It is not a connection the user made, so an
    // untouched, unlinked, unadded one stays out of the list.
    if (!hasContent(entry) && !linked.has(id)) continue;
    const preset = providers.find((p) => p.id === id) || null;
    list.push({
      id,
      source: "cloud",
      label: preset?.label || id,
      kind: "Cloud",
      removable: true,
      preset,
      detail: entry?.model || preset?.default_model || "",
    });
  }
  return list.map((connection) => ({
    ...connection,
    ...readiness(connection, entries[connection.id] || {}, connection.preset),
  }));
}

// The catalogue minus what is already listed. A second connection to one provider
// would need a synthetic id, and the credential map is keyed by provider id, so
// each provider appears once.
export function addableProviders(connections, providers = []) {
  const taken = new Set(connections.map((c) => c.id));
  return providers.filter((p) => !taken.has(p.id));
}

// The connection a style renders on. `""` means the style predates connection
// linking and resolves to whatever the global source picker was last set to, so an
// existing install reads exactly as it did and nothing silently re-routes.
export function styleConnectionId(style, config = {}) {
  const pinned = style?.connection || "";
  if (pinned) return pinned;
  const cloud = config.cloud || {};
  return config.source === "cloud" ? String(cloud.provider || "") : COMFY_CONNECTION;
}

export function findConnection(connections, id) {
  return connections.find((c) => c.id === id) || null;
}

// Every disclosure a save must collect, one per connection a style can reach. Asked
// per connection because a save can turn on a second remote backend without it ever
// being "active"; an unlinked connection is not a boundary anything will cross.
export function pendingDisclosures(config = {}, connections = []) {
  const styles = Array.isArray(config.styles) ? config.styles : [];
  const used = new Set(styles.map((style) => styleConnectionId(style, config)));
  const external = config.external_comfy || {};
  const entries = config.cloud?.providers || {};
  const notices = [];
  for (const connection of connections) {
    if (!used.has(connection.id)) continue;
    const entry = entries[connection.id] || {};
    const notice = privacyDisclosure({
      source: connection.source,
      apiUrl: external.api_url || "",
      providerId: connection.id,
      providerLabel: connection.label,
      sendsImages:
        connection.source === "cloud"
          ? !!entry.reference_source
          : (external.user_graphs || []).some((graph) => (graph?.slots?.references || []).length > 0),
    });
    if (notice) notices.push(notice);
  }
  return notices;
}

// Mirrors backend config.PROMPT_FORMATS. The format decides how the composer writes
// the scene -- booru tags, mixed, or plain sentences -- so both pickers name it
// beside the style rather than only inside the style's own form.
export const PROMPT_FORMATS = [
  ["tags", "Tags"],
  ["hybrid", "Hybrid"],
  ["prose", "Prose"],
];
export const DEFAULT_PROMPT_FORMAT = "hybrid";

// Mirrors backend `_normalize_prompt_format`: unknown or missing renders as the
// default, because that is what the backend substitutes. Every surface reads the
// format through here, so the picker, the summary and the card cannot disagree.
export function normalizePromptFormat(value) {
  return PROMPT_FORMATS.some(([id]) => id === value) ? value : DEFAULT_PROMPT_FORMAT;
}

export function promptFormatLabel(value) {
  const id = normalizePromptFormat(value);
  return PROMPT_FORMATS.find(([f]) => f === id)[1];
}

// Mirrors backend pov.POV_MODES. "auto" runs the local classifier; the other two
// pin the camera by hand. Global, like the style: workflow config, not conversation.
export const POV_MODES = [
  ["auto", "Auto"],
  ["first", "First-person"],
  ["third", "Third-person"],
];

// What the picker offers, and which entry is selected.
//
// Without the classifier `pov.resolve` degrades "Auto" to the fallback camera, so
// offering both would be two options drawing the same shot: Auto is dropped and the
// fallback shown in its place. A config already set to "auto" keeps that stored
// value -- the coerced selection is display only and nothing writes until the user
// picks -- so re-enabling the classifier brings Auto back, still selected.
export function povChoices({ classifier, mode, fallback }) {
  if (classifier) return { modes: POV_MODES, selected: mode };
  return {
    modes: POV_MODES.filter(([id]) => id !== "auto"),
    selected: mode === "auto" ? fallback : mode,
  };
}
