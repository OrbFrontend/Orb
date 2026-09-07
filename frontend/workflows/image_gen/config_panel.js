import {
  api,
  closeModal,
  esc,
  escAttr,
  getActiveConvId,
  registerAction,
  setModalCloseGuard,
  showModal,
  toast,
} from "/static/workflow_api.js";
import {
  initCharacterProfile,
  populateProfile,
  profileIsDirty,
  resetCharacterProfile,
  saveProfile,
} from "./character_profile.js";
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
  connectionLabel,
  connectionList,
  DEFAULT_PROMPT_FORMAT,
  findConnection,
  graphReferenceSlots,
  MAX_REFERENCE_SLOTS,
  maxCloudReferences,
  normalizePromptFormat,
  PROMPT_FORMATS,
  pendingDisclosures,
  povChoices,
  promptFormatLabel,
  providerTakesReferences,
  sizeChoices,
  styleConnectionId,
} from "./policy.js";

const WORKFLOW_ID = "image_gen";

const REFERENCE_SOURCES = [
  ["previous_or_character", "Previous image, else character references"],
  ["previous", "Previous image in the chat"],
  ["character", "Character references"],
  ["character_and_previous", "Character references and the previous image"],
];
const MAX_USER_GRAPHS = 32;

const DEFAULT_EDGE = 1024;
const styleSize = (style) => [Number(style?.width) || DEFAULT_EDGE, Number(style?.height) || DEFAULT_EDGE];

const SIZE_LABELS = { "1024x1820": "Tall", "1820x1024": "Wide" };
const CLOUD_QUALITIES = [
  ["", "Provider default"],
  ["low", "Low"],
  ["medium", "Medium"],
  ["high", "High"],
];

const promptFormatBadge = (value) => `(${promptFormatLabel(value)})`;

const optionList = (pairs, selected) =>
  pairs
    .map(
      ([value, label]) =>
        `<option value="${escAttr(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`,
    )
    .join("");
let cfg;
let pendingGraph = null;
let backends = { sources: [], providers: [] };
let draft = { styles: [], graphs: [], comfy: {}, connections: {} };
let connections = [];
let modelsByConnection = {};
let probeIds = {};
let pendingConnections = new Set();

export function initConfigPanel(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "settings", () => openSettings());
  registerAction(WORKFLOW_ID, "pickStyle", async (el) => {
    await saveConfigPatch({ default_style: el.value }, "Could not save default style");
    refreshCardReadiness();
  });
  registerAction(WORKFLOW_ID, "pickPov", (el) => saveConfigPatch({ pov_mode: el.value }, "Could not save the camera"));
  registerAction(WORKFLOW_ID, "editStyle", (el) => openSettings(el.dataset.styleId));
  registerAction(WORKFLOW_ID, "settingsClose", () => closeModal());
  registerAction(WORKFLOW_ID, "save", () => saveSettings());
  registerAction(WORKFLOW_ID, "graphFile", (el) => importGraphFile(el));
  registerAction(WORKFLOW_ID, "graphAdd", () => addPendingGraph());
  registerAction(WORKFLOW_ID, "graphRemove", (el) => removeGraph(el.dataset.graphId));
  registerAction(WORKFLOW_ID, "styleAdd", () => addStyle());
  registerAction(WORKFLOW_ID, "styleRemove", (el) => removeStyle(Number(el.dataset.styleIndex)));
  registerAction(WORKFLOW_ID, "styleChange", (el) => refreshStyleState(el));
  registerAction(WORKFLOW_ID, "styleConnection", (el) => relinkStyle(el));
  registerAction(WORKFLOW_ID, "resolutionToggle", (el, event) => toggleResolutionMenu(el, event));
  registerAction(WORKFLOW_ID, "resolutionPick", (el) => pickResolution(el));
  registerAction(WORKFLOW_ID, "connAdd", () => addConnection());
  registerAction(WORKFLOW_ID, "connRemove", (el) => removeConnection(el.dataset.connId));
  registerAction(WORKFLOW_ID, "connChange", (el) => refreshConnectionState(el));
  registerAction(WORKFLOW_ID, "connTest", (el) => testConnection(el.dataset.connId));
  registerAction(WORKFLOW_ID, "connOpen", (el) => revealConnection(el.dataset.connId));
  wireResolutionMenus();
  initCharacterProfile();
}

function query(action, extra) {
  return api.post(`/workflows/${WORKFLOW_ID}/query`, { action, ...extra });
}

let cardReadiness = { text: "", ready: true };
let cardPov = { classifier: true, fallback: "third" };
let cardStyles = [];

function cardStyleOptions() {
  return optionList(
    cardStyles.map((s) => {
      const label = s.label || s.id;
      const connection = connectionLabel(styleConnectionId(s, cfg), backends.providers);
      return [s.id, label === connection ? label : `${label} — ${connection}`];
    }),
    cfg?.default_style || "",
  );
}

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
  return `<div class="tool-card-desc">Generate images on demand with ComfyUI on your machine or through a cloud API.</div>
    <div id="ig-card-config">${configPanelBody()}</div>`;
}

async function saveConfigPatch(patch, failure) {
  Object.assign(cfg, patch);
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: { ...cfg, ...patch } });
    if (res?.config) Object.assign(cfg, res.config);
  } catch {
    toast(failure, "error");
  }
}

