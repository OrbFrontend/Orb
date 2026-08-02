import {
  api,
  closeModal,
  convUrl,
  esc,
  escAttr,
  getActiveConvId,
  registerAction,
  showModal,
  toast,
} from "/static/workflow_api.js";
import {
  classTypes,
  graphFromApiJson,
  graphFromPng,
  missingRoles,
  slotCandidates,
  splitCandidate,
} from "./graph_import.js";
import { modelPickerState } from "./model_picker.js";
import {
  addableProviders,
  COMFY_CONNECTION,
  connectionList,
  DEFAULT_PROMPT_FORMAT,
  findConnection,
  normalizePromptFormat,
  PROMPT_FORMATS,
  pendingDisclosures,
  povChoices,
  promptFormatLabel,
  styleConnectionId,
} from "./policy.js";

const WORKFLOW_ID = "image_gen";

// What a mapped LoadImage node can be fed, in menu order — the combined choice
// leads because it is the one with no cold-start cliff on a fresh conversation.
const REFERENCE_SOURCES = [
  ["previous_or_character", "Previous image, else character reference"],
  ["previous", "Previous image in the chat"],
  ["character", "Character reference image"],
];
// Mirrored from the backend normalizer, which drops what it will not store. Checked
// here so an over-count or an unsupported file becomes a message, rather than an
// image that previews fine and is silently gone on the next open.
const MAX_REFERENCE_SLOTS = 4;
const MAX_USER_GRAPHS = 32;
const MAX_REFERENCE_IMAGE_BYTES = 10_000_000;
const REFERENCE_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp"];

// Resolution presets for the cloud picker, stored as pixels even for providers that
// speak aspect ratios — one canonical representation, converted at the wire by a
// pure backend function. The exact mapping is disclosed as a render note rather
// than previewed here; mirroring the aspect math into JS would be a drift risk.
// Mirrors DEFAULT_CLOUD_EDGE in the backend normalizer: what an entry that has
// never been sized renders at, in the one place every surface reads it from.
const DEFAULT_EDGE = 1024;
const entrySize = (entry) => [Number(entry?.width) || DEFAULT_EDGE, Number(entry?.height) || DEFAULT_EDGE];

const CLOUD_SIZES = [
  [1024, 1024, "Square — 1024x1024"],
  [1024, 1536, "Portrait — 1024x1536"],
  [1536, 1024, "Landscape — 1536x1024"],
  [1024, 1820, "Tall — 1024x1820"],
  [1820, 1024, "Wide — 1820x1024"],
];
const CLOUD_QUALITIES = [
  ["", "Provider default"],
  ["low", "Low"],
  ["medium", "Medium"],
  ["high", "High"],
];

// One builder, because the summary is written twice -- at render and as the field
// is edited -- and two spellings of the same badge would drift.
const promptFormatBadge = (value) => `(${promptFormatLabel(value)})`;

// Every <option> list in this panel, from [value, label] pairs. One builder so the
// escaping and the selected-marking cannot differ between two menus.
const optionList = (pairs, selected) =>
  pairs
    .map(
      ([value, label]) =>
        `<option value="${escAttr(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`,
    )
    .join("");
let cfg;
let pendingGraph = null;
// The backend's answer about which sources exist and what each provider can do.
// One payload behind the connection list, the Add picker and the capability lines,
// so the three cannot disagree. Primed by the status query.
let backends = { sources: [], providers: [] };
// Everything the open form is editing. A working copy rather than cfg itself:
// adding a connection then closing without saving must not leave the shared config
// carrying credentials the server never stored.
let draft = { styles: [], graphs: [], comfy: {}, connections: {} };
// Recomputed from `draft` whenever it changes, never stored: a connection *is* its
// credentials, so a list held alongside them is a second copy that can disagree.
let connections = [];
// Discovered model names, per connection id -- checkpoints belong to one ComfyUI
// server and model ids to one provider, so a shared list would offer the wrong menu.
let modelsByConnection = {};
// Probe generation per connection, so a slow answer never overwrites a fresh one.
let probeIds = {};
// Connections added in this modal that hold nothing yet. Several presets ship no
// default model, so a fresh one is genuinely empty until the key is pasted, and the
// derived list would drop the row between the click and the first keystroke.
let pendingConnections = new Set();

export function initConfigPanel(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "settings", () => openSettings());
  // Readiness answers about the style that will render, so picking a different one
  // asks the question again: it may point at a connection with no key, or at a
  // workflow that was never imported.
  registerAction(WORKFLOW_ID, "pickStyle", async (el) => {
    await saveConfigPatch({ default_style: el.value }, "Could not save default style");
    refreshCardReadiness();
  });
  registerAction(WORKFLOW_ID, "pickPov", (el) => saveConfigPatch({ pov_mode: el.value }, "Could not save the camera"));
  registerAction(WORKFLOW_ID, "editStyle", (el) => openSettings(el.dataset.styleId));
  registerAction(WORKFLOW_ID, "settingsClose", () => closeModal());
  registerAction(WORKFLOW_ID, "save", () => saveSettings());
  registerAction(WORKFLOW_ID, "graphFile", (el) => importGraphFile(el));
  registerAction(WORKFLOW_ID, "referenceFile", (el) => pickReferenceImage(el));
  registerAction(WORKFLOW_ID, "referenceClear", () =>
    setReferenceImage({ reference_image_b64: "", reference_mime: "" }),
  );
  registerAction(WORKFLOW_ID, "graphAdd", () => addPendingGraph());
  registerAction(WORKFLOW_ID, "graphRemove", (el) => removeGraph(el.dataset.graphId));
  registerAction(WORKFLOW_ID, "styleAdd", () => addStyle());
  registerAction(WORKFLOW_ID, "styleRemove", (el) => removeStyle(Number(el.dataset.styleIndex)));
  registerAction(WORKFLOW_ID, "styleChange", (el) => refreshStyleState(el));
  registerAction(WORKFLOW_ID, "styleConnection", (el) => relinkStyle(el));
  registerAction(WORKFLOW_ID, "connAdd", () => addConnection());
  registerAction(WORKFLOW_ID, "connRemove", (el) => removeConnection(el.dataset.connId));
  registerAction(WORKFLOW_ID, "connChange", (el) => refreshConnectionState(el));
  registerAction(WORKFLOW_ID, "connTest", (el) => testConnection(el.dataset.connId));
  registerAction(WORKFLOW_ID, "connOpen", (el) => revealConnection(el.dataset.connId));
}

// Every conversation-less config/discovery call rides the one QUERY route. Not
// conversation-scoped, so the path is built by hand (convUrl is for /trigger).
// Handlers report failure in-band as `{error}`; a raise here is a route-level
// fault, which each caller degrades on.
function query(action, extra) {
  return api.post(`/workflows/${WORKFLOW_ID}/query`, { action, ...extra });
}

// Cached so the card renders synchronously from a known value instead of painting
// empty and filling in later. The classifier starts assumed-present, so the picker
// never claims it is off on the strength of a probe that has not run.
let cardReadiness = { text: "", ready: true };
let cardPov = { classifier: true, fallback: "third" };
let cardStyles = [];

