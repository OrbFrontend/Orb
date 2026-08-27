// Settings entrypoint. Endpoint/model configuration and persona management were
// split into settings_models.js and settings_personas.js; this file keeps the
// theme picker, the loadSettings orchestrator, the agent tools panel, the phrase
// bank, and reset-to-defaults, and re-exports the two sub-modules so existing
// importers (app.js, workflow_loader.js) keep working unchanged.
import { api } from "./api.js";
import { renderInspectorSecondary, renderMessages } from "./chat.js";
import { renderInteractiveFragments } from "./library_fragments.js";
import { closeModal, confirmDelete, showModal, showSubConfirmModal } from "./modal.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { initComboboxes, loadAgentModelConfigs, loadEndpoints, renderEndpoints } from "./settings_models.js";
import { loadPersonas, updateUserBtn } from "./settings_personas.js";
import { effectiveWorkflowEnabled, S } from "./state.js";
import { $, esc, escAttr, formatBytes, toast } from "./utils.js";
import { validate } from "./validate.js";

// Re-export the sub-module public surfaces so "./settings.js" remains the stable
// import path for endpoint/model and persona functions.
export {
  loadAgentModelConfigs,
  loadEndpoints,
  loadModelConfigs,
  onHybridInput,
  renderEndpoints,
  saveAgentSetting,
  saveSetting,
  toggleAgentSameAsWriter,
} from "./settings_models.js";
export {
  activatePersona,
  deletePersona,
  editPersona,
  loadPersonas,
  savePersona,
  saveUserProfile,
  setPersonaCharacterLock,
  setPersonaConversationLock,
  showPersonaEditModal,
  showUserModal,
  updateUserBtn,
} from "./settings_personas.js";

// ── Theme
let _themes = null;

export function applyTheme(name) {
  if (_themes && !_themes.includes(name)) name = "dark";
  $("theme-link").href = `/static/themes/${name}.css`;
  localStorage.setItem("ar-theme", name);
  const sel = $("theme-select");
  if (sel) sel.value = name;
}

export function initTheme() {
  applyTheme(localStorage.getItem("ar-theme") || "dark");
}

export async function initThemeList() {
  const { themes } = await api.get("/themes");
  _themes = themes;
  const sel = $("theme-select");
  if (!sel) return;
  const current = localStorage.getItem("ar-theme") || "dark";
  sel.innerHTML = themes
    .map((t) => `<option value="${t}">${t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}</option>`)
    .join("");
  sel.value = _themes.includes(current) ? current : "dark";
}

// ── Settings
export async function loadSettings() {
  S.settings = await api.get("/settings");
  S.activePersonaId = S.settings.active_persona_id || null;
  S.characterBrowserView = S.settings.character_library_view || "grid";
  S.characterBrowserSort = S.settings.character_library_sort || "time-added";
  if (S.settings.enabled_tools) S.enabledTools = { ...S.enabledTools, ...S.settings.enabled_tools };
  if (typeof S.settings.enable_agent === "number") S.agentEnabled = S.settings.enable_agent !== 0;

  // Per-local-ML-feature config (prose rewriter: variant + gpu + batch size). Kept on
  // S.settings rather than promoted to a top-level S key: this panel and the
  // message toolbar's rewrite-button gate are the only readers, and the panel
  // re-fetches /local-ml/status for the disk facts anyway. Normalised here so a
  // transcript painted before any local-ML write reads a missing selection as
  // absent rather than throwing.
  if (!S.settings.local_ml_config || typeof S.settings.local_ml_config !== "object") S.settings.local_ml_config = {};

  // Length guard is a feature flag, not a tool — its own settings columns, not enabled_tools.
  S.lengthGuardEnabled = Boolean(S.settings.length_guard_enabled);
  S.lengthGuardEnforce = Boolean(S.settings.length_guard_enforce);

  // Agentic Lorebook: a feature flag (not a tool). When on, the Director picks
  // relevant lorebook entries each turn instead of keyword matching. Depends on
  // the Director (direct_scene) being enabled.
  S.agenticLorebookEnabled = Boolean(S.settings.agentic_lorebook_enabled);

  // Editor Feedback: a feature flag (post-writer user-facing note). Gated here
  // and again by at least one enabled feedback-type interactive fragment server-side.
  S.feedbackEnabled = Boolean(S.settings.feedback_enabled);
  S.directorIndividualFragments = Boolean(S.settings.director_individual_fragments);
  S.directionNotesRecord = Boolean(S.settings.direction_notes_record);
  S.directionNotesInject = S.settings.direction_notes_inject || "off";
  updateDirectionNotesButton();

  if (S.settings.length_guard_max_words) S.lengthGuardMaxWords = S.settings.length_guard_max_words;
  if (S.settings.length_guard_max_paragraphs) S.lengthGuardMaxParagraphs = S.settings.length_guard_max_paragraphs;
  if (S.settings.reasoning_enabled_passes)
    S.reasoningEnabled = { ...S.reasoningEnabled, ...S.settings.reasoning_enabled_passes };
  if (S.settings.reasoning_prefill_passes)
    S.reasoningPrefill = { ...S.reasoningPrefill, ...S.settings.reasoning_prefill_passes };

  if (S.settings.inspector_open_states) {
    const ios = S.settings.inspector_open_states;
    if (typeof ios.reasoning === "boolean") S.reasoningOpen = ios.reasoning;
    if (typeof ios.tool_calls === "boolean") S.toolCallsOpen = ios.tool_calls;
    if (typeof ios.injection_block === "boolean") S.injectionBlockOpen = ios.injection_block;
    if (typeof ios.context_size === "boolean") S.contextSizeOpen = ios.context_size;
  }

  if (typeof S.settings.show_editor_diff === "number") S.showEditorDiff = S.settings.show_editor_diff !== 0;
  else if (typeof S.settings.show_editor_diff === "boolean") S.showEditorDiff = S.settings.show_editor_diff;

  if (S.settings.editor_audit_toggles && typeof S.settings.editor_audit_toggles === "object")
    S.editorAuditToggles = { ...S.editorAuditToggles, ...S.settings.editor_audit_toggles };

  if (typeof S.settings.hide_streaming_until_baked === "number")
    S.hideUntilBaked = S.settings.hide_streaming_until_baked !== 0;
  else if (typeof S.settings.hide_streaming_until_baked === "boolean")
    S.hideUntilBaked = S.settings.hide_streaming_until_baked;

  if (typeof S.settings.prevent_prompt_overrides === "number")
    S.preventPromptOverrides = S.settings.prevent_prompt_overrides !== 0;
  else if (typeof S.settings.prevent_prompt_overrides === "boolean")
    S.preventPromptOverrides = S.settings.prevent_prompt_overrides;

  if (typeof S.settings.agent_same_as_writer === "number") S.agentSameAsWriter = S.settings.agent_same_as_writer !== 0;
  else if (typeof S.settings.agent_same_as_writer === "boolean") S.agentSameAsWriter = S.settings.agent_same_as_writer;
  S.agentEndpointId = S.settings.agent_endpoint_id || null;

  if (S.agentEndpointId) {
    await loadAgentModelConfigs(S.agentEndpointId);
  }

  // Expand Endpoints section if endpoint_url is empty
  const endpointsSection = $("endpoints-section");
  if (endpointsSection && (!S.settings.endpoint_url || S.settings.endpoint_url.trim() === "")) {
    const header = endpointsSection.previousElementSibling;
    if (header) {
      const arrow = header.querySelector(".arrow");
      if (arrow) arrow.classList.remove("collapsed");
    }
    endpointsSection.classList.remove("collapsed");
  }

  renderEndpoints();
  renderSettings();
  await loadEndpoints();
  initComboboxes(); // Re-initialize comboboxes with loaded endpoints
  renderToolsPanel();
  await loadPersonas();
  updateUserBtn();
}