export async function refreshCardStyles() {
  try {
    const res = await query("styles");
    cardStyles = Array.isArray(res?.styles) ? res.styles : [];
  } catch {
    cardStyles = [];
  }
  refreshCard();
}

export async function refreshCardReadiness() {
  try {
    const status = await query("status");
    cardReadiness = {
      ready: !!status?.ready,
      text: status?.ready
        ? `Ready — ${status.style_count} style${status.style_count === 1 ? "" : "s"}`
        : status?.detail || "Not configured",
    };
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

const styleField = (name) => `data-ig-field="${name}" data-wf-action="image_gen:styleChange" data-wf-on="change"`;

function checkpointField(value) {
  return modelField(modelPickerState(modelsByConnection[COMFY_CONNECTION], value), {
    attrs: styleField("checkpoint"),
    emptyLabel: "Choose a checkpoint",
    placeholder: "checkpoint.safetensors",
  });
}

function styleModelField(style, connectionId) {
  const preset = providerFor(connectionId);
  return modelField(modelPickerState(modelsByConnection[connectionId], style.model || ""), {
    attrs: styleField("model"),
    emptyLabel: preset?.default_model ? `Default — ${preset.default_model}` : "Choose a model",
    placeholder: preset?.default_model || "model name or ID",
  });
}

function sizeOption(value) {
  const [w, h] = value.split("x").map(Number);
  const shape = SIZE_LABELS[value] || (w === h ? "Square" : w < h ? "Portrait" : "Landscape");
  return [value, `${shape} — ${value}`];
}

function resolutionField(style, { preset = null, comfy = false } = {}) {
  const current = styleSize(style).join("x");
  const choices = sizeChoices(preset, comfy);
  const inputId = `ig-size-${style.id}`;
  const options = choices
    .map((value) => {
      const [, label] = sizeOption(value);
      return `<div class="cb-option" role="option" data-wf-action="image_gen:resolutionPick" data-value="${escAttr(value)}"><span class="cb-option-text">${esc(label)}</span></div>`;
    })
    .join("");
  return `<div class="ig-field"><label for="${escAttr(inputId)}">Resolution</label>
    <div class="cb-root ig-resolution" data-ig-resolution>
      <div class="cb-control">
        <input id="${escAttr(inputId)}" type="text" class="cb-input" ${styleField("size")} value="${escAttr(current)}" placeholder="1024x1024" autocomplete="off" aria-autocomplete="list" aria-controls="${escAttr(inputId)}-list">
        <button type="button" class="cb-arrow ig-resolution-arrow" data-wf-action="image_gen:resolutionToggle" aria-label="Show resolution presets" aria-expanded="false"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="2,4 6,8 10,4"></polyline></svg></button>
      </div>
      <div class="cb-dropdown" hidden><div id="${escAttr(inputId)}-list" class="cb-list" role="listbox">${options}<div class="cb-empty" hidden>No matching resolutions</div></div></div>
    </div>
  </div>`;
}

function resolutionOptions(root) {
  return [...root.querySelectorAll(".cb-option")].filter((option) => !option.hidden);
}

function setResolutionMenuOpen(root, open) {
  root.querySelector(".cb-control")?.classList.toggle("open", open);
  const dropdown = root.querySelector(".cb-dropdown");
  if (dropdown) dropdown.hidden = !open;
  root.querySelector(".ig-resolution-arrow")?.setAttribute("aria-expanded", String(open));
  if (!open) root.querySelector(".cb-option.active")?.classList.remove("active");
}

function closeResolutionMenus(except = null) {
  document.querySelectorAll("[data-ig-resolution]").forEach((root) => {
    if (root !== except) setResolutionMenuOpen(root, false);
  });
}

function filterResolutionMenu(input) {
  const root = input.closest("[data-ig-resolution]");
  if (!root) return;
  const query = input.value.trim().toLowerCase();
  let visible = 0;
  root.querySelectorAll(".cb-option").forEach((option) => {
    option.classList.remove("active");
    option.hidden = !String(option.dataset.value || "")
      .toLowerCase()
      .includes(query);
    if (!option.hidden) visible += 1;
  });
  const empty = root.querySelector(".cb-empty");
  if (empty) empty.hidden = visible > 0;
  closeResolutionMenus(root);
  setResolutionMenuOpen(root, true);
}

function showAllResolutions(root) {
  root.querySelectorAll(".cb-option").forEach((option) => {
    option.hidden = false;
  });
  const empty = root.querySelector(".cb-empty");
  if (empty) empty.hidden = true;
}

function toggleResolutionMenu(el, event) {
  event?.preventDefault();
  const root = el.closest("[data-ig-resolution]");
  const dropdown = root?.querySelector(".cb-dropdown");
  if (!root || !dropdown) return;
  const opening = dropdown.hidden;
  closeResolutionMenus(root);
  if (opening) showAllResolutions(root);
  setResolutionMenuOpen(root, opening);
  root.querySelector(".cb-input")?.focus();
}

function pickResolution(el) {
  const root = el.closest("[data-ig-resolution]");
  const input = root?.querySelector(".cb-input");
  if (!root || !input) return;
  input.value = el.dataset.value || "";
  input.dispatchEvent(new Event("change", { bubbles: true }));
  setResolutionMenuOpen(root, false);
  input.focus();
}

let resolutionMenusWired = false;

function wireResolutionMenus() {
  if (resolutionMenusWired) return;
  resolutionMenusWired = true;
  document.addEventListener("input", (event) => {
    if (event.target.matches?.("[data-ig-resolution] .cb-input")) filterResolutionMenu(event.target);
  });
  document.addEventListener("keydown", (event) => {
    const input = event.target.closest?.("[data-ig-resolution] .cb-input");
    if (!input) return;
    const root = input.closest("[data-ig-resolution]");
    if (event.key === "Escape") {
      setResolutionMenuOpen(root, false);
      return;
    }
    if (!["ArrowDown", "ArrowUp"].includes(event.key) && event.key !== "Enter") return;
    const dropdown = root.querySelector(".cb-dropdown");
    if (dropdown.hidden) {
      if (event.key === "Enter") return;
      showAllResolutions(root);
      closeResolutionMenus(root);
      setResolutionMenuOpen(root, true);
    }
    const options = resolutionOptions(root);
    if (!options.length) return;
    const active = options.findIndex((option) => option.classList.contains("active"));
    if (event.key === "Enter") {
      if (active >= 0) {
        event.preventDefault();
        pickResolution(options[active]);
      }
      return;
    }
    event.preventDefault();
    options.forEach((option) => {
      option.classList.remove("active");
    });
    const next =
      event.key === "ArrowDown" ? (active + 1) % options.length : (active - 1 + options.length) % options.length;
    options[next].classList.add("active");
    options[next].scrollIntoView({ block: "nearest" });
  });
  document.addEventListener("mousedown", (event) => {
    if (!event.target.closest?.("[data-ig-resolution]")) closeResolutionMenus();
  });
}

function graphTakesSize(workflowId) {
  const slots = draft.graphs.find((g) => g.id === workflowId)?.slots;
  return !!(slots?.width && slots?.height);
}

const promptFormatOptions = (value) => optionList(PROMPT_FORMATS, normalizePromptFormat(value));

function styleConnectionOptions(selected) {
  const pairs = connections.map((c) => [c.id, c.label]);
  if (selected && !connections.some((c) => c.id === selected)) pairs.unshift([selected, `${selected} (removed)`]);
  if (!selected) pairs.unshift(["", "Choose a connection"]);
  return optionList(pairs, selected);
}

const styleSource = (style) => String(style?.reference_source || "");

function referenceSelect(selected) {
  return `<select data-ig-field="reference_source" data-wf-action="image_gen:styleChange" data-wf-on="change">${optionList(
    [["", "Off — send prompts only"], ...REFERENCE_SOURCES],
    selected || "",
  )}</select>`;
}

function comfyReferenceFields(style) {
  const slots = graphReferenceSlots(draft.graphs, style.workflow);
  if (!slots.length) return "";
  const one = slots.length === 1;
  return `<div class="ig-heading ig-reference-heading">Reference image</div>
    <div class="image-gen-note">This workflow loads ${one ? "one image" : `${slots.length} images`}. Choose what Orb loads for each style, or leave this off to keep the ${one ? "image" : "images"} exported with the workflow.${one ? "" : " With character references, Orb uses separate character images when available and reuses one when the workflow requires more."}</div>
    <div class="ig-grid"><label>Reference image${referenceSelect(styleSource(style))}</label></div>`;
}

function backendFields(style, connection) {
  if (connection && connection.source === "cloud") return cloudStyleFields(style, connection);
  return `<div class="ig-grid">
      <label>Checkpoint${checkpointField(style.checkpoint || "")}</label>
      <label>Workflow${workflowField(style.workflow || "")}</label>
      ${graphTakesSize(style.workflow) ? resolutionField(style, { comfy: true }) : ""}
    </div>
    ${comfyReferenceFields(style)}`;
}

function cloudStyleFields(style, connection) {
  const preset = connection.preset;
  const source = styleSource(style);
  const quality = preset?.supports_quality
    ? `<label>Quality<select ${styleField("quality")}>${optionList(CLOUD_QUALITIES, style.quality || "")}</select></label>`
    : "";
  const references =
    !preset || preset.supports_references ? `<label>Reference images${referenceSelect(source)}</label>` : "";
  const slots = maxCloudReferences(preset);
  const capacityNote =
    source && providerTakesReferences(preset)
      ? `<div class="image-gen-note">${esc(connection.label)} accepts ${slots === 1 ? "one reference image" : `up to ${slots} reference images`}, one for each character in the scene. If the scene has more characters than available reference slots, the rest are described in the prompt.</div>`
      : "";
  const referenceSizeNote =
    preset?.reference_drives_size && source && providerTakesReferences(preset)
      ? `<div class="image-gen-note">${esc(connection.label)} uses the reference image to determine the output size, so Resolution is ignored when references are enabled.</div>`
      : "";
  return `<div class="ig-grid">
      <label>Model${styleModelField(style, connection.id)}</label>
      ${resolutionField(style, { preset })}
      ${quality}
      ${references}
    </div>
    ${capacityNote}${referenceSizeNote}
    ${compatibilityFields(style, connection)}
    <div class="image-gen-note ig-style-backend">${
      preset?.dimension_mode === "aspect_ratio" ? "Aspect ratio is chosen automatically from the resolution. " : ""
    }The API key for ${esc(connection.label)} is stored in its connection settings.
      <button type="button" class="ig-link" data-wf-action="image_gen:connOpen" data-conn-id="${escAttr(connection.id)}">Edit connection</button></div>`;
}

function compatibilityFields(style, connection) {
  const preset = connection.preset;
  const seed = preset?.supports_seed
    ? `<label class="ig-toggle"><input type="checkbox" ${styleField("send_seed")}${style.send_seed === false ? "" : " checked"}><span class="ig-toggle-label">Seed</span></label>
      <input class="ig-seed-max" ${styleField("seed_max")} inputmode="numeric" value="${escAttr(style.seed_max ?? "")}" placeholder="Max seed (optional)" aria-label="Maximum seed"${style.send_seed === false ? " disabled" : ""}>`
    : "";
  const negative = preset?.supports_negative_prompt
    ? `<label class="ig-toggle"><input type="checkbox" ${styleField("send_negative_prompt")}${style.send_negative_prompt === false ? "" : " checked"}><span class="ig-toggle-label">Negative prompt</span></label>`
    : "";
  if (!seed && !negative) return "";
  return `<details class="ig-advanced ig-compatibility"><summary>Compatibility</summary><div class="ig-compatibility-body">${seed}${negative}</div></details>`;
}

function negativeNote(connection) {
  if (connection?.source !== "cloud") return "";
  if (connection.preset?.supports_negative_prompt !== false) return "";
  return `<div class="image-gen-note">${esc(connection.label)} does not support negative prompts, so this text will not be sent.</div>`;
}

function styleBody(style, index, connection) {
  return `<label>Name<input ${styleField("label")} value="${escAttr(style.label || "")}"></label>
      <div class="ig-grid">
        <label>Connection<select data-ig-field="connection" data-wf-action="image_gen:styleConnection" data-wf-on="change">${styleConnectionOptions(styleConnectionId(style, cfg))}</select></label>
        <label>Prompt format<select ${styleField("prompt_format")}>${promptFormatOptions(style.prompt_format)}</select></label>
      </div>
      <label>Positive style prompt<textarea ${styleField("prompt")} placeholder="Optional style prompt">${esc(style.prompt || "")}</textarea></label>
      <label>Negative style prompt<textarea ${styleField("negative_prompt")} placeholder="Optional negative prompt">${esc(style.negative_prompt || "")}</textarea></label>
      ${negativeNote(connection)}
      <label>Extra instructions<textarea ${styleField("extra_instructions")} placeholder="Optional guidance for the prompter model (e.g. emphasize hand placement and use full-body framing).">${esc(style.extra_instructions || "")}</textarea></label>
      ${backendFields(style, connection)}
      <button class="btn btn-sm ig-danger" data-wf-action="image_gen:styleRemove" data-style-index="${index}">Remove style</button>`;
}

function styleTargetBadge(style, connection) {
  if (connection?.source === "cloud") return style.model || connection.preset?.default_model || "";
  return style.checkpoint || draft.graphs.find((g) => g.id === style.workflow)?.label || "";
}

function styleSummary(style, connection) {
  const id = styleConnectionId(style, cfg);
  return `<span class="ig-style-name">${esc(style.label || style.id)}</span>
      <span class="ig-style-conn${connection?.ready === false ? " ig-unready" : ""}">${esc(connection?.label || id || "No connection")}</span>
      <span class="ig-style-model">${esc(styleTargetBadge(style, connection))}</span>
      <span class="ig-style-format">${promptFormatBadge(style.prompt_format)}</span>`;
}

function styleRows(expandIds = "") {
  const expanded = new Set(Array.isArray(expandIds) ? expandIds : [expandIds]);
  return draft.styles
    .map((s, i) => {
      const connection = findConnection(connections, styleConnectionId(s, cfg));
      return `<details class="ig-style" data-style-index="${i}"${expanded.has(s.id) ? " open" : ""}>
        <summary>${styleSummary(s, connection)}</summary>
        <div class="ig-style-body">${styleBody(s, i, connection)}</div>
      </details>`;
    })
    .join("");
}

function capturedSize(row, style) {
  const [storedW, storedH] = styleSize(style);
  const match = String(row.querySelector('[data-ig-field="size"]')?.value ?? "").match(/^\s*(\d+)\s*[x×]\s*(\d+)\s*$/i);
  return { width: Number(match?.[1]) || storedW, height: Number(match?.[2]) || storedH };
}

function captureStyles() {
  draft.styles = draft.styles.map((s, i) => {
    const row = document.querySelector(`[data-style-index="${i}"]`);
    if (!row) return s;
    const get = (name) => row.querySelector(`[data-ig-field="${name}"]`)?.value ?? "";
    const stored = (name) => row.querySelector(`[data-ig-field="${name}"]`)?.value ?? s[name] ?? "";
    const enabled = (name) => {
      const field = row.querySelector(`[data-ig-field="${name}"]`);
      return field ? field.checked : s[name] !== false;
    };
    return {
      ...s,
      label: get("label").trim() || s.label || s.id,
      connection: stored("connection"),
      prompt_format: normalizePromptFormat(get("prompt_format") || s.prompt_format),
      prompt: get("prompt"),
      negative_prompt: get("negative_prompt"),
      extra_instructions: get("extra_instructions"),
      checkpoint: stored("checkpoint"),
      workflow: stored("workflow"),
      model: stored("model"),
      quality: stored("quality"),
      send_seed: enabled("send_seed"),
      seed_max: stored("seed_max"),
      send_negative_prompt: enabled("send_negative_prompt"),
      reference_source: stored("reference_source"),
      ...capturedSize(row, s),
    };
  });
}

function focusedStyleField() {
  const el = document.activeElement;
  const row = el?.closest?.("[data-style-index]");
  if (!row || !document.querySelector(".ig-styles")?.contains(row)) return null;
  const selector = el.dataset.igField ? `[data-ig-field="${el.dataset.igField}"]` : "";
  if (!selector) return null;
  const caret = typeof el.selectionStart === "number" ? [el.selectionStart, el.selectionEnd] : null;
  return { selector: `[data-style-index="${row.dataset.styleIndex}"] ${selector}`, caret };
}

function restoreStyleFocus(focused) {
  if (!focused) return;
  const el = document.querySelector(focused.selector);
  if (!el) return;
  el.focus({ preventScroll: true });
  if (focused.caret && typeof el.setSelectionRange === "function") el.setSelectionRange(...focused.caret);
}

function renderStyles(expandId = "") {
  const host = document.querySelector(".ig-styles");
  if (!host) return;
  const focused = focusedStyleField();
  host.innerHTML = styleRows(expandId);
  restoreStyleFocus(focused);
}

function addStyle() {
  captureStyles();
  const id = `style_${Date.now().toString(36)}`;
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
    model: previous.model || "",
    width: previous.width || DEFAULT_EDGE,
    height: previous.height || DEFAULT_EDGE,
    quality: previous.quality || "",
    send_seed: previous.send_seed !== false,
    seed_max: previous.seed_max ?? "",
    send_negative_prompt: previous.send_negative_prompt !== false,
    reference_source: styleSource(previous),
  });
  renderStyles(id);
}