// An <option> carries no markup, so the prompt format rides the text as "Krea-Alt
// (Prose)". Picking a style here is picking a format too, and that is not visible
// anywhere else on the card.
function cardStyleOptions() {
  return optionList(
    cardStyles.map((s) => [s.id, `${s.label || s.id} ${promptFormatBadge(s.prompt_format)}`]),
    cfg?.default_style || "",
  );
}

// The next useful action, and nothing else: an incomplete setup gets one status
// and one button, normal use gets the pickers. Diagnostics belong in the full form.
function configPanelBody() {
  if (!cardReadiness.ready) {
    return `<div class="image-gen-card-setup">
      <span class="image-gen-card-status" title="${escAttr(cardReadiness.text)}">Setup required</span>
      <button class="btn btn-sm btn-accent image-gen-card-btn" data-wf-action="image_gen:settings">Finish setup</button>
    </div>`;
  }

  const stylePicker = cardStyles.length
    ? `<label for="ig-card-style">Style</label><select id="ig-card-style" class="tool-card-select" data-wf-action="image_gen:pickStyle" data-wf-on="change">${cardStyleOptions()}</select>`
    : "";
  return `<div class="image-gen-card-controls">${stylePicker}${povPicker()}</div>
    <button class="btn btn-sm tool-card-btn" data-wf-action="image_gen:settings">Settings</button>`;
}

// Which modes exist and which shows is a pure decision (see povChoices); the
// fallback is the backend's, not a second copy of the default here.
function cardPovOptions() {
  const { modes, selected } = povChoices({ ...cardPov, mode: cfg?.pov_mode || "auto" });
  return optionList(modes, selected);
}

function povPicker() {
  return `<label for="ig-card-pov">POV</label><select id="ig-card-pov" class="tool-card-select" data-wf-action="image_gen:pickPov" data-wf-on="change">${cardPovOptions()}</select>`;
}

function refreshCard() {
  const el = document.getElementById("ig-card-config");
  if (el) el.innerHTML = configPanelBody();
}

export function configPanelRenderer() {
  return `<div class="tool-card-desc">Generate images on demand with ComfyUI or a cloud API.</div>
    <div id="ig-card-config">${configPanelBody()}</div>`;
}

// The card pickers are the only place the default style and the camera are chosen,
// so their choices persist — otherwise every reload reopens on the shipped
// defaults. The full config round-trips like a Save.
async function saveConfigPatch(patch, failure) {
  Object.assign(cfg, patch);
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: { ...cfg, ...patch } });
    if (res?.config) Object.assign(cfg, res.config);
  } catch {
    toast(failure, "error");
  }
}

// Fetched once at load and after a save, then patched in place so an open tools
// panel need not be re-rendered.
export async function refreshCardStyles() {
  try {
    const res = await query("styles");
    cardStyles = Array.isArray(res?.styles) ? res.styles : [];
  } catch {
    cardStyles = [];
  }
  refreshCard();
}

// A configuration question, not a network one: `status` answers from the saved
// config alone, so the tools panel never waits on a remote server. Reachability is
// the connection probe's job, which runs at the moment it matters.
export async function refreshCardReadiness() {
  try {
    const status = await query("status");
    cardReadiness = {
      ready: !!status?.ready,
      text: status?.ready
        ? `Ready — ${status.style_count} style${status.style_count === 1 ? "" : "s"}`
        : status?.detail || "Not configured",
    };
    // Riding along: what the camera picker needs to label "Auto" honestly, and what
    // the settings form needs to build its pickers.
    cardPov = { classifier: !!status?.classifier_ready, fallback: status?.fallback_mode || cardPov.fallback };
    backends = {
      sources: Array.isArray(status?.sources) ? status.sources : backends.sources,
      providers: Array.isArray(status?.providers) ? status.providers : backends.providers,
    };
  } catch {
    cardReadiness = { ready: false, text: "" };
  }
  refreshCard();
}

// ── styles ───────────────────────────────────────────────────────────────────

// The model control both pickers use: a probed list becomes a select, an empty or
// failed probe stays a text input so an id can still be typed for a server Orb
// cannot list. A configured value that has since disappeared keeps its place
// rather than being silently swapped for whatever sorts first.
function modelField(state, { attrs, emptyLabel, placeholder }) {
  if (state.kind === "input") {
    return `<input ${attrs} value="${escAttr(state.current)}" placeholder="${escAttr(placeholder)}">`;
  }
  const options = [];
  if (!state.current) {
    options.push(`<option value="" selected>${esc(emptyLabel)}</option>`);
  } else if (!state.models.includes(state.current)) {
    options.push(`<option value="${escAttr(state.current)}" selected>${esc(state.current)} (not detected)</option>`);
  }
  return `<select ${attrs}>${options.join("")}${optionList(
    state.models.map((name) => [name, name]),
    state.current,
  )}</select>`;
}

// Always the ComfyUI connection's list, never the active one: a style can be
// linked to a cloud provider and still hold a checkpoint for when it is linked back.
function checkpointField(value) {
  return modelField(modelPickerState(modelsByConnection[COMFY_CONNECTION], value), {
    attrs: 'data-ig-field="checkpoint" data-wf-action="image_gen:styleChange" data-wf-on="change"',
    emptyLabel: "Choose a checkpoint",
    placeholder: "checkpoint.safetensors",
  });
}

const promptFormatOptions = (value) => optionList(PROMPT_FORMATS, normalizePromptFormat(value));

// Where a style renders is a property *of the style*, so a local anime checkpoint
// and a photoreal commercial API are one dropdown apart rather than a global mode
// switch that makes half of every other style inapplicable.
function styleConnectionOptions(selected) {
  const pairs = connections.map((c) => [c.id, c.label]);
  // A style pinned to a since-removed connection keeps naming it rather than
  // adopting whichever sorts first, as the checkpoint and workflow fields do.
  if (selected && !connections.some((c) => c.id === selected)) pairs.unshift([selected, `${selected} (removed)`]);
  if (!selected) pairs.unshift(["", "Choose a connection"]);
  return optionList(pairs, selected);
}

// The half of a style that depends on which backend renders it. Swapped per
// connection rather than always present: a pair of dead fields with a note saying
// they are dead reads as a bug.
function backendFields(style, connection) {
  if (connection && connection.source === "cloud") {
    const [width, height] = entrySize(draft.connections[connection.id]);
    const size = `${width}×${height}`;
    return `<div class="image-gen-note ig-style-backend">Model and resolution come from this connection — ${esc(connection.detail || "no model yet")}, ${esc(size)}.
      <button type="button" class="ig-link" data-wf-action="image_gen:connOpen" data-conn-id="${escAttr(connection.id)}">Edit connection</button></div>`;
  }
  return `<div class="ig-grid">
      <label>Checkpoint${checkpointField(style.checkpoint || "")}</label>
      <label>Workflow${workflowField(style.workflow || "")}</label>
    </div>`;
}

