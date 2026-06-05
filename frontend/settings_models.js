// Endpoint + model-configuration settings: the writer/agent endpoint forms, the
// combobox engine, and the unified (WRITER_CTX / AGENT_CTX) sync helpers that
// persist endpoint + model records. Split out of settings.js; the public
// surface is re-exported from settings.js.
import { api } from "./api.js";
import { showConfirmModal } from "./modal.js";
import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";
import { validate } from "./validate.js";

const MODEL_HYPERPARAM_KEYS = [
  "shared_system_prompt",
  "system_prompt",
  "temperature",
  "max_tokens",
  "top_p",
  "min_p",
  "top_k",
  "repetition_penalty",
];

const SETTING_FIELDS = [
  { k: "endpoint_url", l: "Endpoint URL", t: "text" },
  { k: "api_key", l: "API Key", t: "api_key" },
  { k: "model_name", l: "Model Name", t: "text" },
  { k: "shared_system_prompt", l: "System Prompt (global)", t: "textarea" },
  { k: "system_prompt", l: "System Prompt (model)", t: "textarea" },
  { k: "temperature", l: "Temperature", t: "number", s: "0.05", mn: "0", mx: "2" },
  { k: "max_tokens", l: "Max Tokens", t: "number", s: "64", mn: "64", mx: "8192" },
  { k: "top_p", l: "Top P", t: "number", s: "0.05", mn: "0", mx: "1" },
  { k: "min_p", l: "Min P", t: "number", s: "0.01", mn: "0", mx: "1" },
  { k: "top_k", l: "Top K", t: "number", s: "1", mn: "0", mx: "200" },
  { k: "repetition_penalty", l: "Rep. Penalty", t: "number", s: "0.05", mn: "1", mx: "2" },
];

const AGENT_MODEL_HYPERPARAM_KEYS = [
  "agent_shared_system_prompt",
  "agent_temperature",
  "agent_top_p",
  "agent_repetition_penalty",
];

const AGENT_SETTING_FIELDS = [
  { k: "agent_endpoint_url", l: "Agent Endpoint URL", t: "text" },
  { k: "agent_api_key", l: "Agent API Key", t: "api_key" },
  { k: "agent_model_name", l: "Agent Model Name", t: "text" },
  { k: "agent_shared_system_prompt", l: "Agent System Prompt (global)", t: "textarea" },
  { k: "agent_temperature", l: "Agent Temperature", t: "number", s: "0.05", mn: "0", mx: "2" },
  { k: "agent_top_p", l: "Agent Top P", t: "number", s: "0.05", mn: "0", mx: "1" },
  { k: "agent_repetition_penalty", l: "Agent Rep. Penalty", t: "number", s: "0.05", mn: "1", mx: "2" },
];

// Descriptor objects that parameterise all writer vs. agent differences.
const WRITER_CTX = {
  role: "writer",
  configsKey: "modelConfigs",
  endpointIdKey: "activeEndpointId",
  configIdKey: "activeModelConfigId",
  urlField: "endpoint_url",
  apiKeyField: "api_key",
  modelField: "model_name",
  activeConfigDbField: "active_model_config_id",
  settingsEndpointField: "active_endpoint_id",
  hyperparamKeys: MODEL_HYPERPARAM_KEYS,
  hyperparamPrefix: "",
};

const AGENT_CTX = {
  role: "agent",
  configsKey: "agentModelConfigs",
  endpointIdKey: "agentEndpointId",
  configIdKey: "agentModelConfigId",
  urlField: "agent_endpoint_url",
  apiKeyField: "agent_api_key",
  modelField: "agent_model_name",
  activeConfigDbField: "agent_active_model_config_id",
  settingsEndpointField: "agent_endpoint_id",
  hyperparamKeys: AGENT_MODEL_HYPERPARAM_KEYS,
  hyperparamPrefix: "agent_",
};

export async function toggleAgentSameAsWriter(checked) {
  S.agentSameAsWriter = checked;
  try {
    await api.put("/settings", { agent_same_as_writer: checked });
  } catch (e) {
    toast("Failed to save agent toggle", true);
    return;
  }
  const container = document.getElementById("agent-fields");
  if (container) container.style.display = checked ? "none" : "";
  if (!checked && S.agentEndpointId) {
    await _loadConfigs(AGENT_CTX, S.agentEndpointId);
    initComboboxes();
    _fillEndpointFields(AGENT_CTX);
  }
  updateAgentModelWarning();
}

