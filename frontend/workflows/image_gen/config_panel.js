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
import { graphFromApiJson, graphFromPng, slotCandidates, splitCandidate } from "./graph_import.js";

const WORKFLOW_ID = "image_gen";
const STYLE_FIELDS = ["prompt", "negative_prompt", "checkpoint", "workflow"];
let cfg;
let pendingGraph = null;
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
  registerAction(WORKFLOW_ID, "settingsClose", () => closeModal());
  registerAction(WORKFLOW_ID, "test", () => testConnection());
  registerAction(WORKFLOW_ID, "save", () => saveSettings());
  registerAction(WORKFLOW_ID, "profileSave", () => saveProfile());
  registerAction(WORKFLOW_ID, "graphFile", (el) => importGraphFile(el));
  registerAction(WORKFLOW_ID, "graphAdd", () => addPendingGraph());
  registerAction(WORKFLOW_ID, "styleChange", (el) => refreshStyleState(el));
}

// Tools-panel card: description, the endpoint it will talk to, and one button
// that opens the whole form in a modal.
export function configPanelRenderer() {
  const endpoint = cfg?.external_comfy?.api_url || "http://127.0.0.1:8188";
  return `<div class="tool-card-desc">Generate images on demand with external ComfyUI.</div>
    <div class="image-gen-card-status" title="${escAttr(endpoint)}">${esc(endpoint)}</div>
    <button class="btn btn-sm image-gen-card-btn" data-wf-action="image_gen:settings">Settings</button>`;
}

function configuredStyles() {
  return Array.isArray(cfg.external_comfy?.styles) ? cfg.external_comfy.styles : [];
}

function overrideCount(values) {
  return STYLE_FIELDS.filter((f) => (values[f] || "").trim()).length;
}

function stateLabel(count) {
  return count ? `${count} override${count > 1 ? "s" : ""}` : "Inherits defaults";
}

function placeholder(styleId, field, fallback) {
  return styleDefaults.get(styleId)?.[field] || fallback;
}

// One collapsed row per style: the summary carries the name plus how far the
// style departs from the defaults, so a long list stays scannable without
// opening anything.
function styleRows() {
  return configuredStyles()
    .map((s, i) => {
      const count = overrideCount(s);
      return `<details class="ig-style" data-style-index="${i}">
        <summary>
          <span class="ig-style-name">${esc(s.label || s.id)}</span>
          <span class="ig-style-state" data-ig-state="${count ? "custom" : "inherit"}">${stateLabel(count)}</span>
        </summary>
        <div class="ig-style-body">
          <label>Positive style tags<textarea data-ig-field="prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="${escAttr(placeholder(s.id, "prompt", "Use the shipped default"))}">${esc(s.prompt || "")}</textarea></label>
          <label>Negative style tags<textarea data-ig-field="negative_prompt" data-wf-action="image_gen:styleChange" data-wf-on="change" placeholder="${escAttr(placeholder(s.id, "negative_prompt", "Use the shipped default"))}">${esc(s.negative_prompt || "")}</textarea></label>
          <div class="ig-grid">
            <label>Checkpoint<input data-ig-field="checkpoint" list="${CHECKPOINT_LIST_ID}" data-wf-action="image_gen:styleChange" data-wf-on="change" value="${escAttr(s.checkpoint || "")}" placeholder="Default checkpoint"></label>
            <label>Workflow<select data-ig-field="workflow" data-wf-action="image_gen:styleChange" data-wf-on="change">${workflowOptions(s.workflow || "", true)}</select></label>
          </div>
        </div>
      </details>`;
    })
    .join("");
}

// Recomputes the summary badge from the row's live field values.
function refreshStyleState(el) {
  const row = el.closest("[data-style-index]");
  const badge = row?.querySelector("[data-ig-state]");
  if (!badge) return;
  const count = [...row.querySelectorAll("[data-ig-field]")].filter((f) => f.value.trim()).length;
  badge.textContent = stateLabel(count);
  badge.dataset.igState = count ? "custom" : "inherit";
}

function workflowOptions(selected, includeGlobal = false) {
  const graphs = Array.isArray(cfg.external_comfy?.user_graphs) ? cfg.external_comfy.user_graphs : [];
  const options = [];
  if (includeGlobal) options.push(`<option value=""${selected ? "" : " selected"}>Default workflow</option>`);
  options.push(
    `<option value="external_core"${selected === "external_core" ? " selected" : ""}>Orb core workflow</option>`,
  );
  for (const graph of graphs) {
    options.push(
      `<option value="${escAttr(graph.id)}"${selected === graph.id ? " selected" : ""}>${esc(graph.label || graph.id)}</option>`,
    );
  }
  return options.join("");
}

const CHECKPOINT_LIST_ID = "ig-checkpoints";

function checkpointOptions() {
  return checkpointNames.map((name) => `<option value="${escAttr(name)}"></option>`).join("");
}

// The list is shared by the Defaults field and every style pin, so one refresh
// updates all of them.
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

