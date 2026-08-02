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
// Mirrored from the backend config normalizer. The count is enforced at the
// picker for the same reason the size cap is: the normalizer truncates the
// overflow, and a count that comes back short cannot say which graph went missing.
const MAX_REFERENCE_SLOTS = 4;
const MAX_USER_GRAPHS = 32;
const MAX_REFERENCE_IMAGE_BYTES = 10_000_000;
// Mirrors backend config.REFERENCE_MIMES. The `accept` attribute is a filter
// hint, not enforcement — every OS picker offers "All files" — and the backend
// normalizer drops anything outside this list rather than truncating it. Checking
// here is what turns that into a message instead of an image that previews fine
// and is silently gone on the next open.
const REFERENCE_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp"];

// Resolution presets for the cloud picker. Stored as pixels even for providers
// that speak aspect ratios — one canonical representation, converted at the wire
// by a pure backend function. The exact mapping is disclosed as a render note
// rather than previewed here; mirroring the aspect math into JS would be a drift
// risk for something cosmetic.
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

// How a style's prompt format reads next to its name, in both pickers. One
// builder, because the summary below is written twice -- once at render, once as
// the field is edited -- and two spellings of the same badge would drift.
const promptFormatBadge = (value) => `(${promptFormatLabel(value)})`;
let cfg;
let pendingGraph = null;
// The backend's own answer about which sources exist and what each provider can
// do. One payload behind the connection list, the Add picker and the capability
// lines, so the three can never disagree. Primed by the status query.
let backends = { sources: [], providers: [] };
// Everything the open form is editing. A working copy rather than cfg itself:
// adding a connection and then closing without saving must not leave the widget's
// shared config carrying credentials the server never stored.
let draft = { styles: [], graphs: [], comfy: {}, connections: {} };
// The derived connection list, recomputed from `draft` whenever it changes.
// Nothing stores it: a connection *is* its credentials, so a list held alongside
// them is a second copy that can disagree with the first.
let connections = [];
// Discovered model names, per connection id. Per connection rather than global
// because that is what they are: checkpoints belong to one ComfyUI server, model
// ids to one provider, and one shared list would offer a style the wrong menu.
let modelsByConnection = {};
// Probe generation per connection, so a slow answer for one never overwrites a
// fresh answer for another.
let probeIds = {};
// Connections added in this modal that hold nothing yet. Several presets ship no
// default model, so a fresh one is genuinely empty until the key is pasted — and
// the derived list would otherwise drop the row between the click and the first
// keystroke. Nothing persists them: an empty connection has nothing to store.
let pendingConnections = new Set();

export function initConfigPanel(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "settings", () => openSettings());
  // Readiness is answered about the style that will render, so picking a different
  // one is a question that has to be asked again — the new style may point at a
  // connection with no key, or at a workflow that was never imported.
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

// Every conversation-less config/discovery call rides the one QUERY route.
// It is not conversation-scoped, so the path is built by hand (convUrl is for
// the /trigger surface). Handlers report failure in-band as `{error}`; a raise
// here is a route-level fault (missing/500), which each caller degrades on.
function query(action, extra) {
  return api.post(`/workflows/${WORKFLOW_ID}/query`, { action, ...extra });
}

// Last readiness answer, so the card renders synchronously from a known value
// instead of painting empty and filling in later.
let cardReadiness = { text: "", ready: true };
// What the camera picker needs beyond the saved mode, which lives in cfg. Both
// ride the readiness answer. The classifier starts assumed-present so the picker
// never claims it is off on the strength of a probe that has not run yet.
let cardPov = { classifier: true, fallback: "third" };
// Style list for the card picker, cached the same way. The Visualize button
// reads its choice from cfg.default_style, so the picker is where a style is
// chosen once instead of in a modal on every generate.
let cardStyles = [];

// The card names each style's prompt format alongside it -- an <option> carries no
// markup, so it rides the text as "Krea-Alt (Prose)". Picking a style here is
// picking a format too, and that is not visible anywhere else on the card.
function cardStyleOptions() {
  const selected = cfg?.default_style || "";
  return cardStyles
    .map(
      (s) =>
        `<option value="${escAttr(s.id)}"${s.id === selected ? " selected" : ""}>${esc(s.label || s.id)} ${promptFormatBadge(s.prompt_format)}</option>`,
    )
    .join("");
}

