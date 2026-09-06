import { api } from "./api.js";
import { renderInspector } from "./chat.js";
import { CLOSE_ICON } from "./icons.js";
import { showConfirmModal } from "./modal.js";
import { filterModelChoices, mergeModelChoices } from "./model_catalog.js";
import { S } from "./state.js";
import { $, esc, escAttr, toast } from "./utils.js";
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
  "reasoning_effort",
  "reasoning_effort_param",
  "reasoning_effort_value",
  "extra_headers",
  "extra_body",
];

const STANDARD_REASONING_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh"];

const REASONING_LEVEL_HINTS = [{ url: "nano-gpt.com", model: "glm", levels: ["max"] }];

const SETTING_FIELDS = [
  { k: "endpoint_url", l: "Endpoint URL", t: "text" },
  { k: "api_key", l: "API Key", t: "api_key" },
  { k: "model_name", l: "Model Name", t: "text" },
  {
    k: "completion_mode",
    l: "API Mode",
    t: "select",
    opts: [
      ["chat", "Chat Completions"],
      ["text", "Text Completion (llama.cpp)"],
    ],
  },
  { k: "proxy", l: "Proxy", t: "text", ph: "socks5://127.0.0.1:1080" },
  { k: "shared_system_prompt", l: "System Prompt (global)", t: "textarea" },
  { k: "system_prompt", l: "System Prompt (model)", t: "textarea" },
  { k: "temperature", l: "Temperature", t: "number", s: "0.05", mn: "0", mx: "2" },
  { k: "max_tokens", l: "Max Tokens", t: "number", s: "64", mn: "64", mx: "32768" },
  { k: "top_p", l: "Top P", t: "number", s: "0.05", mn: "0", mx: "1" },
  { k: "min_p", l: "Min P", t: "number", s: "0.01", mn: "0", mx: "1" },
  { k: "top_k", l: "Top K", t: "number", s: "1", mn: "0", mx: "200" },
  { k: "repetition_penalty", l: "Rep. Penalty", t: "number", s: "0.05", mn: "1", mx: "2" },
  { k: "reasoning_effort", l: "Reasoning Effort", t: "reasoning_effort" },
  { k: "extra_headers", l: "Extra Request Headers", t: "textarea", ph: "X-Provider: deepinfra" },
  {
    k: "extra_body",
    l: "Extra Request Body (JSON, chat mode only)",
    t: "textarea",
    ph: '{"provider": {"only": ["deepinfra"]}}',
  },
];

const FIELD_GROUPS = [
  { l: "Prompts", cls: " ep-chat-only", keys: ["shared_system_prompt", "system_prompt"] },
  { l: "Sampling", open: true, keys: ["temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"] },
  { l: "Advanced", keys: ["reasoning_effort", "extra_headers", "extra_body"] },
];

const AGENT_MODEL_HYPERPARAM_KEYS = [
  "agent_shared_system_prompt",
  "agent_temperature",
  "agent_max_tokens",
  "agent_top_p",
  "agent_min_p",
  "agent_top_k",
  "agent_repetition_penalty",
  "agent_reasoning_effort",
  "agent_reasoning_effort_param",
  "agent_reasoning_effort_value",
  "agent_extra_headers",
  "agent_extra_body",
];

const AGENT_SETTING_FIELDS = [
  { k: "agent_endpoint_url", l: "Agent Endpoint URL", t: "text" },
  { k: "agent_api_key", l: "Agent API Key", t: "api_key" },
  { k: "agent_model_name", l: "Agent Model Name", t: "text" },
  {
    k: "agent_completion_mode",
    l: "Agent API Mode",
    t: "select",
    opts: [
      ["chat", "Chat Completions"],
      ["text", "Text Completion (llama.cpp)"],
    ],
  },
  { k: "agent_proxy", l: "Agent Proxy", t: "text", ph: "socks5://127.0.0.1:1080" },
  { k: "agent_shared_system_prompt", l: "Agent System Prompt (global)", t: "textarea" },
  { k: "agent_temperature", l: "Agent Temperature", t: "number", s: "0.05", mn: "0", mx: "2" },
  { k: "agent_max_tokens", l: "Agent Max Tokens", t: "number", s: "64", mn: "64", mx: "32768" },
  { k: "agent_top_p", l: "Agent Top P", t: "number", s: "0.05", mn: "0", mx: "1" },
  { k: "agent_min_p", l: "Agent Min P", t: "number", s: "0.01", mn: "0", mx: "1" },
  { k: "agent_top_k", l: "Agent Top K", t: "number", s: "1", mn: "0", mx: "200" },
  { k: "agent_repetition_penalty", l: "Agent Rep. Penalty", t: "number", s: "0.05", mn: "1", mx: "2" },
  { k: "agent_reasoning_effort", l: "Agent Reasoning Effort", t: "reasoning_effort" },
  { k: "agent_extra_headers", l: "Agent Extra Request Headers", t: "textarea", ph: "X-Provider: deepinfra" },
  {
    k: "agent_extra_body",
    l: "Agent Extra Request Body (JSON, chat mode only)",
    t: "textarea",
    ph: '{"provider": {"only": ["deepinfra"]}}',
  },
];