// Stated where the text is typed, not on the render that discards it: a provider
// with no negative field returns a perfectly good image that ignored it, so
// nothing downstream can report this.
function negativeNote(connection) {
  if (connection?.source !== "cloud") return "";
  if (connection.preset?.supports_negative_prompt !== false) return "";
  return `<div class="image-gen-note">${esc(connection.label)} has no negative prompt field — this text is not sent.</div>`;
}

// One `<details>` per style, its summary carrying name, connection and prompt
// format: all three change what the image looks like, and all three are otherwise
// invisible until the row is opened.
function styleBody(style, index, connection) {
  return `<label>Name<input data-ig-field="label" data-wf-action="image_gen:styleChange" data-wf-on="change" value="${escAttr(style.label || "")}"></label>
      <div class="ig-grid">
        <label>Connection<select data-ig-field="connection" data-wf-action="image_gen:styleConnection" data-wf-on="change">${styleConnectionOptions(styleConnectionId(style, cfg))}</select></label>
        <label>Prompt format<select data-ig-field="prompt_format" data-wf-action="image_gen:styleChange" data-wf-on="change">${promptFormatOptions(style.prompt_format)}</select></label>
      </div>
      <label>Positive style prompt<textarea data-ig-field="prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="No positive style prompt">${esc(style.prompt || "")}</textarea></label>
      <label>Negative style prompt<textarea data-ig-field="negative_prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="No negative style prompt">${esc(style.negative_prompt || "")}</textarea></label>
      ${negativeNote(connection)}
      <label>Extra instructions<textarea data-ig-field="extra_instructions" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="Extra guidance for the prompter model (e.g. emphasize hand placement and use full-body framing).">${esc(style.extra_instructions || "")}</textarea></label>
      ${backendFields(style, connection)}
      <button class="btn btn-sm ig-danger" data-wf-action="image_gen:styleRemove" data-style-index="${index}">Remove style</button>`;
}

function styleRows(expandIds = "") {
  const expanded = new Set(Array.isArray(expandIds) ? expandIds : [expandIds]);
  return draft.styles
    .map((s, i) => {
      const connection = findConnection(connections, styleConnectionId(s, cfg));
      return `<details class="ig-style" data-style-index="${i}"${expanded.has(s.id) ? " open" : ""}>
        <summary>
          <span class="ig-style-name">${esc(s.label || s.id)}</span>
          <span class="ig-style-conn${connection?.ready === false ? " ig-unready" : ""}">${esc(connection?.label || styleConnectionId(s, cfg) || "No connection")}</span>
          <span class="ig-style-format">${promptFormatBadge(s.prompt_format)}</span>
        </summary>
        <div class="ig-style-body">${styleBody(s, i, connection)}</div>
      </details>`;
    })
    .join("");
}

// Reads every style row's live values back into the draft, before anything that
// re-renders the list, so an in-progress edit survives an add or delete elsewhere.
function captureStyles() {
  draft.styles = draft.styles.map((s, i) => {
    const row = document.querySelector(`[data-style-index="${i}"]`);
    if (!row) return s;
    const get = (name) => row.querySelector(`[data-ig-field="${name}"]`)?.value ?? "";
    // Checkpoint and workflow fall back to the stored values, not "": they are not
    // rendered while the style is linked to cloud, and blanking them there would
    // lose the pin on relinking to ComfyUI.
    return {
      ...s,
      label: get("label").trim() || s.label || s.id,
      connection: row.querySelector('[data-ig-field="connection"]')?.value ?? s.connection ?? "",
      prompt_format: normalizePromptFormat(get("prompt_format") || s.prompt_format),
      prompt: get("prompt"),
      negative_prompt: get("negative_prompt"),
      extra_instructions: get("extra_instructions"),
      checkpoint: row.querySelector('[data-ig-field="checkpoint"]')?.value ?? s.checkpoint ?? "",
      workflow: row.querySelector('[data-ig-field="workflow"]')?.value ?? s.workflow ?? "",
    };
  });
}

function renderStyles(expandId = "") {
  const host = document.querySelector(".ig-styles");
  if (host) host.innerHTML = styleRows(expandId);
}

function addStyle() {
  captureStyles();
  const id = `style_${Date.now().toString(36)}`;
  // Adding a style is almost always "the same backend, written differently", so it
  // inherits where the style above it renders, pins included — inheriting the
  // connection alone would ship a style that cannot render until two more fields
  // are filled. The prompts are what the user came to write, so those start empty.
  const previous = draft.styles.at(-1) || {};
  draft.styles.push({
    id,
    label: "New style",
    connection: previous.connection || connections[0]?.id || COMFY_CONNECTION,
    prompt_format: DEFAULT_PROMPT_FORMAT,
    prompt: "",
    negative_prompt: "",
    extra_instructions: "",
    checkpoint: previous.checkpoint || "",
    workflow: previous.workflow || "",
  });
  renderStyles(id);
}

function removeStyle(index) {
  captureStyles();
  // An empty list is repopulated from the shipped defaults on the next load, which
  // reads as an edit undoing itself.
  if (draft.styles.length <= 1) {
    toast("Keep at least one style", "error");
    return;
  }
  draft.styles.splice(index, 1);
  renderStyles();
}

// Keeps the collapsed summary in sync as the fields are edited, so a row collapsed
// straight after a change does not read as the old format. A blank name keeps the
// last one, matching captureStyles; the format is a select and cannot be blanked.
function refreshStyleState(el) {
  const row = el.closest("[data-style-index]");
  const field = (name) => row?.querySelector(`[data-ig-field="${name}"]`)?.value;
  const name = field("label")?.trim();
  const nameEl = row?.querySelector(".ig-style-name");
  const formatEl = row?.querySelector(".ig-style-format");
  if (nameEl && name) nameEl.textContent = name;
  if (formatEl) formatEl.textContent = promptFormatBadge(field("prompt_format"));
}

// Relinking swaps the backend-dependent half of the body, so the row is rebuilt --
// only this row, since rebuilding the list would collapse every other open style.
function relinkStyle(el) {
  const row = el.closest("[data-style-index]");
  const index = Number(row?.dataset.styleIndex);
  captureStyles();
  const style = draft.styles[index];
  const body = row?.querySelector(".ig-style-body");
  if (!style || !body) return;
  const connection = findConnection(connections, style.connection);
  body.innerHTML = styleBody(style, index, connection);
  const label = row.querySelector(".ig-style-conn");
  if (label) {
    label.textContent = connection?.label || style.connection || "No connection";
    label.classList.toggle("ig-unready", connection?.ready === false);
  }
  // A ComfyUI-linked style needs the checkpoint menu, probed only on demand.
  if (connection?.source !== "cloud") loadModels(COMFY_CONNECTION);
}

// With nothing imported there is nothing to pick, so an empty select would be a
// dead menu -- say what to do instead. Once graphs exist it becomes a chooser.
function workflowField(selected) {
  if (!draft.graphs.length) {
    return `<span class="image-gen-note ig-workflow-empty">No workflows detected. Import one in <strong>Imported ComfyUI workflows</strong> below.</span>`;
  }
  return `<select data-ig-field="workflow" data-wf-action="image_gen:styleChange" data-wf-on="change">${workflowOptions(selected)}</select>`;
}