// Keep the card focused on the next useful action. An incomplete setup gets one
// concise status and one action; normal use gets the style picker and Settings.
// Connection details and diagnostics belong in the full form.
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

// Which modes exist and which is showing is a pure decision (see povChoices) --
// the fallback is the backend's, reported by the status query, not a second copy
// of the default here.
function cardPovOptions() {
  const { modes, selected } = povChoices({ ...cardPov, mode: cfg?.pov_mode || "auto" });
  return modes
    .map(([id, label]) => `<option value="${escAttr(id)}"${id === selected ? " selected" : ""}>${esc(label)}</option>`)
    .join("");
}

function povPicker() {
  return `<label for="ig-card-pov">POV</label><select id="ig-card-pov" class="tool-card-select" data-wf-action="image_gen:pickPov" data-wf-on="change">${cardPovOptions()}</select>`;
}

function refreshCard() {
  const el = document.getElementById("ig-card-config");
  if (el) el.innerHTML = configPanelBody();
}

export function configPanelRenderer() {
  // Style, camera and readiness are all global and primed at load, so the card
  // paints synchronously from cache.
  return `<div class="tool-card-desc">Generate images on demand with ComfyUI or a cloud API.</div>
    <div id="ig-card-config">${configPanelBody()}</div>`;
}

// The card pickers are the only place the default style and the camera are
// chosen, so their choices persist — otherwise every reload reopens on the
// shipped defaults, which is the hassle the pickers exist to remove. The full
// config round-trips like a Save.
async function saveConfigPatch(patch, failure) {
  Object.assign(cfg, patch);
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: { ...cfg, ...patch } });
    if (res?.config) Object.assign(cfg, res.config);
  } catch {
    toast(failure, "error");
  }
}

// Styles feed the card picker. Fetched once at load (and after a save), then the
// picker is patched in place so an open tools panel need not be re-rendered.
export async function refreshCardStyles() {
  try {
    const res = await query("styles");
    cardStyles = Array.isArray(res?.styles) ? res.styles : [];
  } catch {
    cardStyles = [];
  }
  refreshCard();
}

