import { api } from "./api.js";
import { renderInspector, renderInspectorWorkflows, renderMessages } from "./chat.js";
import { CLOSE_ICON } from "./icons.js";
import { renderInteractiveFragments } from "./library_fragments.js";
import { loadInspectorOpenStates } from "./message_inspector.js";
import { closeModal, confirmDelete, setModalDismiss, showModal, showSubConfirmModal } from "./modal.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { loadAgentModelConfigs, loadEndpoints, loadJudgeConfig, renderEndpoints } from "./settings_models.js";
import { loadPersonas, updateUserBtn } from "./settings_personas.js";
import { effectiveWorkflowEnabled, localMlReady, S } from "./state.js";
import { $, esc, escAttr, formatBytes, toast } from "./utils.js";
import { validate } from "./validate.js";

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

let _themes = null;

const DEFAULT_THEME = "camono";

export function applyTheme(name) {
  if (_themes && !_themes.includes(name)) name = DEFAULT_THEME;
  $("theme-link").href = `/static/themes/${name}.css`;
  localStorage.setItem("ar-theme", name);
  const sel = $("theme-select");
  if (sel) sel.value = name;
}

export function initTheme() {
  applyTheme(localStorage.getItem("ar-theme") || DEFAULT_THEME);
}

export async function initThemeList() {
  const { themes } = await api.get("/themes");
  _themes = themes;
  const sel = $("theme-select");
  if (!sel) return;
  const current = localStorage.getItem("ar-theme") || DEFAULT_THEME;
  sel.innerHTML = themes
    .map((t) => `<option value="${t}">${t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}</option>`)
    .join("");
  sel.value = _themes.includes(current) ? current : DEFAULT_THEME;
}

export async function loadSettings() {
  S.settings = await api.get("/settings");
  S.activePersonaId = S.settings.active_persona_id || null;
  S.characterBrowserView = S.settings.character_library_view || "grid";
  S.characterBrowserSort = S.settings.character_library_sort || "time-added";
  if (S.settings.enabled_tools) S.enabledTools = { ...S.enabledTools, ...S.settings.enabled_tools };
  if (typeof S.settings.enable_agent === "number") S.agentEnabled = S.settings.enable_agent !== 0;

  S.lengthGuardEnabled = Boolean(S.settings.length_guard_enabled);
  S.lengthGuardEnforce = Boolean(S.settings.length_guard_enforce);

  S.agenticLorebookEnabled = Boolean(S.settings.agentic_lorebook_enabled);

  S.directorIndividualFragments = Boolean(S.settings.director_individual_fragments);

  if (S.settings.length_guard_max_words) S.lengthGuardMaxWords = S.settings.length_guard_max_words;
  if (S.settings.length_guard_max_paragraphs) S.lengthGuardMaxParagraphs = S.settings.length_guard_max_paragraphs;
  if (S.settings.reasoning_enabled_passes)
    S.reasoningEnabled = { ...S.reasoningEnabled, ...S.settings.reasoning_enabled_passes };
  if (S.settings.reasoning_prefill_passes)
    S.reasoningPrefill = { ...S.reasoningPrefill, ...S.settings.reasoning_prefill_passes };

  loadInspectorOpenStates(S.settings.inspector_open_states);

  if (typeof S.settings.show_editor_diff === "number") S.showEditorDiff = S.settings.show_editor_diff !== 0;
  else if (typeof S.settings.show_editor_diff === "boolean") S.showEditorDiff = S.settings.show_editor_diff;

  if (typeof S.settings.show_chat_avatars === "number") S.showChatAvatars = S.settings.show_chat_avatars !== 0;
  else if (typeof S.settings.show_chat_avatars === "boolean") S.showChatAvatars = S.settings.show_chat_avatars;

  S.inspectorInline = Boolean(S.settings.inspector_inline);

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

  const endpointsSection = $("endpoints-section");
  if (endpointsSection && (!S.settings.endpoint_url || S.settings.endpoint_url.trim() === "")) {
    const header = endpointsSection.previousElementSibling;
    if (header) {
      const arrow = header.querySelector(".arrow");
      if (arrow) arrow.classList.remove("collapsed");
    }
    endpointsSection.classList.remove("collapsed");
  }

  renderSettings();
  await loadEndpoints();
  renderEndpoints();
  // After the endpoints, so the Judge lane can name the endpoint its stored id
  // points at rather than painting a blank URL and then correcting itself.
  loadJudgeConfig();
  renderToolsPanel();
  await loadPersonas();
  updateUserBtn();
}