function workflowOptions(selected) {
  const known = draft.graphs.some((graph) => graph.id === selected);
  const pairs = draft.graphs.map((graph) => [graph.id, graph.label || graph.id]);
  // A pinned-but-missing workflow stays visible rather than swapping the style onto
  // whatever sorts first, mirroring the checkpoint field.
  if (selected && !known) pairs.unshift([selected, `${selected} (not found)`]);
  const placeholder = `<option value="" disabled${selected && known ? "" : " selected"}>Choose a workflow</option>`;
  return placeholder + optionList(pairs, selected);
}

function graphRows() {
  if (!draft.graphs.length) return `<div class="image-gen-note">No imported workflows.</div>`;
  return draft.graphs
    .map(
      (g) => `<div class="ig-graph-row">
        <span class="ig-graph-name">${esc(g.label || g.id)}</span>
        <button class="btn btn-sm ig-danger" data-wf-action="image_gen:graphRemove" data-graph-id="${escAttr(g.id)}">Remove</button>
      </div>`,
    )
    .join("");
}

function removeGraph(graphId) {
  captureStyles();
  draft.graphs = draft.graphs.filter((g) => g.id !== graphId);
  // Pins naming it are cleared rather than left to fail at generation: the user
  // just deleted it, so falling back is what they meant.
  draft.styles = draft.styles.map((s) => (s.workflow === graphId ? { ...s, workflow: "" } : s));
  const host = document.getElementById("ig-graph-list");
  if (host) host.innerHTML = graphRows();
  renderStyles();
}

// ── the Connections section ──────────────────────────────────────────────────
//
// A connection is one place an image can be rendered. ComfyUI is always first and
// never removable: a config with no connection would leave every style pointing at
// nothing. The list is derived from the credentials (`connectionList` in policy.js)
// rather than stored beside them, so "this connection exists" and "this connection
// has a key" can never become two facts that disagree.

function providerFor(id) {
  return backends.providers.find((p) => p.id === id) || null;
}

// Recomputed after anything that adds, removes or re-credentials a connection.
// `cfg` supplies only the pre-linking fallback, which the form never edits.
function rebuildConnections() {
  connections = connectionList(
    {
      source: cfg.source,
      styles: draft.styles,
      external_comfy: draft.comfy,
      cloud: { ...(cfg.cloud || {}), providers: draft.connections },
    },
    backends.providers,
    pendingConnections,
  );
}

const connField = (name) => `data-ig-conn-field="${name}" data-wf-action="image_gen:connChange" data-wf-on="change"`;

// Which rows are open, captured before an innerHTML swap discards the answer.
function openConnectionIds() {
  return Array.from(document.querySelectorAll("details.ig-conn[open]")).map((el) => el.dataset.connId);
}

function openStyleIds() {
  return Array.from(document.querySelectorAll(".ig-style[open]"))
    .map((row) => draft.styles[Number(row.dataset.styleIndex)]?.id)
    .filter(Boolean);
}

// The permanent gaps, stated once under the connection rather than on every render:
// a note that fires 100% of the time is one users learn to skip, which then hides
// the per-render disclosures that actually vary.
function capabilityLine(preset) {
  if (!preset) return "";
  const gaps = Array.isArray(preset.gaps) ? preset.gaps : [];
  if (!gaps.length) return "";
  const unverified = preset.verified
    ? ""
    : " This provider's settings are declared from its documentation and have not been verified against the live API.";
  return `<div class="image-gen-note ig-capability">${esc(`${preset.label} ${gaps.join(". ")}.`)}${esc(unverified)}</div>`;
}

function comfyFields() {
  const comfy = draft.comfy;
  return `<div class="ig-grid">
      <label>Server URL<input ${connField("api_url")} value="${escAttr(comfy.api_url || "http://127.0.0.1:8188")}"></label>
      <label>API key<input type="password" ${connField("api_key")} value="${escAttr(comfy.api_key || "")}"></label>
    </div>
    <div class="image-gen-note">Orb's local backend. It cannot be removed — a style whose cloud connection is deleted falls back to it.</div>`;
}

function cloudModelField(id) {
  const preset = providerFor(id);
  return modelField(modelPickerState(modelsByConnection[id], draft.connections[id]?.model || ""), {
    attrs: connField("model"),
    emptyLabel: preset?.default_model ? `Default — ${preset.default_model}` : "Choose a model",
    placeholder: preset?.default_model || "model id",
  });
}

// Resolution, quality and references belong to the connection, not to "cloud":
// they are provider-shaped facts, and two connections can want two answers. Only
// the fields the preset declares are rendered.
function cloudFields(connection) {
  const id = connection.id;
  const entry = draft.connections[id] || {};
  const preset = connection.preset;
  const baseUrl =
    !preset || preset.needs_base_url
      ? `<label>API base URL<input ${connField("base_url")} value="${escAttr(entry.base_url || "")}" placeholder="https://api.example.com/v1"></label>`
      : "";
  const sizes = optionList(
    CLOUD_SIZES.map(([w, h, label]) => [`${w}x${h}`, label]),
    entrySize(entry).join("x"),
  );
  const quality = preset?.supports_quality
    ? `<label>Quality<select ${connField("quality")}>${optionList(CLOUD_QUALITIES, entry.quality || "")}</select></label>`
    : "";
  const references =
    !preset || preset.supports_references
      ? `<label>Reference images<select ${connField("reference_source")}>${optionList(
          [["", "Off — send prompts only"], ...REFERENCE_SOURCES],
          entry.reference_source || "",
        )}</select></label>`
      : "";
  const unknown = preset
    ? ""
    : `<div class="image-gen-note ig-unready">Orb has no preset for "${esc(id)}". Its credentials are kept, but nothing can render on it — this is usually a provider that was renamed in a later release.</div>`;
  const docs = preset?.docs_url
    ? `<div class="image-gen-note"><a href="${escAttr(preset.docs_url)}" target="_blank" rel="noopener noreferrer">${esc(preset.label)} API documentation</a></div>`
    : "";
  return `${unknown}<div class="ig-grid">
      <label>API key<input type="password" ${connField("api_key")} value="${escAttr(entry.api_key || "")}" placeholder="Paste your key"></label>
      ${baseUrl}
      <label class="ig-conn-model">Model${cloudModelField(id)}</label>
      <label>Resolution<select ${connField("size")}>${sizes}</select></label>
      ${quality}
      ${references}
    </div>
    <div class="image-gen-note">Aspect ratio is chosen automatically from the resolution.</div>
    ${capabilityLine(preset)}${docs}`;
}