function removeStyle(index) {
  captureStyles();
  if (draft.styles.length <= 1) {
    toast("Keep at least one style", "error");
    return;
  }
  const open = openStyleIds();
  const [removed] = draft.styles.splice(index, 1);
  if (removed?.id === draft.default_style) {
    draft.default_style = draft.styles[0].id;
    toast(`${draft.styles[0].label || draft.styles[0].id} is now the default style`);
  }
  renderStyles(open);
}

const STRUCTURAL_STYLE_FIELDS = ["model", "reference_source", "workflow"];

function refreshStyleSummary(row) {
  const style = draft.styles[Number(row?.dataset.styleIndex)];
  const summary = row?.querySelector("summary");
  if (style && summary)
    summary.innerHTML = styleSummary(style, findConnection(connections, styleConnectionId(style, cfg)));
}

function rebuildStyleRow(row) {
  const index = Number(row?.dataset.styleIndex);
  captureStyles();
  const style = draft.styles[index];
  const body = row?.querySelector(".ig-style-body");
  if (!style || !body) return null;
  const connection = findConnection(connections, styleConnectionId(style, cfg));
  const focused = focusedStyleField();
  body.innerHTML = styleBody(style, index, connection);
  restoreStyleFocus(focused);
  refreshStyleSummary(row);
  return connection;
}