const divider = (label) =>
  `<div style="display:flex;align-items:center;gap:12px;margin:16px 0 8px"><div style="flex:1;height:1px;background:var(--accent-dim)"></div><span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--accent-dim)">${label}</span><div style="flex:1;height:1px;background:var(--accent-dim)"></div></div>`;

export function renderSettings() {
  $("settings-form").innerHTML = `
    <div class="tool-card ${S.hideUntilBaked ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Hide until baked</span>
        <label class="tog" data-setting-stop>
          <input type="checkbox" ${S.hideUntilBaked ? "checked" : ""} data-setting-toggle="hideUntilBaked">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Hide replies until completion.</div>
    </div>
    <div class="tool-card ${S.showChatAvatars ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Show avatars in chat</span>
        <label class="tog" data-setting-stop>
          <input type="checkbox" ${S.showChatAvatars ? "checked" : ""} data-setting-toggle="showChatAvatars">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Show the speaker's portrait beside each message.</div>
    </div>
    <div class="tool-card ${S.inspectorInline ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Show Inspector in chat</span>
        <label class="tog" data-setting-stop>
          <input type="checkbox" ${S.inspectorInline ? "checked" : ""} data-setting-toggle="inspectorInline">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Show turn details above chatbox rather than in side panel.</div>
    </div>
    <div class="tool-card ${S.preventPromptOverrides ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Prevent prompt overrides</span>
        <label class="tog" data-setting-stop>
          <input type="checkbox" ${S.preventPromptOverrides ? "checked" : ""} data-setting-toggle="preventPromptOverrides">
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
  $("cleanup-btn").addEventListener("click", showCleanupModal);
  wireSettingsToggles($("settings-form"));
  loadLocalMLSection();
}

const SETTING_TOGGLES = {
  hideUntilBaked: toggleHideUntilBaked,
  showChatAvatars: toggleShowChatAvatars,
  inspectorInline: toggleInspectorInline,
  preventPromptOverrides: togglePreventPromptOverrides,
};

function wireSettingsToggles(el) {
  if (el.dataset.togglesWired) return;
  el.dataset.togglesWired = "1";
  el.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-setting-stop]")) ev.stopPropagation();
  });
  el.addEventListener("change", (ev) => {
    const input = ev.target.closest("[data-setting-toggle]");
    if (input) SETTING_TOGGLES[input.dataset.settingToggle]?.(input.checked);
  });
}

const LOCAL_ML_LABELS = {
  autocomplete: "Input Autocomplete",
  slop_classifier: "AI-Slop Classifier",
  emotion_classifier: "Character Expressions",
  pov_classifier: "Auto-POV",
  markup_classifier: "Markup Classifier",
};
const LOCAL_ML_DESCS = {
  autocomplete: "Autocomplete input as you type.",
  slop_classifier: "Unlock AI slop scorer.",
  emotion_classifier: "Track a character's mood with expression images.",
  pov_classifier: "For image-gen and format consistency.",
  markup_classifier: "For more accurate format consistency.",
};

// Models with a single consumer are managed by it: Spark-TTS and the speech
// recognizer in the TTS cloned-voice control, the Prose Rewriter in its
// workflow card.
const LOCAL_ML_MANAGED_ELSEWHERE = new Set([
  "spark_tts_llm",
  "spark_tts_codec",
  "spark_tts_reference",
  "speech_recognizer",
  "prose_rewriter",
]);

const settingsFeatures = (features) =>
  Object.fromEntries(Object.entries(features).filter(([f]) => !LOCAL_ML_MANAGED_ELSEWHERE.has(f)));

/** Publish local model status and repaint dependent surfaces. */
function publishLocalMlFeatures(features) {
  const before = mlReadySignature();
  S.localMlFeatures = features || {};
  if (mlReadySignature() === before) return;
  renderToolsPanel();
  if (!S.isStreaming) renderMessages(); // the prose rewrite button gates on it
}

const mlReadySignature = () =>
  Object.keys(S.localMlFeatures)
    .sort()
    .map((f) => `${f}:${localMlReady(f) ? 1 : 0}`)
    .join(",");

/** Fetch local model status and publish it to every gate that reads it. */
export async function refreshLocalMlStatus() {
  const st = await api.get("/local-ml/status");
  publishLocalMlFeatures(st.features);
  return st;
}

async function loadLocalMLSection() {
  const el = $("local-ml-section");
  if (!el) return;
  let st;
  try {
    st = await refreshLocalMlStatus(); // every feature: gates elsewhere read the ones not shown here
  } catch (_e) {
    el.innerHTML = '<div class="tool-card-desc">Could not load Local ML status.</div>';
    return;
  }
  const shown = settingsFeatures(st.features);
  if (!st.deps_ok) {
    const names = Object.keys(shown)
      .map((f) => `<li>${esc(LOCAL_ML_LABELS[f] || f)}</li>`)
      .join("");
    el.innerHTML = `<div class="tool-card" style="opacity:0.5">
      <div class="tool-card-desc">Opt in to unlock:<ul style="margin:4px 0 0;padding-left:18px">${names}</ul></div>
      <div class="tool-card-desc" style="user-select:all;word-break:break-all">${esc(st.install_cmd || "pip install -r requirements-ml.txt")}</div>
    </div>`;
    return;
  }
  el.innerHTML = Object.entries(shown)
    .map(([f, info]) => localMlCard(f, info))
    .join("");
  wireLocalMLSection(el);
}

function localMlCard(f, info) {
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
      <label class="tog" data-ml-act="stop">
        <input type="checkbox" ${info.enabled ? "checked" : ""} data-ml-act="enabled" data-ml-feature="${escAttr(f)}">
        <span class="tog-slider"></span>
      </label></div>
    ${desc ? `<div class="tool-card-desc">${desc}</div>` : ""}
  </div>`;
}