function connectionBody(connection) {
  const id = connection.id;
  const fields = id === COMFY_CONNECTION ? comfyFields() : cloudFields(connection);
  const remove = connection.removable
    ? `<button class="btn btn-sm ig-danger ig-conn-remove" data-wf-action="image_gen:connRemove" data-conn-id="${escAttr(id)}">Remove</button>`
    : "";
  return `${fields}
    <div class="image-gen-row">
      <button class="btn btn-sm" data-wf-action="image_gen:connTest" data-conn-id="${escAttr(id)}">Test connection</button>
      <span class="image-gen-note ig-conn-test" data-ig-test="${escAttr(id)}"></span>
      ${remove}
    </div>`;
}

function connectionRows(expandIds = []) {
  const expanded = new Set(expandIds);
  return connections
    .map(
      (c) => `<details class="ig-conn" data-conn-id="${escAttr(c.id)}"${expanded.has(c.id) ? " open" : ""}>
        <summary>
          <span class="ig-conn-name">${esc(c.label)}</span>
          <span class="ig-conn-kind">${esc(c.kind)}</span>
          <span class="ig-conn-detail${c.ready ? "" : " ig-unready"}">${esc(c.detail || "Not configured")}</span>
        </summary>
        <div class="ig-conn-body">${connectionBody(c)}</div>
      </details>`,
    )
    .join("");
}

// Each provider once: the credential map is keyed by provider id, so a second
// connection to one provider would need a synthetic id. Running out of options
// beats offering a duplicate that silently overwrites.
function addRowHtml() {
  const options = addableProviders(connections, backends.providers);
  if (!options.length) return `<span class="image-gen-note">Every provider Orb knows already has a connection.</span>`;
  return `<select id="ig-conn-add">${optionList(options.map((p) => [p.id, p.label]))}</select>
    <button class="btn btn-sm" data-wf-action="image_gen:connAdd">Add connection</button>`;
}

// Which rows a freshly opened modal expands: nothing on a working setup, but
// "Finish setup" lands here, and hunting for the row that is blocking them is the
// failure that button exists to avoid.
function setupTargets() {
  if (cardReadiness.ready) return [];
  return [connections.find((c) => !c.ready)?.id || COMFY_CONNECTION];
}

function connectionSummaryText() {
  const unready = connections.filter((c) => !c.ready).length;
  const names = connections.map((c) => c.label).join(", ");
  return unready ? `${names} — ${unready} needs setup` : names;
}

function refreshConnectionSummary() {
  const el = document.getElementById("ig-conn-summary");
  if (el) el.textContent = connectionSummaryText();
}

function renderConnections(alsoOpen = "") {
  const open = new Set(openConnectionIds());
  if (alsoOpen) open.add(alsoOpen);
  const host = document.getElementById("ig-conn-list");
  if (host) host.innerHTML = connectionRows([...open]);
  const add = document.getElementById("ig-conn-add-row");
  if (add) add.innerHTML = addRowHtml();
  refreshConnectionSummary();
}

// Reads every connection body's live values back into the working copy, before
// anything that re-renders. A collapsed row's fields are still in the DOM —
// `<details>` hides its body, it does not drop it — so nothing is lost.
function captureConnections() {
  for (const row of document.querySelectorAll("details.ig-conn")) {
    const id = row.dataset.connId;
    const get = (name) => row.querySelector(`[data-ig-conn-field="${name}"]`)?.value;
    if (id === COMFY_CONNECTION) {
      draft.comfy = {
        ...draft.comfy,
        api_url: get("api_url") ?? draft.comfy.api_url ?? "",
        api_key: get("api_key") ?? draft.comfy.api_key ?? "",
      };
      continue;
    }
    const entry = draft.connections[id] || {};
    const [width, height] = String(get("size") ?? entrySize(entry).join("x")).split("x");
    // Every field falls back to the stored value, not "": a preset declaring no
    // quality or no references renders no such control, and reading a missing
    // element as empty would blank a setting the user never saw.
    draft.connections[id] = {
      ...entry,
      api_key: get("api_key") ?? entry.api_key ?? "",
      model: get("model") ?? entry.model ?? "",
      base_url: get("base_url") ?? entry.base_url ?? "",
      width: Number(width) || DEFAULT_EDGE,
      height: Number(height) || DEFAULT_EDGE,
      quality: get("quality") ?? entry.quality ?? "",
      reference_source: get("reference_source") ?? entry.reference_source ?? "",
    };
  }
}

function addConnection() {
  const id = document.getElementById("ig-conn-add")?.value;
  if (!id) return;
  captureStyles();
  captureConnections();
  const preset = providerFor(id);
  const existing = draft.connections[id] || {};
  draft.connections[id] = {
    width: DEFAULT_EDGE,
    height: DEFAULT_EDGE,
    quality: "",
    reference_source: "",
    ...existing,
    api_key: existing.api_key || "",
    // Seeding the preset's model leaves a fresh connection one field from ready
    // instead of two, and it is the model the provider's own docs open with.
    model: existing.model || preset?.default_model || "",
    base_url: existing.base_url || "",
  };
  pendingConnections.add(id);
  rebuildConnections();
  renderConnections(id);
  renderStyles(openStyleIds());
  // No probe: a brand-new connection has no key, so listing models is guaranteed to
  // 401. `refreshConnectionState` fires one the moment a key is pasted.
}

function removeConnection(id) {
  captureStyles();
  captureConnections();
  delete draft.connections[id];
  delete modelsByConnection[id];
  pendingConnections.delete(id);
  // Styles pinned to it fall back to ComfyUI rather than keeping a dangling id --
  // "renders nowhere" is not a state the render path has an answer for. Said out
  // loud, because it silently changes what those styles produce.
  const orphaned = draft.styles.filter((s) => s.connection === id).length;
  draft.styles = draft.styles.map((s) => (s.connection === id ? { ...s, connection: COMFY_CONNECTION } : s));
  rebuildConnections();
  renderConnections();
  renderStyles(openStyleIds());
  if (orphaned) toast(`${orphaned} style${orphaned > 1 ? "s" : ""} moved to ComfyUI`);
}

// Keeps the collapsed summary honest as fields are edited, and re-renders the
// styles so a linked style's "comes from this connection" line never names last
// minute's model.
function refreshConnectionState(el) {
  const row = el.closest("details.ig-conn");
  const id = row?.dataset.connId;
  if (!id) return;
  captureStyles();
  captureConnections();
  rebuildConnections();
  const connection = findConnection(connections, id);
  const detail = row.querySelector(".ig-conn-detail");
  if (detail && connection) {
    detail.textContent = connection.detail || "Not configured";
    detail.classList.toggle("ig-unready", !connection.ready);
  }
  refreshConnectionSummary();
  renderStyles(openStyleIds());
  // The model list sits behind credentials: pasting a key is the first moment the
  // menu can be filled, and changing a URL is when the old one stopped being true.
  if (["api_key", "base_url", "api_url"].includes(el.dataset.igConnField)) loadModels(id);
}

// Opens the Connections section on one row — the "Edit connection" link on a
// cloud-linked style, which is otherwise a scroll and a guess away.
function revealConnection(id) {
  const section = document.getElementById("ig-connections");
  if (section) section.open = true;
  const row = document.querySelector(`details.ig-conn[data-conn-id="${CSS.escape(id)}"]`);
  if (!row) return;
  row.open = true;
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  loadModels(id);
}