export function renderEndpoints() {
  function renderField(f, isAgent) {
    const v = S.settings[f.k] ?? "";
    const saveFn = isAgent ? "saveAgentSetting" : "saveSetting";
    if (f.t === "textarea") {
      const rows = f.k === "system_prompt" || f.k === "agent_system_prompt" ? ' rows="2"' : "";
      return `<div class="field"><label>${f.l}</label>
                <textarea data-key="${f.k}"${rows} onchange="${saveFn}(this)">${v}</textarea>
              </div>`;
    }
    if (f.t === "api_key") {
      return `<div class="field"><label>${f.l}</label>
        <div class="api-key-wrap">
          <input type="text" class="api-key-input" value="${esc(v)}" data-key="${f.k}" autocomplete="off" onchange="${saveFn}(this)">
          <button type="button" class="api-key-toggle" onclick="toggleApiKeyVisibility(this)" aria-label="Show/hide API key">
            <svg class="eye-show" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <svg class="eye-hide" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
          </button>
        </div>
      </div>`;
    }
    if (f.k === "endpoint_url" || f.k === "model_name" || f.k === "agent_endpoint_url" || f.k === "agent_model_name") {
      const ph =
        f.k === "endpoint_url" || f.k === "agent_endpoint_url" ? "http://localhost:5000/v1" : "google/gemma-4-31b-it";
      const warningHtml =
        f.k === "agent_model_name"
          ? `<div id="agent-model-match-warning" class="field-warning" style="display:none">Warning: Same endpoint and model as writer detected - this increases cache cost significantly.</div>`
          : "";
      return `<div class="field"><label>${f.l}</label>
        <div class="cb-root" data-combobox="${f.k}">
          <div class="cb-control">
            <input type="text" class="cb-input" value="${v}" data-key="${f.k}" placeholder="${ph}" autocomplete="off" onchange="${saveFn}(this)">
            <span class="cb-arrow"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="2,4 6,8 10,4"/></svg></span>
          </div>
          <div class="cb-dropdown" hidden><div class="cb-list"></div></div>
        </div>
        ${warningHtml}
      </div>`;
    }
    const attrs = f.s ? `step="${f.s}" min="${f.mn}" max="${f.mx}"` : "";
    return `<div class="field"><label>${f.l}</label>
              <input type="${f.t}" value="${v}" data-key="${f.k}" ${attrs} onchange="${saveFn}(this)">
            </div>`;
  }

  const agentHidden = S.agentSameAsWriter ? ' style="display:none"' : "";

  $("endpoints-form").innerHTML = `
    ${SETTING_FIELDS.map((f) => renderField(f, false)).join("")}
    <div style="display:flex;align-items:center;gap:12px;margin:12px 0 8px"><div style="flex:1;height:1px;background:var(--accent-dim)"></div><span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--accent-dim)">Agent</span><div style="flex:1;height:1px;background:var(--accent-dim)"></div></div>
    <div class="tool-card" style="margin-bottom:12px">
      <div class="tool-card-header">
        <span class="tool-card-name">Same as Writer</span>
        <label class="tog" onclick="event.stopPropagation()">
          <input type="checkbox" ${S.agentSameAsWriter ? "checked" : ""} onchange="toggleAgentSameAsWriter(this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Use the same endpoint and model for Agent passes as the Writer.</div>
    </div>
    <div id="agent-fields"${agentHidden}>
      ${AGENT_SETTING_FIELDS.map((f) => renderField(f, true)).join("")}
    </div>
  `;
  initComboboxes();
  updateAgentModelWarning();
}

