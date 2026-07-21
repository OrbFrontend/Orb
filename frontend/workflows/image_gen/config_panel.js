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
import { isLoopbackUrl } from "./policy.js";

const WORKFLOW_ID = "image_gen";
const STYLE_FIELDS = ["prompt", "negative_prompt", "checkpoint", "workflow"];
let cfg;
let pendingGraph = null;
// The styles and imported graphs being edited. A working copy rather than cfg
// itself: adding a graph and then closing without saving must not leave the
// widget's shared config carrying an entry the server never stored.
let draft = { styles: [], graphs: [] };
// Shipped per-style prompt defaults, used as placeholders so an empty field
// shows what it actually inherits. Fetched once, then reused on every open.
const styleDefaults = new Map();
// Checkpoint filenames discovered on the configured server. Suggestions only:
// the fields stay free text so they remain editable while the server is
// unreachable and so a pin naming a since-removed file survives an edit.
let checkpointNames = [];

export function initConfigPanel(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "settings", () => openSettings());
  registerAction(WORKFLOW_ID, "pickStyle", (el) => selectDefaultStyle(el.value));
  registerAction(WORKFLOW_ID, "editStyle", (el) => openSettings(el.dataset.styleId));
  registerAction(WORKFLOW_ID, "settingsClose", () => closeModal());
  registerAction(WORKFLOW_ID, "test", () => testConnection());
  registerAction(WORKFLOW_ID, "save", () => saveSettings());
  registerAction(WORKFLOW_ID, "profileSave", () => saveProfile());
  registerAction(WORKFLOW_ID, "graphFile", (el) => importGraphFile(el));
  registerAction(WORKFLOW_ID, "graphAdd", () => addPendingGraph());
  registerAction(WORKFLOW_ID, "graphRemove", (el) => removeGraph(el.dataset.graphId));
  registerAction(WORKFLOW_ID, "styleAdd", () => addStyle());
  registerAction(WORKFLOW_ID, "styleRemove", (el) => removeStyle(Number(el.dataset.styleIndex)));
  registerAction(WORKFLOW_ID, "styleChange", (el) => refreshStyleState(el));
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
    ? `<label class="image-gen-card-style">Style<select id="ig-card-style" data-wf-action="image_gen:pickStyle" data-wf-on="change">${cardStyleOptions()}</select></label>`
    : "";
  return `<div class="tool-card-desc">Generate images on demand with external ComfyUI.</div>
    <div class="image-gen-card-status" title="${escAttr(endpoint)}">${esc(endpoint)}</div>
    <div class="image-gen-card-status" id="ig-card-readiness" data-ig-ready="${cardReadiness.ready ? "yes" : "no"}">${esc(cardReadiness.text)}</div>
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
    const res = await api.get(`/workflows/${WORKFLOW_ID}/styles`);
    cardStyles = Array.isArray(res?.styles) ? res.styles : [];
  } catch {
    cardStyles = [];
  }
  const sel = document.getElementById("ig-card-style");
  if (sel && cardStyles.length) sel.innerHTML = cardStyleOptions();
}

// Readiness is a configuration question, not a network one -- `/status` answers
// from the saved config alone, so the tools panel never waits on a remote
// server. Reachability stays with the Visualize modal's connection probe, which
// runs at the moment it matters.
export async function refreshCardReadiness() {
  try {
    const status = await api.get(`/workflows/${WORKFLOW_ID}/status`);
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

function overrideCount(values) {
  return STYLE_FIELDS.filter((f) => (values[f] || "").trim()).length;
}

function stateLabel(count) {
  return count ? `${count} override${count > 1 ? "s" : ""}` : "Inherits defaults";
}

// An empty field inherits the shipped fragment for that style id, so the
// placeholder shows the text it will actually use. A style the catalog does not
// seed -- anything the user added -- inherits nothing, and says so.
function placeholder(styleId, field) {
  return styleDefaults.get(styleId)?.[field] || "No style tags";
}

// One collapsed row per style: the summary carries the name plus how far the
// style departs from the defaults, so a long list stays scannable without
// opening anything.
function styleRows(expandId = "") {
  return draft.styles
    .map((s, i) => {
      const count = overrideCount(s);
      return `<details class="ig-style" data-style-index="${i}"${s.id === expandId ? " open" : ""}>
        <summary>
          <span class="ig-style-name">${esc(s.label || s.id)}</span>
          <span class="ig-style-state" data-ig-state="${count ? "custom" : "inherit"}">${stateLabel(count)}</span>
        </summary>
        <div class="ig-style-body">
          <label>Name<input data-ig-field="label" data-wf-action="image_gen:styleChange" data-wf-on="change" value="${escAttr(s.label || "")}"></label>
          <label>Positive style tags<textarea data-ig-field="prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="${escAttr(placeholder(s.id, "prompt"))}">${esc(s.prompt || "")}</textarea></label>
          <label>Negative style tags<textarea data-ig-field="negative_prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="${escAttr(placeholder(s.id, "negative_prompt"))}">${esc(s.negative_prompt || "")}</textarea></label>
          <div class="ig-grid">
            <label>Checkpoint<input data-ig-field="checkpoint" list="${CHECKPOINT_LIST_ID}" data-wf-action="image_gen:styleChange" data-wf-on="change" value="${escAttr(s.checkpoint || "")}" placeholder="checkpoint.safetensors"></label>
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
function refreshStyleState(el) {
  const row = el.closest("[data-style-index]");
  const badge = row?.querySelector("[data-ig-state]");
  if (!badge) return;
  const name = row.querySelector('[data-ig-field="label"]')?.value.trim();
  const nameEl = row.querySelector(".ig-style-name");
  if (nameEl && name) nameEl.textContent = name;
  const count = [...row.querySelectorAll("[data-ig-field]")].filter(
    (f) => STYLE_FIELDS.includes(f.dataset.igField) && f.value.trim(),
  ).length;
  badge.textContent = stateLabel(count);
  badge.dataset.igState = count ? "custom" : "inherit";
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

const CHECKPOINT_LIST_ID = "ig-checkpoints";

function checkpointOptions() {
  return checkpointNames.map((name) => `<option value="${escAttr(name)}"></option>`).join("");
}

// The list is shared by every style pin, so one refresh updates all of them.
function applyCheckpoints(names) {
  checkpointNames = Array.isArray(names) ? names.filter((n) => typeof n === "string") : [];
  const list = document.getElementById(CHECKPOINT_LIST_ID);
  if (list) list.innerHTML = checkpointOptions();
}

// Probes the saved connection after the modal is already open; a slow or
// unreachable server must not delay the form. Failure leaves plain text fields.
async function loadCheckpoints() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/external/models`);
    applyCheckpoints(res?.models);
  } catch {
    applyCheckpoints([]);
  }
}

// Fills the placeholder cache. Failure is non-fatal: rows fall back to the
// generic "Use the shipped default" hint.
async function loadStyleDefaults() {
  if (styleDefaults.size) return;
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/styles`);
    for (const s of res?.styles || []) {
      styleDefaults.set(s.id, { prompt: s.prompt_default || "", negative_prompt: s.negative_prompt_default || "" });
    }
  } catch {
    // Placeholders stay generic.
  }
}