function refreshStyleState(el) {
  const row = el.closest("[data-style-index]");
  if (STRUCTURAL_STYLE_FIELDS.includes(el.dataset.igField)) {
    rebuildStyleRow(row);
    return;
  }
  captureStyles();
  if (el.dataset.igField === "send_seed") {
    const maximum = row?.querySelector('[data-ig-field="seed_max"]');
    if (maximum) maximum.disabled = !el.checked;
  }
  refreshStyleSummary(row);
}

function relinkStyle(el) {
  const connection = rebuildStyleRow(el.closest("[data-style-index]"));
  if (connection) loadModels(connection.source === "cloud" ? connection.id : COMFY_CONNECTION);
}

function workflowField(selected) {
  if (!draft.graphs.length) {
    return `<span class="image-gen-note ig-workflow-empty">No workflows found. Import one below under <strong>Imported ComfyUI workflows</strong>.</span>`;
  }
  return `<select data-ig-field="workflow" data-wf-action="image_gen:styleChange" data-wf-on="change">${workflowOptions(selected)}</select>`;
}

function workflowOptions(selected) {
  const known = draft.graphs.some((graph) => graph.id === selected);
  const pairs = draft.graphs.map((graph) => [graph.id, graph.label || graph.id]);
  if (selected && !known) pairs.unshift([selected, `${selected} (not found)`]);
  const placeholder = `<option value="" disabled${selected && known ? "" : " selected"}>Choose a workflow</option>`;
  return placeholder + optionList(pairs, selected);
}