// Labelled hairline separator used by the settings panel and its modals.
const divider = (label) =>
  `<div style="display:flex;align-items:center;gap:12px;margin:16px 0 8px"><div style="flex:1;height:1px;background:var(--accent-dim)"></div><span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--accent-dim)">${label}</span><div style="flex:1;height:1px;background:var(--accent-dim)"></div></div>`;

export function renderSettings() {
  $("settings-form").innerHTML = `
    <div class="tool-card ${S.hideUntilBaked ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Hide until baked</span>
        <label class="tog" onclick="event.stopPropagation()">
          <input type="checkbox" ${S.hideUntilBaked ? "checked" : ""} onchange="toggleHideUntilBaked(this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Hide replies until completion.</div>
    </div>
    <div class="tool-card ${S.preventPromptOverrides ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Prevent prompt overrides</span>
        <label class="tog" onclick="event.stopPropagation()">
          <input type="checkbox" ${S.preventPromptOverrides ? "checked" : ""} onchange="togglePreventPromptOverrides(this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Ignore system prompt and post-history instructions from character cards.</div>
    </div>
    ${divider("Local ML")}
    <div id="local-ml-section"><div class="tool-card-desc">Loading…</div></div>
    ${divider("Data")}
    <div class="field" style="display:flex;flex-direction:column;gap:8px">
      <button class="btn btn-block btn-sm" id="cleanup-btn">🧹 Data Hygiene</button>
      <button class="btn btn-block btn-sm" onclick="showPresetsModal()">💾 Backup &amp; Presets</button>
    </div>
  `;
  // Wired here rather than as inline on*= handlers: check_frontend_layers.py
  // ratchets the inline-handler count and it is already at its ceiling.
  $("cleanup-btn").addEventListener("click", showCleanupModal);
  loadLocalMLSection();
}

// Human labels for local-ML features (keys match backend local_ml.MODELS).
const LOCAL_ML_LABELS = {
  autocomplete: "Input Autocomplete",
  slop_classifier: "AI-Slop Classifier",
  emotion_classifier: "Character Expressions",
  pov_classifier: "Image POV",
  prose_rewriter: "Prose Rewriter",
};
const LOCAL_ML_DESCS = {
  autocomplete: "Autocomplete input as you type.",
  slop_classifier: "Unlock AI slop scorer.",
  emotion_classifier: "Track a character's mood with expression images in the avatar popup.",
  pov_classifier: "Auto POV for image-gen.",
  prose_rewriter: "Locally rewrite prose, automatically or on demand.",
};

// Tri-state per feature: deps missing → grayed Download + hint; deps ok & model
// absent → active Download; model present → enable/disable toggle. Fetched fresh
// (not from S.settings) because deps/present are server-filesystem facts.
async function loadLocalMLSection({ expectLoad = false } = {}) {
  stopMlStateWatch();
  const el = $("local-ml-section");
  if (!el) return;
  let st;
  try {
    st = await api.get("/local-ml/status");
  } catch (_e) {
    el.innerHTML = '<div class="tool-card-desc">Could not load Local ML status.</div>';
    return;
  }
  // Deps missing → one grouped opt-in card instead of repeating the install
  // command on every feature.
  if (!st.deps_ok) {
    const names = Object.keys(st.features)
      .map((f) => `<li>${esc(LOCAL_ML_LABELS[f] || f)}</li>`)
      .join("");
    el.innerHTML = `<div class="tool-card" style="opacity:0.5">
      <div class="tool-card-desc">Opt in to unlock:<ul style="margin:4px 0 0;padding-left:18px">${names}</ul></div>
      <div class="tool-card-desc" style="user-select:all;word-break:break-all">${esc(st.install_cmd || "pip install -r requirements-ml.txt")}</div>
    </div>`;
    return;
  }
  el.innerHTML = Object.entries(st.features)
    .map(([f, info]) => (info.variants ? variantCard(f, info) : simpleCard(f, info)))
    .join("");
  wireLocalMLSection(el);
  watchMlStates(st.features, expectLoad);
}

// One enable toggle over one file: the shape every local-ML feature had before
// the rewriter arrived.
function simpleCard(f, info) {
  const name = esc(LOCAL_ML_LABELS[f] || f);
  if (!info.present) {
    return `<div class="tool-card">
      <div class="tool-card-header"><span class="tool-card-name">${name}</span>
        <button class="btn btn-sm" data-ml-act="download" data-ml-feature="${escAttr(f)}">Download</button></div>
      <div class="tool-card-desc">Not downloaded (~${info.size_mb} MB)</div>
    </div>`;
  }
  const desc = LOCAL_ML_DESCS[f] || "";
  return `<div class="tool-card ${info.enabled ? "tool-on" : ""}">
    <div class="tool-card-header"><span class="tool-card-name">${name}</span>
      ${enableToggle(f, info.enabled)}</div>
    ${desc ? `<div class="tool-card-desc">${desc}</div>` : ""}
  </div>`;
}

const enableToggle = (f, on) =>
  `<label class="tog" data-ml-act="stop">
    <input type="checkbox" ${on ? "checked" : ""} data-ml-act="enabled" data-ml-feature="${escAttr(f)}">
    <span class="tog-slider"></span>
  </label>`;

// A feature that ships several interchangeable checkpoints renders as one
// hairline-separated table: a radio to pick, the size, one action, and the
// blurb on its own line beneath the name — a wrapping blurb sharing a row with
// the controls is what made this card ragged. The enable toggle appears only
// once something is on disk: nothing to enable, nothing to say.
//
// The whole table is gated on the runtime, because a checkpoint without the
// llama-server binary rewrites nothing: offering three multi-GB downloads
// first, and the 100 MB that makes them work as a footnote underneath, had it
// exactly backwards. `data-ml-selected` carries the stored pick for the writes
// that happen while no radio is on screen (see `saveLocalMlConfig`).
function variantCard(f, info) {
  const name = esc(LOCAL_ML_LABELS[f] || f);
  const desc = LOCAL_ML_DESCS[f] || "";
  const ready = Boolean(info.runtime_ok);
  const anyPresent = info.variants.some((v) => v.present);
  // One exception to the gate: a checkpoint already on disk keeps its row so
  // its delete button stays reachable — a removed binary must not strand 5 GB.
  const showVariants = ready || anyPresent;
  const rows = info.variants.map((v) => variantRow(f, v, info.selected, ready)).join("");
  return `<div class="tool-card ${ready && anyPresent && info.enabled ? "tool-on" : ""}"
       data-ml-feature="${escAttr(f)}" data-ml-selected="${escAttr(info.selected || "")}">
    <div class="tool-card-header"><span class="tool-card-name">${name}</span>
      ${ready && anyPresent ? enableToggle(f, info.enabled) : ""}</div>
    ${desc ? `<div class="tool-card-desc">${desc}</div>` : ""}
    ${ready ? "" : runtimeGate(f, info)}
    ${showVariants ? `<div class="ml-variants">${rows}</div>` : ""}
    ${ready ? batchSizeControl(f, info) : ""}
    ${ready ? `<div class="ml-foot">${gpuCheck(f, info)}${stateRow(f, info)}</div>` : ""}
  </div>`;
}