function styleOptions(selected) {
  return configuredStyles()
    .map(
      (s) => `<option value="${escAttr(s.id)}"${s.id === selected ? " selected" : ""}>${esc(s.label || s.id)}</option>`,
    )
    .join("");
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

async function openSettings() {
  const ext = cfg.external_comfy || {};
  pendingGraph = null;
  await loadStyleDefaults();
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
      <div class="ig-heading">Defaults</div>
      <div class="image-gen-note">Every style starts from these. A style only needs its own value where it should differ.</div>
      <div class="ig-grid">
        <label>Checkpoint<input id="ig-checkpoint" list="${CHECKPOINT_LIST_ID}" value="${escAttr(ext.checkpoint || "")}" placeholder="checkpoint.safetensors"></label>
        <label>Workflow<select id="ig-workflow">${workflowOptions(ext.workflow || "external_core")}</select></label>
        <label>Style picked by default<select id="ig-default-style">${styleOptions(cfg.default_style || "")}</select></label>
        <label>Render timeout (seconds)<input id="ig-timeout" type="number" min="10" max="900" value="${escAttr(cfg.timeout_seconds || 180)}"></label>
      </div>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Styles</div>
      <div class="ig-styles">${styleRows()}</div>
    </section>
    <section class="ig-section">
      <div class="ig-heading">Character appearance</div>
      <div id="ig-profile" class="image-gen-note">Open a conversation to edit appearance tags.</div>
    </section>
    <details class="ig-advanced">
      <summary>Import a ComfyUI workflow</summary>
      <div class="ig-advanced-body">
        <div class="image-gen-note">Use a PNG generated by ComfyUI or a dev-mode Export (API) JSON file. Imported workflows run only on your external server.</div>
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
  const styles = configuredStyles().map((s, i) => {
    const row = document.querySelector(`[data-style-index="${i}"]`);
    const get = (name) => row?.querySelector(`[data-ig-field="${name}"]`)?.value || "";
    return {
      ...s,
      prompt: get("prompt"),
      negative_prompt: get("negative_prompt"),
      checkpoint: get("checkpoint"),
      workflow: get("workflow"),
    };
  });
  return {
    source: "external_comfy",
    default_style:
      document.getElementById("ig-default-style")?.value || cfg.default_style || styles[0]?.id || "realistic",
    scene_analysis: false,
    timeout_seconds: Number(document.getElementById("ig-timeout")?.value) || 180,
    external_comfy: {
      ...(cfg.external_comfy || {}),
      api_url: document.getElementById("ig-url")?.value || "",
      api_key: document.getElementById("ig-key")?.value || "",
      checkpoint: document.getElementById("ig-checkpoint")?.value || "",
      workflow: document.getElementById("ig-workflow")?.value || "external_core",
      styles,
      user_graphs: cfg.external_comfy?.user_graphs || [],
    },
  };
}

function candidateOptions(items, selectedIndex = 0) {
  return items
    .map(
      (item, i) =>
        `<option value="${escAttr(item.value)}"${i === selectedIndex ? " selected" : ""}>${esc(item.label)}</option>`,
    )
    .join("");
}

async function importGraphFile(input) {
  const file = input.files?.[0];
  const picker = document.getElementById("ig-graph-picker");
  if (!file || !picker) return;
  try {
    const graph = file.name.toLowerCase().endsWith(".png")
      ? graphFromPng(await file.arrayBuffer())
      : graphFromApiJson(await file.text());
    const candidates = slotCandidates(graph);
    if (candidates.text.length < 2 || !candidates.seed.length || !candidates.output.length) {
      throw new Error("Could not find enough prompt, seed, and image-output candidates in this workflow.");
    }
    pendingGraph = { graph, label: file.name.replace(/\.(json|png)$/i, ""), candidates };
    picker.innerHTML = `<div class="image-gen-graph-picker">
      <label>Name<input id="ig-graph-label" value="${escAttr(pendingGraph.label)}"></label>
      <div class="ig-grid">
        <label>Positive prompt<select id="ig-slot-positive">${candidateOptions(candidates.text, 0)}</select></label>
        <label>Negative prompt<select id="ig-slot-negative">${candidateOptions(candidates.text, 1)}</select></label>
        <label>Seed<select id="ig-slot-seed">${candidateOptions(candidates.seed)}</select></label>
        <label>Image output<select id="ig-slot-output">${candidateOptions(candidates.output)}</select></label>
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
  if (!positive || !negative || !seed || !output) return;
  const id = `user_${Date.now().toString(36)}`;
  const label = document.getElementById("ig-graph-label")?.value.trim() || pendingGraph.label;
  if (!Array.isArray(cfg.external_comfy.user_graphs)) cfg.external_comfy.user_graphs = [];
  cfg.external_comfy.user_graphs.push({
    id,
    label,
    graph: pendingGraph.graph,
    slots: { positive, negative, seed, output },
  });
  const workflow = document.getElementById("ig-workflow");
  if (workflow) {
    workflow.insertAdjacentHTML("beforeend", `<option value="${escAttr(id)}">${esc(label)}</option>`);
    workflow.value = id;
  }
  // Per-style overrides are rendered from the same graph list, so they need the
  // new entry too — the modal is not re-rendered on import.
  for (const select of document.querySelectorAll('[data-ig-field="workflow"]')) {
    select.insertAdjacentHTML("beforeend", `<option value="${escAttr(id)}">${esc(label)}</option>`);
  }
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
  } catch {
    if (result) result.textContent = "Connection failed";
  }
}

async function saveSettings() {
  const next = readConfig();
  if (!confirmRemotePrivacy(next.external_comfy.api_url)) return;
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: next });
    Object.assign(cfg, res?.config || next);
    toast("Image generation settings saved");
    closeModal();
  } catch {
    toast("Could not save image generation settings", "error");
  }
}

function confirmRemotePrivacy(apiUrl) {
  let parsed;
  try {
    parsed = new URL(apiUrl);
  } catch {
    return true;
  }
  const host = parsed.hostname.toLowerCase();
  if (host === "127.0.0.1" || host === "localhost" || host === "::1") return true;
  const key = `orb:image-gen-privacy:${parsed.origin}`;
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
