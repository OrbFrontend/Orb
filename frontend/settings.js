import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";
import { api } from "./api.js";
import { showModal, closeModal, showConfirmModal } from "./modal.js";
import { validate } from "./validate.js";

// ── Theme
const THEMES = [
  "dark",
  "halloween",
  "dark_forest",
  "ocean_depths",
  "ghostly",
  "pastel_neon",
  "vintage_wood",
  "newspaper",
];

export function applyTheme(name) {
  if (!THEMES.includes(name)) name = "dark";
  $("theme-link").href = "/static/themes/" + name + ".css";
  localStorage.setItem("ar-theme", name);
  const sel = $("theme-select");
  if (sel) sel.value = name;
}

export function initTheme() {
  applyTheme(localStorage.getItem("ar-theme") || "dark");
}

// ── Settings
const MODEL_HYPERPARAM_KEYS = ["system_prompt", "temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"];

const SETTING_FIELDS = [
  { k: "endpoint_url", l: "Endpoint URL", t: "text" },
  { k: "model_name", l: "Model Name", t: "text" },
  { k: "api_key", l: "API Key", t: "api_key" },
  { k: "system_prompt", l: "System Prompt", t: "textarea" },
  { k: "temperature", l: "Temperature", t: "number", s: "0.05", mn: "0", mx: "2" },
  { k: "max_tokens", l: "Max Tokens", t: "number", s: "64", mn: "64", mx: "8192" },
  { k: "top_p", l: "Top P", t: "number", s: "0.05", mn: "0", mx: "1" },
  { k: "min_p", l: "Min P", t: "number", s: "0.01", mn: "0", mx: "1" },
  { k: "top_k", l: "Top K", t: "number", s: "1", mn: "0", mx: "200" },
  { k: "repetition_penalty", l: "Rep. Penalty", t: "number", s: "0.05", mn: "1", mx: "2" },
];

export async function loadSettings() {
  S.settings = await api.get("/settings");
  S.activePersonaId = S.settings.active_persona_id || null;
  S.characterBrowserView = S.settings.character_library_view || "grid";
  S.characterBrowserSort = S.settings.character_library_sort || "time-added";
  if (S.settings.enabled_tools) S.enabledTools = { ...S.enabledTools, ...S.settings.enabled_tools };
  if (typeof S.settings.enable_agent === "number") S.agentEnabled = S.settings.enable_agent !== 0;

  if (S.settings.enabled_tools && "length_guard" in S.settings.enabled_tools) {
    S.lengthGuardEnabled = Boolean(S.settings.enabled_tools.length_guard);
  } else {
    S.lengthGuardEnabled = false;
  }

  if (S.settings.enabled_tools && "length_guard_enforce" in S.settings.enabled_tools) {
    S.lengthGuardEnforce = Boolean(S.settings.enabled_tools.length_guard_enforce);
  } else {
    S.lengthGuardEnforce = false;
  }

  if (S.settings.length_guard_max_words) S.lengthGuardMaxWords = S.settings.length_guard_max_words;
  if (S.settings.length_guard_max_paragraphs) S.lengthGuardMaxParagraphs = S.settings.length_guard_max_paragraphs;
  if (S.settings.reasoning_enabled_passes)
    S.reasoningEnabled = { ...S.reasoningEnabled, ...S.settings.reasoning_enabled_passes };

  // Expand Settings section if endpoint_url is empty
  const settingsSection = $("settings-section");
  if (settingsSection && (!S.settings.endpoint_url || S.settings.endpoint_url.trim() === "")) {
    const header = settingsSection.previousElementSibling;
    if (header) {
      const arrow = header.querySelector(".arrow");
      if (arrow) arrow.classList.remove("collapsed");
    }
    settingsSection.classList.remove("collapsed");
  }

  renderSettings();
  await loadEndpoints();
  initComboboxes(); // Re-initialize comboboxes with loaded endpoints
  renderToolsPanel();
  await loadPersonas();
  updateUserBtn();
}

export async function loadPersonas() {
  try {
    S.personas = await api.get("/user-personas");
  } catch (e) {
    console.error("Failed to load personas:", e);
    S.personas = [];
  }
}