// One control, two homes: while the runtime is missing this box picks which
// build to fetch (Vulkan or CPU-only, baked into the binary), and afterwards it
// is the running model's own placement switch. Same setting either way, so it
// is the same checkbox rather than two that could disagree.
const gpuCheck = (f, info, title) =>
  `<label class="lg-enforce-label ml-check" title="${escAttr(title || "Offload the model to the GPU.")}">
    <input type="checkbox" ${info.gpu ? "checked" : ""} data-ml-act="gpu" data-ml-feature="${escAttr(f)}">
    Run on GPU
  </label>`;

// llama.cpp calls these parallel slots; from the user's side it is the number
// of paragraphs in one continuous batch. Each slot owns a full KV lane, so the
// control names the workload and explains the memory consequence together.
function batchSizeControl(f, info) {
  if (!Number.isInteger(info.batch_size)) return "";
  const id = escAttr(`ml-batch-size-${f}`);
  const options = [
    [1, "1 · lowest VRAM"],
    [2, "2"],
    [3, "3"],
    [4, "4 · fastest"],
  ]
    .map(
      ([value, label]) => `<option value="${value}" ${info.batch_size === value ? "selected" : ""}>${label}</option>`,
    )
    .join("");
  return `<div class="ml-batch">
    <label for="${id}">Parallel batch</label>
    <select class="tool-card-select" id="${id}" data-ml-act="batch-size" data-ml-feature="${escAttr(f)}">${options}</select>
    <div>Lower values use less VRAM (~140–190 MB per slot).</div>
  </div>`;
}

// Every cell is placed explicitly by class, so a row missing its radio (an
// undownloaded variant) still lines its name up with the rows that have one.
// Without the runtime a row can only ever be deleted: there is nothing to pick
// between and nothing worth fetching, so the radio goes and Download greys out.
function variantRow(f, v, selected, ready) {
  const attrs = `data-ml-feature="${escAttr(f)}" data-ml-variant="${escAttr(v.id)}"`;
  const rid = escAttr(`ml-var-${f}-${v.id}`);
  const on = ready && v.present && v.id === selected;
  const label = escAttr(v.label);
  const pick =
    ready && v.present
      ? `<input type="radio" id="${rid}" name="local-ml-variant-${escAttr(f)}" ${on ? "checked" : ""}
              aria-label="Use ${label}" data-ml-act="select" ${attrs}>
       <label class="ml-variant-name" for="${rid}">${label}</label>`
      : `<span class="ml-variant-name">${label}</span>`;
  const act = v.present
    ? `<button class="btn btn-xs btn-danger ml-variant-act" title="Delete" aria-label="Delete ${label}"
               data-ml-act="delete" ${attrs}>×</button>`
    : `<button class="btn btn-xs ml-variant-act" data-ml-act="download" ${attrs}
               ${ready ? "" : 'disabled title="Download the llama.cpp runtime first"'}>Download</button>`;
  return `<div class="ml-variant${on ? " ml-variant-on" : ""}">
    ${pick}
    <span class="ml-variant-size">${(v.size_mb / 1024).toFixed(1)} GB</span>
    ${act}
    <div class="ml-variant-detail">${esc(v.detail)}</div>
  </div>`;
}

// The card's whole content while the binary is missing, rather than a footnote
// under the models it is a prerequisite for: it says what it is, why it is
// wanted, and which build to fetch. An installed runtime is the expected state,
// so the box vanishing is its own confirmation.
function runtimeGate(f, info) {
  return `<div class="ml-gate">
    <div class="ml-gate-title">llama.cpp runtime required</div>
    <div class="ml-gate-desc">Rewrites run in a local llama-server. Fetch it to unlock the models.</div>
    <div class="ml-gate-act">
      ${gpuCheck(f, info, "Ticked fetches the Vulkan build; unticked fetches the CPU-only one.")}
      <button class="btn btn-sm" data-ml-act="runtime">Download · 100 MB</button>
    </div>
  </div>`;
}

// `state` is the host's own swap state, so "loading" here is a real model load
// rather than a spinner this panel invented.
function stateRow(f, info) {
  const cls = info.state === "loading" ? " ml-foot-loading" : info.error ? " ml-foot-error" : "";
  return `<span class="ml-foot-state${cls}" id="local-ml-state-${escAttr(f)}">${esc(mlStateText(info))}</span>`;
}

const mlStateText = (info) => `${info.state || "idle"}${info.error ? `: ${info.error}` : ""}`;

// Status is a snapshot, and one field in it is not settled when the snapshot is
// taken: picking a variant, flipping GPU or enabling the feature kicks off a
// background pre-warm that runs for as long as it takes to read 2-5 GB off
// disk. The read that follows the write therefore always says "loading", and
// with nothing re-reading it, that is what the card said forever. So poll while
// a load is in flight — and patch only the state line, never re-render: a
// re-render would wipe the busy row off a download running in the same card.
const ML_STATE_POLL_MS = 1500;
let mlStateTimer = null;

function stopMlStateWatch() {
  if (mlStateTimer !== null) clearTimeout(mlStateTimer);
  mlStateTimer = null;
}

// `expectLoad` covers the race the state field cannot: a pre-warm task that has
// not reached the host yet still reads "idle", so the write paths ask for one
// poll regardless and let its answer decide whether to keep going.
function watchMlStates(features, expectLoad) {
  stopMlStateWatch();
  const loading = Object.values(features).some((info) => info.state === "loading");
  if (!loading && !expectLoad) return;
  mlStateTimer = setTimeout(pollMlStates, ML_STATE_POLL_MS);
}

async function pollMlStates() {
  mlStateTimer = null;
  if (!$("local-ml-section")) return; // panel closed — nothing to write into
  let st;
  try {
    st = await api.get("/local-ml/status");
  } catch (_e) {
    return; // a dropped poll costs nothing; the next render re-reads
  }
  for (const [f, info] of Object.entries(st.features)) {
    const el = $(`local-ml-state-${f}`);
    if (!el) continue;
    el.textContent = mlStateText(info);
    el.classList.toggle("ml-foot-error", Boolean(info.error));
    el.classList.toggle("ml-foot-loading", info.state === "loading");
  }
  watchMlStates(st.features, false);
}

// ONE delegated listener per section, not inline on*= handlers: a variant card
// carries three rows of up to three controls each plus two checkboxes and two
// buttons, and check_frontend_layers.py's inline-handler ratchet is already at
// its ceiling (it may only decrease). Re-attached by marker rather than
// unconditionally, because renderSettings() rebuilds this element from scratch
// while a plain repaint reuses it.
function wireLocalMLSection(el) {
  if (el.dataset.mlWired) return;
  el.dataset.mlWired = "1";
  el.addEventListener("click", onLocalMLClick);
  el.addEventListener("change", onLocalMLChange);
}

async function onLocalMLClick(ev) {
  const target = ev.target.closest("[data-ml-act]");
  if (!target) return;
  const { mlAct: act, mlFeature: feature, mlVariant: variant } = target.dataset;
  if (act === "stop") return ev.stopPropagation();
  if (act === "download") return downloadLocalMlModel(feature, variant, target);
  if (act === "delete") return deleteLocalMlModel(feature, variant);
  if (act === "runtime") return fetchLlamaRuntime(target);
}