function wireLocalMLSection(el) {
  if (el.dataset.mlWired) return;
  el.dataset.mlWired = "1";
  el.addEventListener("click", onLocalMLClick);
  el.addEventListener("change", onLocalMLChange);
}

function onLocalMLClick(ev) {
  const target = ev.target.closest("[data-ml-act]");
  if (!target) return;
  const { mlAct: act, mlFeature: feature } = target.dataset;
  if (act === "stop") return ev.stopPropagation();
  if (act === "download") return downloadLocalMlModel(feature, target);
}

function onLocalMLChange(ev) {
  const target = ev.target.closest("[data-ml-act]");
  if (target?.dataset.mlAct === "enabled") return toggleLocalMlEnabled(target.dataset.mlFeature, target.checked);
}

function applyLocalMlResponse(res) {
  if (!res || typeof res !== "object") return;
  if (typeof res.local_ml_enabled === "object") S.settings.local_ml_enabled = res.local_ml_enabled;
  renderMessages();
}

async function downloadLocalMlModel(feature, btn) {
  const card = btn.closest(".tool-card");
  card?.classList.add("ml-busy");
  card?.setAttribute("aria-busy", "true");
  btn.disabled = true;
  try {
    applyLocalMlResponse(await api.post(`/local-ml/${feature}/download`, {}));
    await loadLocalMLSection(); // flips the card to a toggle
  } catch (e) {
    toast(e.message || "Download failed", true);
    card?.classList.remove("ml-busy");
    card?.removeAttribute("aria-busy");
    btn.disabled = false;
  }
}

