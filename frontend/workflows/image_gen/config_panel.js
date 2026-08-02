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
  DEFAULT_PROMPT_FORMAT,
  normalizePromptFormat,
  PROMPT_FORMATS,
  povChoices,
  privacyDisclosure,
  promptFormatLabel,
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
// do. One payload behind the source picker, the provider dropdown and the
// capability line, so the three can never disagree. Primed by the status query.
let backends = { sources: [], providers: [] };
// The source the open form is editing. Read back on save rather than from `cfg`,
// so switching source and saving in one go does what it looks like it does.
let draftSource = "external_comfy";
// The styles and imported graphs being edited. A working copy rather than cfg
// itself: adding a graph and then closing without saving must not leave the
// widget's shared config carrying an entry the server never stored.
let draft = { styles: [], graphs: [] };
// Checkpoint filenames discovered on the configured server. A non-empty probe
// renders real selects; an empty/failed probe leaves plain text inputs.
let checkpointNames = [];
let checkpointProbeId = 0;

export function initConfigPanel(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "settings", () => openSettings());
  registerAction(WORKFLOW_ID, "pickStyle", (el) =>
    saveConfigPatch({ default_style: el.value }, "Could not save default style"),
  );
  registerAction(WORKFLOW_ID, "pickPov", (el) => saveConfigPatch({ pov_mode: el.value }, "Could not save the camera"));
  registerAction(WORKFLOW_ID, "editStyle", (el) => openSettings(el.dataset.styleId));
  registerAction(WORKFLOW_ID, "settingsClose", () => closeModal());
  registerAction(WORKFLOW_ID, "test", () => testConnection());
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
  registerAction(WORKFLOW_ID, "pickSource", (el) => pickSource(el.value));
  registerAction(WORKFLOW_ID, "pickProvider", (el) => pickProvider(el.value));
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