// The config as it would be if the style rendering next were on `id`. The backend
// routes on the *default style's* connection, so overriding that style is how a
// probe asks about a connection nothing points at, with no special case on the
// query route.
function configForConnection(id) {
  const next = readConfig();
  const styles = next.styles.map((s) => ({ ...s }));
  const active = styles.find((s) => s.id === next.default_style) || styles[0];
  if (active) active.connection = id;
  return { ...next, styles };
}

// Swap the model controls this probe answers for; live values and open accordions
// survive the asynchronous re-render.
function applyModels(id, names) {
  const openStyles = openStyleIds();
  captureStyles();
  captureConnections();
  modelsByConnection[id] = modelPickerState(names).models;
  rebuildConnections();
  if (id === COMFY_CONNECTION) {
    // The ComfyUI list feeds every style's checkpoint field.
    renderStyles(openStyles);
    return;
  }
  const host = document.querySelector(`details.ig-conn[data-conn-id="${CSS.escape(id)}"] .ig-conn-model`);
  if (host) host.innerHTML = `Model${cloudModelField(id)}`;
}

// Probes one connection after the modal is already open; a slow or unreachable
// server must not delay the form. Failure leaves a plain text field.
async function loadModels(id) {
  probeIds[id] = (probeIds[id] || 0) + 1;
  const probeId = probeIds[id];
  let models = [];
  try {
    const res = await query("models", { config: configForConnection(id) });
    models = Array.isArray(res?.models) ? res.models : [];
  } catch {
    models = [];
  }
  if (probeIds[id] === probeId) applyModels(id, models);
}

async function testConnection(id) {
  const result = document.querySelector(`[data-ig-test="${CSS.escape(id)}"]`);
  if (result) result.textContent = "Testing...";
  probeIds[id] = (probeIds[id] || 0) + 1;
  const probeId = probeIds[id];
  try {
    const res = await query("test", { config: configForConnection(id) });
    if (probeIds[id] !== probeId) return;
    // The probe names the unmet prerequisite in `error`, which is more use than
    // "failed"; a route-level fault falls through to the catch instead.
    if (res?.error) {
      applyModels(id, []);
      if (result) result.textContent = res.error;
      return;
    }
    // Tested against the form's unsaved credentials, so this is the only probe that
    // can fill the list for a connection not yet saved.
    applyModels(id, res?.models);
    // ComfyUI names a GPU, a cloud provider names itself, and neither is
    // guaranteed — a bare "Connected" is still a true answer.
    const who = res?.system?.devices?.[0]?.name || res?.system?.provider;
    if (result) result.textContent = who ? `Connected — ${who}` : "Connected";
  } catch {
    if (probeIds[id] !== probeId) return;
    applyModels(id, []);
    if (result) result.textContent = "Connection failed";
  }
}

function openSettings(expandStyleId = "") {
  const ext = cfg.external_comfy || {};
  const cloud = cfg.cloud || {};
  pendingGraph = null;
  // Start honest: a previous modal's discovery must not make this one look probed
  // before its own request completes, and a reference image picked for a different
  // character must not survive into this form.
  probeIds = {};
  modelsByConnection = {};
  pendingConnections = new Set();
  referenceImage = { reference_image_b64: "", reference_mime: "" };
  draft = {
    styles: (Array.isArray(cfg.styles) ? cfg.styles : []).map((s) => ({ ...s })),
    graphs: (Array.isArray(ext.user_graphs) ? ext.user_graphs : []).map((g) => ({ ...g })),
    comfy: { api_url: ext.api_url || "", api_key: ext.api_key || "" },
    connections: Object.fromEntries(Object.entries(cloud.providers || {}).map(([id, entry]) => [id, { ...entry }])),
  };
  rebuildConnections();
  // Ordered by how often each is touched. Styles first, since the sidebar card
  // picks between them; Connections under them because that is what a style links
  // to, collapsed because a working setup is configured once and left alone.
  showModal(`<h2>Image Generation</h2><div class="image-gen-settings">
    <section class="ig-section">
      <div class="ig-heading">Styles</div>
      <div class="ig-styles">${styleRows(expandStyleId)}</div>
      <button class="btn btn-sm" data-wf-action="image_gen:styleAdd">Add style</button>
    </section>
    <details class="ig-advanced" id="ig-connections"${cardReadiness.ready ? "" : " open"}>
      <summary>Connections<span class="ig-summary-note" id="ig-conn-summary">${esc(connectionSummaryText())}</span></summary>
      <div class="ig-advanced-body">
        <div class="image-gen-note">Where images render. Every style links to one, so a local checkpoint and a commercial API can sit side by side. ComfyUI is always available and cannot be removed.</div>
        <div id="ig-conn-list" class="ig-conn-list">${connectionRows(setupTargets())}</div>
        <div id="ig-conn-add-row" class="image-gen-row">${addRowHtml()}</div>
      </div>
    </details>
    <section class="ig-section">
      <div class="ig-heading">This Character Only</div>
      <div id="ig-profile" class="image-gen-note">Open a conversation to edit its character-specific prompt.</div>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Generation</div>
      <div class="ig-grid">
        <label>Render timeout (seconds)<input id="ig-timeout" type="number" min="10" max="900" value="${escAttr(cfg.timeout_seconds || 180)}"></label>
      </div>
      <label class="ig-toggle"><input id="ig-scene-analysis" type="checkbox"${cfg.scene_analysis === true ? " checked" : ""}><span class="ig-toggle-body"><span class="ig-toggle-label">Analyze complex scenes</span><span class="image-gen-note">More accurate outfits and positions for scenes; one extra model call.</span></span></label>
      <label class="ig-toggle"><input id="ig-prompter-reasoning" type="checkbox"${cfg.prompter_reasoning === true ? " checked" : ""}><span class="ig-toggle-body"><span class="ig-toggle-label">Enable prompter thinking</span><span class="image-gen-note">Uses thinking for scene analysis and prompt composition. For best prompt-cache reuse, match Editor reasoning config.</span></span></label>
    </section>
    <details class="ig-advanced">
      <summary>Imported ComfyUI workflows<span class="ig-summary-note">${draft.graphs.length || "none"}</span></summary>
      <div class="ig-advanced-body">
        <div class="image-gen-note">Use a PNG generated by ComfyUI or a dev-mode Export (API) JSON file. Imported workflows run only on the ComfyUI connection, and are kept whichever connection a style links to.</div>
        <div id="ig-graph-list" class="ig-graph-list">${graphRows()}</div>
        <input type="file" accept=".json,.png,application/json,image/png" data-wf-action="image_gen:graphFile" data-wf-on="change">
        <div id="ig-graph-picker"></div>
      </div>
    </details>
  </div><div class="modal-actions"><button class="btn" data-wf-action="image_gen:settingsClose">Close</button><button class="btn btn-accent" data-wf-action="image_gen:save">Save</button></div>`);
  populateProfile();
  // ComfyUI always, because every style's checkpoint field reads its list. Cloud
  // connections only once they hold a key: a probe per configured provider on every
  // open is a burst of requests for menus most of which are never looked at.
  loadModels(COMFY_CONNECTION);
  for (const connection of connections) {
    if (connection.source === "cloud" && draft.connections[connection.id]?.api_key) loadModels(connection.id);
  }
}