const WRITER_CTX = {
  role: "writer",
  configsKey: "modelConfigs",
  endpointIdKey: "activeEndpointId",
  configIdKey: "activeModelConfigId",
  urlField: "endpoint_url",
  apiKeyField: "api_key",
  modelField: "model_name",
  completionModeField: "completion_mode",
  proxyField: "proxy",
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
  completionModeField: "agent_completion_mode",
  proxyField: "agent_proxy",
  activeConfigDbField: "agent_active_model_config_id",
  settingsEndpointField: "agent_endpoint_id",
  hyperparamKeys: AGENT_MODEL_HYPERPARAM_KEYS,
  hyperparamPrefix: "agent_",
};

export async function toggleAgentSameAsWriter(checked) {
  S.agentSameAsWriter = checked;
  try {
    await api.put("/settings", { agent_same_as_writer: checked });
  } catch (_e) {
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
  renderInspector(); // the lane swap changes which endpoint gates the prefill box
}

export function renderEndpoints() {
  function renderField(f, isAgent) {
    const v = S.settings[f.k] ?? "";
    const saveFn = isAgent ? "saveAgentSetting" : "saveSetting";
    if (f.t === "textarea") {
      const rows = f.k === "system_prompt" || f.k === "agent_system_prompt" ? ' rows="2"' : "";
      const cls = f.k === "system_prompt" || f.k === "shared_system_prompt" ? " ep-chat-only" : "";
      const ph = f.ph ? ` placeholder="${escAttr(f.ph)}"` : "";
      return `<div class="field${cls}"><label>${f.l}</label>
                <textarea data-key="${f.k}"${rows}${ph} onchange="${saveFn}(this)">${v}</textarea>
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
    if (f.t === "select") {
      const opts = f.opts
        .map(([val, label]) => `<option value="${val}"${v === val ? " selected" : ""}>${esc(label)}</option>`)
        .join("");
      return `<div class="field"><label>${f.l}</label>
                <select data-key="${f.k}" onchange="${saveFn}(this)">${opts}</select>
              </div>`;
    }
    if (f.t === "reasoning_effort") {
      const p = isAgent ? "agent_" : "";
      const paramV = S.settings[`${p}reasoning_effort_param`] ?? "";
      const valueV = S.settings[`${p}reasoning_effort_value`] ?? "";
      return `<div class="field"><label>${f.l}</label>
                <select data-key="${f.k}" data-desired="${esc(v)}"></select>
              </div>
              <div data-reasoning-custom="${p}" style="display:none">
                <div class="field"><label>Reasoning Param Name</label>
                  <input type="text" value="${esc(paramV)}" data-key="${p}reasoning_effort_param" placeholder="reasoning_effort">
                </div>
                <div class="field"><label>Reasoning Param Value</label>
                  <input type="text" value="${esc(valueV)}" data-key="${p}reasoning_effort_value" placeholder="high, 4096, or {&quot;effort&quot;:&quot;high&quot;}">
                </div>
              </div>`;
    }
    const attrs = f.s ? `step="${f.s}" min="${f.mn}" max="${f.mx}"` : "";
    const ph = f.ph ? ` placeholder="${esc(f.ph)}"` : "";
    return `<div class="field"><label>${f.l}</label>
              <input type="${f.t}" value="${v}" data-key="${f.k}" ${attrs}${ph} onchange="${saveFn}(this)">
            </div>`;
  }

  function renderForm(fields, isAgent) {
    const p = isAgent ? "agent_" : "";
    const byKey = new Map(fields.map((f) => [f.k, f]));
    const grouped = new Set(FIELD_GROUPS.flatMap((g) => g.keys.map((k) => p + k)));
    let html = fields
      .filter((f) => !grouped.has(f.k))
      .map((f) => renderField(f, isAgent))
      .join("");
    for (const g of FIELD_GROUPS) {
      const members = g.keys.map((k) => byKey.get(p + k)).filter(Boolean);
      if (!members.length) continue;
      html += `<details class="ep-group${g.cls || ""}"${g.open ? " open" : ""}>
        <summary>${g.l}</summary>
        ${members.map((f) => renderField(f, isAgent)).join("")}
      </details>`;
    }
    return html;
  }

  const agentHidden = S.agentSameAsWriter ? ' style="display:none"' : "";

  $("endpoints-form").innerHTML = `
    ${renderForm(SETTING_FIELDS, false)}
    <div class="ep-chat-only">
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
        ${renderForm(AGENT_SETTING_FIELDS, true)}
      </div>
    </div>
  `;
  initComboboxes();
  updateReasoningEffortFields();
  updateAgentModelWarning();
  updateEndpointsLabel();
}

function _reasoningLevelExtras(prefix) {
  const url = (document.querySelector(`[data-key="${prefix}endpoint_url"]`)?.value || "").toLowerCase();
  const model = (document.querySelector(`[data-key="${prefix}model_name"]`)?.value || "").toLowerCase();
  const extras = [];
  for (const h of REASONING_LEVEL_HINTS) {
    if (!url.includes(h.url)) continue;
    if (h.model && !model.includes(h.model)) continue;
    for (const lvl of h.levels) {
      if (!extras.includes(lvl) && !STANDARD_REASONING_LEVELS.includes(lvl)) extras.push(lvl);
    }
  }
  return extras;
}

function updateReasoningEffortFields() {
  for (const prefix of ["", "agent_"]) {
    const sel = document.querySelector(`[data-key="${prefix}reasoning_effort"]`);
    if (!sel) continue;
    const save = prefix ? saveAgentSetting : saveSetting;
    const desired = sel.dataset.desired ?? sel.value ?? "";
    const levels = [...STANDARD_REASONING_LEVELS, ..._reasoningLevelExtras(prefix)];
    if (desired && desired !== "custom" && !levels.includes(desired)) levels.push(desired);
    sel.innerHTML = [
      `<option value="">Provider default</option>`,
      ...levels.map((l) => `<option value="${esc(l)}"${desired === l ? " selected" : ""}>${esc(l)}</option>`),
      `<option value="custom"${desired === "custom" ? " selected" : ""}>Other...</option>`,
    ].join("");
    sel.value = desired;
    sel.onchange = () => {
      sel.dataset.desired = sel.value;
      save(sel);
      updateReasoningEffortFields();
    };
    const wrap = document.querySelector(`[data-reasoning-custom="${prefix}"]`);
    if (wrap) {
      wrap.style.display = desired === "custom" ? "" : "none";
      for (const input of wrap.querySelectorAll("input[data-key]")) {
        input.onchange = () => save(input);
      }
    }
  }
}

export function updateEndpointsLabel() {
  const el = document.getElementById("endpoints-label");
  if (!el) return;
  const input = document.querySelector('[data-key="model_name"]');
  const model = (input ? input.value : S.settings.model_name || "").trim();
  if (!model || model.toLowerCase() === "default") {
    el.textContent = "Endpoints";
    el.title = "";
    return;
  }
  const MAX = 30;
  const EDGE = 12;
  el.textContent = model.length <= MAX ? model : `${model.slice(0, EDGE)}...${model.slice(-EDGE)}`;
  el.title = model;
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

let _comboboxCleanups = [];
// Finger travel allowed before a touch counts as a scroll rather than a tap.
const TAP_SLOP_PX = 10;
const _availableModels = new Map();
const _availableModelRequests = new Map();

function _invalidateAvailableModels(endpointId) {
  _availableModels.delete(endpointId);
  _availableModelRequests.delete(endpointId);
}

function _modelChoices(ctx) {
  const endpointId = S[ctx.endpointIdKey];
  return mergeModelChoices(S[ctx.configsKey], _availableModels.get(endpointId));
}

async function _loadAvailableModels(ctx) {
  const endpointId = S[ctx.endpointIdKey];
  if (!endpointId) throw new Error("Choose or save an endpoint first");
  if (_availableModels.has(endpointId)) return;

  let request = _availableModelRequests.get(endpointId);
  if (!request) {
    request = api.get(`/endpoints/${endpointId}/available-models`).then((payload) => {
      if (!Array.isArray(payload?.models)) throw new Error("Endpoint returned an invalid models response");
      if (_availableModelRequests.get(endpointId) === request) _availableModels.set(endpointId, payload.models);
    });
    _availableModelRequests.set(endpointId, request);
  }
  try {
    await request;
  } finally {
    if (_availableModelRequests.get(endpointId) === request) _availableModelRequests.delete(endpointId);
  }
}

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
  _comboboxCleanups.forEach((fn) => {
    fn();
  });
  _comboboxCleanups = [];
  const epRoot = document.querySelector('[data-combobox="endpoint_url"]');
  if (epRoot) initCombobox(epRoot, () => S.endpoints.map((e) => ({ value: e.url, id: e.id, type: "endpoint" })));
  const mdRoot = document.querySelector('[data-combobox="model_name"]');
  if (mdRoot)
    initCombobox(mdRoot, () => _modelChoices(WRITER_CTX), {
      searchable: true,
      loadItems: () => _loadAvailableModels(WRITER_CTX),
    });
  const agentEpRoot = document.querySelector('[data-combobox="agent_endpoint_url"]');
  if (agentEpRoot)
    initCombobox(agentEpRoot, () => S.endpoints.map((e) => ({ value: e.url, id: e.id, type: "endpoint" })), {
      isAgent: true,
    });
  const agentMdRoot = document.querySelector('[data-combobox="agent_model_name"]');
  if (agentMdRoot)
    initCombobox(agentMdRoot, () => _modelChoices(AGENT_CTX), {
      isAgent: true,
      searchable: true,
      loadItems: () => _loadAvailableModels(AGENT_CTX),
    });
}

window.deleteComboboxItem = (_btn, type, id, isAgent = false) => {
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
          _invalidateAvailableModels(id);
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
        toast(`Failed to delete: ${e.message}`, true);
      }
    },
  );
};

function initCombobox(rootEl, getItems, { isAgent = false, searchable = false, loadItems = null } = {}) {
  const input = rootEl.querySelector(".cb-input");
  const control = rootEl.querySelector(".cb-control");
  const dropdown = rootEl.querySelector(".cb-dropdown");
  const list = rootEl.querySelector(".cb-list");
  let activeIdx = -1;
  let isOpen = false;
  let isLoading = false;
  let loadError = "";
  let destroyed = false;
  let valueBeforeInput = input.value;
  let valueBeforeSearch = input.value;
  let searchQuery = "";
  let touchTap = null;

  function getFiltered() {
    const items = getItems();
    return searchable ? filterModelChoices(items, searchQuery) : items;
  }

  function render() {
    const items = getFiltered();
    const total = items.length;
    activeIdx = Math.max(-1, Math.min(activeIdx, total - 1));
    const q = searchQuery.trim();
    const optionHtml = items
      .map((item, i) => {
        const value = item.value;
        const id = item.id;
        const type = item.type;
        const agentArg = isAgent ? ", true" : "";
        const idAttrs = id == null ? "" : ` data-id="${id}"`;
        const deleteHtml =
          id == null
            ? ""
            : `<button class="cb-delete-btn" title="Delete" onclick="event.stopPropagation(); deleteComboboxItem(this, '${type}', ${id}${agentArg})">${CLOSE_ICON}</button>`;
        return `
              <div class="cb-option${i === activeIdx ? " active" : ""}" data-value="${escAttr(value)}"${idAttrs} data-type="${escAttr(type)}">
                <span class="cb-option-text">${highlightMatch(value, q)}</span>
                ${deleteHtml}
              </div>`;
      })
      .join("");
    let statusHtml = "";
    if (isLoading) statusHtml = '<div class="cb-status">Loading available models…</div>';
    else if (loadError)
      statusHtml = `<div class="cb-status cb-status-error" title="${escAttr(loadError)}">Available models unavailable; type a model name.</div>`;
    else if (!total)
      statusHtml = `<div class="cb-empty">${searchable ? (q ? "No matching models" : "No available models") : "No saved options"}</div>`;
    list.innerHTML = optionHtml + statusHtml;
    list.querySelectorAll(".cb-option").forEach((el, i) => {
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

  async function openDropdown({ revertValue = input.value, query = "" } = {}) {
    if (isOpen) return;
    isOpen = true;
    valueBeforeSearch = revertValue;
    searchQuery = searchable ? query : "";
    activeIdx = -1;
    control.classList.add("open");
    dropdown.hidden = false;
    isLoading = Boolean(loadItems);
    loadError = "";
    render();
    if (!loadItems) return;
    try {
      await loadItems();
    } catch (e) {
      loadError = e.message || "Model discovery failed";
    } finally {
      isLoading = false;
      if (!destroyed && isOpen) render();
    }
  }

  function closeDropdown() {
    if (!isOpen) return;
    isOpen = false;
    control.classList.remove("open");
    dropdown.hidden = true;
  }

  async function selectVal(val) {
    input.value = val;
    searchQuery = "";
    closeDropdown();
    await onHybridInput(input);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const onInput = () => {
    if (!searchable) return;
    activeIdx = -1;
    searchQuery = input.value;
    if (!isOpen) void openDropdown({ revertValue: valueBeforeInput, query: searchQuery });
    else render();
  };
  const onBeforeInput = () => {
    if (searchable && !isOpen) valueBeforeInput = input.value;
  };
  const onKeydown = (e) => {
    if (e.key === "Escape") {
      if (isOpen) {
        e.preventDefault();
        e.stopPropagation();
        if (searchable) input.value = valueBeforeSearch;
        searchQuery = "";
        closeDropdown();
      }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!isOpen) {
        void openDropdown();
        return;
      }
      const total = getFiltered().length;
      if (!total) return;
      activeIdx = e.key === "ArrowDown" ? (activeIdx + 1) % total : (activeIdx - 1 + total) % total;
      render();
      list.querySelector(".cb-option.active")?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && isOpen && activeIdx >= 0) {
      e.preventDefault();
      const item = getFiltered()[activeIdx];
      if (item) void selectVal(item.value);
    }
  };
  const onControlDown = (e) => {
    if (!e.target.closest(".cb-arrow")) return;
    e.preventDefault();
    const opening = !isOpen;
    if (opening) void openDropdown();
    else closeDropdown();
    input.focus();
    if (opening && searchable) input.select();
  };
  const onControlTouch = (e) => {
    if (!e.target.closest(".cb-arrow")) return;
    e.preventDefault();
    if (isOpen) closeDropdown();
    else void openDropdown();
  };
  // Touch selects on touchend, not touchstart: a finger landing on an option is
  // usually the start of a scroll, and preventDefault-on-touchstart kills it.
  const onListTouchStart = (e) => {
    const touch = e.touches[0];
    const option = e.target.closest(".cb-option");
    touchTap =
      touch && option
        ? {
            x: touch.clientX,
            y: touch.clientY,
            scrollTop: list.scrollTop,
            option,
            deleteBtn: e.target.closest(".cb-delete-btn"),
          }
        : null;
  };
  const onListTouchMove = (e) => {
    if (!touchTap) return;
    const touch = e.touches[0];
    if (!touch) return;
    if (Math.abs(touch.clientX - touchTap.x) > TAP_SLOP_PX || Math.abs(touch.clientY - touchTap.y) > TAP_SLOP_PX)
      touchTap = null;
  };
  const onListTouchCancel = () => {
    touchTap = null;
  };
  const onListTouchEnd = (e) => {
    const tap = touchTap;
    touchTap = null;
    if (!tap || list.scrollTop !== tap.scrollTop) return;
    e.preventDefault();
    if (tap.deleteBtn) {
      window.deleteComboboxItem(tap.deleteBtn, tap.option.dataset.type, Number(tap.option.dataset.id), isAgent);
      return;
    }
    void selectVal(tap.option.dataset.value);
  };
  const onDocDown = (e) => {
    if (!rootEl.contains(e.target)) closeDropdown();
  };
  const onDocTouch = (e) => {
    if (!rootEl.contains(e.target)) closeDropdown();
  };
  input.addEventListener("beforeinput", onBeforeInput);
  input.addEventListener("input", onInput);
  input.addEventListener("keydown", onKeydown);
  control.addEventListener("mousedown", onControlDown);
  control.addEventListener("touchstart", onControlTouch, { passive: false });
  list.addEventListener("touchstart", onListTouchStart, { passive: true });
  list.addEventListener("touchmove", onListTouchMove, { passive: true });
  list.addEventListener("touchcancel", onListTouchCancel, { passive: true });
  list.addEventListener("touchend", onListTouchEnd, { passive: false });
  document.addEventListener("mousedown", onDocDown);
  document.addEventListener("touchstart", onDocTouch, { passive: true });
  _comboboxCleanups.push(() => {
    destroyed = true;
    input.removeEventListener("beforeinput", onBeforeInput);
    input.removeEventListener("input", onInput);
    input.removeEventListener("keydown", onKeydown);
    control.removeEventListener("mousedown", onControlDown);
    control.removeEventListener("touchstart", onControlTouch);
    list.removeEventListener("touchstart", onListTouchStart);
    list.removeEventListener("touchmove", onListTouchMove);
    list.removeEventListener("touchcancel", onListTouchCancel);
    list.removeEventListener("touchend", onListTouchEnd);
    document.removeEventListener("mousedown", onDocDown);
    document.removeEventListener("touchstart", onDocTouch);
    control.classList.remove("open");
    dropdown.hidden = true;
  });
}

export async function loadEndpoints() {
  try {
    S.endpoints = await api.get("/endpoints");
    S.activeEndpointId = S.settings.active_endpoint_id || null;
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
  } catch (_e) {
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
  const reSel = document.querySelector(`[data-key="${p}reasoning_effort"]`);
  if (reSel) {
    reSel.dataset.desired = config.reasoning_effort ?? "";
    updateReasoningEffortFields();
  }
}

function _fillEndpointFields(ctx) {
  const ep = S.endpoints.find((e) => e.id === S[ctx.endpointIdKey]);
  if (ep) {
    const epEl = document.querySelector(`[data-key="${ctx.urlField}"]`);
    if (epEl) epEl.value = ep.url || "";
    const keyEl = document.querySelector(`[data-key="${ctx.apiKeyField}"]`);
    if (keyEl) keyEl.value = ep.api_key || "";
    const cmEl = document.querySelector(`[data-key="${ctx.completionModeField}"]`);
    if (cmEl) cmEl.value = ep.completion_mode || "chat";
    const pxEl = document.querySelector(`[data-key="${ctx.proxyField}"]`);
    if (pxEl) pxEl.value = ep.proxy || "";
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
      _invalidateAvailableModels(existing.id);
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
      reasoning_effort: get("reasoning_effort", ""),
      reasoning_effort_param: get("reasoning_effort_param", ""),
      reasoning_effort_value: get("reasoning_effort_value", ""),
      extra_headers: get("extra_headers", ""),
      extra_body: get("extra_body", ""),
    });
    S[ctx.configsKey].push(mc);
    S[ctx.configIdKey] = mc.id;
    await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { [ctx.activeConfigDbField]: mc.id });
    if (ctx.role === "writer") populateModelDatalist();
    initComboboxes();
  }
}

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
    toast(`Failed: ${e.message}`, true);
    return;
  }
  try {
    if (key === ctx.urlField) {
      await _syncEndpointRecord(ctx, v, payload[ctx.apiKeyField] || "");
    } else if (key === ctx.apiKeyField && S[ctx.endpointIdKey]) {
      await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { api_key: v });
      _invalidateAvailableModels(S[ctx.endpointIdKey]);
    } else if (baseKey === "completion_mode" && S[ctx.endpointIdKey]) {
      await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { completion_mode: v });
      const row = S.endpoints.find((e) => e.id === S[ctx.endpointIdKey]);
      if (row) row.completion_mode = v;
    } else if (baseKey === "proxy" && S[ctx.endpointIdKey]) {
      await api.put(`/endpoints/${S[ctx.endpointIdKey]}`, { proxy: v });
      _invalidateAvailableModels(S[ctx.endpointIdKey]);
    } else if (key === ctx.modelField) {
      await _syncModelConfigRecord(ctx, v, payload);
    } else if (ctx.hyperparamKeys.includes(key) && S[ctx.configIdKey]) {
      const configId = S[ctx.configIdKey];
      await api.put(`/models/${configId}`, { [baseKey]: v });
      S.settings[key] = v;
      const cfg = S[ctx.configsKey].find((m) => m.id === configId);
      if (cfg) cfg[baseKey] = v;
    }
  } catch (e) {
    console.error("Endpoint/model sync error:", e);
    toast(`Failed to sync ${key === ctx.modelField ? "model" : "endpoint"}: ${e.message}`, true);
  }
  updateAgentModelWarning();
  updateEndpointsLabel();
  renderInspector();
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
    const cmEl = document.querySelector(`[data-key="${ctx.completionModeField}"]`);
    if (cmEl) cmEl.value = match.completion_mode || "chat";
    const pxEl = document.querySelector(`[data-key="${ctx.proxyField}"]`);
    if (pxEl) pxEl.value = match.proxy || "";
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
  updateEndpointsLabel();
  renderInspector();
}

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