async function openSettings(expandStyleId = "") {
  const ext = cfg.external_comfy || {};
  pendingGraph = null;
  draft = {
    styles: (Array.isArray(ext.styles) ? ext.styles : []).map((s) => ({ ...s })),
    graphs: (Array.isArray(ext.user_graphs) ? ext.user_graphs : []).map((g) => ({ ...g })),
  };
  await loadStyleDefaults();
  showModal(`<h2>Image Generation</h2><div class="image-gen-settings">
    <section class="ig-section">
      <div class="ig-heading">Connection</div>
      <div class="ig-grid">
        <label>ComfyUI URL<input id="ig-url" value="${escAttr(ext.api_url || "http://127.0.0.1:8188")}"></label>
        <label>API key<input id="ig-key" type="password" value="${escAttr(ext.api_key || "")}"></label>
        <label>Render timeout (seconds)<input id="ig-timeout" type="number" min="10" max="900" value="${escAttr(cfg.timeout_seconds || 180)}"></label>
      </div>
      <div class="image-gen-row"><button class="btn btn-sm" data-wf-action="image_gen:test">Test connection</button><span id="ig-test-result" class="image-gen-note"></span></div>
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
    <datalist id="${CHECKPOINT_LIST_ID}">${checkpointOptions()}</datalist>
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
    // No control for this yet; carry the saved value rather than resetting it.
    scene_analysis: cfg.scene_analysis === true,
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
    const res = await api.post(`/workflows/${WORKFLOW_ID}/external/node-types`, {
      class_types: classTypes(graph),
      config: readConfig(),
    });
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
    picker.textContent = e.message || "Could not import this workflow.";
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
  if (picker) picker.textContent = `Added ${label}. Test the connection, then save settings.`;
  pendingGraph = null;
}

async function testConnection() {
  const result = document.getElementById("ig-test-result");
  if (result) result.textContent = "Testing...";
  try {
    const res = await api.post(`/workflows/${WORKFLOW_ID}/connections/test`, { config: readConfig() });
    // Tested against the form's unsaved URL, so this is the only probe that can
    // fill the checkpoint list for a server that has not been saved yet.
    applyCheckpoints(res?.models);
    const device = res?.system?.devices?.[0]?.name;
    if (result) result.textContent = device ? `Connected — ${device}` : "Connected";
  } catch (e) {
    if (result) result.textContent = failureDetail(e, "Connection failed");
  }
}

// `api` rejects with the raw body; the route sends `{"detail": "..."}` naming
// the unmet prerequisite, which is more use than "failed".
function failureDetail(error, fallback) {
  try {
    const detail = JSON.parse(error?.message)?.detail;
    if (typeof detail === "string" && detail) return detail;
  } catch {
    // Not JSON — keep the caller's bounded message.
  }
  return fallback;
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
        <label>Appearance tags<textarea id="ig-appearance">${esc(res.profile.appearance_prompt || "")}</textarea></label>
        <label>Negative tags<textarea id="ig-profile-negative">${esc(res.profile.negative_prompt || "")}</textarea></label>
      </div>
      <button class="btn btn-sm" data-wf-action="image_gen:profileSave">Save appearance</button>`;
  } catch {
    el.textContent = "Could not load character appearance.";
  }
}

async function saveProfile() {
  if (!getActiveConvId()) return;
  try {
    await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
      action: "set_profile",
      profile: {
        appearance_prompt: document.getElementById("ig-appearance")?.value || "",
        negative_prompt: document.getElementById("ig-profile-negative")?.value || "",
      },
    });
    toast("Character appearance saved");
  } catch {
    toast("Could not save character appearance", "error");
  }
}