function graphRows() {
  if (!draft.graphs.length) return `<div class="image-gen-note">No workflows imported yet.</div>`;
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
  draft.styles = draft.styles.map((s) => (s.workflow === graphId ? { ...s, workflow: "" } : s));
  const host = document.getElementById("ig-graph-list");
  if (host) host.innerHTML = graphRows();
  renderStyles();
}

function providerFor(id) {
  return backends.providers.find((p) => p.id === id) || null;
}

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

function openConnectionIds() {
  return Array.from(document.querySelectorAll("details.ig-conn[open]")).map((el) => el.dataset.connId);
}

function openStyleIds() {
  return Array.from(document.querySelectorAll(".ig-style[open]"))
    .map((row) => draft.styles[Number(row.dataset.styleIndex)]?.id)
    .filter(Boolean);
}

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
    <div class="image-gen-note">ComfyUI is the built-in local connection and cannot be removed. Styles using a removed cloud connection fall back to ComfyUI.</div>`;
}

function cloudFields(connection) {
  const id = connection.id;
  const entry = draft.connections[id] || {};
  const preset = connection.preset;
  const baseUrl =
    !preset || preset.needs_base_url
      ? `<label>API base URL<input ${connField("base_url")} value="${escAttr(entry.base_url || "")}" placeholder="https://api.example.com/v1"></label>`
      : "";
  const unknown = preset
    ? ""
    : `<div class="image-gen-note ig-unready">Orb no longer recognizes "${esc(id)}". Its credentials are kept, but it cannot generate images. The provider may have been renamed in a later release.</div>`;
  const docs = preset?.docs_url
    ? `<div class="image-gen-note"><a href="${escAttr(preset.docs_url)}" target="_blank" rel="noopener noreferrer">${esc(preset.label)} API documentation</a></div>`
    : "";
  return `${unknown}<div class="ig-grid">
      <label>API key<input type="password" ${connField("api_key")} value="${escAttr(entry.api_key || "")}" placeholder="Paste your key"></label>
      ${baseUrl}
    </div>
    <div class="image-gen-note">Choose the model, resolution, and reference image for each style under <strong>Styles</strong> above.</div>
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

