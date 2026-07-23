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
import { isLoopbackUrl } from "./policy.js";

const WORKFLOW_ID = "image_gen";
let cfg;
let pendingGraph = null;
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
  registerAction(WORKFLOW_ID, "pickStyle", (el) => selectDefaultStyle(el.value));
  registerAction(WORKFLOW_ID, "editStyle", (el) => openSettings(el.dataset.styleId));
  registerAction(WORKFLOW_ID, "settingsClose", () => closeModal());
  registerAction(WORKFLOW_ID, "test", () => testConnection());
  registerAction(WORKFLOW_ID, "save", () => saveSettings());
  registerAction(WORKFLOW_ID, "graphFile", (el) => importGraphFile(el));
  registerAction(WORKFLOW_ID, "graphAdd", () => addPendingGraph());
  registerAction(WORKFLOW_ID, "graphRemove", (el) => removeGraph(el.dataset.graphId));
  registerAction(WORKFLOW_ID, "styleAdd", () => addStyle());
  registerAction(WORKFLOW_ID, "styleRemove", (el) => removeStyle(Number(el.dataset.styleIndex)));
  registerAction(WORKFLOW_ID, "styleChange", (el) => refreshStyleState(el));
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
// Style list for the card picker, cached the same way. The Visualize button
// reads its choice from cfg.default_style, so the picker is where a style is
// chosen once instead of in a modal on every generate.
let cardStyles = [];

function cardStyleOptions() {
  const selected = cfg?.default_style || "";
  return cardStyles
    .map(
      (s) => `<option value="${escAttr(s.id)}"${s.id === selected ? " selected" : ""}>${esc(s.label || s.id)}</option>`,
    )
    .join("");
}

// Tools-panel card: what this will do, whether it can do it right now, the style
// the Visualize button will use, and one button that opens the whole form.
export function configPanelRenderer() {
  const endpoint = cfg?.external_comfy?.api_url || "http://127.0.0.1:8188";
  const stylePicker = cardStyles.length
    ? `<label class="image-gen-card-style">Style<select id="ig-card-style" class="tool-card-select" data-wf-action="image_gen:pickStyle" data-wf-on="change">${cardStyleOptions()}</select></label>`
    : "";
  return `<div class="tool-card-desc">Generate images on demand with external ComfyUI.</div>
    <div class="image-gen-card-status" id="ig-card-readiness" data-ig-ready="${cardReadiness.ready ? "yes" : "no"}">${esc(cardReadiness.text)}</div>
    <div class="image-gen-card-endpoint" title="${escAttr(endpoint)}">${esc(endpoint)}</div>
    ${stylePicker}
    <button class="btn btn-sm image-gen-card-btn" data-wf-action="image_gen:settings">Settings</button>`;
}

// The card picker is the only place a default style is chosen, so its choice
// persists — otherwise every reload reopens on the shipped default, which is the
// hassle the picker exists to remove. The full config round-trips like a Save.
async function selectDefaultStyle(styleId) {
  cfg.default_style = styleId;
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: { ...cfg, default_style: styleId } });
    if (res?.config) Object.assign(cfg, res.config);
  } catch {
    toast("Could not save default style", "error");
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
  const sel = document.getElementById("ig-card-style");
  if (sel && cardStyles.length) sel.innerHTML = cardStyleOptions();
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
  } catch {
    cardReadiness = { ready: false, text: "" };
  }
  const el = document.getElementById("ig-card-readiness");
  if (el) {
    el.textContent = cardReadiness.text;
    el.dataset.igReady = cardReadiness.ready ? "yes" : "no";
  }
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