// Readiness is a configuration question, not a network one -- the `status`
// query answers from the saved config alone, so the tools panel never waits on
// a remote server. Reachability stays with the Visualize modal's connection
// probe, which runs at the moment it matters.
export async function refreshCardReadiness() {
  try {
    const status = await query("status");
    cardReadiness = {
      ready: !!status?.ready,
      text: status?.ready
        ? `Ready — ${status.style_count} style${status.style_count === 1 ? "" : "s"}`
        : status?.detail || "Not configured",
    };
    // Rides along: what the camera picker needs to label "Auto" honestly.
    cardPov = { classifier: !!status?.classifier_ready, fallback: status?.fallback_mode || cardPov.fallback };
    // And what the settings form needs to build its pickers. One payload, so the
    // source list, the provider list and the capability line cannot disagree.
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

// The checkpoint menu is the ComfyUI connection's model list, never the active
// one: a style can be linked to a cloud provider and still be holding a
// checkpoint for when it is linked back.
function checkpointField(value) {
  const state = modelPickerState(modelsByConnection[COMFY_CONNECTION], value);
  const attrs = 'data-ig-field="checkpoint" data-wf-action="image_gen:styleChange" data-wf-on="change"';
  if (state.kind === "input") {
    return `<input ${attrs} value="${escAttr(state.current)}" placeholder="checkpoint.safetensors">`;
  }

  const options = [];
  if (!state.current) {
    options.push('<option value="" selected>Choose a checkpoint</option>');
  } else if (!state.models.includes(state.current)) {
    // Keep a configured checkpoint visible if it disappeared from the server;
    // choosing another detected model replaces it normally.
    options.push(`<option value="${escAttr(state.current)}" selected>${esc(state.current)} (not detected)</option>`);
  }
  options.push(
    ...state.models.map(
      (name) => `<option value="${escAttr(name)}"${name === state.current ? " selected" : ""}>${esc(name)}</option>`,
    ),
  );
  return `<select ${attrs}>${options.join("")}</select>`;
}

function promptFormatOptions(value) {
  const selected = normalizePromptFormat(value);
  return PROMPT_FORMATS.map(
    ([id, label]) => `<option value="${id}"${id === selected ? " selected" : ""}>${label}</option>`,
  ).join("");
}

// The style's own connection picker. This is the control the redesign exists for:
// where a style renders is a property *of the style*, so an anime checkpoint on
// the local box and a photoreal commercial API are one dropdown apart instead of a
// global mode switch that makes half of every other style inapplicable.
function styleConnectionOptions(selected) {
  const options = connections.map(
    (c) => `<option value="${escAttr(c.id)}"${c.id === selected ? " selected" : ""}>${esc(c.label)}</option>`,
  );
  // A style pinned to a connection that has since been removed keeps naming it,
  // rather than silently adopting whichever connection sorts first — the same rule
  // the checkpoint and workflow fields already follow.
  if (selected && !connections.some((c) => c.id === selected)) {
    options.unshift(`<option value="${escAttr(selected)}" selected>${esc(selected)} (removed)</option>`);
  }
  if (!selected) options.unshift(`<option value="" selected>Choose a connection</option>`);
  return options.join("");
}

// The half of a style that depends on which backend renders it. Rendered per
// connection rather than always-present-and-explained: a pair of dead fields with
// a note saying they are dead reads as a bug, and the note has to be believed
// before the fields can be ignored.
function backendFields(style, connection) {
  if (connection && connection.source === "cloud") {
    const entry = draft.connections[connection.id] || {};
    const size = `${entry.width || 1024}×${entry.height || 1024}`;
    return `<div class="image-gen-note ig-style-backend">Model and resolution come from this connection — ${esc(connection.detail || "no model yet")}, ${esc(size)}.
      <button type="button" class="ig-link" data-wf-action="image_gen:connOpen" data-conn-id="${escAttr(connection.id)}">Edit connection</button></div>`;
  }
  return `<div class="ig-grid">
      <label>Checkpoint${checkpointField(style.checkpoint || "")}</label>
      <label>Workflow${workflowField(style.workflow || "")}</label>
    </div>`;
}

// Stated where the text is typed, not on the render that discards it. A provider
// with no negative field returns a perfectly good image that ignored it, so
// nothing downstream will ever report this.
function negativeNote(connection) {
  if (!connection || connection.source !== "cloud") return "";
  if (connection.preset?.supports_negative_prompt !== false) return "";
  return `<div class="image-gen-note">${esc(connection.label)} has no negative prompt field — this text is not sent.</div>`;
}

// One `<details>` per style. The summary carries the name, the connection and the
// prompt format: all three change what the image looks like, and all three are
// otherwise invisible until the row is opened.
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

// Reads every style row's live field values back into the draft. Called before
// anything that re-renders the list, so an in-progress edit survives an add or
// a delete elsewhere in it.
function captureStyles() {
  draft.styles = draft.styles.map((s, i) => {
    const row = document.querySelector(`[data-style-index="${i}"]`);
    if (!row) return s;
    const get = (name) => row.querySelector(`[data-ig-field="${name}"]`)?.value ?? "";
    // Checkpoint and workflow fall back to the stored values rather than to "":
    // they are not rendered while the style is linked to a cloud connection, and
    // blanking them there would make relinking to ComfyUI lose the pin.
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
  // inherits where the style above it renders — connection, and with it the
  // ComfyUI pins that are meaningless anywhere else. Inheriting the connection but
  // not the workflow would ship a style that cannot render until two more fields
  // are filled in, which is the papercut this avoids. The prompts are what the
  // user came here to write, so those start empty.
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
  // The dropdown must always offer something; an empty list would be repopulated
  // from the shipped defaults on the next load, which reads as an edit undoing
  // itself.
  if (draft.styles.length <= 1) {
    toast("Keep at least one style", "error");
    return;
  }
  draft.styles.splice(index, 1);
  renderStyles();
}

// Recomputes the summary badge from the row's live field values.
// Keep the collapsed summary's name and prompt format in sync as they are edited,
// so a row collapsed straight after a change does not read as the old format.
function refreshStyleState(el) {
  const row = el.closest("[data-style-index]");
  const field = (name) => row?.querySelector(`[data-ig-field="${name}"]`)?.value;
  const name = field("label")?.trim();
  const nameEl = row?.querySelector(".ig-style-name");
  const formatEl = row?.querySelector(".ig-style-format");
  // A blank name keeps the last one -- captureStyles falls back the same way, so
  // an empty field never reads as a nameless style. The format cannot be blanked:
  // it is a select, and an unknown value normalizes to the default.
  if (nameEl && name) nameEl.textContent = name;
  if (formatEl) formatEl.textContent = promptFormatBadge(field("prompt_format"));
}

// Relinking a style swaps the half of its body that depends on the backend, so
// the row is rebuilt rather than patched. Only this row: rebuilding the list would
// collapse every other open style around a single dropdown change.
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
  // A ComfyUI-linked style needs the checkpoint menu, which is only probed when
  // something asks for it.
  if (!connection || connection.source !== "cloud") loadModels(COMFY_CONNECTION);
}

// The per-style workflow control. With nothing imported there is nothing to
// pick, so the select would be an empty (or single dead-option) menu -- show the
// user what to do instead. Once graphs exist it becomes a real chooser.
function workflowField(selected) {
  if (!draft.graphs.length) {
    return `<span class="image-gen-note ig-workflow-empty">No workflows detected. Import one in <strong>Imported ComfyUI workflows</strong> below.</span>`;
  }
  return `<select data-ig-field="workflow" data-wf-action="image_gen:styleChange" data-wf-on="change">${workflowOptions(selected)}</select>`;
}

function workflowOptions(selected) {
  const known = draft.graphs.some((graph) => graph.id === selected);
  const options = [`<option value="" disabled${selected && known ? "" : " selected"}>Choose a workflow</option>`];
  // Keep a pinned-but-missing workflow visible rather than silently swapping the
  // style onto whatever graph happens to sort first, mirroring the checkpoint field.
  if (selected && !known) {
    options.push(`<option value="${escAttr(selected)}" selected>${esc(selected)} (not found)</option>`);
  }
  for (const graph of draft.graphs) {
    options.push(
      `<option value="${escAttr(graph.id)}"${selected === graph.id ? " selected" : ""}>${esc(graph.label || graph.id)}</option>`,
    );
  }
  return options.join("");
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
  // Pins naming the removed graph are cleared here rather than left to fail at
  // generation: the user just deleted it, so falling back is what they meant.
  draft.styles = draft.styles.map((s) => (s.workflow === graphId ? { ...s, workflow: "" } : s));
  const host = document.getElementById("ig-graph-list");
  if (host) host.innerHTML = graphRows();
  renderStyles();
}

// ── the Connections section ──────────────────────────────────────────────────
//
// A connection is one place an image can be rendered. ComfyUI is always the first
// row and is never removable: it is Orb's local backend, and a config with no
// connection at all would leave every style pointing at nothing.
//
// The list is derived from the credentials themselves (`connectionList` in
// policy.js) rather than stored beside them, so "this connection exists" and
// "this connection has a key" can never become two facts that disagree.

function providerFor(id) {
  return backends.providers.find((p) => p.id === id) || null;
}

// Recomputed from the working copy after anything that adds, removes or
// re-credentials a connection. `cfg` supplies only the pre-linking fallback
// (`source`/`cloud.provider`), which the form has no control over and never edits.
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

// The permanent gaps, stated once under the connection instead of as a note on
// every render. "xAI ignores negative prompts" is true of every image forever, and
// a note that fires 100% of the time is one users learn to skip — which then hides
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

// The same picker the checkpoint field uses, per connection: a probed list becomes
// a select, an empty or failed probe stays a text input so a model id can still be
// typed for a provider whose listing endpoint Orb cannot read.
function cloudModelField(id) {
  const entry = draft.connections[id] || {};
  const preset = providerFor(id);
  const state = modelPickerState(modelsByConnection[id], entry.model || "");
  if (state.kind === "input") {
    return `<input ${connField("model")} value="${escAttr(state.current)}" placeholder="${escAttr(preset?.default_model || "model id")}">`;
  }
  const options = [];
  if (!state.current) {
    options.push(
      `<option value="" selected>${esc(preset?.default_model ? `Default — ${preset.default_model}` : "Choose a model")}</option>`,
    );
  } else if (!state.models.includes(state.current)) {
    options.push(`<option value="${escAttr(state.current)}" selected>${esc(state.current)} (not detected)</option>`);
  }
  options.push(
    ...state.models.map(
      (name) => `<option value="${escAttr(name)}"${name === state.current ? " selected" : ""}>${esc(name)}</option>`,
    ),
  );
  return `<select ${connField("model")}>${options.join("")}</select>`;
}

// Resolution, quality and references belong to the connection, not to "cloud":
// they are provider-shaped facts, and two connections can legitimately want two
// answers. Only the fields the preset declares are rendered.
function cloudFields(connection) {
  const id = connection.id;
  const entry = draft.connections[id] || {};
  const preset = connection.preset;
  const baseUrl =
    !preset || preset.needs_base_url
      ? `<label>API base URL<input ${connField("base_url")} value="${escAttr(entry.base_url || "")}" placeholder="https://api.example.com/v1"></label>`
      : "";
  const sizes = CLOUD_SIZES.map(
    ([w, h, label]) =>
      `<option value="${w}x${h}"${Number(entry.width) === w && Number(entry.height) === h ? " selected" : ""}>${esc(label)}</option>`,
  ).join("");
  const qualities = CLOUD_QUALITIES.map(
    ([value, label]) =>
      `<option value="${escAttr(value)}"${(entry.quality || "") === value ? " selected" : ""}>${esc(label)}</option>`,
  ).join("");
  const referenceOptions = [
    `<option value=""${entry.reference_source ? "" : " selected"}>Off — send prompts only</option>`,
  ]
    .concat(
      REFERENCE_SOURCES.map(
        ([value, text]) =>
          `<option value="${value}"${entry.reference_source === value ? " selected" : ""}>${esc(text)}</option>`,
      ),
    )
    .join("");
  const quality = preset?.supports_quality
    ? `<label>Quality<select ${connField("quality")}>${qualities}</select></label>`
    : "";
  const references =
    !preset || preset.supports_references
      ? `<label>Reference images<select ${connField("reference_source")}>${referenceOptions}</select></label>`
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

// Each provider appears once: the credential map is keyed by provider id, so a
// second connection to the same provider would need a synthetic id. Saying so by
// running out of options beats offering a duplicate that silently overwrites.
function addRowHtml() {
  const options = addableProviders(connections, backends.providers);
  if (!options.length) return `<span class="image-gen-note">Every provider Orb knows already has a connection.</span>`;
  return `<select id="ig-conn-add">${options.map((p) => `<option value="${escAttr(p.id)}">${esc(p.label)}</option>`).join("")}</select>
    <button class="btn btn-sm" data-wf-action="image_gen:connAdd">Add connection</button>`;
}

// Which rows a freshly opened modal should already have expanded. Nothing, on a
// working setup — but "Finish setup" lands here, and making the user hunt for the
// row that is actually blocking them is the whole failure that button exists to
// avoid. The first unready connection, else ComfyUI, which is the one every
// install starts from.
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

// Reads every open connection body's live values back into the working copy.
// Called before anything that re-renders, so an in-progress edit survives an add
// or a delete elsewhere in the list. A collapsed row's fields are still in the
// DOM — `<details>` hides its body, it does not drop it — so nothing is lost.
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
    const [width, height] = String(get("size") ?? `${entry.width || 1024}x${entry.height || 1024}`).split("x");
    // Every field falls back to the stored value rather than to "": a preset that
    // declares no quality or no references renders no such control, and reading a
    // missing element as empty would blank a setting the user never saw.
    draft.connections[id] = {
      ...entry,
      api_key: get("api_key") ?? entry.api_key ?? "",
      model: get("model") ?? entry.model ?? "",
      base_url: get("base_url") ?? entry.base_url ?? "",
      width: Number(width) || 1024,
      height: Number(height) || 1024,
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
    width: 1024,
    height: 1024,
    quality: "",
    reference_source: "",
    ...existing,
    api_key: existing.api_key || "",
    // Seeding the preset's model is what makes a fresh connection one field (the
    // key) from ready, instead of two — and it is the model the provider's own
    // documentation opens with.
    model: existing.model || preset?.default_model || "",
    base_url: existing.base_url || "",
  };
  pendingConnections.add(id);
  rebuildConnections();
  renderConnections(id);
  renderStyles(openStyleIds());
  // No probe here on purpose: a brand-new connection has no key, so listing models
  // is a request guaranteed to 401. `refreshConnectionState` fires one the moment
  // a key is pasted, which is the first time the answer can be real.
}

function removeConnection(id) {
  captureStyles();
  captureConnections();
  delete draft.connections[id];
  delete modelsByConnection[id];
  pendingConnections.delete(id);
  // Styles pinned to it fall back to ComfyUI rather than keeping a dangling id:
  // the user just deleted the connection, and "renders nowhere" is not a state the
  // render path has an answer for. Said out loud, because it silently changes what
  // those styles will produce.
  const orphaned = draft.styles.filter((s) => s.connection === id).length;
  draft.styles = draft.styles.map((s) => (s.connection === id ? { ...s, connection: COMFY_CONNECTION } : s));
  rebuildConnections();
  renderConnections();
  renderStyles(openStyleIds());
  if (orphaned) toast(`${orphaned} style${orphaned > 1 ? "s" : ""} moved to ComfyUI`);
}

// Keeps the collapsed summary honest as fields are edited, and re-renders the
// styles so a linked style's "model and resolution come from this connection" line
// never names last minute's model.
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
  // Credentials are what the model list sits behind: pasting a key is the first
  // moment the menu can be filled, and changing a URL is when the old one stopped
  // being true. Picking a resolution is neither.
  if (["api_key", "base_url", "api_url"].includes(el.dataset.igConnField)) loadModels(id);
}

// Opens the Connections section on a specific row — the "Edit connection" link on
// a cloud-linked style, which is otherwise a scroll and a guess away.
function revealConnection(id) {
  const section = document.getElementById("ig-connections");
  if (section) section.open = true;
  const row = document.querySelector(`details.ig-conn[data-conn-id="${CSS.escape(id)}"]`);
  if (!row) return;
  row.open = true;
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  loadModels(id);
}

// The config as it would be if the style rendering next were on `id`. Every
// discovery call answers for one connection, and the backend routes on the
// *default style's* connection — so overriding that style is how a probe asks
// about a connection nothing is currently pointed at, with no special case on the
// query route.
function configForConnection(id) {
  const next = readConfig();
  const styles = next.styles.map((s) => ({ ...s }));
  const active = styles.find((s) => s.id === next.default_style) || styles[0];
  if (active) active.connection = id;
  return { ...next, styles };
}

// Swap the model controls this probe answers for. Live values and open accordions
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
    // can fill the model list for a connection that has not been saved yet.
    applyModels(id, res?.models);
    // ComfyUI names a GPU; a cloud provider names itself. Neither is guaranteed,
    // and a bare "Connected" is still a true answer.
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
  // Start honest: discovery for a previous modal must not make this one look
  // probed before its own request completes, and a reference image picked for a
  // different character must not survive into this form.
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
  // Sections are ordered by how often they are touched. Styles first: the sidebar
  // card picks between them, and this is where they are written. Connections sits
  // directly under them because that is what a style links to — and it collapses,
  // because a working setup is configured once and then left alone.
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
  // connections only once they hold a key: listing models is free, but a probe per
  // configured provider on every modal open is a burst of requests for menus most
  // of which will not be looked at.
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
    // `source` and `cloud.provider` are derived by the backend normalizer from the
    // connection the default style links to. The form has no control for either any
    // more; passing the stored values through is what keeps a config whose styles
    // predate connection linking routing exactly where it always did.
    source: cfg.source || "external_comfy",
    // Chosen in the tools-panel card now, not here; carry the live values through.
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
    // `draft.connections` is seeded from the *whole* stored map, not from the
    // rendered list, so an entry the panel never shows — the inert shipped row, or
    // an unknown id retained across a provider rename — survives the save with its
    // key intact. It is also therefore the authority on what exists: writing it
    // alone is what makes Remove actually remove.
    cloud: { ...(cfg.cloud || {}), providers: { ...draft.connections } },
  };
}