function addRowHtml() {
  const options = addableProviders(connections, backends.providers);
  if (!options.length) return `<span class="image-gen-note">All available providers are already connected.</span>`;
  return `<select id="ig-conn-add">${optionList(options.map((p) => [p.id, p.label]))}</select>
    <button class="btn btn-sm" data-wf-action="image_gen:connAdd">Add connection</button>`;
}

function setupTargets() {
  if (cardReadiness.ready) return [];
  return [connections.find((c) => !c.ready)?.id || COMFY_CONNECTION];
}

const SUMMARY_NAMES = 2;

function connectionSummaryText() {
  const unready = connections.filter((c) => !c.ready).length;
  const hidden = connections.length - SUMMARY_NAMES;
  const shown = connections.slice(0, hidden > 1 ? SUMMARY_NAMES : connections.length).map((c) => c.label);
  const names = hidden > 1 ? `${shown.join(", ")}, and ${hidden} others` : shown.join(", ");
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
    draft.connections[id] = {
      ...entry,
      api_key: get("api_key") ?? entry.api_key ?? "",
      base_url: get("base_url") ?? entry.base_url ?? "",
    };
  }
}

function captureForm() {
  captureStyles();
  captureConnections();
}

function addConnection() {
  const id = document.getElementById("ig-conn-add")?.value;
  if (!id) return;
  captureForm();
  const existing = draft.connections[id] || {};
  draft.connections[id] = {
    ...existing,
    api_key: existing.api_key || "",
    base_url: existing.base_url || "",
  };
  pendingConnections.add(id);
  rebuildConnections();
  renderConnections(id);
  renderStyles(openStyleIds());
}

function removeConnection(id) {
  captureForm();
  delete draft.connections[id];
  delete modelsByConnection[id];
  pendingConnections.delete(id);
  const orphaned = draft.styles.filter((s) => s.connection === id).length;
  draft.styles = draft.styles.map((s) => (s.connection === id ? { ...s, connection: COMFY_CONNECTION } : s));
  rebuildConnections();
  renderConnections();
  renderStyles(openStyleIds());
  if (orphaned) toast(`${orphaned} style${orphaned > 1 ? "s" : ""} moved to ComfyUI`);
}

function refreshConnectionState(el) {
  const row = el.closest("details.ig-conn");
  const id = row?.dataset.connId;
  if (!id) return;
  captureForm();
  rebuildConnections();
  const connection = findConnection(connections, id);
  const detail = row.querySelector(".ig-conn-detail");
  if (detail && connection) {
    detail.textContent = connection.detail || "Not configured";
    detail.classList.toggle("ig-unready", !connection.ready);
  }
  refreshConnectionSummary();
  renderStyles(openStyleIds());
  if (["api_key", "base_url", "api_url"].includes(el.dataset.igConnField)) loadModels(id);
}

function revealConnection(id) {
  const section = document.getElementById("ig-connections");
  if (section) section.open = true;
  const row = document.querySelector(`details.ig-conn[data-conn-id="${CSS.escape(id)}"]`);
  if (!row) return;
  row.open = true;
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  loadModels(id);
}

function configForConnection(id) {
  const next = readConfig();
  const styles = next.styles.map((s) => ({ ...s }));
  const active = styles.find((s) => s.id === next.default_style) || styles[0];
  if (active) active.connection = id;
  return { ...next, styles };
}