function onLocalMLChange(ev) {
  const target = ev.target.closest("[data-ml-act]");
  if (!target) return;
  const { mlAct: act, mlFeature: feature, mlVariant: variant } = target.dataset;
  if (act === "enabled") return toggleLocalMlEnabled(feature, target.checked);
  if (act === "select") return saveLocalMlConfig(feature, { variant });
  if (act === "gpu") return saveLocalMlConfig(feature, { gpu: target.checked });
  if (act === "batch-size") return saveLocalMlConfig(feature, { batchSize: Number(target.value) });
}

// Every local-ML write lands here. Four routes can now move what the message
// toolbar's rewrite button is gated on — the enable toggle moves the flag, and
// config, download and delete can all move the selected variant — so each hands
// its map(s) back and this syncs whichever it was given. The repaint is the
// point: nothing else re-reads S.settings, so without it the transcript keeps
// whatever gate it was painted with until something unrelated re-renders it.
function applyLocalMlResponse(res) {
  if (!res || typeof res !== "object") return;
  if (typeof res.local_ml_enabled === "object") S.settings.local_ml_enabled = res.local_ml_enabled;
  if (typeof res.local_ml_config === "object") S.settings.local_ml_config = res.local_ml_config;
  renderMessages();
}

// The config route takes the whole object, so a change to one field has to
// carry the others: read the current values out of the rendered controls
// rather than keeping a second copy of it in module state. The card's
// `data-ml-selected` stands in for the radios while the runtime gate is up —
// flipping GPU there must not silently clear a variant the user already picked.
async function saveLocalMlConfig(feature, patch) {
  const root = $("local-ml-section");
  const card = root?.querySelector(`.tool-card[data-ml-feature="${feature}"]`);
  const picked = root?.querySelector(`input[name="local-ml-variant-${feature}"]:checked`);
  const gpuBox = root?.querySelector(`input[data-ml-act="gpu"][data-ml-feature="${feature}"]`);
  const batchSelect = root?.querySelector(`select[data-ml-act="batch-size"][data-ml-feature="${feature}"]`);
  const body = {
    variant: patch.variant ?? picked?.dataset.mlVariant ?? (card?.dataset.mlSelected || null),
    gpu: patch.gpu ?? Boolean(gpuBox?.checked),
    batch_size: patch.batchSize ?? Number(batchSelect?.value || 4),
  };
  try {
    applyLocalMlResponse(await api.post(`/local-ml/${feature}/config`, body));
  } catch (e) {
    toast(e.message || "Failed to save", true);
  }
  loadLocalMLSection({ expectLoad: true }); // the config write pre-warms in the background
}

function deleteLocalMlModel(feature, variant) {
  confirmDelete("Model", "Delete this downloaded model file? It can be downloaded again.", async () => {
    try {
      // Deleting the selected checkpoint hands the selection to another one
      // that is present, so the response carries the config back.
      applyLocalMlResponse(
        await api.del(`/local-ml/${feature}/model${variant ? `?variant=${encodeURIComponent(variant)}` : ""}`),
      );
    } catch (e) {
      toast(e.message || "Delete failed", true);
    }
    loadLocalMLSection();
  });
}

// A download is a progressless multi-minute POST, so "working" has to be a state
// of the row rather than a longer word on the button: a "Downloading…" label
// outgrew the fixed action column and printed over the size beside it. The row
// (or the whole card, for a feature with no variant rows) takes the generation
// bar's sliding sliver, and every action in the card goes inert — a second
// multi-GB fetch queued behind the first is not a thing anyone means to ask for.
// Returns the undo, for the failure path; success re-renders the section anyway.
function beginMlBusy(btn) {
  const scope = btn?.closest(".ml-variant, .ml-gate, .tool-card");
  if (!scope) return () => {};
  const others = [...(btn.closest(".tool-card")?.querySelectorAll("button[data-ml-act]") ?? [btn])];
  scope.classList.add("ml-busy");
  scope.setAttribute("aria-busy", "true");
  for (const b of others) b.disabled = true;
  return () => {
    scope.classList.remove("ml-busy");
    scope.removeAttribute("aria-busy");
    for (const b of others) b.disabled = false;
  };
}

async function fetchLlamaRuntime(btn) {
  // Scoped to the card the button is in, not the first GPU box in the section:
  // which archive to fetch is that card's own question, and a second
  // variant-bearing feature would otherwise silently answer it for this one.
  const gpu = btn.closest(".tool-card")?.querySelector('input[data-ml-act="gpu"]');
  const endBusy = beginMlBusy(btn);
  try {
    await api.post("/local-ml/prose_rewriter/runtime", { backend: gpu?.checked === false ? "cpu" : "gpu" });
  } catch (e) {
    toast(e.message || "Runtime download failed", true);
    endBusy();
  }
  loadLocalMLSection();
}

async function downloadLocalMlModel(feature, variant, btn) {
  const endBusy = beginMlBusy(btn);
  try {
    // A downloaded checkpoint arms the feature when nothing usable was
    // selected, so the response carries the (possibly new) selection back.
    applyLocalMlResponse(await api.post(`/local-ml/${feature}/download`, variant ? { variant } : {}));
    await loadLocalMLSection(); // flips the card to a toggle
  } catch (e) {
    toast(e.message || "Download failed", true);
    endBusy();
  }
}

async function toggleLocalMlEnabled(feature, on) {
  try {
    // Enabling also repairs a selection that points at nothing, so the response
    // carries both maps back.
    applyLocalMlResponse(await api.post(`/local-ml/${feature}/enabled`, { enabled: on }));
  } catch (_e) {
    toast("Failed to toggle", true);
  }
  loadLocalMLSection({ expectLoad: on }); // enabling pre-warms; disabling has nothing to wait for
}

// ── Agent Tools Panel
const TOOL_DEFS = [
  {
    id: "direct_scene",
    name: "Direction",
    desc: "Gives written direction and manages fragments based on scene context.",
  },
  {
    id: "editor_apply_patch",
    name: "Output Auditor",
    desc: "Scans for LLM slop and repetition, then surgically patches the draft.",
  },
];

// Individual scanners the Output Auditor can run; keys match backend AUDIT_TYPES.
// Exported so the doc-mode pane (document_audit.js) reuses the same labels and
// tooltips, filtered to its four doc-applicable keys.
export const AUDIT_TYPE_DEFS = [
  { key: "banned_phrases", label: "Banned phrases", title: "Flag phrases from the Phrase Bank." },
  {
    key: "repetitive_openers",
    label: "Repetitive openers",
    title: "Flag many consecutive sentences that start the same way.",
  },
  {
    key: "repetitive_templates",
    label: "Repetitive templates",
    title: "Flag sentences sharing the same structural template.",
  },
  { key: "contrastive_negation", label: "Contrastive negation", title: "Flag `not X, but Y` constructions." },
  { key: "phrase_repetition", label: "Phrase repetition", title: "Flag exact phrases echoed across recent messages." },
  {
    key: "structural_repetition",
    label: "Structural repetition",
    title: "Flag messages that share a similar block structure.",
  },
  {
    key: "anti_echo",
    label: "Anti-echo",
    title: 'Flag questions that parrot the user\'s last message back (e.g. "Ice cream?").',
  },
];

// The single settings-write path. Every toggle in the app funnels through here
// so one failure story ("Failed to save setting") covers them all.
export async function persistSettings(payload) {
  try {
    S.settings = await api.put("/settings", payload);
  } catch (_e) {
    toast("Failed to save setting", true);
  }
}