function readConfig() {
  captureStyles();
  captureConnections();
  const ext = cfg.external_comfy || {};
  return {
    // `source`, `cloud.provider`, `default_style` and `pov_mode` have no control in
    // this form: the first two are derived by the backend normalizer from the
    // default style's connection, the last two are chosen in the tools-panel card.
    // Passing the stored values through is what keeps a config whose styles predate
    // connection linking routing exactly where it always did.
    source: cfg.source || "external_comfy",
    default_style: cfg.default_style || draft.styles[0]?.id || "realistic",
    pov_mode: cfg.pov_mode || "auto",
    scene_analysis: document.getElementById("ig-scene-analysis")?.checked === true,
    prompter_reasoning: document.getElementById("ig-prompter-reasoning")?.checked === true,
    timeout_seconds: Number(document.getElementById("ig-timeout")?.value) || 180,
    styles: draft.styles,
    external_comfy: {
      ...ext,
      api_url: draft.comfy.api_url ?? ext.api_url ?? "",
      api_key: draft.comfy.api_key ?? ext.api_key ?? "",
      user_graphs: draft.graphs,
    },
    // Seeded from the *whole* stored map, not the rendered list, so an entry the
    // panel never shows — the inert shipped row, or an id retained across a provider
    // rename — survives the save with its key intact. It is therefore also the
    // authority on what exists, which is what makes Remove actually remove.
    cloud: { ...(cfg.cloud || {}), providers: { ...draft.connections } },
  };
}

// Slot candidates are chosen by *position*, not by value — two nodes can offer the
// same input name — so this cannot go through `optionList`.
function candidateOptions(items, selectedIndex = 0, noneLabel = "") {
  const none = noneLabel ? `<option value=""${selectedIndex < 0 ? " selected" : ""}>${esc(noneLabel)}</option>` : "";
  return (
    none +
    items
      .map(
        (item, i) =>
          `<option value="${escAttr(item.value)}"${i === selectedIndex ? " selected" : ""}>${esc(item.label)}</option>`,
      )
      .join("")
  );
}

// Slot roles are typed from the server's /object_info. Failure degrades rather
// than refuses: the picker falls back to conventional input names, so a graph can
// still be imported while the server is unreachable.
async function graphNodeTypes(graph) {
  try {
    const res = await query("node_types", { class_types: classTypes(graph), config: readConfig() });
    return res?.nodes || {};
  } catch {
    return {};
  }
}

// One row per detected image-upload widget, defaulting to "Not used": a plain
// text-to-image graph imports unchanged, and an edit graph is opt-in per node —
// Orb never guesses which LoadImage is the identity.
function referenceRows(items) {
  if (!items.length) return "";
  const options = optionList([["", "Not used"], ...REFERENCE_SOURCES], "");
  const rows = items
    .slice(0, MAX_REFERENCE_SLOTS)
    .map(
      (item, i) =>
        `<label>${esc(item.label)}<select data-ig-ref="${i}" data-ig-ref-slot="${escAttr(item.value)}">${options}</select></label>`,
    )
    .join("");
  return `<div class="ig-heading ig-reference-heading">Reference images</div>
    <div class="image-gen-note">This workflow loads images. Point each one at what Orb should feed it, or leave it unused to keep the file the workflow was exported with.</div>
    <div class="ig-grid">${rows}</div>`;
}

async function importGraphFile(input) {
  const file = input.files?.[0];
  const picker = document.getElementById("ig-graph-picker");
  if (!file || !picker) return;
  try {
    if (draft.graphs.length >= MAX_USER_GRAPHS)
      throw new Error(`Orb stores at most ${MAX_USER_GRAPHS} imported workflows. Remove one before importing another.`);
    const graph = file.name.toLowerCase().endsWith(".png")
      ? graphFromPng(await file.arrayBuffer())
      : graphFromApiJson(await file.text());
    const candidates = slotCandidates(graph, await graphNodeTypes(graph));
    const missing = missingRoles(candidates);
    if (missing.length) throw new Error(`This workflow has no ${missing.join(", no ")}.`);
    pendingGraph = { graph, label: file.name.replace(/\.(json|png)$/i, ""), candidates };
    const negative = candidates.text.length > 1 ? 1 : -1;
    // Default to overriding the model when the graph has a loader: an imported PNG
    // pins a filename from another machine, so Orb's checkpoint should win. "None"
    // keeps the graph's own model for the self-contained case.
    const model = candidates.checkpoint.length ? 0 : -1;
    picker.innerHTML = `<div class="image-gen-graph-picker">
      <label>Name<input id="ig-graph-label" value="${escAttr(pendingGraph.label)}"></label>
      <div class="ig-grid">
        <label>Positive prompt<select id="ig-slot-positive">${candidateOptions(candidates.text, 0)}</select></label>
        <label>Negative prompt<select id="ig-slot-negative">${candidateOptions(candidates.text, negative, "None — this workflow has no negative prompt")}</select></label>
        <label>Seed<select id="ig-slot-seed">${candidateOptions(candidates.seed)}</select></label>
        <label>Image output<select id="ig-slot-output">${candidateOptions(candidates.output)}</select></label>
        <label>Model<select id="ig-slot-model">${candidateOptions(candidates.checkpoint, model, "None — keep the workflow's own model")}</select></label>
      </div>
      ${referenceRows(candidates.image)}
      <button class="btn btn-sm" data-wf-action="image_gen:graphAdd">Confirm slots and add workflow</button>
    </div>`;
  } catch (e) {
    pendingGraph = null;
    picker.innerHTML = `<div class="image-gen-note">${esc(e.message || "Could not import this workflow.")}</div>`;
  }
}

// Reads the rows back into the stored `slots.references` shape. An unmapped row is
// simply absent — how "Not used" is encoded, and what keeps a t2i graph's slot map
// byte-identical.
function readReferenceRows() {
  const references = [];
  for (const el of document.querySelectorAll("[data-ig-ref]")) {
    const slot = splitCandidate(el.dataset.igRefSlot);
    if (!el.value || !slot) continue;
    const item = pendingGraph?.candidates?.image?.[Number(el.dataset.igRef)];
    references.push({ slot, source: el.value, label: item?.label || `${slot[0]} — ${slot[1]}` });
  }
  return references;
}