function applyModels(id, names) {
  const models = modelPickerState(names).models;
  const previous = modelsByConnection[id];
  if (previous && previous.length === models.length && previous.every((name, i) => name === models[i])) return;
  const openStyles = openStyleIds();
  captureForm();
  modelsByConnection[id] = models;
  rebuildConnections();
  renderStyles(openStyles);
}

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
    if (res?.error) {
      applyModels(id, []);
      if (result) result.textContent = res.error;
      return;
    }
    applyModels(id, res?.models);
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
  probeIds = {};
  modelsByConnection = {};
  pendingConnections = new Set();
  resetCharacterProfile();
  draft = {
    styles: (Array.isArray(cfg.styles) ? cfg.styles : []).map((s) => ({ ...s })),
    graphs: (Array.isArray(ext.user_graphs) ? ext.user_graphs : []).map((g) => ({ ...g })),
    comfy: { api_url: ext.api_url || "", api_key: ext.api_key || "" },
    connections: Object.fromEntries(Object.entries(cloud.providers || {}).map(([id, entry]) => [id, { ...entry }])),
    default_style: cfg.default_style || "",
  };
  rebuildConnections();
  showModal(`<h2>Image Generation</h2><div class="image-gen-settings">
    <section class="ig-section">
      <div class="ig-heading">Styles</div>
      <div class="ig-styles">${styleRows(expandStyleId)}</div>
      <button class="btn btn-sm" data-wf-action="image_gen:styleAdd">Add style</button>
    </section>
    <details class="ig-advanced" id="ig-connections"${cardReadiness.ready ? "" : " open"}>
      <summary>Connections<span class="ig-summary-note" id="ig-conn-summary">${esc(connectionSummaryText())}</span></summary>
      <div class="ig-advanced-body">
        <div class="image-gen-note">Where images render. Every style links to a connection, which can be local or cloud-based. ComfyUI is always available and cannot be removed.</div>
        <div id="ig-conn-list" class="ig-conn-list">${connectionRows(setupTargets())}</div>
        <div id="ig-conn-add-row" class="image-gen-row">${addRowHtml()}</div>
      </div>
    </details>
    ${
      getActiveConvId()
        ? `<section class="ig-section">
      <div class="ig-heading">This Character Only</div>
      <div id="ig-profile" class="image-gen-note">Loading this character's prompt…</div>
    </section>`
        : ""
    }
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
        <div class="image-gen-note">Import a PNG from ComfyUI or an API-format JSON export. Imported workflows run through ComfyUI and remain available no matter which connection a style uses.</div>
        <div id="ig-graph-list" class="ig-graph-list">${graphRows()}</div>
        <input type="file" accept=".json,.png,application/json,image/png" data-wf-action="image_gen:graphFile" data-wf-on="change">
        <div id="ig-graph-picker"></div>
      </div>
    </details>
  </div><div class="modal-actions"><button class="btn" data-wf-action="image_gen:settingsClose">Close</button><button class="btn btn-accent" id="ig-save" data-wf-action="image_gen:save">Save</button></div>`);
  baseline = JSON.stringify(readConfig());
  setModalCloseGuard(() => !isDirty() || window.confirm(DISCARD_MESSAGE));
  populateProfile();
  loadModels(COMFY_CONNECTION);
  const linked = new Set(draft.styles.map((style) => styleConnectionId(style, cfg)));
  for (const connection of connections) {
    if (connection.source === "cloud" && linked.has(connection.id) && draft.connections[connection.id]?.api_key)
      loadModels(connection.id);
  }
}

const DISCARD_MESSAGE = "Discard your unsaved image generation settings?";

let baseline = "";

function isDirty() {
  if (baseline === "") return false;
  return JSON.stringify(readConfig()) !== baseline || profileIsDirty();
}

const MIN_TIMEOUT = 10;
const MAX_TIMEOUT = 900;

function readTimeout() {
  const value = Number(document.getElementById("ig-timeout")?.value);
  if (!Number.isFinite(value) || value <= 0) return 180;
  return Math.min(MAX_TIMEOUT, Math.max(MIN_TIMEOUT, Math.round(value)));
}

function readConfig() {
  captureForm();
  const ext = cfg.external_comfy || {};
  const styles = draft.styles;
  return {
    source: cfg.source || "external_comfy",
    default_style: styles.some((s) => s.id === draft.default_style)
      ? draft.default_style
      : styles[0]?.id || cfg.default_style || "realistic",
    pov_mode: cfg.pov_mode || "auto",
    scene_analysis: document.getElementById("ig-scene-analysis")?.checked === true,
    prompter_reasoning: document.getElementById("ig-prompter-reasoning")?.checked === true,
    timeout_seconds: readTimeout(),
    styles: draft.styles,
    external_comfy: {
      ...ext,
      api_url: draft.comfy.api_url ?? ext.api_url ?? "",
      api_key: draft.comfy.api_key ?? ext.api_key ?? "",
      user_graphs: draft.graphs,
    },
    cloud: { ...(cfg.cloud || {}), providers: { ...draft.connections } },
  };
}

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

async function graphNodeTypes(graph) {
  try {
    const res = await query("node_types", { class_types: classTypes(graph), config: readConfig() });
    return res?.nodes || {};
  } catch {
    return {};
  }
}

function dimensionRows(items) {
  const offered = (edge) => items.filter((item) => item.input.toLowerCase() === edge);
  const edges = [
    ["width", "Width", offered("width")],
    ["height", "Height", offered("height")],
  ];
  if (edges.some(([, , found]) => !found.length)) return "";
  return edges
    .map(
      ([edge, label, found]) =>
        `<label>${label}<select id="ig-slot-${edge}">${candidateOptions(found, -1, "None — use the workflow's setting")}</select></label>`,
    )
    .join("");
}

function referenceRows() {
  const slots = declaredReferenceSlots();
  if (!slots.length) return "";
  const one = slots.length === 1;
  return `<div class="ig-heading ig-reference-heading">Reference images</div>
    <div class="image-gen-note">This workflow loads ${one ? "one image" : `${slots.length} images`}. Choose what Orb loads for each style under <strong>Styles</strong> above, or leave this off to keep the ${one ? "image" : "images"} exported with the workflow.${one ? "" : " With character references, Orb uses separate character images when available and reuses one when the workflow requires more."}</div>
    <ul class="ig-slot-list">${slots.map((item) => `<li>${esc(item.label)}</li>`).join("")}</ul>`;
}