export function toggleToolsPanel() {
  if (isUtilityPanelOpen("tools-panel")) {
    closeUtilityPanel("tools-panel", "tools-panel-btn");
  } else {
    openUtilityPanel("tools-panel", "tools-panel-btn", renderToolsPanel);
  }
}

export async function setAgentEnabled(on) {
  S.agentEnabled = on;
  $("tools-panel-btn").style.opacity = on ? "1" : "0.5";
  renderToolsPanel();
  await persistSettings({ enable_agent: on });
}

export async function toggleToolEnabled(id, on) {
  S.enabledTools[id] = on;
  renderToolsPanel();
  await persistSettings({ enabled_tools: S.enabledTools });
}

export async function toggleLengthGuard(on) {
  S.lengthGuardEnabled = on;
  renderToolsPanel();
  await persistSettings({ length_guard_enabled: on });
}

export async function toggleLengthGuardEnforce(on) {
  S.lengthGuardEnforce = on;
  renderToolsPanel();
  await persistSettings({ length_guard_enforce: on });
}

export async function toggleAgenticLorebook(on) {
  S.agenticLorebookEnabled = on;
  renderToolsPanel();
  await persistSettings({ agentic_lorebook_enabled: on });
}

export async function toggleFeedbackEnabled(on) {
  S.feedbackEnabled = on;
  renderToolsPanel();
  // Feedback fragments in the sidebar are greyed out when this feature is off.
  renderInteractiveFragments();
  await persistSettings({ feedback_enabled: on });
}

export async function toggleDirectorIndividualFragments(on) {
  S.directorIndividualFragments = on;
  renderToolsPanel();
  await persistSettings({ director_individual_fragments: on });
}

export async function setDirectionNotesRecord(on) {
  S.directionNotesRecord = on;
  renderToolsPanel();
  // Direction-note fragments in the sidebar are greyed out when recording is off.
  renderInteractiveFragments();
  // The per-message add-note button is gated on this switch, so repaint the messages too.
  renderMessages();
  updateDirectionNotesButton();
  await persistSettings({ direction_notes_record: on });
}

export async function setDirectionNotesInject(val) {
  S.directionNotesInject = val;
  renderToolsPanel();
  updateDirectionNotesButton();
  await persistSettings({ direction_notes_inject: val });
}

// The Notes button and its panel only matter while notes are being recorded or injected;
// with both off the feature is dormant, so hide the entry points and close the panel.
function updateDirectionNotesButton() {
  const on = S.directionNotesRecord || S.directionNotesInject !== "off";
  for (const id of ["direction-notes-panel-btn", "mobile-direction-notes-btn"]) {
    const el = $(id);
    if (el) el.classList.toggle("hidden", !on);
  }
  if (!on && isUtilityPanelOpen("direction-notes-panel")) {
    closeUtilityPanel("direction-notes-panel", "direction-notes-panel-btn");
  }
}

export async function toggleShowEditorDiff(on) {
  S.showEditorDiff = on;
  renderMessages();
  renderToolsPanel();
  await persistSettings({ show_editor_diff: on });
}

export async function toggleAuditType(type, on) {
  S.editorAuditToggles = { ...S.editorAuditToggles, [type]: on };
  renderToolsPanel();
  await persistSettings({ editor_audit_toggles: S.editorAuditToggles });
}

export async function toggleHideUntilBaked(on) {
  S.hideUntilBaked = on;
  renderMessages();
  renderSettings();
  await persistSettings({ hide_streaming_until_baked: on });
}

export async function togglePreventPromptOverrides(on) {
  S.preventPromptOverrides = on;
  renderSettings();
  await persistSettings({ prevent_prompt_overrides: on });
}

export async function saveLengthGuardConfig() {
  const words = parseInt($("lg-max-words").value, 10);
  const paras = parseInt($("lg-max-paragraphs").value, 10);
  const wordsValidation = validate.validateSetting("length_guard_max_words", words);
  if (!wordsValidation.valid) {
    toast(wordsValidation.error, true);
    return;
  }
  const parasValidation = validate.validateSetting("length_guard_max_paragraphs", paras);
  if (!parasValidation.valid) {
    toast(parasValidation.error, true);
    return;
  }
  S.lengthGuardMaxWords = words;
  S.lengthGuardMaxParagraphs = paras;
  try {
    S.settings = await api.put("/settings", { length_guard_max_words: words, length_guard_max_paragraphs: paras });
    toast("Length guard saved");
  } catch (_e) {
    toast("Failed to save length guard config", true);
  }
}

// -- Workflow enable/disable toggles (Agents panel, Secondary tab)
//
// Two storage columns back these: workflows_globally_enabled (master) and the
// workflow_enabled {wid: bool} map. The master persists through the normal settings
// PUT; each per-workflow flip goes through its dedicated per-key route (which writes
// only that key), never the full-column settings PUT, so two tabs flipping different
// workflows cannot clobber each other.

export async function toggleWorkflowsGlobal(on) {
  await persistSettings({ workflows_globally_enabled: on });
  renderToolsPanel();
  renderMessages();
  renderInspectorSecondary();
}

export async function toggleWorkflowEnabled(wid, on) {
  try {
    const res = await api.post(`/workflows/${wid}/enabled`, { enabled: on });
    if (res && typeof res.workflow_enabled === "object") S.settings.workflow_enabled = res.workflow_enabled;
  } catch (_e) {
    toast("Failed to toggle workflow", true);
  }
  // Re-render regardless: on success the reassigned map drives the new state; on
  // failure the unchanged stored value reverts the checkbox.
  renderToolsPanel();
  renderMessages();
  renderInspectorSecondary();
}

// One card per manifest workflow, under a master switch, in the Secondary tab.
// Empty when no workflow exists so the panel's "no workflows" fallback still shows.
// Each per-workflow checkbox reflects effective state -- so the master being off
// shows every card unchecked, greyed, and disabled (the dependent-disable pattern)
// while the stored per-workflow value is preserved (the master writes a separate
// column). A workflow that ships a config panel folds it into the same card (one
// entry, not a separate toggle and settings card) -- the registered renderer returns
// the card body (description + any controls), shown only while the workflow is on.
function buildWorkflowToggleRows() {
  if (!S.workflowManifest.length) return "";
  const g = S.settings?.workflows_globally_enabled;
  const globalOn = g === undefined ? true : Boolean(g);

  const masterRow = `<div class="tool-card ${globalOn ? "tool-on" : ""}">
    <div class="tool-card-header">
      <span class="tool-card-name">Secondary Workflows</span>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${globalOn ? "checked" : ""} onchange="toggleWorkflowsGlobal(this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
    <div class="tool-card-desc">Turns all the workflows below on or off at once.</div>
  </div>`;

  const panels = new Map(S.workflowToolsPanelRenderers.map(({ workflowId, render }) => [workflowId, render]));

  const workflowRows = S.workflowManifest
    .map((w) => {
      const effOn = effectiveWorkflowEnabled(w.id);
      let body = "";
      if (!globalOn) {
        body = '<div class="tool-card-desc"><em>Workflows globally off.</em></div>';
      } else if (effOn && panels.has(w.id)) {
        try {
          const piece = panels.get(w.id)();
          if (typeof piece === "string") body = piece;
        } catch (e) {
          console.error("workflow tools-panel renderer threw:", e);
        }
      }
      return `<div class="tool-card ${effOn ? "tool-on" : ""}"${globalOn ? "" : ' style="opacity:0.5"'}>
    <div class="tool-card-header">
      <span class="tool-card-name">${esc(w.display_name || w.id)}</span>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${effOn ? "checked" : ""} ${globalOn ? "" : "disabled"} onchange="toggleWorkflowEnabled('${w.id}', this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
    ${body}
  </div>`;
    })
    .join("");

  return masterRow + workflowRows;
}