function candidateOptions(items, selectedIndex = 0, noneLabel = "") {
  const options = noneLabel
    ? [`<option value=""${selectedIndex < 0 ? " selected" : ""}>${esc(noneLabel)}</option>`]
    : [];
  return options
    .concat(
      items.map(
        (item, i) =>
          `<option value="${escAttr(item.value)}"${i === selectedIndex ? " selected" : ""}>${esc(item.label)}</option>`,
      ),
    )
    .join("");
}

// Slot roles are typed from the server's /object_info. Failure is degradation,
// not refusal: the picker falls back to conventional input names so a graph can
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
// text-to-image graph imports exactly as it did before this existed, and an edit
// graph is opt-in per node — Orb never guesses which LoadImage is the identity.
function referenceRows(items) {
  if (!items.length) return "";
  const options = [`<option value="" selected>Not used</option>`]
    .concat(REFERENCE_SOURCES.map(([id, text]) => `<option value="${id}">${esc(text)}</option>`))
    .join("");
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
    // Default to overriding the model when the graph has a loader: an imported
    // PNG pins a filename from another machine, so the checkpoint the user picks
    // in Orb should win. "None" keeps the graph's own model for the rare case a
    // workflow is self-contained.
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
// simply absent — that is how "Not used" is encoded, and it keeps a t2i graph's
// slot map byte-identical to what it was before.
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
  // The model slot is patched from the style's checkpoint at render time, so the
  // user's Orb selection overrides the model baked into the imported graph.
  if (model) slots.checkpoint = model;
  const references = readReferenceRows();
  if (references.length) slots.references = references;
  draft.graphs.push({ id, label, graph: pendingGraph.graph, slots });
  const list = document.getElementById("ig-graph-list");
  if (list) list.innerHTML = graphRows();
  // Per-style pins are rendered from the same graph list, so they need the new
  // entry too — the modal is not re-rendered on import.
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
    // The response is the *normalized* config: the backend bounds and drops what
    // it will not honour, and adopting its answer is what stops the panel from
    // listing settings the render path ignores.
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: next });
    const stored = res?.config || next;
    const droppedGraphs = next.external_comfy.user_graphs.length - (stored.external_comfy?.user_graphs?.length || 0);
    Object.assign(cfg, stored);
    await saveProfile();
    // Deliberately unattributed: the picker already gates the two causes the user
    // can act on (size, count), and a bare count cannot tell the rest apart.
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
// key, is `privacyDisclosure` in policy.js — a decision with two sources to get
// wrong now, which is why it lives somewhere `node --test` can reach it.
//
// Uploading conversation images is a materially bigger disclosure than sending
// prompt text, so it gets its own acknowledgement key: a user who accepted the
// prompt-only wording is asked again the first time references are turned on.
//
// One question per connection a style can reach, because a save can now light up a
// second remote backend without it ever being the "active" one.
function confirmRemotePrivacy(next) {
  // Derived from `next` rather than read off module state: this is the last gate
  // before credentials are sent, and it must describe the config being saved even
  // if a field changed after the last re-render.
  for (const disclosure of pendingDisclosures(next, connectionList(next, backends.providers))) {
    if (localStorage.getItem(disclosure.key) === "acknowledged") continue;
    if (!window.confirm(disclosure.message)) return false;
    localStorage.setItem(disclosure.key, "acknowledged");
  }
  return true;
}

// The character's reference image as the form currently holds it — loaded with
// the profile, replaced by the file picker, emptied by Clear, and written back on
// Save. Kept in module state rather than read off the rendered <img>, so a save
// that never touched the picker round-trips the stored bytes untouched.
let referenceImage = { reference_image_b64: "", reference_mime: "" };

function referenceImageHtml() {
  const stored = !!referenceImage.reference_image_b64;
  // With no image there is no preview column, so the controls start at the same
  // left edge as the prompt fields above rather than floating inset.
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
    // Chunked so a multi-MB image does not blow the argument limit of
    // String.fromCharCode, which a single spread of the whole array would.
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
  // No profile fields rendered (no active character) => nothing to persist, and
  // sending blanks here would wipe a saved appearance.
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
  // The normalizer drops a reference image it cannot accept. A save that reports
  // success while quietly discarding what the form is still previewing is the
  // one outcome the user cannot diagnose, so the handler's warning is shown and
  // the local copy is brought back in line with what was actually stored.
  if (res?.warning) {
    toast(res.warning, "error");
    referenceImage = {
      reference_image_b64: res.profile?.reference_image_b64 || "",
      reference_mime: res.profile?.reference_mime || "",
    };
  }
}
