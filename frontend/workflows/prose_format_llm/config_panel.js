// Settings modal for the prose_format_llm workflow. Global behavior config goes
// through the /config route; the per-conversation prose-format spec goes through
// the on-demand trigger RPC. Mirrors the TTS panel pattern.

import { api, closeModal, convUrl, esc, getActiveConvId, registerAction, showModal } from "/static/workflow_api.js";

const WORKFLOW_ID = "prose_format_llm";

// One array of {name, description, value} drives both spec sections, so renaming
// an element never desyncs its recorded value. Rebuilt from the server on every
// open / analyze / reset.
let spec = [];

export function initConfigPanel() {
  registerAction(WORKFLOW_ID, "settings", () => openSettings());
  registerAction(WORKFLOW_ID, "close", () => closeModal());
  registerAction(WORKFLOW_ID, "saveGlobal", () => saveGlobal());
  registerAction(WORKFLOW_ID, "addRow", () => addRow());
  registerAction(WORKFLOW_ID, "delRow", (el) => delRow(Number(el.dataset.i)));
  registerAction(WORKFLOW_ID, "analyze", () => analyze());
  registerAction(WORKFLOW_ID, "saveSpec", () => saveSpec());
  registerAction(WORKFLOW_ID, "reset", () => reset());
}

export function configCardRenderer() {
  return `<div class="tool-card-desc">Hold replies to a recorded prose format with an LLM judge/enforce pass.</div>
    <button class="btn btn-sm pf-settings-btn" data-wf-action="prose_format_llm:settings">Settings</button>`;
}

function triggerUrl() {
  return convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger");
}

async function openSettings() {
  showModal(modalShell());
  await loadGlobal();
  await populateSpec();
}

function modalShell() {
  return `<h2>Prose Format</h2>
    <p class="modal-subtitle">An LLM judge finds where a reply breaks this conversation's recorded format; an enforcer makes the minimal fixes. Dormant until a conversation is analyzed.</p>
    <div class="pf-section-title">Enforcement</div>
    <div class="field">
      <label for="pf-cfg-iters">Max enforce iterations</label>
      <input type="number" id="pf-cfg-iters" min="0" step="1" value="1" data-wf-action="prose_format_llm:saveGlobal" data-wf-on="change">
    </div>
    <div class="field">
      <label for="pf-cfg-mode">Prompt mode</label>
      <select id="pf-cfg-mode" data-wf-action="prose_format_llm:saveGlobal" data-wf-on="change">
        <option value="minimal">Minimal (draft + format only)</option>
        <option value="extend">Full context (whole conversation)</option>
      </select>
    </div>
    <label class="modal-checkbox-label pf-check"><input type="checkbox" id="pf-cfg-auto" data-wf-action="prose_format_llm:saveGlobal" data-wf-on="change"> Auto-analyze a conversation once</label>
    <label class="modal-checkbox-label pf-check"><input type="checkbox" id="pf-cfg-reasoning" data-wf-action="prose_format_llm:saveGlobal" data-wf-on="change"> Reasoning -- let every agent think before acting</label>
    <label class="modal-checkbox-label pf-check"><input type="checkbox" id="pf-cfg-stream" data-wf-action="prose_format_llm:saveGlobal" data-wf-on="change"> Stream that reasoning to the inspector rail (needs reasoning on)</label>
    <div class="pf-spec" id="pf-spec">Loading...</div>
    <div class="modal-actions"><button class="btn" data-wf-action="prose_format_llm:close">Close</button></div>`;
}

async function loadGlobal() {
  let cfg = {};
  try {
    const res = await api.get("/workflows/" + WORKFLOW_ID + "/config");
    cfg = (res && res.config) || {};
  } catch (e) {
    console.warn("prose_format_llm: config load failed", e);
  }
  const iters = document.getElementById("pf-cfg-iters");
  const mode = document.getElementById("pf-cfg-mode");
  const auto = document.getElementById("pf-cfg-auto");
  const reasoning = document.getElementById("pf-cfg-reasoning");
  const stream = document.getElementById("pf-cfg-stream");
  if (iters) iters.value = Number.isInteger(cfg.max_iterations) ? cfg.max_iterations : 1;
  if (mode) mode.value = cfg.prompt_mode === "extend" ? "extend" : "minimal";
  if (auto) auto.checked = !!cfg.auto_analyze;
  if (reasoning) reasoning.checked = !!cfg.reasoning;
  if (stream) stream.checked = !!cfg.stream_reasoning;
}

// The config slot is replaced wholesale on write, so every key must be sent or an
// omitted one reverts to its default.
async function saveGlobal() {
  const iters = parseInt(document.getElementById("pf-cfg-iters")?.value, 10);
  const config = {
    max_iterations: Number.isFinite(iters) && iters >= 0 ? iters : 1,
    prompt_mode: document.getElementById("pf-cfg-mode")?.value === "extend" ? "extend" : "minimal",
    auto_analyze: !!document.getElementById("pf-cfg-auto")?.checked,
    reasoning: !!document.getElementById("pf-cfg-reasoning")?.checked,
    stream_reasoning: !!document.getElementById("pf-cfg-stream")?.checked,
  };
  try {
    await api.put("/workflows/" + WORKFLOW_ID + "/config", { config });
  } catch (e) {
    console.warn("prose_format_llm: config save failed", e);
  }
}