async function toggleLocalMlEnabled(feature, on) {
  try {
    applyLocalMlResponse(await api.post(`/local-ml/${feature}/enabled`, { enabled: on }));
  } catch (_e) {
    toast("Failed to toggle", true);
  }
  loadLocalMLSection();
}

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
  renderInteractiveFragments();
  await persistSettings({ enable_agent: on });
}

export async function toggleToolEnabled(id, on) {
  S.enabledTools[id] = on;
  renderToolsPanel();
  // Direction gates before-Writer state updates, which the fragment list notes.
  renderInteractiveFragments();
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

export async function toggleDirectorIndividualFragments(on) {
  S.directorIndividualFragments = on;
  renderToolsPanel();
  await persistSettings({ director_individual_fragments: on });
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

export async function toggleShowChatAvatars(on) {
  S.showChatAvatars = on;
  renderMessages();
  renderSettings();
  await persistSettings({ show_chat_avatars: on });
}

export async function toggleInspectorInline(on) {
  S.inspectorInline = on;
  renderMessages();
  renderInspector();
  renderSettings();
  await persistSettings({ inspector_inline: on });
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

export async function toggleWorkflowsGlobal(on) {
  await persistSettings({ workflows_globally_enabled: on });
  renderToolsPanel();
  renderMessages();
  renderInspectorWorkflows();
}

export async function toggleWorkflowEnabled(wid, on) {
  try {
    const res = await api.post(`/workflows/${wid}/enabled`, { enabled: on });
    if (res && typeof res.workflow_enabled === "object") S.settings.workflow_enabled = res.workflow_enabled;
  } catch (_e) {
    toast("Failed to toggle workflow", true);
  }
  renderToolsPanel();
  renderMessages();
  renderInspectorWorkflows();
}

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
    <div class="tool-card-desc">Toggle everything below.</div>
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
    <div class="tool-card-desc">Reigns the final response length by word count. MAX PARAGRAPHS is suggested to the Writer in rewrite pass.</div>
    ${lgConfig}
  </div>`;

  const divider = (label) => `<div class="tools-divider"><span>${label}</span></div>`;
  $("tools-list").classList.toggle("workflows-off", !S.agentEnabled);
  $("tools-list").innerHTML =
    divider("Director") +
    cardById.direct_scene +
    agenticLorebookCard +
    divider("Editor") +
    cardById.editor_apply_patch +
    lengthGuardCard;

  const secEl = $("tools-list-secondary");
  if (secEl) {
    secEl.innerHTML =
      buildWorkflowToggleRows() ||
      `<div style="color:var(--text-muted);font-size:12px;padding:8px 0;">No workflows registered.</div>`;
  }
}

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
        <button class="btn btn-sm" onclick="showAddPhraseGroupModal()">+ New group</button>
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
      <button class="btn btn-xs btn-danger btn-square" onclick="removeVariantRow(this)" title="Remove" aria-label="Remove variant">${CLOSE_ICON}</button>
    </div>`;

  const variantsHtml = variants.map((v) => variantRow(v)).join("");

  const deleteButton = isEdit
    ? `<button class="btn btn-danger" onclick="deletePhraseGroup(${editId})">Delete</button>`
    : "";

  showModal(`
    <h2>${isEdit ? "Edit" : "New"} phrase group</h2>
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
      <button class="btn" onclick="showPhraseBankModal()">Cancel</button>
      <button class="btn btn-accent" id="phrase-save-btn" onclick="savePhraseGroup(${editId || "null"})">${isEdit ? "Save" : "Create"}</button>
    </div>
  `);
  setModalDismiss(showPhraseBankModal);

  _refreshPhraseSaveState();
}

function _phraseMode() {
  const active = document.querySelector(".phrase-mode-btn.active");
  return active ? active.dataset.mode : "literal";
}

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
  const showError = !result.valid && value.trim().length > 0;
  if (errEl) errEl.textContent = showError ? result.error : "";
  if (input) input.classList.toggle("invalid", showError);
  if (saveBtn) saveBtn.disabled = !result.valid;
}

window.addVariantRow = () => {
  const container = document.getElementById("variant-list");
  const row = document.createElement("div");
  row.className = "variant-row";
  row.innerHTML = `
    <input type="text" class="variant-input" placeholder="e.g., a mix of">
    <button class="btn btn-xs btn-danger btn-square" onclick="removeVariantRow(this)" title="Remove" aria-label="Remove variant">${CLOSE_ICON}</button>
  `;
  container.appendChild(row);
  const input = row.querySelector(".variant-input");
  input.focus();
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
};

window.removeVariantRow = (btn) => {
  const rows = document.querySelectorAll(".variant-row");
  if (rows.length > 1) {
    btn.closest(".variant-row").remove();
  } else {
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
  confirmDelete("phrase group", "Delete this phrase group? This cannot be undone.", async () => {
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

const CLEANUP_AGES = [
  [0, "Now (everything)"],
  [7, "7 days"],
  [30, "30 days"],
  [90, "90 days"],
];

async function saveAttachmentBudget(el) {
  const mb = Math.max(50, Math.round(Number(el.value) || 0));
  el.value = String(mb);
  await persistSettings({ attachment_cache_budget_bytes: mb * 1048576 });
}

export async function showCleanupModal() {
  showModal(`
    <h2>Data Hygiene</h2>
    <div class="field">
      <label for="attach-budget-mb">Artifact cache limit before auto-eviction (MB)</label>
      <input id="attach-budget-mb" type="number" min="50" step="50"
             value="${Math.round((S.settings?.attachment_cache_budget_bytes ?? 524288000) / 1048576)}">
    </div>
    <div class="modal-heading" role="heading" aria-level="3">Reclaim space</div>
    <div class="field">
      <label for="cleanup-days">Older than</label>
      <select id="cleanup-days">
        ${CLEANUP_AGES.map(([d, label]) => `<option value="${d}">${label}</option>`).join("")}
      </select>
    </div>
    <div class="field cleanup-targets">
      <label class="modal-checkbox-label">
        <input type="checkbox" id="cleanup-artifacts" checked>
        <span>Image &amp; audio artifacts (regenerable)<span class="cleanup-size" id="cleanup-artifacts-size">…</span></span>
      </label>
      <label class="modal-checkbox-label">
        <input type="checkbox" id="cleanup-logs">
        <span>Agent logs (deleted for good)<span class="cleanup-size" id="cleanup-logs-size">…</span></span>
      </label>
    </div>
    <div class="modal-actions">
      <span class="modal-action-status" id="cleanup-db" role="status">…</span>
      <button class="btn btn-danger" id="cleanup-go">Clean Up</button>
    </div>
    <div class="modal-heading" role="heading" aria-level="3">Danger zone</div>
    <button class="btn btn-danger btn-block" id="cleanup-reset">⚠️ Reset to Defaults</button>`);

  $("attach-budget-mb").addEventListener("change", (e) => saveAttachmentBudget(e.target));
  $("cleanup-reset").addEventListener("click", showResetConfirmModal);

  const daysEl = $("cleanup-days");
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

export async function showResetConfirmModal() {
  showSubConfirmModal(
    {
      title: "Reset to Defaults",
      message:
        "This will reset Mood Fragments, Interactive Fragments, Phrase Bank, and all Settings to their original default values. All custom data will be lost. Characters, conversations and lorebooks are kept.",
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