export function renderToolsPanel() {
  $("agent-enable-chk").checked = S.agentEnabled;
  $("agent-master-card").classList.toggle("tool-on", S.agentEnabled);
  $("tools-panel-btn").style.opacity = S.agentEnabled ? "1" : "0.5";

  // Agentic Lorebook is independent of Direction (direct_scene): the picks run in
  // their own select_lorebook call, so it works whenever the Agent is on with at
  // least one non-constant lorebook entry.
  const alOn = S.agenticLorebookEnabled;
  const agenticLorebookCard = `<div class="tool-card ${alOn ? "tool-on" : ""}">
    <div class="tool-card-header">
      <span class="tool-card-name">Agentic Lorebook</span>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${alOn ? "checked" : ""} onchange="toggleAgenticLorebook(this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
    <div class="tool-card-desc">Let the Agent pick relevant Lorebook entries each turn.</div>
  </div>`;

  const cardById = {};
  for (const t of TOOL_DEFS) {
    const on = !!S.enabledTools[t.id];
    const auditChecks = AUDIT_TYPE_DEFS.map(
      (a) => `<label class="lg-enforce-label" title="${a.title}">
               <input type="checkbox" ${S.editorAuditToggles[a.key] !== false ? "checked" : ""} onchange="toggleAuditType('${a.key}',this.checked)">
               ${a.label}
             </label>`,
    ).join("");
    let extras = "";
    if (t.id === "editor_apply_patch" && on)
      extras = `<div class="lg-config">
             <div class="audit-types">${auditChecks}</div>
             <label class="lg-enforce-label" title="Highlight edited sentences with green/red strikethrough when the editor pass rewrites the writer's output.">
               <input type="checkbox" ${S.showEditorDiff ? "checked" : ""} onchange="toggleShowEditorDiff(this.checked)">
               Show diff highlights
             </label>
           </div>`;
    else if (t.id === "direct_scene" && on)
      extras = `<div class="lg-config">
             <label class="lg-enforce-label" title="Director fills each interactive fragment in its own LLM call. More focused output; higher latency.">
               <input type="checkbox" ${S.directorIndividualFragments ? "checked" : ""} onchange="toggleDirectorIndividualFragments(this.checked)">
               Individual fragment processing
             </label>
           </div>`;
    cardById[t.id] = `<div class="tool-card ${on ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">${t.name}</span>
        <label class="tog" onclick="event.stopPropagation()">
          <input type="checkbox" ${on ? "checked" : ""} onchange="toggleToolEnabled('${t.id}',this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">${t.desc}</div>
      ${extras}
    </div>`;
  }

  const lgOn = S.lengthGuardEnabled;
  const lgEnforce = S.lengthGuardEnforce;
  const lgConfig = lgOn
    ? `
    <div class="lg-config">
      <div class="lg-config-row">
        <div class="lg-field">
          <label>Max words</label>
          <input id="lg-max-words" type="number" min="50" max="4000" step="50" value="${S.lengthGuardMaxWords}" onchange="saveLengthGuardConfig()">
        </div>
        <div class="lg-field">
          <label>Max paragraphs</label>
          <input id="lg-max-paragraphs" type="number" min="1" max="20" step="1" value="${S.lengthGuardMaxParagraphs}" onchange="saveLengthGuardConfig()">
        </div>
      </div>
      <label class="lg-enforce-label" title="Always suggest max length and paragraphs to the writer.">
        <input type="checkbox" ${lgEnforce ? "checked" : ""} onchange="toggleLengthGuardEnforce(this.checked)">
        Enforce
      </label>
    </div>`
    : "";

  const lengthGuardCard = `<div class="tool-card ${lgOn ? "tool-on" : ""}">
    <div class="tool-card-header">
      <span class="tool-card-name">Length Guard</span>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${lgOn ? "checked" : ""} onchange="toggleLengthGuard(this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
    <div class="tool-card-desc">Reigns the model's response length by word count. MAX PARAGRAPHS is suggested to the AI in rewrite pass.</div>
    ${lgConfig}
  </div>`;

  const fbOn = S.feedbackEnabled;
  const feedbackCard = `<div class="tool-card ${fbOn ? "tool-on" : ""}">
    <div class="tool-card-header">
      <span class="tool-card-name">Editor Feedback</span>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${fbOn ? "checked" : ""} onchange="toggleFeedbackEnabled(this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
    <div class="tool-card-desc">After each reply, surfaces a note to you (e.g. what you could do next). Runs only when at least one interactive fragment has its Field Type set to "feedback".</div>
  </div>`;

  const dnRecord = S.directionNotesRecord === true;
  const dnInject = S.directionNotesInject || "off";
  const directionNotesCard = `<div class="tool-card ${dnRecord || dnInject !== "off" ? "tool-on" : ""}">
    <div class="tool-card-header">
      <span class="tool-card-name">Direction Notes</span>
    </div>
    <div class="dn-config">
      <label>Recording</label>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${dnRecord ? "checked" : ""} onchange="setDirectionNotesRecord(this.checked)">
        <span class="tog-slider"></span>
      </label>
      <label>Injection</label>
      <select class="tool-card-select" onchange="setDirectionNotesInject(this.value)">
        <option value="off" ${dnInject === "off" ? "selected" : ""}>Off</option>
        <option value="director" ${dnInject === "director" ? "selected" : ""}>Director</option>
        <option value="writer" ${dnInject === "writer" ? "selected" : ""}>Writer</option>
        <option value="both" ${dnInject === "both" ? "selected" : ""}>Director and writer</option>
      </select>
    </div>
    <div class="tool-card-desc">Lets the AI keep lasting notes as the story unfolds. <b>Recording</b> saves them; <b>Injection</b> feeds saved notes back to the director, writer, or both.</div>
  </div>`;

  // Grouped by pipeline stage: Director (before the writer) vs Editor (post-writing cleanup).
  const divider = (label) => `<div class="tools-divider"><span>${label}</span></div>`;
  $("tools-list").classList.toggle("workflows-off", !S.agentEnabled);
  $("tools-list").innerHTML =
    divider("Director") +
    cardById.direct_scene +
    agenticLorebookCard +
    directionNotesCard +
    divider("Editor") +
    cardById.editor_apply_patch +
    lengthGuardCard +
    feedbackCard;

  const secEl = $("tools-list-secondary");
  if (secEl) {
    secEl.innerHTML =
      buildWorkflowToggleRows() ||
      `<div style="color:var(--text-muted);font-size:12px;padding:8px 0;">No workflows registered.</div>`;
  }
}

// ── Phrase Bank