function styleRows(expandIds = "") {
  const expanded = new Set(Array.isArray(expandIds) ? expandIds : [expandIds]);
  return draft.styles
    .map((s, i) => {
      return `<details class="ig-style" data-style-index="${i}"${expanded.has(s.id) ? " open" : ""}>
        <summary>
          <span class="ig-style-name">${esc(s.label || s.id)}</span>
        </summary>
        <div class="ig-style-body">
          <label>Name<input data-ig-field="label" data-wf-action="image_gen:styleChange" data-wf-on="change" value="${escAttr(s.label || "")}"></label>
          <label>Positive style tags<textarea data-ig-field="prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="No style tags">${esc(s.prompt || "")}</textarea></label>
          <label>Negative style tags<textarea data-ig-field="negative_prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="No style tags">${esc(s.negative_prompt || "")}</textarea></label>
          <div class="ig-grid">
            <label>Checkpoint${checkpointField(s.checkpoint || "")}</label>
            <label>Workflow<select data-ig-field="workflow" data-wf-action="image_gen:styleChange" data-wf-on="change">${workflowOptions(s.workflow || "external_core")}</select></label>
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
      prompt: get("prompt"),
      negative_prompt: get("negative_prompt"),
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
  draft.styles.push({ id, label: "New style", prompt: "", negative_prompt: "", checkpoint: "", workflow: "" });
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
// Keep the collapsed summary's name in sync as the label field is edited.
function refreshStyleState(el) {
  const row = el.closest("[data-style-index]");
  const name = row?.querySelector('[data-ig-field="label"]')?.value.trim();
  const nameEl = row?.querySelector(".ig-style-name");
  if (nameEl && name) nameEl.textContent = name;
}

function workflowOptions(selected) {
  const options = [
    `<option value="external_core"${selected === "external_core" ? " selected" : ""}>Orb core workflow</option>`,
  ];
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

// Swap every style checkpoint control together when discovery completes. Live
// values and open accordions survive the asynchronous re-render.
function applyCheckpoints(names) {
  const openIds = Array.from(document.querySelectorAll(".ig-style[open]"))
    .map((row) => draft.styles[Number(row.dataset.styleIndex)]?.id)
    .filter(Boolean);
  captureStyles();
  checkpointNames = modelPickerState(names).models;
  renderStyles(openIds);
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

function openSettings(expandStyleId = "") {
  const ext = cfg.external_comfy || {};
  pendingGraph = null;
  // Start honest: discovery for a previous modal/server must not make this one
  // look probed before its own request completes.
  checkpointProbeId += 1;
  checkpointNames = [];
  draft = {
    styles: (Array.isArray(ext.styles) ? ext.styles : []).map((s) => ({ ...s })),
    graphs: (Array.isArray(ext.user_graphs) ? ext.user_graphs : []).map((g) => ({ ...g })),
  };
  showModal(`<h2>Image Generation</h2><div class="image-gen-settings">
    <section class="ig-section">
      <div class="ig-heading">Connection</div>
      <div class="ig-grid">
        <label>ComfyUI URL<input id="ig-url" value="${escAttr(ext.api_url || "http://127.0.0.1:8188")}"></label>
        <label>API key<input id="ig-key" type="password" value="${escAttr(ext.api_key || "")}"></label>
      </div>
      <div class="image-gen-row"><button class="btn btn-sm" data-wf-action="image_gen:test">Test connection</button><span id="ig-test-result" class="image-gen-note"></span></div>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Generation</div>
      <div class="ig-grid">
        <label>Render timeout (seconds)<input id="ig-timeout" type="number" min="10" max="900" value="${escAttr(cfg.timeout_seconds || 180)}"></label>
      </div>
      <label class="ig-toggle"><input id="ig-scene-analysis" type="checkbox"${cfg.scene_analysis === true ? " checked" : ""}><span class="ig-toggle-body"><span class="ig-toggle-label">Analyze complex scenes</span><span class="image-gen-note">More accurate outfits and positions for scenes; one extra model call.</span></span></label>
      <label class="ig-toggle"><input id="ig-prompter-reasoning" type="checkbox"${cfg.prompter_reasoning === true ? " checked" : ""}><span class="ig-toggle-body"><span class="ig-toggle-label">Enable prompter thinking</span><span class="image-gen-note">Uses thinking for scene analysis and prompt composition. Changing this may reduce prompt-cache reuse on some providers.</span></span></label>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Styles</div>
      <div class="ig-styles">${styleRows(expandStyleId)}</div>
      <button class="btn btn-sm" data-wf-action="image_gen:styleAdd">Add style</button>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Character appearance</div>
      <div id="ig-profile" class="image-gen-note">Open a conversation to edit appearance tags.</div>
    </section>
    <details class="ig-advanced">
      <summary>Imported ComfyUI workflows</summary>
      <div class="ig-advanced-body">
        <div class="image-gen-note">Use a PNG generated by ComfyUI or a dev-mode Export (API) JSON file. Imported workflows run only on your external server.</div>
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
  return {
    source: "external_comfy",
    // Chosen in the tools-panel card now, not here; carry the live value through.
    default_style: cfg.default_style || draft.styles[0]?.id || "realistic",
    scene_analysis: document.getElementById("ig-scene-analysis")?.checked === true,
    prompter_reasoning: document.getElementById("ig-prompter-reasoning")?.checked === true,
    timeout_seconds: Number(document.getElementById("ig-timeout")?.value) || 180,
    external_comfy: {
      ...(cfg.external_comfy || {}),
      api_url: document.getElementById("ig-url")?.value || "",
      api_key: document.getElementById("ig-key")?.value || "",
      styles: draft.styles,
      user_graphs: draft.graphs,
    },
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

async function importGraphFile(input) {
  const file = input.files?.[0];
  const picker = document.getElementById("ig-graph-picker");
  if (!file || !picker) return;
  try {
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
      <button class="btn btn-sm" data-wf-action="image_gen:graphAdd">Confirm slots and add workflow</button>
    </div>`;
  } catch (e) {
    pendingGraph = null;
    picker.innerHTML = `<div class="image-gen-note">${esc(e.message || "Could not import this workflow.")}</div>`;
  }
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
    // Tested against the form's unsaved URL, so this is the only probe that can
    // fill the checkpoint list for a server that has not been saved yet.
    applyCheckpoints(res?.models);
    const device = res?.system?.devices?.[0]?.name;
    if (result) result.textContent = device ? `Connected — ${device}` : "Connected";
  } catch {
    if (probeId !== checkpointProbeId) return;
    applyCheckpoints([]);
    if (result) result.textContent = "Connection failed";
  }
}

async function saveSettings() {
  const next = readConfig();
  if (!confirmRemotePrivacy(next.external_comfy.api_url)) return;
  try {
    // The response is the *normalized* config: the backend bounds and drops what
    // it will not honour, and adopting its answer is what stops the panel from
    // listing settings the render path ignores.
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: next });
    const stored = res?.config || next;
    const droppedGraphs = next.external_comfy.user_graphs.length - (stored.external_comfy?.user_graphs?.length || 0);
    Object.assign(cfg, stored);
    await saveProfile();
    toast(
      droppedGraphs > 0
        ? `Saved, but ${droppedGraphs} imported workflow${droppedGraphs > 1 ? "s were" : " was"} rejected as too large`
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

function confirmRemotePrivacy(apiUrl) {
  if (isLoopbackUrl(apiUrl)) return true;
  const key = `orb:image-gen-privacy:${new URL(apiUrl).origin}`;
  if (localStorage.getItem(key) === "acknowledged") return true;
  const accepted = window.confirm(
    "This ComfyUI server is not on this machine. Your scene prompts leave Orb, other clients may read queued prompts, and generated files remain on that server. Save this connection?",
  );
  if (accepted) localStorage.setItem(key, "acknowledged");
  return accepted;
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
    el.innerHTML = `<div class="ig-profile-fields">
        <label>Appearance tags<textarea id="ig-appearance" placeholder="Booru tags. For a canon character the model knows, just its tag (e.g. hatsune miku). Leave blank for OCs.">${esc(res.profile.appearance_prompt || "")}</textarea></label>
        <label>Negative tags<textarea id="ig-profile-negative" placeholder="Per-character things to never render (e.g. glasses, hat). Quality and scene negatives are already handled.">${esc(res.profile.negative_prompt || "")}</textarea></label>
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
    },
  });
}