// One collapsed row per style: the summary carries just the name, so a long
// list stays scannable without opening anything.
function checkpointField(value) {
  const state = modelPickerState(checkpointNames, value);
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

// Checkpoint and Workflow are ComfyUI-only fields on a now-shared style. They stay
// rendered under cloud rather than hidden, because they are still stored and a
// switch back must find them where they were left — but an unexplained pair of
// dead fields reads as a bug, so say which backend they belong to.
function comfyOnlyNote() {
  if (draftSource !== "cloud") return "";
  return `<div class="image-gen-note">Used only by the ComfyUI backend. A cloud provider uses one model for every style, set under <strong>Connection</strong>.</div>`;
}

function styleRows(expandIds = "") {
  const expanded = new Set(Array.isArray(expandIds) ? expandIds : [expandIds]);
  return draft.styles
    .map((s, i) => {
      return `<details class="ig-style" data-style-index="${i}"${expanded.has(s.id) ? " open" : ""}>
        <summary>
          <span class="ig-style-name">${esc(s.label || s.id)}</span>
          <span class="ig-style-format">${promptFormatBadge(s.prompt_format)}</span>
        </summary>
        <div class="ig-style-body">
          <label>Name<input data-ig-field="label" data-wf-action="image_gen:styleChange" data-wf-on="change" value="${escAttr(s.label || "")}"></label>
          <label>Prompt format<select data-ig-field="prompt_format" data-wf-action="image_gen:styleChange" data-wf-on="change">${promptFormatOptions(s.prompt_format)}</select></label>
          <label>Positive style prompt<textarea data-ig-field="prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="No positive style prompt">${esc(s.prompt || "")}</textarea></label>
          <label>Negative style prompt<textarea data-ig-field="negative_prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="No negative style prompt">${esc(s.negative_prompt || "")}</textarea></label>
          <label>Extra instructions<textarea data-ig-field="extra_instructions" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="Extra guidance for the prompter model (e.g. emphasize hand placement and use full-body framing).">${esc(s.extra_instructions || "")}</textarea></label>
          ${comfyOnlyNote()}
          <div class="ig-grid">
            <label>Checkpoint${checkpointField(s.checkpoint || "")}</label>
            <label>Workflow${workflowField(s.workflow || "")}</label>
          </div>
          <button class="btn btn-sm ig-danger" data-wf-action="image_gen:styleRemove" data-style-index="${i}">Remove style</button>
        </div>
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
    return {
      ...s,
      label: get("label").trim() || s.label || s.id,
      prompt_format: normalizePromptFormat(get("prompt_format") || s.prompt_format),
      prompt: get("prompt"),
      negative_prompt: get("negative_prompt"),
      extra_instructions: get("extra_instructions"),
      checkpoint: get("checkpoint"),
      workflow: get("workflow"),
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
  draft.styles.push({
    id,
    label: "New style",
    prompt_format: DEFAULT_PROMPT_FORMAT,
    prompt: "",
    negative_prompt: "",
    extra_instructions: "",
    checkpoint: "",
    workflow: "",
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

// Swap every model control together when discovery completes. Live values and
// open accordions survive the asynchronous re-render.
function applyCheckpoints(names) {
  const openIds = Array.from(document.querySelectorAll(".ig-style[open]"))
    .map((row) => draft.styles[Number(row.dataset.styleIndex)]?.id)
    .filter(Boolean);
  captureStyles();
  captureCloud();
  checkpointNames = modelPickerState(names).models;
  renderStyles(openIds);
  // Under cloud the model control lives in the Connection section rather than on
  // each style, so a discovery that only re-rendered the styles would leave the
  // one control the probe was for as a plain text input.
  const host = document.getElementById("ig-cloud-model")?.parentElement;
  if (draftSource === "cloud" && host) {
    const { entry, preset } = cloudDraft();
    host.innerHTML = `Model${cloudModelField(entry.model || "", preset)}`;
  }
}

// Probes the saved connection after the modal is already open; a slow or
// unreachable server must not delay the form. Failure leaves plain text fields.
async function loadCheckpoints() {
  const probeId = ++checkpointProbeId;
  try {
    const res = await query("models");
    if (probeId === checkpointProbeId) applyCheckpoints(res?.models);
  } catch {
    if (probeId === checkpointProbeId) applyCheckpoints([]);
  }
}

// ── the Backend section ──────────────────────────────────────────────────────

function sourceOptions() {
  const known = backends.sources.length
    ? backends.sources
    : [
        { id: "external_comfy", label: "External ComfyUI" },
        { id: "cloud", label: "Cloud API" },
      ];
  return known
    .map(
      (s) =>
        `<option value="${escAttr(s.id)}"${s.id === draftSource ? " selected" : ""}>${esc(s.label || s.id)}</option>`,
    )
    .join("");
}

function providerFor(id) {
  return backends.providers.find((p) => p.id === id) || null;
}

function cloudDraft() {
  const cloud = cfg.cloud || {};
  const providers = cloud.providers || {};
  const id = String(cloud.provider || "xai");
  return { cloud, id, entry: providers[id] || {}, preset: providerFor(id) };
}

// The permanent gaps, stated once under the picker instead of as a note on every
// render. "xAI ignores negative prompts" is true of every image forever, and a
// note that fires 100% of the time is one users learn to skip — which then hides
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

function comfyConnectionHtml(ext) {
  return `<div class="ig-grid">
      <label>ComfyUI URL<input id="ig-url" value="${escAttr(ext.api_url || "http://127.0.0.1:8188")}"></label>
      <label>API key<input id="ig-key" type="password" value="${escAttr(ext.api_key || "")}"></label>
    </div>`;
}

function cloudConnectionHtml() {
  const { cloud, id, entry, preset } = cloudDraft();
  const options = backends.providers
    .map((p) => `<option value="${escAttr(p.id)}"${p.id === id ? " selected" : ""}>${esc(p.label)}</option>`)
    .join("");
  const baseUrl = preset?.needs_base_url
    ? `<label>API base URL<input id="ig-cloud-base-url" value="${escAttr(entry.base_url || "")}" placeholder="https://api.example.com/v1"></label>`
    : "";
  const docs = preset?.docs_url
    ? `<div class="image-gen-note"><a href="${escAttr(preset.docs_url)}" target="_blank" rel="noopener noreferrer">${esc(preset.label)} API documentation</a></div>`
    : "";
  const sizes = CLOUD_SIZES.map(
    ([w, h, label]) =>
      `<option value="${w}x${h}"${Number(cloud.width) === w && Number(cloud.height) === h ? " selected" : ""}>${esc(label)}</option>`,
  ).join("");
  const qualities = CLOUD_QUALITIES.map(
    ([value, label]) =>
      `<option value="${escAttr(value)}"${(cloud.quality || "") === value ? " selected" : ""}>${esc(label)}</option>`,
  ).join("");
  const referenceOptions = [
    `<option value=""${cloud.reference_source ? "" : " selected"}>Off — send prompts only</option>`,
  ]
    .concat(
      REFERENCE_SOURCES.map(
        ([value, text]) =>
          `<option value="${value}"${cloud.reference_source === value ? " selected" : ""}>${esc(text)}</option>`,
      ),
    )
    .join("");
  const quality = preset?.supports_quality
    ? `<label>Quality<select id="ig-cloud-quality">${qualities}</select></label>`
    : "";
  return `<div class="ig-grid">
      <label>Provider<select id="ig-cloud-provider" data-wf-action="image_gen:pickProvider" data-wf-on="change">${options}</select></label>
      <label>API key<input id="ig-cloud-key" type="password" value="${escAttr(entry.api_key || "")}" placeholder="Paste your key"></label>
      ${baseUrl}
      <label>Model${cloudModelField(entry.model || "", preset)}</label>
      <label>Resolution<select id="ig-cloud-size">${sizes}</select></label>
      ${quality}
      <label>Reference images<select id="ig-cloud-reference">${referenceOptions}</select></label>
    </div>
    <div class="image-gen-note">Aspect ratio is chosen automatically from the resolution.</div>
    ${capabilityLine(preset)}${docs}`;
}

// The same unmodified picker the checkpoint field uses: a probed list becomes a
// select, a failed probe stays a text input so a model can still be typed.
function cloudModelField(value, preset) {
  const state = modelPickerState(checkpointNames, value);
  if (state.kind === "input") {
    return `<input id="ig-cloud-model" value="${escAttr(state.current)}" placeholder="${escAttr(preset?.default_model || "model id")}">`;
  }
  const options = [];
  if (!state.current) {
    options.push(
      `<option value="" selected>${escAttr(preset?.default_model ? `Default — ${preset.default_model}` : "Choose a model")}</option>`,
    );
  } else if (!state.models.includes(state.current)) {
    options.push(`<option value="${escAttr(state.current)}" selected>${esc(state.current)} (not detected)</option>`);
  }
  options.push(
    ...state.models.map(
      (name) => `<option value="${escAttr(name)}"${name === state.current ? " selected" : ""}>${esc(name)}</option>`,
    ),
  );
  return `<select id="ig-cloud-model">${options.join("")}</select>`;
}

function connectionHtml() {
  return draftSource === "cloud" ? cloudConnectionHtml() : comfyConnectionHtml(cfg.external_comfy || {});
}

// Re-renders only the Connection section and re-runs the model probe. Styles and
// imported graphs are untouched, because they are untouched by a source switch.
function renderConnection() {
  const host = document.getElementById("ig-connection");
  if (host) host.innerHTML = connectionHtml();
  const result = document.getElementById("ig-test-result");
  if (result) result.textContent = "";
  checkpointNames = [];
  renderStyles();
  loadCheckpoints();
}

function pickSource(value) {
  captureStyles();
  captureCloud();
  draftSource = value === "cloud" ? "cloud" : "external_comfy";
  renderConnection();
}

function pickProvider(value) {
  // Read the open form back into cfg first: the key the user just typed belongs to
  // the *previous* provider, and the whole point of the per-provider map is that
  // switching does not destroy it.
  captureCloud();
  cfg.cloud = { ...(cfg.cloud || {}), provider: value };
  renderConnection();
}

// The cloud form's live values, folded into the shared config's provider map.
// Called before anything re-renders the section and once more on save.
function captureCloud() {
  if (draftSource !== "cloud") return;
  const provider = document.getElementById("ig-cloud-provider");
  if (!provider) return;
  const cloud = cfg.cloud || {};
  const id = String(cloud.provider || provider.value || "xai");
  const [width, height] = String(document.getElementById("ig-cloud-size")?.value || "1024x1024").split("x");
  cfg.cloud = {
    ...cloud,
    provider: id,
    width: Number(width) || 1024,
    height: Number(height) || 1024,
    quality: document.getElementById("ig-cloud-quality")?.value ?? cloud.quality ?? "",
    reference_source: document.getElementById("ig-cloud-reference")?.value ?? cloud.reference_source ?? "",
    providers: {
      ...(cloud.providers || {}),
      [id]: {
        ...(cloud.providers?.[id] || {}),
        api_key: document.getElementById("ig-cloud-key")?.value ?? "",
        model: document.getElementById("ig-cloud-model")?.value ?? "",
        base_url: document.getElementById("ig-cloud-base-url")?.value ?? cloud.providers?.[id]?.base_url ?? "",
      },
    },
  };
}

function openSettings(expandStyleId = "") {
  const ext = cfg.external_comfy || {};
  pendingGraph = null;
  draftSource = cfg.source === "cloud" ? "cloud" : "external_comfy";
  // Start honest: discovery for a previous modal/server must not make this one
  // look probed before its own request completes, and a reference image picked
  // for a different character must not survive into this form.
  checkpointProbeId += 1;
  checkpointNames = [];
  referenceImage = { reference_image_b64: "", reference_mime: "" };
  draft = {
    styles: (Array.isArray(cfg.styles) ? cfg.styles : []).map((s) => ({ ...s })),
    graphs: (Array.isArray(ext.user_graphs) ? ext.user_graphs : []).map((g) => ({ ...g })),
  };
  showModal(`<h2>Image Generation</h2><div class="image-gen-settings">
    <section class="ig-section">
      <div class="ig-heading">Backend</div>
      <div class="ig-grid">
        <label>Image source<select id="ig-source" data-wf-action="image_gen:pickSource" data-wf-on="change">${sourceOptions()}</select></label>
      </div>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Connection</div>
      <div id="ig-connection">${connectionHtml()}</div>
      <div class="image-gen-row"><button class="btn btn-sm" data-wf-action="image_gen:test">Test connection</button><span id="ig-test-result" class="image-gen-note"></span></div>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Styles</div>
      <div class="ig-styles">${styleRows(expandStyleId)}</div>
      <button class="btn btn-sm" data-wf-action="image_gen:styleAdd">Add style</button>
    </section>
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
      <summary>Imported ComfyUI workflows</summary>
      <div class="ig-advanced-body">
        <div class="image-gen-note">Use a PNG generated by ComfyUI or a dev-mode Export (API) JSON file. Imported workflows run only on your external server, and are kept whichever image source is selected.</div>
        <div id="ig-graph-list" class="ig-graph-list">${graphRows()}</div>
        <input type="file" accept=".json,.png,application/json,image/png" data-wf-action="image_gen:graphFile" data-wf-on="change">
        <div id="ig-graph-picker"></div>
      </div>
    </details>
  </div><div class="modal-actions"><button class="btn" data-wf-action="image_gen:settingsClose">Close</button><button class="btn btn-accent" data-wf-action="image_gen:save">Save</button></div>`);
  populateProfile();
  loadCheckpoints();
}

function readConfig() {
  captureStyles();
  captureCloud();
  const ext = cfg.external_comfy || {};
  return {
    source: document.getElementById("ig-source")?.value || draftSource,
    // Chosen in the tools-panel card now, not here; carry the live values through.
    default_style: cfg.default_style || draft.styles[0]?.id || "realistic",
    pov_mode: cfg.pov_mode || "auto",
    scene_analysis: document.getElementById("ig-scene-analysis")?.checked === true,
    prompter_reasoning: document.getElementById("ig-prompter-reasoning")?.checked === true,
    timeout_seconds: Number(document.getElementById("ig-timeout")?.value) || 180,
    styles: draft.styles,
    external_comfy: {
      ...ext,
      // The ComfyUI fields are only rendered under that source. Falling back to the
      // stored values rather than to "" is what keeps a switch to cloud and back
      // from silently blanking the URL and the key.
      api_url: document.getElementById("ig-url")?.value ?? ext.api_url ?? "",
      api_key: document.getElementById("ig-key")?.value ?? ext.api_key ?? "",
      user_graphs: draft.graphs,
    },
    // Spread whole, so per-provider credentials the form never rendered survive
    // the save. The map is the reason switching provider is not destructive.
    cloud: { ...(cfg.cloud || {}) },
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

async function testConnection() {
  const result = document.getElementById("ig-test-result");
  if (result) result.textContent = "Testing...";
  const probeId = ++checkpointProbeId;
  try {
    const res = await query("test", { config: readConfig() });
    if (probeId !== checkpointProbeId) return;
    // The probe names the unmet prerequisite in `error`, which is more use than
    // "failed"; a route-level fault falls through to the catch instead.
    if (res?.error) {
      applyCheckpoints([]);
      if (result) result.textContent = res.error;
      return;
    }
    // Tested against the form's unsaved URL/key, so this is the only probe that can
    // fill the model list for a backend that has not been saved yet.
    applyCheckpoints(res?.models);
    // ComfyUI names a GPU; a cloud provider names itself. Neither is guaranteed,
    // and a bare "Connected" is still a true answer.
    const who = res?.system?.devices?.[0]?.name || res?.system?.provider;
    if (result) result.textContent = who ? `Connected — ${who}` : "Connected";
  } catch {
    if (probeId !== checkpointProbeId) return;
    applyCheckpoints([]);
    if (result) result.textContent = "Connection failed";
  }
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
function confirmRemotePrivacy(next) {
  const provider = String(next.cloud?.provider || "");
  const disclosure = privacyDisclosure({
    source: next.source,
    apiUrl: next.external_comfy?.api_url || "",
    providerId: provider,
    providerLabel: providerFor(provider)?.label || provider,
    sendsImages:
      next.source === "cloud"
        ? !!next.cloud?.reference_source
        : (next.external_comfy?.user_graphs || []).some((g) => (g?.slots?.references || []).length > 0),
  });
  if (!disclosure) return true;
  if (localStorage.getItem(disclosure.key) === "acknowledged") return true;
  const accepted = window.confirm(disclosure.message);
  if (accepted) localStorage.setItem(disclosure.key, "acknowledged");
  return accepted;
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
  try {
    // Chunked so a multi-MB image does not blow the argument limit of
    // String.fromCharCode, which a single spread of the whole array would.
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    setReferenceImage({ reference_image_b64: btoa(binary), reference_mime: file.type || "image/png" });
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
  await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
    action: "set_profile",
    profile: {
      appearance_prompt: appearanceEl.value || "",
      negative_prompt: document.getElementById("ig-profile-negative")?.value || "",
      ...referenceImage,
    },
  });
}