export function renderSettings() {
  $("settings-form").innerHTML = SETTING_FIELDS.map((f) => {
    const v = S.settings[f.k] ?? "";
    if (f.t === "textarea") {
      return `<div class="field"><label>${f.l}</label>
                <textarea data-key="${f.k}" onchange="saveSetting(this)">${v}</textarea>
              </div>`;
    }
    if (f.t === "api_key") {
      return `<div class="field"><label>${f.l}</label>
        <div class="api-key-wrap">
          <input type="text" class="api-key-input" value="${esc(v)}" data-key="api_key" autocomplete="off" onchange="saveSetting(this)">
          <button type="button" class="api-key-toggle" onclick="toggleApiKeyVisibility(this)" aria-label="Show/hide API key">
            <svg class="eye-show" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <svg class="eye-hide" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
          </button>
        </div>
      </div>`;
    }
    if (f.k === "endpoint_url" || f.k === "model_name") {
      const ph = f.k === "endpoint_url" ? "http://localhost:5000/v1" : "google/gemma-4-31b-it";
      return `<div class="field"><label>${f.l}</label>
        <div class="cb-root" data-combobox="${f.k}">
          <div class="cb-control">
            <input type="text" class="cb-input" value="${v}" data-key="${f.k}" placeholder="${ph}" onchange="saveSetting(this)">
            <span class="cb-arrow"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="2,4 6,8 10,4"/></svg></span>
          </div>
          <div class="cb-dropdown" hidden><div class="cb-list"></div></div>
        </div>
      </div>`;
    }
    const attrs = f.s ? `step="${f.s}" min="${f.mn}" max="${f.mx}"` : "";
    return `<div class="field"><label>${f.l}</label>
              <input type="${f.t}" value="${v}" data-key="${f.k}" ${attrs} onchange="saveSetting(this)">
            </div>`;
  }).join("");
  $("settings-form").innerHTML += `
    <div class="field" style="margin-top:16px;padding-top:16px;border-top:1px solid var(--accent-dim)">
      <button class="btn btn-danger" onclick="showResetConfirmModal()" style="width:100%;justify-content:center">Reset to Defaults</button>
    </div>
  `;
  initComboboxes();
}

export async function saveSetting(el) {
  let v = el.value;
  if (el.type === "number") v = parseFloat(v);
  const key = el.dataset.key;
  const validation = validate.validateSetting(key, v);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }

  // Build payload — include cascaded fields for endpoint_url and model_name
  const payload = { [key]: v };
  if (key === "endpoint_url") {
    const apiKeyEl = document.querySelector('[data-key="api_key"]');
    if (apiKeyEl) payload.api_key = apiKeyEl.value;
  } else if (key === "model_name") {
    MODEL_HYPERPARAM_KEYS.forEach((k) => {
      const fieldEl = document.querySelector(`[data-key="${k}"]`);
      if (fieldEl) payload[k] = fieldEl.type === "number" ? parseFloat(fieldEl.value) : fieldEl.value;
    });
  }

  try {
    S.settings = await api.put("/settings", payload);
    toast("Settings saved");
  } catch (e) {
    toast("Failed: " + e.message, true);
    return;
  }

  // Secondary: sync endpoint/model_config records
  try {
    if (key === "endpoint_url") {
      await syncEndpointRecord(v, payload.api_key || "");
    } else if (key === "api_key" && S.activeEndpointId) {
      await api.put(`/endpoints/${S.activeEndpointId}`, { api_key: v });
    } else if (key === "model_name") {
      await syncModelConfigRecord(v, payload);
    } else if (MODEL_HYPERPARAM_KEYS.includes(key) && S.activeModelConfigId) {
      await api.put(`/models/${S.activeModelConfigId}`, { [key]: v });
    }
  } catch (e) {
    console.error("Endpoint/model sync error:", e);
  }
}

// ── Combobox engine

let _comboboxCleanups = [];

function highlightMatch(text, query) {
  if (!query) return esc(text);
  const lText = text.toLowerCase();
  const lQuery = query.toLowerCase();
  const idx = lText.indexOf(lQuery);
  if (idx === -1) return esc(text);
  return (
    esc(text.slice(0, idx)) +
    `<mark class="cb-hl">${esc(text.slice(idx, idx + query.length))}</mark>` +
    esc(text.slice(idx + query.length))
  );
}