function updateAgentModelWarning() {
  const el = document.getElementById("agent-model-match-warning");
  if (!el) return;
  if (S.agentSameAsWriter) {
    el.style.display = "none";
    return;
  }
  const writerUrlEl = document.querySelector('[data-key="endpoint_url"]');
  const writerModelEl = document.querySelector('[data-key="model_name"]');
  const agentUrlEl = document.querySelector('[data-key="agent_endpoint_url"]');
  const agentModelEl = document.querySelector('[data-key="agent_model_name"]');
  if (!writerUrlEl || !writerModelEl || !agentUrlEl || !agentModelEl) return;
  const writerUrl = writerUrlEl.value.trim();
  const writerModel = writerModelEl.value.trim();
  const agentUrl = agentUrlEl.value.trim();
  const agentModel = agentModelEl.value.trim();
  const same =
    writerUrl && agentUrl && writerUrl === agentUrl && writerModel && agentModel && writerModel === agentModel;
  el.style.display = same ? "" : "none";
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

export function initComboboxes() {
  _comboboxCleanups.forEach((fn) => fn());
  _comboboxCleanups = [];
  const epRoot = document.querySelector('[data-combobox="endpoint_url"]');
  if (epRoot) initCombobox(epRoot, () => S.endpoints.map((e) => ({ value: e.url, id: e.id, type: "endpoint" })), false);
  const mdRoot = document.querySelector('[data-combobox="model_name"]');
  if (mdRoot)
    initCombobox(mdRoot, () => S.modelConfigs.map((m) => ({ value: m.model_name, id: m.id, type: "model" })), false);
  const agentEpRoot = document.querySelector('[data-combobox="agent_endpoint_url"]');
  if (agentEpRoot)
    initCombobox(agentEpRoot, () => S.endpoints.map((e) => ({ value: e.url, id: e.id, type: "endpoint" })), true);
  const agentMdRoot = document.querySelector('[data-combobox="agent_model_name"]');
  if (agentMdRoot)
    initCombobox(
      agentMdRoot,
      () => S.agentModelConfigs.map((m) => ({ value: m.model_name, id: m.id, type: "model" })),
      true,
    );
}

// Global delete function for combobox items
window.deleteComboboxItem = (btn, type, id, isAgent = false) => {
  const typeName = type === "endpoint" ? "endpoint" : "model configuration";
  showConfirmModal(
    {
      title: `Delete ${typeName}?`,
      message: `Are you sure you want to delete this ${typeName}? This action cannot be undone.`,
      confirmText: "Delete",
      confirmClass: "btn-danger",
    },
    async () => {
      try {
        let wasActive = false;
        if (type === "endpoint") {
          await api.del(`/endpoints/${id}`);
          const index = S.endpoints.findIndex((e) => e.id === id);
          if (index > -1) S.endpoints.splice(index, 1);
          if (isAgent) {
            if (S.agentEndpointId === id) {
              S.agentEndpointId = null;
              S.agentModelConfigId = null;
              S.agentModelConfigs = [];
              wasActive = true;
            }
          } else {
            if (S.activeEndpointId === id) {
              S.activeEndpointId = null;
              S.activeModelConfigId = null;
              S.modelConfigs = [];
              wasActive = true;
            }
          }
        } else if (type === "model") {
          await api.del(`/models/${id}`);
          if (isAgent) {
            const index = S.agentModelConfigs.findIndex((m) => m.id === id);
            if (index > -1) S.agentModelConfigs.splice(index, 1);
            if (S.agentModelConfigId === id) {
              S.agentModelConfigId = null;
              wasActive = true;
            }
          } else {
            const index = S.modelConfigs.findIndex((m) => m.id === id);
            if (index > -1) S.modelConfigs.splice(index, 1);
            if (S.activeModelConfigId === id) {
              S.activeModelConfigId = null;
              wasActive = true;
            }
          }
        }

        if (wasActive) {
          let inputSelector;
          if (isAgent) {
            inputSelector = type === "endpoint" ? '[data-key="agent_endpoint_url"]' : '[data-key="agent_model_name"]';
          } else {
            inputSelector = type === "endpoint" ? '[data-key="endpoint_url"]' : '[data-key="model_name"]';
          }
          const input = document.querySelector(inputSelector);
          if (input) {
            input.value = "";
            input.dispatchEvent(new Event("change", { bubbles: true }));
          }
        }

        initComboboxes();
        populateEndpointDatalist();
        populateModelDatalist();
        toast("Deleted");
      } catch (e) {
        toast("Failed to delete: " + e.message, true);
      }
    },
  );
};

function initCombobox(rootEl, getItems, isAgent = false) {
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
        .map((item, i) => {
          const value = item.value;
          const id = item.id;
          const type = item.type;
          const agentArg = isAgent ? ", true" : "";
          return `
              <div class="cb-option${i === activeIdx ? " active" : ""}" data-value="${esc(value)}" data-id="${id}" data-type="${type}">
                <span class="cb-option-text">${highlightMatch(value, q)}</span>
                <button class="cb-delete-btn" title="Delete" onclick="event.stopPropagation(); deleteComboboxItem(this, '${type}', ${id}${agentArg})">×</button>
              </div>`;
        })
        .join("");
    }
    list.querySelectorAll(".cb-option").forEach((el, i) => {
      el.addEventListener(
        "touchstart",
        (e) => {
          if (e.target.classList.contains("cb-delete-btn")) return;
          e.preventDefault();
          selectVal(el.dataset.value);
        },
        { passive: false },
      );
      el.onmousedown = (e) => {
        if (e.target.classList.contains("cb-delete-btn")) return;
        e.preventDefault();
        selectVal(el.dataset.value);
      };
      el.onmouseenter = () => {
        activeIdx = i;
        render();
      };
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

  async function selectVal(val) {
    input.value = val;
    closeDropdown();
    await onHybridInput(input);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  input.addEventListener("keydown", (e) => {
    // Only handle Escape to close dropdown - mouse-only navigation
    if (e.key === "Escape") {
      closeDropdown();
      return;
    }
    // Allow typing, tab navigation, etc. but no arrow key or Enter navigation
  });
  control.addEventListener("mousedown", (e) => {
    // Only toggle when clicking the arrow (cb-arrow), not the input or control background
    if (!e.target.closest(".cb-arrow")) return;
    e.preventDefault();
    // Toggle dropdown
    if (isOpen) closeDropdown();
    else openDropdown();
    // Focus input
    input.focus();
  });
  control.addEventListener(
    "touchstart",
    (e) => {
      if (!e.target.closest(".cb-arrow")) return;
      e.preventDefault();
      if (isOpen) closeDropdown();
      else openDropdown();
    },
    { passive: false },
  );
  const onDocDown = (e) => {
    if (!rootEl.contains(e.target)) closeDropdown();
  };
  const onDocTouch = (e) => {
    if (!rootEl.contains(e.target)) closeDropdown();
  };
  document.addEventListener("mousedown", onDocDown);
  document.addEventListener("touchstart", onDocTouch, { passive: true });
  _comboboxCleanups.push(() => {
    document.removeEventListener("mousedown", onDocDown);
    document.removeEventListener("touchstart", onDocTouch);
  });
}

// ── Endpoint / Model Config helpers

export async function loadEndpoints() {
  try {
    S.endpoints = await api.get("/endpoints");
    S.activeEndpointId = S.settings.active_endpoint_id || null;
    // active_model_config_id lives on the endpoint row, not settings
    const activeEp = S.endpoints.find((e) => e.id === S.activeEndpointId);
    S.activeModelConfigId = activeEp?.active_model_config_id || null;
    const agentEp = S.endpoints.find((e) => e.id === S.agentEndpointId);
    S.agentModelConfigId = agentEp?.agent_active_model_config_id || null;
    populateEndpointDatalist();
    if (S.activeEndpointId) {
      await loadModelConfigs(S.activeEndpointId);
    }
  } catch (e) {
    console.error("Failed to load endpoints:", e);
    S.endpoints = [];
  }
}

function populateEndpointDatalist() {
  const dl = document.getElementById("endpoint-datalist");
  if (!dl) return;
  dl.innerHTML = S.endpoints.map((e) => `<option value="${esc(e.url)}"></option>`).join("");
}

// ── Unified endpoint / model-config helpers (parameterised by WRITER_CTX / AGENT_CTX)

async function _loadConfigs(ctx, endpointId) {
  if (!endpointId) {
    S[ctx.configsKey] = [];
    initComboboxes();
    return;
  }
  try {
    const all = await api.get(`/endpoints/${endpointId}/models`);
    S[ctx.configsKey] = all.filter((m) => m.role === ctx.role || (ctx.role === "writer" && !m.role));
    initComboboxes();
  } catch (e) {
    S[ctx.configsKey] = [];
    initComboboxes();
  }
}

function _fillConfigFields(ctx, config) {
  const p = ctx.hyperparamPrefix;
  ctx.hyperparamKeys.forEach((k) => {
    const el = document.querySelector(`[data-key="${k}"]`);
    const configKey = p ? k.replace(p, "") : k;
    if (el && config[configKey] !== undefined) el.value = config[configKey];
  });
}

function _fillEndpointFields(ctx) {
  const ep = S.endpoints.find((e) => e.id === S[ctx.endpointIdKey]);
  if (ep) {
    const epEl = document.querySelector(`[data-key="${ctx.urlField}"]`);
    if (epEl) epEl.value = ep.url || "";
    const keyEl = document.querySelector(`[data-key="${ctx.apiKeyField}"]`);
    if (keyEl) keyEl.value = ep.api_key || "";
  }
  const activeModel = S[ctx.configsKey].find((m) => m.id === S[ctx.configIdKey]) || S[ctx.configsKey][0];
  if (activeModel) {
    const modelEl = document.querySelector(`[data-key="${ctx.modelField}"]`);
    if (modelEl) modelEl.value = activeModel.model_name || "";
    _fillConfigFields(ctx, activeModel);
  }
}

async function _syncEndpointRecord(ctx, url, apiKey) {
  const existing = S.endpoints.find((e) => e.url === url);
  if (existing) {
    S[ctx.endpointIdKey] = existing.id;
    if (existing.api_key !== apiKey) {
      await api.put(`/endpoints/${existing.id}`, { api_key: apiKey });
      existing.api_key = apiKey;
    }
    await api.put("/settings", { [ctx.settingsEndpointField]: existing.id });
    if (!S[ctx.configsKey].length || S[ctx.configsKey][0]?.endpoint_id !== existing.id) {
      await _loadConfigs(ctx, existing.id);
    }
  } else if (url) {
    const ep = await api.post("/endpoints", { url, api_key: apiKey });
    S.endpoints.push(ep);
    S[ctx.endpointIdKey] = ep.id;
    S[ctx.configIdKey] = null;
    await api.put("/settings", { [ctx.settingsEndpointField]: ep.id });
    populateEndpointDatalist();
    await _loadConfigs(ctx, ep.id);
  }
}

async function _syncModelConfigRecord(ctx, modelName, hyperparams) {
  if (!S[ctx.endpointIdKey] || !modelName) return;
  const existing = S[ctx.configsKey].find((m) => m.model_name === modelName);
  const p = ctx.hyperparamPrefix;
  if (existing) {
    S[ctx.configIdKey] = existing.id;
    const update = {};
    ctx.hyperparamKeys.forEach((k) => {
      const base = p ? k.replace(p, "") : k;
      if (hyperparams[k] !== undefined) update[base] = hyperparams[k];
    });
    if (Object.keys(update).length) {
      await api.put(`/models/${existing.id}`, update);
      Object.assign(existing, update);
    }
    await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { [ctx.activeConfigDbField]: existing.id });
  } else {
    const get = (key, def) => hyperparams[`${p}${key}`] ?? def;
    const mc = await api.post(`/endpoints/${S[ctx.endpointIdKey]}/models`, {
      role: ctx.role,
      model_name: modelName,
      system_prompt: get("system_prompt", ""),
      temperature: get("temperature", 0.8),
      min_p: get("min_p", 0),
      top_k: get("top_k", 40),
      top_p: get("top_p", 0.95),
      repetition_penalty: get("repetition_penalty", 1.0),
      max_tokens: get("max_tokens", 4096),
    });
    S[ctx.configsKey].push(mc);
    S[ctx.configIdKey] = mc.id;
    await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { [ctx.activeConfigDbField]: mc.id });
    if (ctx.role === "writer") populateModelDatalist();
    initComboboxes();
  }
}

// Serialize endpoint-related saves. When a user fills endpoint_url + api_key + model_name and
// clicks outside, the three change events fire near-simultaneously and run concurrently. The
// model save reads S[ctx.endpointIdKey], which is only populated after the endpoint POST resolves
// — so without serialization the model save sees a null id and silently no-ops.
let _endpointSaveQueue = Promise.resolve();

function _saveEndpointSetting(ctx, el) {
  const next = _endpointSaveQueue.catch(() => {}).then(() => _doSaveEndpointSetting(ctx, el));
  _endpointSaveQueue = next;
  return next;
}

async function _doSaveEndpointSetting(ctx, el) {
  let v = el.value;
  if (el.type === "number") v = parseFloat(v);
  const key = el.dataset.key;
  const p = ctx.hyperparamPrefix;
  const baseKey = p ? key.replace(p, "") : key;
  const validation = validate.validateSetting(baseKey, v);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  const payload = { [key]: v };
  if (key === ctx.urlField) {
    const apiKeyEl = document.querySelector(`[data-key="${ctx.apiKeyField}"]`);
    if (apiKeyEl) payload[ctx.apiKeyField] = apiKeyEl.value;
  } else if (key === ctx.modelField) {
    ctx.hyperparamKeys.forEach((k) => {
      const fieldEl = document.querySelector(`[data-key="${k}"]`);
      if (!fieldEl) return;
      if (fieldEl.type === "number") {
        // Empty number inputs would parseFloat to NaN, which JSON-serializes as null and is
        // rejected by the backend's Pydantic float fields. Skip them so the model-create POST
        // can fall back to its defaults.
        if (fieldEl.value.trim() === "") return;
        const parsed = parseFloat(fieldEl.value);
        if (Number.isNaN(parsed)) return;
        payload[k] = parsed;
      } else {
        payload[k] = fieldEl.value;
      }
    });
  }
  try {
    S.settings = await api.put("/settings", payload);
    toast("Settings saved");
  } catch (e) {
    toast("Failed: " + e.message, true);
    return;
  }
  try {
    if (key === ctx.urlField) {
      await _syncEndpointRecord(ctx, v, payload[ctx.apiKeyField] || "");
    } else if (key === ctx.apiKeyField && S[ctx.endpointIdKey]) {
      await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { api_key: v });
    } else if (key === ctx.modelField) {
      await _syncModelConfigRecord(ctx, v, payload);
    } else if (ctx.hyperparamKeys.includes(key) && S[ctx.configIdKey]) {
      await api.put(`/models/${S[ctx.configIdKey]}`, { [baseKey]: v });
    }
  } catch (e) {
    console.error("Endpoint/model sync error:", e);
    toast("Failed to sync " + (key === ctx.modelField ? "model" : "endpoint") + ": " + e.message, true);
  }
  updateAgentModelWarning();
}

