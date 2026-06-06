// Settings entrypoint. Endpoint/model configuration and persona management were
// split into settings_models.js and settings_personas.js; this file keeps the
// theme picker, the loadSettings orchestrator, the agent tools panel, the phrase
// bank, and reset-to-defaults, and re-exports the two sub-modules so existing
// importers (app.js, workflow_loader.js) keep working unchanged.
import { api } from "./api.js";
import { renderMessages } from "./chat.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { initComboboxes, loadAgentModelConfigs, loadEndpoints, renderEndpoints } from "./settings_models.js";
import { loadPersonas, updateUserBtn } from "./settings_personas.js";
import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";
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
  showPersonaEditModal,
  showUserModal,
  updateUserBtn,
} from "./settings_personas.js";

// ── Theme
let _themes = null;

export function applyTheme(name) {
  if (_themes && !_themes.includes(name)) name = "dark";
  $("theme-link").href = "/static/themes/" + name + ".css";
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

  // Length guard is a feature flag, not a tool — its own settings columns, not enabled_tools.
  S.lengthGuardEnabled = Boolean(S.settings.length_guard_enabled);
  S.lengthGuardEnforce = Boolean(S.settings.length_guard_enforce);

  if (S.settings.length_guard_max_words) S.lengthGuardMaxWords = S.settings.length_guard_max_words;
  if (S.settings.length_guard_max_paragraphs) S.lengthGuardMaxParagraphs = S.settings.length_guard_max_paragraphs;
  if (S.settings.reasoning_enabled_passes)
    S.reasoningEnabled = { ...S.reasoningEnabled, ...S.settings.reasoning_enabled_passes };

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
    <div class="field" style="margin-top:16px;padding-top:16px;border-top:1px solid var(--accent-dim);display:flex;flex-direction:column;gap:8px">
      <button class="btn btn-block btn-sm" onclick="showPresetsModal()">💾 Backup &amp; Presets</button>
      <button class="btn btn-danger" onclick="showResetConfirmModal()" style="width:100%;justify-content:center">⚠️ Reset to Defaults</button>
    </div>
  `;
}

// ── Agent Tools Panel
const TOOL_DEFS = [
  {
    id: "direct_scene",
    name: "Director",
    desc: "Gives written direction and selects active mood fragments based on scene context",
  },
  {
    id: "rewrite_user_prompt",
    name: "Prompt Rewriter",
    desc: "Expands user's vague or lazy messages into richer input",
  },
  {
    id: "editor_apply_patch",
    name: "Output Auditor",
    desc: "Scans for LLM slop and repetition, then surgically patches the draft",
  },
];

// Individual scanners the Output Auditor can run; keys match backend AUDIT_TYPES.
const AUDIT_TYPE_DEFS = [
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
];

async function persistSettings(payload) {
  try {
    S.settings = await api.put("/settings", payload);
  } catch (e) {
    toast("Failed to save setting", true);
  }
}

export function toggleToolsPanel() {
  const panel = $("tools-panel");
  const inspector = $("inspector");
  const btn = $("tools-panel-btn");
  const inspectorBtn = $("inspector-toggle");
  const wasOpen = panel.classList.contains("open");
  const switching = !wasOpen && inspector.classList.contains("open");

  if (wasOpen) {
    panel.classList.remove("open");
    btn.classList.remove("btn-active");
  } else if (switching) {
    // Both panels are the same width: swap instantly with no slide animation.
    panel.classList.add("no-anim");
    inspector.classList.add("no-anim");
    inspector.classList.remove("open");
    inspectorBtn.classList.remove("btn-active");
    panel.classList.add("open");
    btn.classList.add("btn-active");
    renderToolsPanel();
    // Force a synchronous reflow so the swapped state is committed with
    // transitions disabled before we re-enable them.
    void panel.offsetWidth;
    panel.classList.remove("no-anim");
    inspector.classList.remove("no-anim");
  } else {
    panel.classList.add("open");
    btn.classList.add("btn-active");
    renderToolsPanel();
  }
}

export async function setAgentEnabled(on) {
  S.agentEnabled = on;
  $("tools-panel-btn").style.opacity = on ? "1" : "0.5";
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
  } catch (e) {
    toast("Failed to save length guard config", true);
  }
}

export function renderToolsPanel() {
  $("agent-enable-chk").checked = S.agentEnabled;
  $("tools-panel-btn").style.opacity = S.agentEnabled ? "1" : "0.5";
  const toolCards = TOOL_DEFS.map((t) => {
    const on = !!S.enabledTools[t.id];
    const auditChecks = AUDIT_TYPE_DEFS.map(
      (a) => `<label class="lg-enforce-label" title="${a.title}">
               <input type="checkbox" ${S.editorAuditToggles[a.key] !== false ? "checked" : ""} onchange="toggleAuditType('${a.key}',this.checked)">
               ${a.label}
             </label>`,
    ).join("");
    const extras =
      t.id === "editor_apply_patch" && on
        ? `<div class="lg-config">
             <div class="audit-types">${auditChecks}</div>
             <label class="lg-enforce-label" title="Highlight edited sentences with green/red strikethrough when the editor pass rewrites the writer's output.">
               <input type="checkbox" ${S.showEditorDiff ? "checked" : ""} onchange="toggleShowEditorDiff(this.checked)">
               Show diff highlights
             </label>
           </div>`
        : "";
    return `<div class="tool-card ${on ? "tool-on" : ""}">
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
  }).join("");

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

  $("tools-list").innerHTML = toolCards + lengthGuardCard;

  const secEl = $("tools-list-secondary");
  if (secEl) {
    let secHtml = "";
    for (const fn of S.workflowToolsPanelRenderers) {
      try {
        const piece = fn();
        if (typeof piece === "string" && piece) secHtml += piece;
      } catch (e) {
        console.error("workflow tools-panel renderer threw:", e);
      }
    }
    secEl.innerHTML =
      secHtml || `<div style="color:var(--text-muted);font-size:12px;padding:8px 0;">No workflows registered.</div>`;
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
      const count = isRegex
        ? `<span class="phrase-kind-badge">regex</span>`
        : `${g.variants.length} variant${g.variants.length !== 1 ? "s" : ""}`;
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
      <input type="text" class="variant-input" value="${esc(v)}" placeholder="e.g., a mix of">
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
        value="${esc(pattern)}" placeholder="e.g., the air (is|was) (thick|heavy|charged)"
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
  showConfirmModal(
    {
      title: "Delete Phrase Group",
      message: "Are you sure you want to delete this phrase group?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del(`/phrase-bank/${groupId}`);
        toast("Phrase group deleted");
        showPhraseBankModal();
      } catch (e) {
        toast("Failed to delete: " + e.message, true);
      }
    },
  );
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
    toast("Failed to save: " + e.message, true);
  }
};

// ── Reset to Defaults ──

export async function showResetConfirmModal() {
  showConfirmModal(
    {
      title: "Reset to Defaults",
      message:
        "This will reset Mood Fragments, Director Fragments, Phrase Bank, and all Settings to their original default values. All custom data will be lost.<br><br>The following will be retained: Characters, Conversations, Lorebooks.",
      confirmText: "Reset Everything",
    },
    async () => {
      try {
        await api.post("/reset", { confirm: true });
        toast("Reset successful — reloading…");
        window.location.reload();
      } catch (e) {
        toast("Failed to reset: " + e.message, true);
      }
    },
  );
}

// Expose to global scope for inline onclick handlers
window.showResetConfirmModal = showResetConfirmModal;