function addPendingGraph() {
  if (!pendingGraph) return;
  const positive = splitCandidate(document.getElementById("ig-slot-positive")?.value);
  const negative = splitCandidate(document.getElementById("ig-slot-negative")?.value);
  const seed = splitCandidate(document.getElementById("ig-slot-seed")?.value);
  const output = splitCandidate(document.getElementById("ig-slot-output")?.value);
  const model = splitCandidate(document.getElementById("ig-slot-model")?.value);
  if (!positive || !seed || !output) return;
  captureStyles();
  const id = `user_${Date.now().toString(36)}`;
  const label = document.getElementById("ig-graph-label")?.value.trim() || pendingGraph.label;
  const slots = { positive, seed, output };
  if (negative) slots.negative = negative;
  // Patched from the style's checkpoint at render time, so Orb's selection
  // overrides the model baked into the imported graph.
  if (model) slots.checkpoint = model;
  const references = readReferenceRows();
  if (references.length) slots.references = references;
  draft.graphs.push({ id, label, graph: pendingGraph.graph, slots });
  const list = document.getElementById("ig-graph-list");
  if (list) list.innerHTML = graphRows();
  // Per-style pins render from the same list and the modal is not re-rendered on
  // import, so they need the new entry too.
  renderStyles();
  const picker = document.getElementById("ig-graph-picker");
  if (picker)
    picker.innerHTML = `<div class="image-gen-note">Added ${esc(label)}. Test the connection, then save settings.</div>`;
  pendingGraph = null;
}

async function saveSettings() {
  const next = readConfig();
  if (!confirmRemotePrivacy(next)) return;
  try {
    // The response is the *normalized* config: the backend bounds and drops what it
    // will not honour, and adopting its answer stops the panel listing settings the
    // render path ignores.
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: next });
    const stored = res?.config || next;
    const droppedGraphs = next.external_comfy.user_graphs.length - (stored.external_comfy?.user_graphs?.length || 0);
    Object.assign(cfg, stored);
    await saveProfile();
    // Deliberately unattributed: the picker already gates the two causes the user
    // can act on, and a bare count cannot tell the rest apart.
    toast(
      droppedGraphs > 0
        ? `Saved, but ${droppedGraphs} imported workflow${droppedGraphs > 1 ? "s" : ""} could not be stored`
        : "Image generation settings saved",
      droppedGraphs > 0 ? "error" : undefined,
    );
    closeModal();
    refreshCardReadiness();
    refreshCardStyles();
  } catch {
    toast("Could not save image generation settings", "error");
  }
}

// The localStorage + window.confirm shell. *Which* disclosure fires, under which
// key, is `privacyDisclosure` in policy.js, where `node --test` can reach it. One
// question per connection a style can reach, because a save can light up a second
// remote backend without it ever being the "active" one.
function confirmRemotePrivacy(next) {
  // Derived from `next`, not module state: this is the last gate before credentials
  // are sent, and it must describe the config being saved even if a field changed
  // after the last re-render.
  for (const disclosure of pendingDisclosures(next, connectionList(next, backends.providers))) {
    if (localStorage.getItem(disclosure.key) === "acknowledged") continue;
    if (!window.confirm(disclosure.message)) return false;
    localStorage.setItem(disclosure.key, "acknowledged");
  }
  return true;
}

// The character's reference image as the form holds it — loaded with the profile,
// replaced by the picker, emptied by Clear, written back on Save. Module state
// rather than read off the rendered <img>, so a save that never touched the picker
// round-trips the stored bytes untouched.
let referenceImage = { reference_image_b64: "", reference_mime: "" };

function referenceImageHtml() {
  const stored = !!referenceImage.reference_image_b64;
  return `<div class="ig-reference-image">
      ${stored ? `<div class="ig-reference-preview"><img class="ig-reference-thumb" alt="Character reference image" src="data:${escAttr(referenceImage.reference_mime || "image/png")};base64,${escAttr(referenceImage.reference_image_b64)}"></div>` : ""}
      <div class="ig-reference-controls">
        <input type="file" accept="image/png,image/jpeg,image/webp" data-wf-action="image_gen:referenceFile" data-wf-on="change">
        ${
          stored
            ? `<button class="btn btn-sm" data-wf-action="image_gen:referenceClear">Clear</button>`
            : `<span class="image-gen-note ig-reference-empty">No reference image — the character card's avatar is used.</span>`
        }
      </div>
    </div>`;
}

function setReferenceImage(next) {
  referenceImage = next;
  const host = document.getElementById("ig-reference-host");
  if (host) host.innerHTML = referenceImageHtml();
}

async function pickReferenceImage(input) {
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > MAX_REFERENCE_IMAGE_BYTES) {
    toast("That image is too large — use one under 10 MB", "error");
    input.value = "";
    return;
  }
  if (!REFERENCE_IMAGE_MIMES.includes((file.type || "").toLowerCase())) {
    toast("Orb accepts PNG, JPEG and WebP reference images", "error");
    input.value = "";
    return;
  }
  try {
    // Chunked: a single spread of the whole array blows String.fromCharCode's
    // argument limit on a multi-MB image.
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    setReferenceImage({ reference_image_b64: btoa(binary), reference_mime: file.type.toLowerCase() });
  } catch {
    toast("Could not read that image", "error");
  }
  input.value = "";
}

async function populateProfile() {
  const el = document.getElementById("ig-profile");
  if (!el || !getActiveConvId()) return;
  try {
    const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
      action: "get_profile",
    });
    if (!res?.profile) {
      el.textContent = "This conversation has no character.";
      return;
    }
    el.classList.remove("image-gen-note");
    referenceImage = {
      reference_image_b64: res.profile.reference_image_b64 || "",
      reference_mime: res.profile.reference_mime || "",
    };
    el.innerHTML = `<div class="ig-profile-fields">
        <label>Positive prompt<textarea id="ig-appearance" placeholder="Permanent tags, fill with permanent traits (e.g. Hatsune Miku, black and white)">${esc(res.profile.appearance_prompt || "")}</textarea></label>
        <label>Negative prompt<textarea id="ig-profile-negative" placeholder="Things to never render (e.g. 3D, colored, color). Quality and scene negatives are already handled.">${esc(res.profile.negative_prompt || "")}</textarea></label>
        <div class="ig-profile-reference">
          <span class="ig-profile-reference-label">Reference image</span>
          <span class="image-gen-note">Used by workflows with reference image slots.</span>
          <div id="ig-reference-host">${referenceImageHtml()}</div>
        </div>
      </div>`;
  } catch {
    el.textContent = "Could not load character appearance.";
  }
}

async function saveProfile() {
  // No fields rendered means no active character: sending blanks would wipe a
  // saved appearance.
  const appearanceEl = document.getElementById("ig-appearance");
  if (!appearanceEl || !getActiveConvId()) return;
  const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
    action: "set_profile",
    profile: {
      appearance_prompt: appearanceEl.value || "",
      negative_prompt: document.getElementById("ig-profile-negative")?.value || "",
      ...referenceImage,
    },
  });
  // A save that reports success while discarding what the form is still previewing
  // is the one outcome the user cannot diagnose, so the handler's warning is shown
  // and the local copy is brought back in line with what was stored.
  if (res?.warning) {
    toast(res.warning, "error");
    referenceImage = {
      reference_image_b64: res.profile?.reference_image_b64 || "",
      reference_mime: res.profile?.reference_mime || "",
    };
  }
}