function initComboboxes() {
  _comboboxCleanups.forEach((fn) => fn());
  _comboboxCleanups = [];
  const epRoot = document.querySelector('[data-combobox="endpoint_url"]');
  if (epRoot) initCombobox(epRoot, () => S.endpoints.map(e => ({ value: e.url, id: e.id, type: 'endpoint' })));
  const mdRoot = document.querySelector('[data-combobox="model_name"]');
  if (mdRoot) initCombobox(mdRoot, () => S.modelConfigs.map(m => ({ value: m.model_name, id: m.id, type: 'model' })));
}

// Global delete function for combobox items
window.deleteComboboxItem = function(btn, type, id) {
  const typeName = type === 'endpoint' ? 'endpoint' : 'model configuration';
  showConfirmModal(
    {
      title: `Delete ${typeName}?`,
      message: `Are you sure you want to delete this ${typeName}? This action cannot be undone.`,
      confirmText: "Delete",
      confirmClass: "btn-danger"
    },
    async () => {
      try {
        let wasActive = false;
        if (type === 'endpoint') {
          await api.del(`/endpoints/${id}`);
          // Remove from S.endpoints
          const index = S.endpoints.findIndex(e => e.id === id);
          if (index > -1) S.endpoints.splice(index, 1);
          // If this was the active endpoint, clear active
          if (S.activeEndpointId === id) {
            S.activeEndpointId = null;
            S.activeModelConfigId = null;
            S.modelConfigs = [];
            wasActive = true;
          }
        } else if (type === 'model') {
          await api.del(`/models/${id}`);
          // Remove from S.modelConfigs
          const index = S.modelConfigs.findIndex(m => m.id === id);
          if (index > -1) S.modelConfigs.splice(index, 1);
          // If this was the active model config, clear active
          if (S.activeModelConfigId === id) {
            S.activeModelConfigId = null;
            wasActive = true;
          }
        }
        
        // If the deleted item was active, clear the corresponding combobox input
        if (wasActive) {
          const inputSelector = type === 'endpoint' ? '[data-key="endpoint_url"]' : '[data-key="model_name"]';
          const input = document.querySelector(inputSelector);
          if (input) {
            input.value = '';
            // Trigger change event to save empty value
            input.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
        
        // Re-render both comboboxes
        initComboboxes();
        // Update datalists
        populateEndpointDatalist();
        populateModelDatalist();
        toast("Deleted");
      } catch (e) {
        toast("Failed to delete: " + e.message, true);
      }
    }
  );
};

function initCombobox(rootEl, getItems) {
  const input = rootEl.querySelector(".cb-input");
  const control = rootEl.querySelector(".cb-control");
  const dropdown = rootEl.querySelector(".cb-dropdown");
  const list = rootEl.querySelector(".cb-list");
  let activeIdx = -1;
  let isOpen = false;

  function getFiltered() {
    // Always return all items, no filtering (for creating new records)
    return getItems();
  }

  function render() {
    const items = getFiltered();
    const total = items.length;
    activeIdx = Math.max(-1, Math.min(activeIdx, total - 1));
    const q = input.value.trim();
    if (!total) {
      list.innerHTML = '<div class="cb-empty">No saved options</div>';
    } else {
      list.innerHTML = items
        .map(
          (item, i) => {
            const value = item.value;
            const id = item.id;
            const type = item.type;
            return `
              <div class="cb-option${i === activeIdx ? " active" : ""}" data-value="${esc(value)}" data-id="${id}" data-type="${type}">
                <span class="cb-option-text">${highlightMatch(value, q)}</span>
                <button class="cb-delete-btn" title="Delete" onclick="event.stopPropagation(); deleteComboboxItem(this, '${type}', ${id})">×</button>
              </div>`;
          }
        )
        .join("");
    }
    list.querySelectorAll(".cb-option").forEach((el, i) => {
      el.onmousedown = (e) => {
        if (e.target.classList.contains('cb-delete-btn')) return;
        e.preventDefault();
        selectVal(el.dataset.value);
      };
      el.onmouseenter = () => { activeIdx = i; render(); };
    });
  }

  function openDropdown() {
    if (isOpen) return;
    isOpen = true;
    activeIdx = -1;
    control.classList.add("open");
    dropdown.hidden = false;
    render(); // Show all options
  }

  function closeDropdown() {
    if (!isOpen) return;
    isOpen = false;
    control.classList.remove("open");
    dropdown.hidden = true;
  }

  function selectVal(val) {
    input.value = val;
    closeDropdown();
    onHybridInput(input);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  input.addEventListener("input", () => { if (!isOpen) openDropdown(); else render(); });
  input.addEventListener("keydown", (e) => {
    // Only handle Escape to close dropdown - mouse-only navigation
    if (e.key === "Escape") { closeDropdown(); return; }
    // Allow typing, tab navigation, etc. but no arrow key or Enter navigation
  });
  control.addEventListener("mousedown", (e) => {
    if (e.target === input) return;
    e.preventDefault();
    // Toggle dropdown
    if (isOpen) closeDropdown(); else openDropdown();
    // Focus input
    input.focus();
  });
  const onDocDown = (e) => { if (!rootEl.contains(e.target)) closeDropdown(); };
  document.addEventListener("mousedown", onDocDown);
  _comboboxCleanups.push(() => document.removeEventListener("mousedown", onDocDown));
}

// ── Endpoint / Model Config helpers

export async function loadEndpoints() {
  try {
    S.endpoints = await api.get("/endpoints");
    S.activeEndpointId = S.settings.active_endpoint_id || null;
    S.activeModelConfigId = S.settings.active_model_config_id || null;
    populateEndpointDatalist();
    if (S.activeEndpointId) {
      await loadModelConfigs(S.activeEndpointId);
    }
  } catch (e) {
    console.error("Failed to load endpoints:", e);
    S.endpoints = [];
  }
}

export async function loadModelConfigs(endpointId) {
  if (!endpointId) {
    S.modelConfigs = [];
    populateModelDatalist();
    initComboboxes(); // Re-initialize comboboxes
    return;
  }
  try {
    S.modelConfigs = await api.get(`/endpoints/${endpointId}/models`);
    populateModelDatalist();
    initComboboxes(); // Re-initialize comboboxes with loaded model configs
  } catch (e) {
    S.modelConfigs = [];
    initComboboxes(); // Re-initialize comboboxes even on error
  }
}

function populateEndpointDatalist() {
  const dl = document.getElementById("endpoint-datalist");
  if (!dl) return;
  dl.innerHTML = S.endpoints.map((e) => `<option value="${esc(e.url)}"></option>`).join("");
}

function populateModelDatalist() {
  const dl = document.getElementById("model-datalist");
  if (!dl) return;
  dl.innerHTML = S.modelConfigs.map((m) => `<option value="${esc(m.model_name)}"></option>`).join("");
}

function fillModelConfigFields(config) {
  MODEL_HYPERPARAM_KEYS.forEach((k) => {
    const el = document.querySelector(`[data-key="${k}"]`);
    if (el && config[k] !== undefined) el.value = config[k];
  });
}

async function syncEndpointRecord(url, apiKey) {
  const existing = S.endpoints.find((e) => e.url === url);
  if (existing) {
    S.activeEndpointId = existing.id;
    if (existing.api_key !== apiKey) {
      await api.put(`/endpoints/${existing.id}`, { api_key: apiKey });
      existing.api_key = apiKey;
    }
    await api.put("/settings", { active_endpoint_id: existing.id });
    if (!S.modelConfigs.length || S.modelConfigs[0]?.endpoint_id !== existing.id) {
      await loadModelConfigs(existing.id);
    }
  } else if (url) {
    const ep = await api.post("/endpoints", { url, api_key: apiKey });
    S.endpoints.push(ep);
    S.activeEndpointId = ep.id;
    S.activeModelConfigId = null;
    await api.put("/settings", { active_endpoint_id: ep.id, active_model_config_id: null });
    populateEndpointDatalist();
    await loadModelConfigs(ep.id);
  }
}

async function syncModelConfigRecord(modelName, hyperparams) {
  if (!S.activeEndpointId || !modelName) return;
  const existing = S.modelConfigs.find((m) => m.model_name === modelName);
  if (existing) {
    S.activeModelConfigId = existing.id;
    const update = {};
    MODEL_HYPERPARAM_KEYS.forEach((k) => {
      if (hyperparams[k] !== undefined) update[k] = hyperparams[k];
    });
    if (Object.keys(update).length) {
      await api.put(`/models/${existing.id}`, update);
      Object.assign(existing, update);
    }
    await api.put("/settings", { active_model_config_id: existing.id });
  } else {
    const mc = await api.post(`/endpoints/${S.activeEndpointId}/models`, {
      model_name: modelName,
      system_prompt: hyperparams.system_prompt || "",
      temperature: hyperparams.temperature || 0.8,
      min_p: hyperparams.min_p || 0,
      top_k: hyperparams.top_k || 40,
      top_p: hyperparams.top_p || 0.95,
      repetition_penalty: hyperparams.repetition_penalty || 1.0,
      max_tokens: hyperparams.max_tokens || 4096,
    });
    S.modelConfigs.push(mc);
    S.activeModelConfigId = mc.id;
    await api.put("/settings", { active_model_config_id: mc.id });
    populateModelDatalist();
  }
}

export function onHybridInput(el) {
  const key = el.dataset.key;
  if (key === "endpoint_url") {
    const match = S.endpoints.find((e) => e.url === el.value);
    if (!match) return;
    const apiKeyEl = document.querySelector('[data-key="api_key"]');
    if (apiKeyEl) apiKeyEl.value = match.api_key;
    loadModelConfigs(match.id).then(() => {
      const modelEl = document.querySelector('[data-key="model_name"]');
      if (!modelEl) return;
      const matchModel = S.modelConfigs.find((m) => m.model_name === modelEl.value);
      if (matchModel) {
        fillModelConfigFields(matchModel);
      } else if (S.modelConfigs.length > 0) {
        modelEl.value = S.modelConfigs[0].model_name;
        fillModelConfigFields(S.modelConfigs[0]);
      }
    });
  } else if (key === "model_name") {
    const match = S.modelConfigs.find((m) => m.model_name === el.value);
    if (match) fillModelConfigFields(match);
  }
}

// ── User Profile
export function updateUserBtn() {
  let displayName = "User";
  if (S.activePersonaId && S.personas.length) {
    const activePersona = S.personas.find((p) => p.id === S.activePersonaId);
    if (activePersona) displayName = activePersona.name;
  }
  $("user-profile-btn").textContent = "👤 " + displayName;
}

export function showUserModal() {
  const personaItems = S.personas
    .map((p) => {
      const isActive = p.id === S.activePersonaId;
      const avatarColor = p.avatar_color || "#E1F5EE";
      const avatarTextColor = isActive ? "var(--accent)" : "#085041";
      const avatarBg = isActive ? "var(--accent-glow)" : avatarColor;
      const initials = p.name.charAt(0).toUpperCase();
      return `
      <div class="persona-item${isActive ? " persona-item-active" : ""}" onclick="activatePersona(${p.id})">
        <div class="persona-avatar" style="background:${avatarBg};color:${avatarTextColor}">${initials}</div>
        <div class="persona-info">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="persona-name">${esc(p.name)}</span>
            ${isActive ? '<span class="persona-active-badge">Active</span>' : ""}
          </div>
          <span class="persona-desc">${esc(p.description || "")}</span>
        </div>
        <button class="btn btn-sm" onclick="event.stopPropagation();editPersona(${p.id})">Edit</button>
      </div>
    `;
    })
    .join("");

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>User personas</h2>
        <p class="modal-subtitle">Click a persona to activate it.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn" onclick="showPersonaEditModal(null)">+ New persona</button>
      </div>
    </div>
    <div class="persona-list">
      ${personaItems.length ? personaItems : '<p class="modal-subtitle" style="text-align:center;padding:1rem 0">No personas yet. Create one to get started.</p>'}
    </div>
  `);
}

export async function saveUserProfile() {
  const name = $("user-name-input").value.trim();
  const desc = $("user-desc-input").value.trim();
  const validation = validate.validateUserProfile(name, desc);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    S.settings = await api.put("/settings", { user_name: name || "User", user_description: desc });
    updateUserBtn();
    closeModal();
    toast("User profile saved");
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export function showPersonaEditModal(personaId) {
  const persona = personaId ? S.personas.find((p) => p.id === personaId) : null;
  const isEdit = persona !== null;
  showModal(`
    <h2>${isEdit ? "Edit persona" : "New persona"}</h2>
    <div class="field">
      <label>Name</label>
      <input id="persona-name-input" type="text" placeholder="e.g. Kai" value="${esc(persona?.name || "")}">
    </div>
    <div class="field">
      <label>Description <span style="font-weight:400;text-transform:none;letter-spacing:0">(injected into system prompt)</span></label>
      <textarea id="persona-desc-input" placeholder="Describe yourself — appearance, personality, background…" rows="4" style="resize:vertical;min-height:90px">${esc(persona?.description || "")}</textarea>
    </div>
    <label class="modal-checkbox-label" style="margin-bottom:1.25rem">
      <input type="checkbox" id="persona-active-checkbox" ${!personaId || personaId === S.activePersonaId ? "checked" : ""} style="width:14px;height:14px;margin:0;flex-shrink:0">
      <span style="font-size:13px;text-transform:none;letter-spacing:0;font-weight:400">Set as active persona after saving</span>
    </label>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger" onclick="deletePersona(${personaId})">Delete</button>` : ""}
      <button class="btn" onclick="showUserModal()">Cancel</button>
      <button class="btn btn-accent" onclick="savePersona(${personaId || "null"})">${isEdit ? "Update" : "Create"}</button>
    </div>
  `);
}

export async function savePersona(personaId) {
  const name = $("persona-name-input").value.trim();
  const description = $("persona-desc-input").value.trim();
  const setActive = $("persona-active-checkbox").checked;
  const validation = validate.validatePersona(name, description);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    let newId;
    if (personaId && personaId !== "null") {
      await api.put("/user-personas/" + personaId, { name, description });
      newId = parseInt(personaId, 10);
    } else {
      const result = await api.post("/user-personas", { name, description });
      newId = result.id;
    }
    await loadPersonas();
    if (setActive) {
      await api.put("/settings", { active_persona_id: newId });
      S.activePersonaId = newId;
      updateUserBtn();
    }
    showUserModal();
    toast("Persona saved");
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export async function deletePersona(personaId) {
  showConfirmModal(
    {
      title: "Delete Persona",
      message: "Are you sure you want to delete this persona?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/user-personas/" + personaId);
        if (S.activePersonaId === personaId) {
          await api.put("/settings", { active_persona_id: null });
          S.activePersonaId = null;
          updateUserBtn();
        }
        await loadPersonas();
        showUserModal();
        toast("Persona deleted");
      } catch (e) {
        toast("Failed: " + e.message, true);
      }
    },
  );
}

export async function activatePersona(personaId) {
  if (S.activePersonaId === personaId) return;
  try {
    await api.put("/settings", { active_persona_id: personaId });
    S.activePersonaId = personaId;
    updateUserBtn();
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export async function editPersona(personaId) {
  showPersonaEditModal(personaId);
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
    desc: "Scans for banned phrases, repetitive openers & templates, then surgically patches the draft",
  },
];

export function toggleToolsPanel() {
  const panel = $("tools-panel");
  const inspector = $("inspector");
  const wasOpen = panel.classList.contains("open");

  if (wasOpen) {
    panel.classList.remove("open");
    $("tools-panel-btn").style.background = "";
    $("tools-panel-btn").style.borderColor = "";
  } else {
    inspector.classList.remove("open");
    panel.classList.add("open");
    $("tools-panel-btn").style.background = "var(--accent-glow)";
    $("tools-panel-btn").style.borderColor = "var(--accent-dim)";
    renderToolsPanel();
  }
}

export async function setAgentEnabled(on) {
  S.agentEnabled = on;
  $("tools-panel-btn").style.opacity = on ? "1" : "0.5";
  try {
    S.settings = await api.put("/settings", { enable_agent: on });
  } catch (e) {
    toast("Failed to save agent state", true);
  }
}

export async function toggleToolEnabled(id, on) {
  S.enabledTools[id] = on;
  renderToolsPanel();
  try {
    S.settings = await api.put("/settings", { enabled_tools: S.enabledTools });
  } catch (e) {
    toast("Failed to save tool state", true);
  }
}

export async function toggleLengthGuard(on) {
  S.lengthGuardEnabled = on;
  S.enabledTools.length_guard = on;
  renderToolsPanel();
  try {
    S.settings = await api.put("/settings", { enabled_tools: S.enabledTools });
  } catch (e) {
    toast("Failed to save length guard state", true);
  }
}

export async function toggleLengthGuardEnforce(on) {
  S.lengthGuardEnforce = on;
  S.enabledTools.length_guard_enforce = on;
  renderToolsPanel();
  try {
    S.settings = await api.put("/settings", { enabled_tools: S.enabledTools });
  } catch (e) {
    toast("Failed to save length guard enforce state", true);
  }
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
    return `<div class="tool-card ${on ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">${t.name}</span>
        <label class="tog" onclick="event.stopPropagation()">
          <input type="checkbox" ${on ? "checked" : ""} onchange="toggleToolEnabled('${t.id}',this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">${t.desc}</div>
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
          <label>Max sections</label>
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
    <div class="tool-card-desc">Reigns the model's response length by word count. MAX SECTIONS is suggested to the AI in rewrite pass.</div>
    ${lgConfig}
  </div>`;

  $("tools-list").innerHTML = toolCards + lengthGuardCard;
}

// ── Phrase Bank

export async function showPhraseBankModal() {
  const groups = await api.get("/phrase-bank");

  const groupRows = groups
    .map(
      (g) => `
    <div class="phrase-group-item" onclick="editPhraseGroup(${g.id})" data-id="${g.id}">
      <div class="phrase-group-variants">
        ${g.variants.map((v) => `<span class="phrase-variant">${esc(v)}</span>`).join(", ")}
      </div>
      <div class="phrase-group-count">${g.variants.length} variant${g.variants.length !== 1 ? "s" : ""}</div>
    </div>
  `,
    )
    .join("");

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Phrase Bank</h2>
        <p class="modal-subtitle">Manage banned/overused phrase groups. Each group contains variants that are considered equivalent. Click a group to edit it.</p>
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

export function showAddPhraseGroupModal(editId = null, initialVariants = []) {
  const isEdit = editId !== null;
  const variantsHtml = initialVariants
    .map(
      (v) => `
    <div class="variant-row">
      <input type="text" class="variant-input" value="${esc(v)}" placeholder="e.g., a mix of">
      <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
    </div>
  `,
    )
    .join("");

  const emptyRow = `<div class="variant-row">
    <input type="text" class="variant-input" placeholder="e.g., a mix of">
    <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
  </div>`;

  const deleteButton = isEdit
    ? `
    <button class="btn btn-danger" onclick="deletePhraseGroup(${editId})" style="margin-right: auto;">Delete</button>
  `
    : "";

  showModal(`
    <h2>${isEdit ? "Edit" : "Add"} Phrase Group</h2>
    <p class="modal-subtitle">Enter variant phrases that are considered equivalent. The first variant is treated as the canonical name.</p>
    
    <div id="variant-list" style="margin-bottom: 15px;">
      ${variantsHtml || emptyRow}
    </div>
    
    <button class="btn btn-sm" onclick="addVariantRow()" style="margin-bottom: 20px;">+ Add Another Variant</button>
    
    <div class="modal-actions">
      ${deleteButton}
      <button class="btn" onclick="showPhraseBankModal()">Cancel</button>
      <button class="btn btn-accent" onclick="savePhraseGroup(${editId || "null"})">${isEdit ? "Update" : "Save"}</button>
    </div>
  `);
}

// Helper functions exposed to window
window.addVariantRow = function () {
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

window.removeVariantRow = function (btn) {
  const rows = document.querySelectorAll(".variant-row");
  if (rows.length > 1) {
    btn.closest(".variant-row").remove();
  } else {
    // If it's the last row, just clear it
    btn.closest(".variant-row").querySelector(".variant-input").value = "";
  }
};

window.editPhraseGroup = async function (groupId) {
  const groups = await api.get("/phrase-bank");
  const group = groups.find((g) => g.id === groupId);
  if (group) {
    showAddPhraseGroupModal(groupId, group.variants);
  }
};

window.deletePhraseGroup = async function (groupId) {
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

window.savePhraseGroup = async function (editId) {
  const variantInputs = document.querySelectorAll(".variant-input");
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

  try {
    if (editId && editId !== "null") {
      await api.put(`/phrase-bank/${editId}`, { variants });
      toast("Phrase group updated");
    } else {
      await api.post("/phrase-bank", { variants });
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
        "This will reset Mood Fragments, Director Fragments, Phrase Bank, and all Settings to their original default values. All custom data will be lost.<br><br>The following will be retained: Characters, Conversations.",
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

window.toggleApiKeyVisibility = function(btn) {
  const input = btn.closest(".api-key-wrap").querySelector(".api-key-input");
  const visible = btn.dataset.visible === "1";
  if (!visible) {
    input.style.webkitTextSecurity = "none";
    btn.dataset.visible = "1";
    btn.querySelector(".eye-show").style.display = "none";
    btn.querySelector(".eye-hide").style.display = "";
  } else {
    input.style.webkitTextSecurity = "disc";
    btn.dataset.visible = "";
    btn.querySelector(".eye-show").style.display = "";
    btn.querySelector(".eye-hide").style.display = "none";
  }
};