export async function showPhraseBankModal() {
  const groups = await api.get("/phrase-bank");

  const groupRows = groups
    .map((g) => {
      const isRegex = g.kind === "regex";
      const body = isRegex
        ? `<code class="phrase-regex-pattern">${esc(g.pattern)}</code>`
        : g.variants.map((v) => `<span class="phrase-variant">${esc(v)}</span>`).join("");
      const count = isRegex ? "regex" : `${g.variants.length} variant${g.variants.length !== 1 ? "s" : ""}`;
      return `
    <div class="phrase-group-item" onclick="editPhraseGroup(${g.id})" data-id="${g.id}">
      <div class="phrase-group-variants">${body}</div>
      <div class="phrase-group-count">${count}</div>
    </div>
  `;
    })
    .join("");

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Phrase Bank</h2>
        <p class="modal-subtitle">Manage banned/overused phrase groups. A group is either a set of equivalent variants or a single regex. Click a group to edit it.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn btn-accent" onclick="showAddPhraseGroupModal()">+ Add Group</button>
      </div>
    </div>

    <div id="phrase-bank-list" class="phrase-bank-list">
      ${groupRows.length ? groupRows : '<div class="phrase-bank-empty">No phrase groups yet</div>'}
    </div>
  `);
}

export function showAddPhraseGroupModal(editId = null, group = null) {
  const isEdit = editId !== null;
  const kind = group?.kind === "regex" ? "regex" : "literal";
  const variants = group?.variants || [];
  const pattern = group?.pattern || "";

  const variantRow = (v = "") => `
    <div class="variant-row">
      <input type="text" class="variant-input" value="${escAttr(v)}" placeholder="e.g., a mix of">
      <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
    </div>`;

  const variantsHtml = variants.map((v) => variantRow(v)).join("");

  const deleteButton = isEdit
    ? `<button class="btn btn-danger" onclick="deletePhraseGroup(${editId})">Delete</button>`
    : "";

  showModal(`
    <h2>${isEdit ? "Edit" : "Add"} Phrase Group</h2>
    <p class="modal-subtitle">A group is either a set of equivalent literal variants <em>or</em> a single regular expression — never both.</p>

    <div class="phrase-mode-toggle" id="phrase-mode-toggle">
      <button type="button" class="phrase-mode-btn ${kind === "literal" ? "active" : ""}" data-mode="literal" onclick="setPhraseGroupMode('literal')">Literal variants</button>
      <button type="button" class="phrase-mode-btn ${kind === "regex" ? "active" : ""}" data-mode="regex" onclick="setPhraseGroupMode('regex')">Regular expression</button>
    </div>

    <div id="phrase-literal-panel" style="display:${kind === "regex" ? "none" : "block"}">
      <div id="variant-list" style="margin-bottom: 15px;">
        ${variantsHtml || variantRow("")}
      </div>
      <button class="btn btn-sm" onclick="addVariantRow()" style="margin-bottom: 20px;">+ Add Another Variant</button>
    </div>

    <div id="phrase-regex-panel" style="display:${kind === "regex" ? "block" : "none"}">
      <input type="text" id="phrase-regex-input" class="variant-input phrase-regex-input" spellcheck="false"
        value="${escAttr(pattern)}" placeholder="e.g., the air (is|was) (thick|heavy|charged)"
        oninput="onPhraseRegexInput()">
      <div id="phrase-regex-error" class="phrase-regex-error"></div>
      <div class="phrase-regex-hint">
        <p style="margin:0 0 6px;">Standard JS regex, matched case-insensitively, one sentence at a time. Common patterns:</p>
        <ul style="list-style:none; margin:0; padding:0;">
          <li style="margin-bottom:3px;"><code>(thick|heavy|charged)</code> &mdash; match any one of these words</li>
          <li style="margin-bottom:3px;"><code>colou?r</code> &mdash; <code>?</code> makes the char before it optional (matches "color" or "colour")</li>
          <li style="margin-bottom:3px;"><code>(ever so )?slightly</code> &mdash; <code>?</code> after a group makes the whole group optional</li>
          <li style="margin-bottom:3px;"><code>\\s+</code> &mdash; flexible spacing (spaces, tabs, newlines)</li>
          <li style="margin-bottom:3px;"><code>\\bword\\b</code> &mdash; whole word only, not inside another</li>
          <li style="margin-bottom:3px;"><code>\\w+</code> &mdash; one word; <code>.*?</code> &mdash; any text in between (shortest match)</li>
          <li style="margin-bottom:3px;"><code>[.,!?]</code> &mdash; any one of the listed characters</li>
          <li style="margin-bottom:3px;"><code>\\.\\.\\.</code> &mdash; escape special chars with <code>\\</code> (here, a literal "...")</li>
        </ul>
      </div>
    </div>

    <div class="modal-actions">
      ${deleteButton}
      <div style="flex:1"></div>
      <button class="btn" onclick="showPhraseBankModal()">Cancel</button>
      <button class="btn btn-accent" id="phrase-save-btn" onclick="savePhraseGroup(${editId || "null"})">${isEdit ? "Update" : "Save"}</button>
    </div>
  `);

  _refreshPhraseSaveState();
}

// Current mode is whichever toggle button carries the `active` class.
function _phraseMode() {
  const active = document.querySelector(".phrase-mode-btn.active");
  return active ? active.dataset.mode : "literal";
}

// Live-validate the regex field and gate the Save/Update button on it.
function _refreshPhraseSaveState() {
  const saveBtn = document.getElementById("phrase-save-btn");
  const errEl = document.getElementById("phrase-regex-error");
  const input = document.getElementById("phrase-regex-input");

  if (_phraseMode() !== "regex") {
    if (errEl) errEl.textContent = "";
    if (input) input.classList.remove("invalid");
    if (saveBtn) saveBtn.disabled = false;
    return;
  }

  const value = input ? input.value : "";
  const result = validate.validatePhraseRegex(value);
  // Only surface an error once the user has actually typed something.
  const showError = !result.valid && value.trim().length > 0;
  if (errEl) errEl.textContent = showError ? result.error : "";
  if (input) input.classList.toggle("invalid", showError);
  if (saveBtn) saveBtn.disabled = !result.valid;
}

// Helper functions exposed to window
window.addVariantRow = () => {
  const container = document.getElementById("variant-list");
  const row = document.createElement("div");
  row.className = "variant-row";
  row.innerHTML = `
    <input type="text" class="variant-input" placeholder="e.g., a mix of">
    <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
  `;
  container.appendChild(row);
  // Focus the new input and scroll it into view
  const input = row.querySelector(".variant-input");
  input.focus();
  // Scroll the modal to show the new row
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
};

window.removeVariantRow = (btn) => {
  const rows = document.querySelectorAll(".variant-row");
  if (rows.length > 1) {
    btn.closest(".variant-row").remove();
  } else {
    // If it's the last row, just clear it
    btn.closest(".variant-row").querySelector(".variant-input").value = "";
  }
};

window.setPhraseGroupMode = (mode) => {
  document.querySelectorAll(".phrase-mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  const literalPanel = document.getElementById("phrase-literal-panel");
  const regexPanel = document.getElementById("phrase-regex-panel");
  if (literalPanel) literalPanel.style.display = mode === "literal" ? "block" : "none";
  if (regexPanel) regexPanel.style.display = mode === "regex" ? "block" : "none";
  _refreshPhraseSaveState();
  if (mode === "regex") {
    const input = document.getElementById("phrase-regex-input");
    if (input) input.focus();
  }
};

window.onPhraseRegexInput = () => _refreshPhraseSaveState();

window.editPhraseGroup = async (groupId) => {
  const groups = await api.get("/phrase-bank");
  const group = groups.find((g) => g.id === groupId);
  if (group) {
    showAddPhraseGroupModal(groupId, group);
  }
};

window.deletePhraseGroup = async (groupId) => {
  confirmDelete("Phrase Group", "Are you sure you want to delete this phrase group?", async () => {
    try {
      await api.del(`/phrase-bank/${groupId}`);
      toast("Phrase group deleted");
      showPhraseBankModal();
    } catch (e) {
      toast(`Failed to delete: ${e.message}`, true);
    }
  });
};

window.savePhraseGroup = async (editId) => {
  const mode = _phraseMode();
  let payload;

  if (mode === "regex") {
    const input = document.getElementById("phrase-regex-input");
    const pattern = input ? input.value.trim() : "";
    const result = validate.validatePhraseRegex(pattern);
    if (!result.valid) {
      toast(result.error, true);
      return;
    }
    payload = { kind: "regex", pattern, variants: [] };
  } else {
    // Exclude the regex field, which shares the .variant-input class.
    const variantInputs = document.querySelectorAll(".variant-input:not(.phrase-regex-input)");
    const rawVariants = Array.from(variantInputs).map((input) => input.value);
    const variants = rawVariants.map((v) => v.trim()).filter((v) => v.length > 0);

    const validation = validate.validatePhraseVariants(rawVariants);
    if (!validation.valid) {
      toast(validation.error, true);
      return;
    }
    if (variants.length === 0) {
      toast("At least one variant is required", true);
      return;
    }
    payload = { kind: "literal", variants, pattern: "" };
  }

  try {
    if (editId && editId !== "null") {
      await api.put(`/phrase-bank/${editId}`, payload);
      toast("Phrase group updated");
    } else {
      await api.post("/phrase-bank", payload);
      toast("Phrase group added");
    }
    showPhraseBankModal(); // Refresh the main modal
  } catch (e) {
    toast(`Failed to save: ${e.message}`, true);
  }
};

// ── Data cleanup ──
//
// Two axes: which categories to clean, and how far back. Sizes are fetched per
// cutoff so the age choice is made against real numbers rather than a guess.
const CLEANUP_AGES = [
  [0, "Now (everything)"],
  [7, "7 days"],
  [30, "30 days"],
  [90, "90 days"],
];

// The cap the LRU-3 eviction in the backend already enforces on every artifact
// write — setting it is what makes artifacts self-trim without pressing anything.
async function saveAttachmentBudget(el) {
  const mb = Math.max(50, Math.round(Number(el.value) || 0));
  el.value = String(mb);
  await persistSettings({ attachment_cache_budget_bytes: mb * 1048576 });
}

export async function showCleanupModal() {
  showModal(`
    <h2>Data Hygiene</h2>
    <div class="field">
      <label class="tool-card-desc" style="display:flex;align-items:center;gap:8px;margin:0">
        <span style="flex:1">Artifact cache limit before auto-eviction</span>
        <input id="attach-budget-mb" type="number" min="50" step="50" style="width:90px"
               value="${Math.round((S.settings?.attachment_cache_budget_bytes ?? 524288000) / 1048576)}"> MB
      </label>
    </div>
    ${divider("Reclaim Space")}
    <div class="field">
      <label for="cleanup-days">Older than</label>
      <select id="cleanup-days">
        ${CLEANUP_AGES.map(([d, label]) => `<option value="${d}">${label}</option>`).join("")}
      </select>
    </div>
    <div class="field" style="display:flex;flex-direction:column;gap:10px">
      <label style="display:flex;gap:8px;align-items:flex-start">
        <input type="checkbox" id="cleanup-artifacts" checked>
        <span>Image &amp; audio artifacts (regenerable)<br><span class="tool-card-desc" id="cleanup-artifacts-size">…</span></span>
      </label>
      <label style="display:flex;gap:8px;align-items:flex-start">
        <input type="checkbox" id="cleanup-logs">
        <span>Agent logs (deleted for good)<br><span class="tool-card-desc" id="cleanup-logs-size">…</span></span>
      </label>
    </div>
    <p class="tool-card-desc" id="cleanup-db">…</p>
    <div class="modal-actions">
      <button class="btn" id="cleanup-cancel">Cancel</button>
      <button class="btn btn-danger" id="cleanup-go">Clean Up</button>
    </div>
    ${divider("Danger Zone")}
    <button class="btn btn-danger" id="cleanup-reset" style="width:100%;justify-content:center">⚠️ Reset to Defaults</button>`);

  // e.target, not the bare handler: saveAttachmentBudget wants the input, and
  // addEventListener hands it an Event (the old wiring silently saved 50 MB).
  $("attach-budget-mb").addEventListener("change", (e) => saveAttachmentBudget(e.target));
  $("cleanup-reset").addEventListener("click", showResetConfirmModal);

  const daysEl = $("cleanup-days");
  // free_bytes is dead space *already* on the freelist, not what this run frees
  // — right after a cleanup it is 0 while the checkboxes still show data, which
  // read as a bug. The estimate is what the boxes select plus that freelist,
  // since the cleanup VACUUMs either way.
  let stats = null;
  const paint = () => {
    if (!stats) return;
    const picked =
      ($("cleanup-artifacts").checked ? stats.artifacts.bytes : 0) + ($("cleanup-logs").checked ? stats.logs.bytes : 0);
    const total = picked + stats.free_bytes;
    $("cleanup-db").textContent = `Database ${formatBytes(stats.db_bytes)} · this cleanup frees ~${formatBytes(total)}`;
    $("cleanup-go").disabled = total === 0;
  };
  const refresh = async () => {
    try {
      stats = await api.get(`/storage?days=${daysEl.value}`);
      $("cleanup-artifacts-size").textContent =
        `${formatBytes(stats.artifacts.bytes)} · ${stats.artifacts.count} items`;
      $("cleanup-logs-size").textContent = `${formatBytes(stats.logs.bytes)} · ${stats.logs.count} entries`;
      paint();
    } catch (_e) {
      toast("Failed to read storage usage", true);
    }
  };

  daysEl.addEventListener("change", refresh);
  $("cleanup-artifacts").addEventListener("change", paint);
  $("cleanup-logs").addEventListener("change", paint);
  $("cleanup-cancel").addEventListener("click", closeModal);
  $("cleanup-go").addEventListener("click", async () => {
    const btn = $("cleanup-go");
    btn.disabled = true;
    btn.textContent = "Cleaning…";
    try {
      const r = await api.post("/storage/cleanup", {
        artifacts: $("cleanup-artifacts").checked,
        logs: $("cleanup-logs").checked,
        days: Number(daysEl.value),
      });
      closeModal();
      // A lost VACUUM race still frees the pages, it just cannot hand them back
      // to the OS until the next boot — say so rather than report a smaller win.
      const tail = r.compacted ? "" : " — disk space is returned on next restart";
      toast(`Freed ${formatBytes(r.bytes_reclaimed)}${tail}`);
      renderMessages();
    } catch (e) {
      toast(`Cleanup failed: ${e.message}`, true);
      btn.disabled = false;
      btn.textContent = "Clean Up";
    }
  });
  await refresh();
}

// ── Reset to Defaults ──

// Sub-modal layer: opened from inside Data Hygiene, so a cancel leaves that
// modal standing instead of tearing it down.
export async function showResetConfirmModal() {
  showSubConfirmModal(
    {
      title: "Reset to Defaults",
      message:
        "This will reset Mood Fragments, Interactive Fragments, Phrase Bank, and all Settings to their original default values. All custom data will be lost.<br><br>The following will be retained: Characters, Conversations, Lorebooks.",
      confirmText: "Reset Everything",
    },
    async () => {
      try {
        await api.post("/reset", { confirm: true });
        toast("Reset successful — reloading…");
        window.location.reload();
      } catch (e) {
        toast(`Failed to reset: ${e.message}`, true);
      }
    },
  );
}