async function _onHybridInputCtx(ctx, el) {
  const key = el.dataset.key;
  if (key === ctx.urlField) {
    const match = S.endpoints.find((e) => e.url === el.value);
    if (!match) return;
    S[ctx.endpointIdKey] = match.id;
    try {
      const ep = await api.get(`/endpoints/${match.id}`);
      Object.assign(match, ep);
    } catch (e) {
      console.error("Failed to fetch endpoint:", e);
    }
    const apiKeyEl = document.querySelector(`[data-key="${ctx.apiKeyField}"]`);
    if (apiKeyEl) apiKeyEl.value = match.api_key || "";
    await _loadConfigs(ctx, match.id);
    const modelEl = document.querySelector(`[data-key="${ctx.modelField}"]`);
    if (!modelEl || !S[ctx.configsKey].length) return;
    const activeModel = S[ctx.configsKey].find((m) => m.id === match[ctx.activeConfigDbField]) || S[ctx.configsKey][0];
    modelEl.value = activeModel.model_name;
    _fillConfigFields(ctx, activeModel);
    S[ctx.configIdKey] = activeModel.id;
    try {
      await api.put(`/endpoints/${match.id}`, { [ctx.activeConfigDbField]: activeModel.id });
    } catch (e) {
      console.error("Failed to save active model config:", e);
    }
  } else if (key === ctx.modelField) {
    if (S[ctx.endpointIdKey]) {
      try {
        await _loadConfigs(ctx, S[ctx.endpointIdKey]);
      } catch (e) {
        console.error("Failed to refresh model configs:", e);
      }
    }
    const match = S[ctx.configsKey].find((m) => m.model_name === el.value);
    if (!match) return;
    _fillConfigFields(ctx, match);
    S[ctx.configIdKey] = match.id;
    try {
      await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { [ctx.activeConfigDbField]: match.id });
    } catch (e) {
      console.error("Failed to save active model config:", e);
    }
  }
  updateAgentModelWarning();
}

