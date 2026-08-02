// DOM-free, facade-free policy predicates shared by the settings panel.
//
// Separate from config_panel.js for the same reason render.js is separate from
// widget.js: anything importing the plugin facade pulls in the chat spine and
// touches the DOM at load, so it cannot be exercised under `node --test`. These
// rules decide what the user is warned about, which is exactly the kind of thing
// that should have a test.

// Loopback gets no privacy banner: none of the warning's claims — your prompts
// leave this machine, other clients can read the queue, files stay on that disk
// — describe a boundary being crossed when the server is this machine. A warning
// shown on every configuration is one users learn to click through.
export function isLoopbackUrl(apiUrl) {
  let parsed;
  try {
    parsed = new URL(apiUrl);
  } catch {
    // An unparseable URL is rejected by the backend normalizer before it can
    // reach a server, so there is no remote boundary to warn about.
    return true;
  }
  // URL.hostname keeps the brackets on an IPv6 literal, so `[::1]` is the form
  // that actually arrives here; comparing against a bare `::1` never matches.
  const host = parsed.hostname.toLowerCase().replace(/^\[/, "").replace(/\]$/, "");
  return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "0:0:0:0:0:0:0:1";
}

// What the user must be told before their prompts leave this machine, and under
// which acknowledgement key.
//
// The decision lives here rather than in the settings panel because the panel's
// version could only ask about the ComfyUI URL. That is *correct* while ComfyUI is
// the only source — and it becomes a silent hole the moment cloud is selectable: a
// config with cloud active and the ComfyUI URL still at its loopback default reads
// `isLoopbackUrl(apiUrl) === true`, and no cloud warning ever fires. So the branch
// belongs in the DOM-free module, under `node --test`, in the same change that
// gives it a second source to get wrong.
//
// Returns null when there is genuinely no boundary being crossed, else
// `{key, message}`: `key` is what the panel remembers an acknowledgement under,
// `message` is what it asks.
export function privacyDisclosure({ source, apiUrl, providerId, providerLabel, sendsImages }) {
  if (source === "cloud") {
    // Always non-null. There is no such thing as a loopback commercial API, and
    // the disclosure is materially larger than ComfyUI's: this one bills.
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
  // Loopback ComfyUI gets no banner: none of the warning's claims — your prompts
  // leave this machine, other clients can read the queue, files stay on that disk —
  // describe a boundary being crossed when the server is this machine. A warning
  // shown on every configuration is one users learn to click through.
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
// configured cloud provider. Styles link to a connection by id, which is what
// lets a hand-tuned anime checkpoint and a commercial API be one dropdown apart
// in the same conversation instead of a global mode switch away.
//
// The list is **derived, not stored**. ComfyUI is implied by `external_comfy`,
// and a cloud connection is implied by a `cloud.providers` entry that either
// holds something or is linked by a style. Deriving it is what keeps "this
// connection exists" and "this connection has credentials" from becoming two
// facts that can disagree — the failure the old global source picker had, where
// a config could be pointed at cloud with no key anywhere in it.
//
// The ComfyUI id is reserved: `_ID_RE` on the backend accepts it as a provider
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

// Whether this connection could render right now, mirroring the two backend
// `readiness()` implementations — but only their *connection-level* clauses. A
// ComfyUI style missing a workflow is a style problem and is reported on the
// style row; it says nothing about whether the server is configured.
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
// `providers` is the backend's preset catalogue (`status.providers`). A stored
// entry whose provider the catalogue no longer knows is still listed, with
// `preset: null` — the backend retains such rows precisely so a renamed provider
// does not erase a key on the next save, and hiding the row here would make that
// stored credential unreachable.
//
// `pending` is the ids the user has just added and not yet filled in. Several
// presets ship no default model, so a freshly added one holds nothing at all —
// and "counts as a connection because it holds something" would make it vanish
// between the click and the first keystroke.
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
    // can see the `api_key` leaf under the map level. It is not a connection the
    // user made, so an untouched, unlinked, unadded one stays out of the list.
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

// Which providers "Add connection" may still offer: the catalogue minus what is
// already on the list. Adding a second connection to one provider would need a
// synthetic id, and the credential map is keyed by provider id — so the honest
// answer while that is true is that each provider appears once.
export function addableProviders(connections, providers = []) {
  const taken = new Set(connections.map((c) => c.id));
  return providers.filter((p) => !taken.has(p.id));
}

// The connection a style renders on.
//
// `""` means the style predates connection linking. Those resolve to whatever
// the global source picker was last set to, so an existing install reads exactly
// as it did before this section existed and nothing silently re-routes.
export function styleConnectionId(style, config = {}) {
  const pinned = style?.connection || "";
  if (pinned) return pinned;
  const cloud = config.cloud || {};
  return config.source === "cloud" ? String(cloud.provider || "") : COMFY_CONNECTION;
}

export function findConnection(connections, id) {
  return connections.find((c) => c.id === id) || null;
}

// Every disclosure a save must collect, one per connection a style can actually
// reach. The old panel asked about the *active* source only, which was already
// the whole story while one global picker chose it. With styles linking
// connections independently, a save can turn on a second remote backend without
// that backend ever being "active" — so the question is asked per connection,
// and an unlinked one is not a boundary anything will cross.
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

// Prompt formats, mirroring backend config.PROMPT_FORMATS. The format decides how
// the composer writes the scene -- booru tags, mixed, or plain sentences -- so two
// styles with the same name produce very different prompts. Both pickers name it
// beside the style rather than only inside the style's own form.
export const PROMPT_FORMATS = [
  ["tags", "Tags"],
  ["hybrid", "Hybrid"],
  ["prose", "Prose"],
];
export const DEFAULT_PROMPT_FORMAT = "hybrid";

// What a stored value actually means, mirroring backend `_normalize_prompt_format`:
// anything unknown or missing renders as the default, because that is what the
// backend substitutes for it. Every surface reads the format through here, so the
// picker, the summary and the card can never disagree about a style's format.
export function normalizePromptFormat(value) {
  return PROMPT_FORMATS.some(([id]) => id === value) ? value : DEFAULT_PROMPT_FORMAT;
}

export function promptFormatLabel(value) {
  const id = normalizePromptFormat(value);
  return PROMPT_FORMATS.find(([f]) => f === id)[1];
}

// Camera modes, mirroring backend pov.POV_MODES. "auto" runs the local POV
// classifier; the other two pin the camera by hand. Global, like the style: it
// lives in the workflow config, not on a conversation.
export const POV_MODES = [
  ["auto", "Auto"],
  ["first", "First-person"],
  ["third", "Third-person"],
];

// What the picker offers, and which entry is selected.
//
// Without the classifier, "Auto" is a second name for the fallback camera --
// `pov.resolve` degrades it to exactly that -- so offering both would be two
// options that draw the same shot. Auto is dropped and the fallback shown in its
// place, which is the camera the next image will actually use.
//
// A config already set to "auto" keeps that stored value; the coerced selection
// is display only, and nothing writes until the user picks something.
// So installing or re-enabling the classifier brings Auto back, still selected.
export function povChoices({ classifier, mode, fallback }) {
  if (classifier) return { modes: POV_MODES, selected: mode };
  return {
    modes: POV_MODES.filter(([id]) => id !== "auto"),
    selected: mode === "auto" ? fallback : mode,
  };
}