async function populateSpec() {
  const host = document.getElementById("pf-spec");
  if (!host) return;
  if (!getActiveConvId()) {
    host.innerHTML = `<div class="pf-note">Open a conversation to edit its prose format.</div>`;
    return;
  }
  try {
    applyState(await postAction({ action: "get" }));
  } catch (e) {
    // The trigger route is 404 when the workflow is toggled off.
    host.innerHTML = `<div class="pf-note">Enable this workflow to view or edit its prose format.</div>`;
  }
}

function postAction(body) {
  return api.post(triggerUrl(), body);
}

function applyState(res) {
  const schema = (res && res.schema) || {};
  const values = (res && res.values) || {};
  const names = Object.keys(schema);
  for (const k of Object.keys(values)) if (!names.includes(k)) names.push(k);
  spec = names.map((n) => ({ name: n, description: schema[n] || "", value: values[n] || "" }));
  renderSpec();
}

// Read the editable fields back into the spec array before any mutation or save.
function gather() {
  for (const el of document.querySelectorAll("[data-pf-name]"))
    if (spec[+el.dataset.i]) spec[+el.dataset.i].name = el.value;
  for (const el of document.querySelectorAll("[data-pf-desc]"))
    if (spec[+el.dataset.i]) spec[+el.dataset.i].description = el.value;
  for (const el of document.querySelectorAll("[data-pf-val]"))
    if (spec[+el.dataset.i]) spec[+el.dataset.i].value = el.value;
}

function renderSpec() {
  const host = document.getElementById("pf-spec");
  if (!host) return;
  const schemaRows = spec
    .map(
      (r, i) => `<div class="pf-row">
      <input class="pf-name" type="text" data-pf-name data-i="${i}" value="${esc(r.name)}" placeholder="element">
      <textarea class="pf-field" data-pf-desc data-i="${i}" rows="2" placeholder="how the analyzer should describe this element">${esc(r.description)}</textarea>
      <button class="btn btn-xs btn-danger pf-del" data-wf-action="prose_format_llm:delRow" data-i="${i}">Remove</button>
    </div>`,
    )
    .join("");
  const valueRows = spec
    .map(
      (r, i) => `<div class="pf-row">
      <span class="pf-elem">${esc(r.name) || "(unnamed)"}</span>
      <textarea class="pf-field" data-pf-val data-i="${i}" rows="2" placeholder="recorded format (filled by Analyze)">${esc(r.value)}</textarea>
    </div>`,
    )
    .join("");
  host.innerHTML = `<div class="pf-section-title">Elements (analyzer guidance)</div>
    <div class="pf-rows">${schemaRows}</div>
    <button class="btn btn-sm pf-add" data-wf-action="prose_format_llm:addRow">+ Add element</button>
    <div class="pf-section-title">Recorded format (what gets enforced)</div>
    <div class="pf-rows">${valueRows}</div>
    <div class="pf-actions">
      <button class="btn btn-sm" data-wf-action="prose_format_llm:analyze">Analyze now</button>
      <button class="btn btn-sm" data-wf-action="prose_format_llm:reset">Reset elements to default</button>
      <span id="pf-status" class="pf-status"></span>
      <button class="btn btn-sm btn-accent" data-wf-action="prose_format_llm:saveSpec">Save</button>
    </div>`;
}

function setStatus(msg) {
  const el = document.getElementById("pf-status");
  if (el) el.textContent = msg;
}

function buildMaps() {
  const schema = {};
  const values = {};
  for (const r of spec) {
    const name = (r.name || "").trim();
    if (!name) continue;
    schema[name] = r.description || "";
    values[name] = r.value || "";
  }
  return { schema, values };
}

function addRow() {
  gather();
  spec.push({ name: "", description: "", value: "" });
  renderSpec();
}

function delRow(i) {
  gather();
  spec.splice(i, 1);
  renderSpec();
}

async function saveSpec() {
  gather();
  const { schema, values } = buildMaps();
  try {
    applyState(await postAction({ action: "save", schema, values }));
    setStatus("Saved");
  } catch (e) {
    setStatus("Save failed");
  }
}

async function analyze() {
  gather();
  const { schema, values } = buildMaps();
  setStatus("Analyzing...");
  try {
    // Persist the current guidance first so the analyzer reads the edited schema.
    await postAction({ action: "save", schema, values });
    applyState(await postAction({ action: "analyze" }));
    setStatus("Analyzed");
  } catch (e) {
    setStatus("Analyze failed");
  }
}

async function reset() {
  try {
    applyState(await postAction({ action: "reset" }));
    setStatus("Reset to defaults");
  } catch (e) {
    setStatus("Reset failed");
  }
}