// ── Public API

function populateModelDatalist() {
  const dl = document.getElementById("model-datalist");
  if (!dl) return;
  dl.innerHTML = S.modelConfigs.map((m) => `<option value="${esc(m.model_name)}"></option>`).join("");
}

export async function loadModelConfigs(endpointId) {
  await _loadConfigs(WRITER_CTX, endpointId);
  populateModelDatalist();
}

export async function loadAgentModelConfigs(endpointId) {
  await _loadConfigs(AGENT_CTX, endpointId);
}

export async function saveSetting(el) {
  await _saveEndpointSetting(WRITER_CTX, el);
}

export async function saveAgentSetting(el) {
  await _saveEndpointSetting(AGENT_CTX, el);
}

export async function onHybridInput(el) {
  const key = el.dataset.key;
  if (key === WRITER_CTX.urlField || key === WRITER_CTX.modelField) {
    await _onHybridInputCtx(WRITER_CTX, el);
  } else if (key === AGENT_CTX.urlField || key === AGENT_CTX.modelField) {
    await _onHybridInputCtx(AGENT_CTX, el);
  }
}

// Expose to global scope for inline onclick handlers
window.saveAgentSetting = saveAgentSetting;
window.toggleAgentSameAsWriter = toggleAgentSameAsWriter;

window.toggleApiKeyVisibility = (btn) => {
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