async function importGraphFile(input) {
  const file = input.files?.[0];
  const picker = document.getElementById("ig-graph-picker");
  if (!file || !picker) return;
  try {
    if (draft.graphs.length >= MAX_USER_GRAPHS)
      throw new Error(`You can save up to ${MAX_USER_GRAPHS} imported workflows. Remove one before importing another.`);
    const graph = file.name.toLowerCase().endsWith(".png")
      ? graphFromPng(await file.arrayBuffer())
      : graphFromApiJson(await file.text());
    const candidates = slotCandidates(graph, await graphNodeTypes(graph));
    const missing = missingRoles(candidates);
    if (missing.length) throw new Error(`This workflow is missing: ${missing.join(", ")}.`);
    pendingGraph = { graph, label: file.name.replace(/\.(json|png)$/i, ""), candidates };
    const negative = candidates.text.length > 1 ? 1 : -1;
    const model = candidates.checkpoint.length ? 0 : -1;
    picker.innerHTML = `<div class="image-gen-graph-picker">
      <label>Name<input id="ig-graph-label" value="${escAttr(pendingGraph.label)}"></label>
      <div class="ig-grid">
        <label>Positive prompt<select id="ig-slot-positive">${candidateOptions(candidates.text, 0)}</select></label>
        <label>Negative prompt<select id="ig-slot-negative">${candidateOptions(candidates.text, negative, "None — this workflow has no negative prompt")}</select></label>
        <label>Seed<select id="ig-slot-seed">${candidateOptions(candidates.seed)}</select></label>
        <label>Image output<select id="ig-slot-output">${candidateOptions(candidates.output)}</select></label>
        <label>Model<select id="ig-slot-model">${candidateOptions(candidates.checkpoint, model, "None — keep the workflow's model")}</select></label>
        ${dimensionRows(candidates.dimension)}
      </div>
      ${referenceRows()}
      <button class="btn btn-sm" data-wf-action="image_gen:graphAdd">Confirm slots and add workflow</button>
    </div>`;
  } catch (e) {
    pendingGraph = null;
    picker.innerHTML = `<div class="image-gen-note">${esc(e.message || "Could not import this workflow.")}</div>`;
  }
}

function declaredReferenceSlots() {
  const declared = [];
  for (const item of (pendingGraph?.candidates?.image || []).slice(0, MAX_REFERENCE_SLOTS)) {
    const slot = splitCandidate(item.value);
    if (slot) declared.push({ slot, label: item.label || `${slot[0]} — ${slot[1]}` });
  }
  return declared;
}

function addPendingGraph() {
  if (!pendingGraph) return;
  const positive = splitCandidate(document.getElementById("ig-slot-positive")?.value);
  const negative = splitCandidate(document.getElementById("ig-slot-negative")?.value);
  const seed = splitCandidate(document.getElementById("ig-slot-seed")?.value);
  const output = splitCandidate(document.getElementById("ig-slot-output")?.value);
  const model = splitCandidate(document.getElementById("ig-slot-model")?.value);
  const width = splitCandidate(document.getElementById("ig-slot-width")?.value);
  const height = splitCandidate(document.getElementById("ig-slot-height")?.value);
  if (!positive || !seed || !output) return;
  captureStyles();
  const id = `user_${Date.now().toString(36)}`;
  const label = document.getElementById("ig-graph-label")?.value.trim() || pendingGraph.label;
  const slots = { positive, seed, output };
  if (negative) slots.negative = negative;
  if (model) slots.checkpoint = model;
  if (width && height) {
    slots.width = width;
    slots.height = height;
  }
  const references = declaredReferenceSlots();
  if (references.length) slots.references = references;
  draft.graphs.push({ id, label, graph: pendingGraph.graph, slots });
  const list = document.getElementById("ig-graph-list");
  if (list) list.innerHTML = graphRows();
  renderStyles();
  const picker = document.getElementById("ig-graph-picker");
  if (picker)
    picker.innerHTML = `<div class="image-gen-note">Added ${esc(label)}. Test the connection, then save your settings.</div>`;
  pendingGraph = null;
}

async function saveSettings() {
  const next = readConfig();
  if (!confirmRemotePrivacy(next)) {
    toast("Nothing was saved — approve the connection before generating images", "error");
    return;
  }
  const button = document.getElementById("ig-save");
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: next });
    const stored = res?.config || next;
    const droppedGraphs = next.external_comfy.user_graphs.length - (stored.external_comfy?.user_graphs?.length || 0);
    Object.assign(cfg, stored);
    await saveProfile();
    toast(
      droppedGraphs > 0
        ? `Saved, but ${droppedGraphs} imported workflow${droppedGraphs > 1 ? "s" : ""} could not be saved`
        : "Image generation settings saved",
      droppedGraphs > 0 ? "error" : undefined,
    );
    setModalCloseGuard(null);
    closeModal();
    refreshCardReadiness();
    refreshCardStyles();
  } catch {
    toast("Could not save image generation settings", "error");
  } finally {
    if (button) button.disabled = false;
  }
}

function confirmRemotePrivacy(next) {
  for (const disclosure of pendingDisclosures(next, connectionList(next, backends.providers))) {
    if (localStorage.getItem(disclosure.key) === "acknowledged") continue;
    if (!window.confirm(disclosure.message)) return false;
    localStorage.setItem(disclosure.key, "acknowledged");
  }
  return true;
}
