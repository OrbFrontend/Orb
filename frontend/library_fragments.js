import { api } from "./api.js";
import { decisionConfig, loadDecisionConfig } from "./decisions.js";
import { initDragReorder } from "./drag_reorder.js";
import { GRIP_ICON } from "./icons.js";
import {
  applyDecisionProblems,
  decisionDraftProblems,
  decisionSectionHtml,
  fitDecisionTextareas,
  initDecisionDraft,
  readDecisionFields,
  repaintDecisionSection,
} from "./library_decisions.js";
import { closeModal, closeSubModal, confirmDelete, showModal, showSubModal } from "./modal.js";
import { S, upgradeLegacyFragment } from "./state.js";
import { refreshState, updateStateTab } from "./state_panel.js";
import { $, boolFlag, esc, escAttr, escHandlerArg, toast } from "./utils.js";
import { validate } from "./validate.js";

const _dragAndDropContainers = new WeakSet();

export async function loadMoodFragments() {
  try {
    S.moodFragments = await api.get("/fragments");
    renderMoodFragments();
  } catch (error) {
    console.error("Failed to load mood fragments:", error);
    throw error;
  }
}

export function renderMoodFragments() {
  const cardHtml = _cardMoodSidepanelHtml();
  const addBtn = `<button class="btn btn-block btn-sm" onclick="showMoodFragmentModal()" style="margin-top:6px">+ Add Mood Fragment</button>`;
  if ((!S.moodFragments || S.moodFragments.length === 0) && !cardHtml) {
    $("frag-list").innerHTML =
      `<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No mood fragments</div>${addBtn}`;
    return;
  }

  const html = (S.moodFragments || [])
    .map((f) => {
      const enabled = boolFlag(f.enabled);
      const toggleId = `frag-toggle-${f.id}`;
      return `
    <div class="fragment-item" style="cursor:pointer" title="${escAttr(f.description)}" onclick="showMoodFragmentModal('${escHandlerArg(f.id)}')">
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label)}</span>
      </div>
      <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
        <label class="tog" for="${toggleId}">
          <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                 onchange="toggleMoodFragmentEnabled('${escHandlerArg(f.id)}', this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
    </div>`;
    })
    .join("");

  $("frag-list").innerHTML = html + addBtn + cardHtml;
}

function _moodFragFormHtml(d, isEdit) {
  return `
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="frag-id" value="${escAttr(d.id)}" ${isEdit ? "disabled" : ""} placeholder="terse"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="frag-label" value="${escAttr(d.label)}" placeholder="Terse"></div>
    </div>
    <div class="field"><label>Description <span style="font-size:10px;color:var(--text-muted)">(tells the Director when to activate)</span></label>
      <input id="frag-desc" value="${escAttr(d.description)}" placeholder="The scene is finalizing and deserves a spontaneous haiku."></div>
    <div class="field"><label>Prompt Text <span style="font-size:10px;color:var(--text-muted)">(injected into the writer context when this mood is active)</span></label>
      <textarea id="frag-text" rows="4" placeholder="Write the reply like a haiku, strictly following haiku format (5-7-5).">${esc(d.prompt_text)}</textarea></div>
    <div class="field">
      <label>Negative Prompt <span style="font-size:10px;color:var(--text-muted)">(injected if this fragment is removed next turn)</span></label>
      <textarea id="frag-neg" rows="3" placeholder="Stop writing like it's a haiku.">${esc(d.negative_prompt || "")}</textarea>
    </div>
    <div class="field-row">
      <div class="field"><label>Cooldown (turns)</label>
        <input id="frag-cooldown" type="number" min="0" max="50" step="1" value="${escAttr(d.cooldown_turns || 0)}"></div>
    </div>`;
}

function _readMoodFragForm() {
  return {
    id: $("frag-id").value.trim(),
    label: $("frag-label").value.trim(),
    description: $("frag-desc").value.trim(),
    prompt_text: $("frag-text").value.trim(),
    negative_prompt: $("frag-neg").value.trim(),
    cooldown_turns: parseInt($("frag-cooldown").value, 10) || 0,
  };
}

export function showMoodFragmentModal(fragId = null) {
  const f = fragId ? S.moodFragments.find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  const d = f || { id: "", label: "", description: "", prompt_text: "", negative_prompt: "", cooldown_turns: 0 };

  showModal(`
    <h2>${isEdit ? "Edit Mood Fragment" : "New Mood Fragment"}</h2>
    ${_moodFragFormHtml(d, isEdit)}
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteMoodFragment('${escHandlerArg(d.id)}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveMoodFragment(${isEdit})">${isEdit ? "Save" : "Create"}</button>
    </div>`);
}

export async function saveMoodFragment(isEdit) {
  const d = _readMoodFragForm();
  const validation = validate.validateMoodFragment(d);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    if (isEdit) await api.put(`/fragments/${d.id}`, d);
    else await api.post("/fragments", d);
    closeModal();
    await loadMoodFragments();
    toast("Mood fragment saved");
  } catch (e) {
    toast(e.message, true);
  }
}

export async function deleteMoodFragment(id) {
  confirmDelete("Mood Fragment", "Are you sure you want to delete this mood fragment?", async () => {
    try {
      await api.del(`/fragments/${id}`);
      await loadMoodFragments();
      toast("Mood fragment deleted");
    } catch (e) {
      toast(e.message, true);
    }
  });
}

export async function toggleMoodFragmentEnabled(id, newEnabled) {
  try {
    await api.put(`/fragments/${id}`, { enabled: newEnabled });
    const frag = S.moodFragments.find((f) => f.id === id);
    if (frag) frag.enabled = newEnabled;
    renderMoodFragments();
    toast(newEnabled ? "Mood fragment enabled" : "Mood fragment disabled");
  } catch (e) {
    toast(e.message, true);
  }
}

// Group interactive fragments by Judge, Director, and Editor stages.
const EDITOR_LANE_FIELD_TYPES = new Set(["feedback", "post_processing"]);

const INTERACTIVE_LANES = [
  { id: "judge", label: "Judge", hint: "Decides questions before the Director runs" },
  { id: "director", label: "Director", hint: "Directs the scene the writer works from" },
  {
    id: "state",
    label: "State",
    hint: "Kept across turns; updated by the Agent or by you in the Inspector's State tab",
  },
  { id: "editor", label: "Editor", hint: "Acts on the reply after it is written" },
];

function _interactiveLane(f) {
  if (f.field_type === "decision") return "judge";
  if (f.field_type === "state") return "state";
  return EDITOR_LANE_FIELD_TYPES.has(f.field_type) ? "editor" : "director";
}

export async function loadInteractiveFragments() {
  try {
    // Load config before opening the editor so its controls are ready to render.
    loadDecisionConfig();
    S.interactiveFragments = await api.get("/interactive-fragments");
    renderInteractiveFragments();
    refreshState();
  } catch (error) {
    console.error("Failed to load interactive fragments:", error);
    throw error;
  }
}

export function renderInteractiveFragments() {
  updateStateTab();
  const el = document.getElementById("interactive-frag-list");
  if (!el) return;
  const cardHtml = _cardInteractiveSidepanelHtml();
  const addBtn = `<button class="btn btn-block btn-sm" onclick="showInteractiveFragmentModal()" style="margin-top:6px">+ Add Interactive Fragment</button>`;
  if ((!S.interactiveFragments || S.interactiveFragments.length === 0) && !cardHtml) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No interactive fragments</div>${addBtn}`;
    return;
  }

  const sorted = [...(S.interactiveFragments || [])].sort((a, b) => {
    const orderA = a.sort_order || 0;
    const orderB = b.sort_order || 0;
    if (orderA !== orderB) return orderA - orderB;
    return a.id.localeCompare(b.id);
  });

  const lanes = INTERACTIVE_LANES.map((lane) => ({
    ...lane,
    items: sorted.filter((f) => _interactiveLane(f) === lane.id),
  })).filter((lane) => lane.items.length);
  // Only one lane in play needs no grouping at all: the section header already
  // accounts for it, and tinting every row would be decoration without a point.
  const grouped = lanes.length > 1;

  const html = lanes
    .map((lane) => {
      const rows = lane.items.map(_interactiveFragmentRowHtml).join("");
      if (!grouped) return rows;
      // One band behind the whole lane, rather than a tint per row: the rows
      // keep the same shape and spacing they have under Mood Fragments.
      return `<div class="frag-lane frag-lane-${lane.id}">
      <div class="frag-lane-heading" title="${escAttr(lane.hint)}">${esc(lane.label)}</div>${rows}
    </div>`;
    })
    .join("");

  el.innerHTML = html + addBtn + cardHtml;
  setupDragAndDrop(el);
}

function _interactiveFragmentRowHtml(f) {
  const enabled = boolFlag(f.enabled);
  const toggleId = `interactive-frag-toggle-${f.id}`;
  const userBadge = _interactiveTypeBadge(f);
  const { disabled: featureDisabled, title: itemTitle } = _featureGate(f);
  return `
    <div class="fragment-item${featureDisabled ? " frag-feature-disabled" : ""}" data-id="${escAttr(f.id)}" title="${escAttr(itemTitle)}" onclick="showInteractiveFragmentModal('${escHandlerArg(f.id)}')">
      <button type="button" class="frag-drag-handle" title="Drag, or use the arrow keys, to reorder" aria-label="Reorder ${escAttr(f.label)}" onclick="event.stopPropagation()">${GRIP_ICON}</button>
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label)}</span>${userBadge}
      </div>
      <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
        <label class="tog" for="${toggleId}">
          <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                 onchange="toggleInteractiveFragmentEnabled('${escHandlerArg(f.id)}', this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
    </div>`;
}

function setupDragAndDrop(container) {
  if (_dragAndDropContainers.has(container)) return;
  _dragAndDropContainers.add(container);
  initDragReorder(container, {
    itemSelector: ".fragment-item",
    handleSelector: ".frag-drag-handle",
    itemContainer: (item, root) => item.closest(".frag-lane") || root,
    onReorder: updateFragmentOrder,
  });
}

function updateFragmentOrder(container) {
  const items = container.querySelectorAll(".fragment-item");
  // A lane's order is its priority for the passes that consume it. Retain the
  // lane's existing global priority slots instead of renumbering every
  // fragment: that keeps each lane's priorities independent.
  const prioritySlots = Array.from(items)
    .map((item) => {
      const fragment = S.interactiveFragments.find((f) => f.id === item.dataset.id);
      return Number(fragment?.sort_order) || 0;
    })
    .sort((a, b) => a - b);
  const updatedOrder = Array.from(items).map((item, index) => ({
    id: item.dataset.id,
    sort_order: prioritySlots[index],
  }));
  updatedOrder.forEach(({ id, sort_order }) => {
    const frag = S.interactiveFragments.find((f) => f.id === id);
    if (frag) frag.sort_order = sort_order;
  });
  api
    .put("/interactive-fragments/reorder", { items: updatedOrder })
    .then(() => {
      toast("Interactive fragments reordered");
    })
    .catch((e) => {
      console.error("Reorder failed", e);
      toast("Failed to save order", true);
    });
}

const INTERACTIVE_FRAGMENT_EXAMPLES = {
  string: {
    id: "e.g. pacing",
    label: "e.g. Pacing",
    injection_label: "e.g. Pacing",
    description: "Set the pace of the narration, e.g. 'slow', 'fast', 'time-skip'",
    inj_hint: "sent to the writer",
    desc_hint: "tells the Director what this is about",
  },
  array: {
    id: "e.g. plot_threads",
    label: "e.g. Plot Threads",
    injection_label: "e.g. Active Threads",
    description: "List the active plot threads, e.g. 'unresolved rivalry', 'looming deadline'",
    inj_hint: "sent to the writer",
    desc_hint: "tells the Director what this is about",
  },
  state: {
    id: "e.g. trust",
    label: "e.g. Trust",
    injection_label: "e.g. Trust",
    description:
      "How far the character trusts the user now, and what earned or cost it, e.g. 'wary: the user lied about the key'",
    inj_hint: "state block heading",
    desc_hint: "tells the Agent what to record",
  },
  feedback: {
    id: "e.g. next_actions",
    label: "e.g. Next Actions",
    injection_label: "e.g. What you could do next",
    description:
      "A short out-of-character note shown to you after each reply, e.g. 'suggest what the player could do or say next'",
    inj_hint: "shown to you",
    desc_hint: "tells the Editor what this is about",
  },
  decision: {
    id: "e.g. outcome",
    label: "e.g. Outcome",
    injection_label: "e.g. Outcome",
    inj_hint: "for Director & Writer",
  },
  post_processing: {
    id: "e.g. tighten_dialogue",
    label: "e.g. Tighten Dialogue",
    injection_label: "e.g. Tighten Dialogue",
    description:
      "Rewrite spoken dialogue to be shorter and more natural. Preserve meaning and characterization; do not change narration.",
    inj_hint: "sent to the Editor",
    desc_hint: "editing instruction followed by the Editor",
  },
};

// Track whether changing the current type will clear saved decision fields.
let _editingStoredDecision = false;

function _openDecisionDraft(fragment) {
  _editingStoredDecision = fragment.field_type === "decision";
  initDecisionDraft(fragment);
  loadDecisionConfig().then(repaintDecisionSection);
}

export function updateInteractiveFragmentExample(fieldType) {
  const ex = INTERACTIVE_FRAGMENT_EXAMPLES[fieldType] || INTERACTIVE_FRAGMENT_EXAMPLES.string;
  const isDecision = fieldType === "decision";
  const set = (elId, placeholder) => {
    const el = document.getElementById(elId);
    if (el) el.placeholder = placeholder;
  };
  set("interactive-frag-id", ex.id);
  set("interactive-frag-label", ex.label);
  set("interactive-frag-inj-label", ex.injection_label);
  set("interactive-frag-desc", ex.description || "");
  const setHint = (elId, text) => {
    const el = document.getElementById(elId);
    if (el) el.textContent = `(${text})`;
  };
  setHint("interactive-frag-inj-hint", ex.inj_hint);
  setHint("interactive-frag-desc-hint", ex.desc_hint || "");
  // Decision definitions use their own question and outcome fields.
  const descRow = document.getElementById("interactive-frag-desc-row");
  if (descRow) descRow.style.display = isDecision ? "none" : "";
  _syncStateControls();
  const decisionSection = document.getElementById("decision-section");
  if (decisionSection) decisionSection.style.display = isDecision ? "" : "none";
  if (isDecision) fitDecisionTextareas();
  const leaving = document.getElementById("interactive-frag-decision-warning");
  // Warn before a type change clears the saved decision fields.
  if (leaving) leaving.style.display = _editingStoredDecision && !isDecision ? "" : "none";
}

// The three state settings; new fragments start from the backend defaults.
const STATE_SETTING_KEYS = ["state_mode", "state_update", "state_inject"];
const STATE_DEFAULTS = { mode: "value", update: "after_reply", inject: "both" };
const STATE_CHOICES = {
  mode: [
    ["value", "One value"],
    ["entries", "Multiple entries"],
  ],
  update: [
    ["after_reply", "After the reply"],
    ["before_writer", "Before the Writer"],
    ["manual", "Manual only"],
  ],
  inject: [
    ["off", "Off"],
    ["director", "Director"],
    ["writer", "Writer"],
    ["both", "Both"],
  ],
};

/** Required applies where the Director fills the field: scene fields, and a value it sets before the Writer. */
function _requiredApplies(fieldType, mode, update) {
  if (fieldType === "state") return (mode || STATE_DEFAULTS.mode) === "value" && update === "before_writer";
  return fieldType !== "post_processing" && fieldType !== "decision";
}

function _stateSelectValue(setting) {
  return document.getElementById(`interactive-frag-state-${setting}`)?.value || STATE_DEFAULTS[setting];
}

/** What the chosen settings do, by their effect. */
function _stateHint(mode, update) {
  const modeText =
    mode === "entries"
      ? "Keeps up to 12 entries; updates add new ones and retire stale ones."
      : "Keeps one value; each update replaces it.";
  const updateText = {
    after_reply: "Updated after the reply, from what it actually showed.",
    before_writer: `Updated with the Director's scene direction, so it records intent the Writer may not carry out. Kept as is while the Agent or Direction is off.`,
    manual: "Only you change it, in the Inspector's State tab.",
  }[update];
  return `${modeText} ${updateText} Changing the mode keeps saved state.`;
}

function _stateSectionHtml(d) {
  const current = {
    mode: d.state_mode || STATE_DEFAULTS.mode,
    update: d.state_update || STATE_DEFAULTS.update,
    inject: d.state_inject || STATE_DEFAULTS.inject,
  };
  const select = (setting, label) => `<div class="field"><label>${label}</label>
      <select id="interactive-frag-state-${setting}" data-state-setting="${setting}">
        ${STATE_CHOICES[setting].map(([value, text]) => `<option value="${value}" ${current[setting] === value ? "selected" : ""}>${text}</option>`).join("")}
      </select></div>`;
  return `<div id="interactive-frag-state-section" style="${d.field_type === "state" ? "" : "display:none"}">
    <div class="field-row">
      ${select("mode", "Mode")}
      ${select("update", "Update")}
      ${select("inject", "Inject")}
    </div>
    <div class="field-hint" id="interactive-frag-state-hint">${esc(_stateHint(current.mode, current.update))}</div>
  </div>`;
}

/** Show the state settings for a state fragment, and Required only where it applies. */
function _syncStateControls() {
  const fieldType = document.getElementById("interactive-frag-type")?.value;
  const isState = fieldType === "state";
  const section = document.getElementById("interactive-frag-state-section");
  if (section) section.style.display = isState ? "" : "none";
  const mode = _stateSelectValue("mode");
  const update = _stateSelectValue("update");
  const hint = document.getElementById("interactive-frag-state-hint");
  if (hint) hint.textContent = _stateHint(mode, update);
  const applies = _requiredApplies(fieldType, mode, update);
  const requiredRow = document.getElementById("interactive-frag-required-row");
  if (requiredRow) requiredRow.style.display = applies ? "" : "none";
  const required = document.getElementById("interactive-frag-required");
  if (required && !applies) required.checked = false;
}

document.addEventListener("change", (e) => {
  if (e.target.closest?.("[data-state-setting]")) _syncStateControls();
});

function _interactiveFragFormHtml(d, isEdit) {
  const ex = INTERACTIVE_FRAGMENT_EXAMPLES[d.field_type] || INTERACTIVE_FRAGMENT_EXAMPLES.string;
  return `<div class="ifrag-form">
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="interactive-frag-id" value="${escAttr(d.id)}" ${isEdit ? "disabled" : ""} placeholder="${escAttr(ex.id)}"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="interactive-frag-label" value="${escAttr(d.label)}" placeholder="${escAttr(ex.label)}"></div>
      <div class="field field-narrow"><label>Cooldown</label>
        <input id="interactive-frag-cooldown" type="number" min="0" max="50" step="1" value="${escAttr(d.cooldown_turns || 0)}" title="Turns to wait before this fragment can fire again"></div>
    </div>
    <div class="field-row">
      <div class="field"><label>Injection Label <span id="interactive-frag-inj-hint" style="font-size:10px;color:var(--text-muted)">(${esc(ex.inj_hint)})</span></label>
        <input id="interactive-frag-inj-label" value="${escAttr(d.injection_label)}" placeholder="${escAttr(ex.injection_label)}"></div>
      <div class="field"><label>Field Type</label>
        <select id="interactive-frag-type" onchange="updateInteractiveFragmentExample(this.value)">
          <option value="string" ${d.field_type === "string" ? "selected" : ""}>single</option>
          <option value="array" ${d.field_type === "array" ? "selected" : ""}>list</option>
          <option value="state" ${d.field_type === "state" ? "selected" : ""}>state (kept across turns)</option>
          <option value="feedback" ${d.field_type === "feedback" ? "selected" : ""}>feedback (note to you)</option>
          <option value="post_processing" ${d.field_type === "post_processing" ? "selected" : ""}>post-processing (edits reply)</option>
          <option value="decision" ${d.field_type === "decision" ? "selected" : ""}>decision (asks a question)</option>
        </select>
      </div>
    </div>
    ${_stateSectionHtml(d)}
    <div class="field" id="interactive-frag-desc-row" style="${d.field_type === "decision" ? "display:none" : ""}">
      <label>Description <span id="interactive-frag-desc-hint" style="font-size:10px;color:var(--text-muted)">(${esc(ex.desc_hint || "")})</span></label>
      <textarea id="interactive-frag-desc" rows="4" placeholder="${escAttr(ex.description || "")}">${esc(d.description)}</textarea></div>
    <div class="field-row" id="interactive-frag-required-row" style="${_requiredApplies(d.field_type, d.state_mode, d.state_update) ? "" : "display:none"}">
      <div class="field" style="align-self:flex-end;padding-bottom:4px">
        <label class="modal-checkbox-label">
          <input type="checkbox" id="interactive-frag-required" ${d.required ? "checked" : ""}> Required
        </label>
      </div>
    </div>
    <div class="field-warning" id="interactive-frag-decision-warning" style="display:none">
      Saving this as another field type clears the question, its outcomes and its guidance.
    </div>
    ${decisionSectionHtml(d.field_type)}
  </div>`;
}

function _readInteractiveFragForm() {
  const fieldType = document.getElementById("interactive-frag-type").value;
  const base = {
    id: document.getElementById("interactive-frag-id").value.trim(),
    label: document.getElementById("interactive-frag-label").value.trim(),
    description: fieldType === "decision" ? "" : document.getElementById("interactive-frag-desc").value.trim(),
    field_type: fieldType,
    required: _requiredApplies(fieldType, _stateSelectValue("mode"), _stateSelectValue("update"))
      ? document.getElementById("interactive-frag-required").checked
      : false,
    injection_label: document.getElementById("interactive-frag-inj-label").value.trim(),
    cooldown_turns: parseInt(document.getElementById("interactive-frag-cooldown").value, 10) || 0,
  };
  if (fieldType === "state") {
    base.state_mode = _stateSelectValue("mode");
    base.state_update = _stateSelectValue("update");
    base.state_inject = _stateSelectValue("inject");
  }
  // The backend clears decision fields when a write names another type.
  return fieldType === "decision" ? { ...base, ...readDecisionFields() } : base;
}

export function showInteractiveFragmentModal(fragId = null) {
  const f = fragId ? S.interactiveFragments.find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  const d = f || {
    id: "",
    label: "",
    description: "",
    field_type: "string",
    required: false,
    injection_label: "",
    sort_order: 0,
    cooldown_turns: 0,
  };
  _openDecisionDraft(d);

  showModal(`
    <h2>${isEdit ? "Edit" : "New"} Interactive Fragment</h2>
    ${_interactiveFragFormHtml(d, isEdit)}
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteInteractiveFragment('${escHandlerArg(d.id)}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveInteractiveFragment(${isEdit})">${isEdit ? "Save" : "Create"}</button>
    </div>`);
}

export async function saveInteractiveFragment(isEdit) {
  const d = _readInteractiveFragForm();
  const validation = validate.validateInteractiveFragment(d);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  // Option names collapse into the JSON objects the columns are sent as, so a
  // blank or repeated one has to be caught before the request or it is caught
  // by nobody. Routed through the same renderer as a 422.
  if (d.field_type === "decision" && _showDecisionProblems(decisionDraftProblems())) return;
  try {
    if (isEdit) await api.put(`/interactive-fragments/${d.id}`, d);
    else await api.post("/interactive-fragments", d);
    closeModal();
    await loadInteractiveFragments();
    toast("Interactive fragment saved");
  } catch (e) {
    // The whole definition is validated on the merged row and comes back as
    // problems joined by "; ". Render them against the fields they name -- a
    // toast would scroll a rule away from the field it is about.
    if (e.status === 422 && d.field_type === "decision" && _showDecisionProblems(e.message)) return;
    toast(e.message, true);
  }
}

/** Render *detail*'s problems against the decision fields; false when there were none. */
function _showDecisionProblems(detail) {
  if (!applyDecisionProblems(detail)) return false;
  toast("This decision is not valid yet; see the highlighted fields", true);
  return true;
}

export async function deleteInteractiveFragment(id) {
  confirmDelete("Interactive Fragment", "Are you sure you want to delete this interactive fragment?", async () => {
    try {
      await api.del(`/interactive-fragments/${id}`);
      await loadInteractiveFragments();
      toast("Interactive fragment deleted");
    } catch (e) {
      toast(e.message, true);
    }
  });
}

export async function toggleInteractiveFragmentEnabled(id, newEnabled) {
  try {
    await api.put(`/interactive-fragments/${id}`, { enabled: newEnabled });
    const frag = S.interactiveFragments.find((f) => f.id === id);
    if (frag) frag.enabled = newEnabled;
    renderInteractiveFragments();
    // A disabled state fragment turns read-only in the Inspector's State tab.
    if (frag?.field_type === "state") refreshState();
    toast(newEnabled ? "Interactive fragment enabled" : "Interactive fragment disabled");
  } catch (e) {
    toast(e.message, true);
  }
}

function _interactiveTypeBadge(f) {
  return f.field_type === "feedback"
    ? ` <span class="frag-type-badge" title="Feedback fragment">F</span>`
    : f.field_type === "state"
      ? ` <span class="frag-type-badge" title="State fragment: ${f.state_mode === "entries" ? "multiple entries" : "one value"}">S</span>`
      : f.field_type === "post_processing"
        ? ` <span class="frag-type-badge" title="Post-processing fragment">P</span>`
        : f.field_type === "decision"
          ? ` <span class="frag-type-badge" title="Decision fragment">?</span>`
          : "";
}

/** Why a state fragment's automatic updates do not run, or "" when they do. */
function _stateUpdatesOffReason(f) {
  if (f.field_type !== "state" || f.state_update === "manual") return "";
  if (!S.stateUpdates) return "State updates are off";
  if (!S.agentEnabled) return "The Agent is off";
  if (f.state_update === "before_writer" && !S.enabledTools.direct_scene) return "The Direction tool is off";
  return "";
}

function _featureGate(f) {
  // Not disabled: the fragment is still injected and editable by hand.
  const stateUpdatesOff = _stateUpdatesOffReason(f);
  const agentOff = (f.field_type === "post_processing" || f.field_type === "feedback") && !S.agentEnabled;
  // Without a Judge, decisions remain enabled but are skipped at runtime.
  const judgeOff = f.field_type === "decision" && decisionConfig()?.configured === false;
  const title = stateUpdatesOff
    ? `${stateUpdatesOff} -- this fragment is still injected and editable in the Inspector's State tab, but not updated automatically`
    : agentOff
      ? "Agent is disabled -- enable it to use this fragment"
      : judgeOff
        ? "No Judge endpoint is configured -- this decision is skipped"
        : f.description || "";
  return { disabled: agentOff, title };
}

function _cardMoodSidepanelHtml() {
  const frags = S.cardMoodFragments || [];
  if (!frags.length) return "";
  const items = frags.map((f) => `<span title="${escAttr(f.description || "")}">${esc(f.label)}</span>`).join("");
  return `<div class="frag-divider">From character</div><div class="frag-card-list">${items}</div>`;
}

function _cardInteractiveSidepanelHtml() {
  const frags = S.cardInteractiveFragments || [];
  if (!frags.length) return "";
  const items = frags
    .map((f) => {
      const { disabled, title } = _featureGate(f);
      return `<span${disabled ? ' class="frag-feature-disabled"' : ""} title="${escAttr(title)}">${esc(f.label)}${_interactiveTypeBadge(f)}</span>`;
    })
    .join("");
  return `<div class="frag-divider">From character</div><div class="frag-card-list">${items}</div>`;
}

let _cardFragPending = null;

export function initCardFragments(fragments) {
  _cardFragPending = {
    mood: Array.isArray(fragments?.mood) ? structuredClone(fragments.mood) : [],
    interactive: Array.isArray(fragments?.interactive)
      ? structuredClone(fragments.interactive).map(upgradeLegacyFragment)
      : [],
  };
}

export function readCardFragments() {
  return _cardFragPending;
}

export function renderCardFragmentsTab() {
  const el = document.getElementById("ce-card-frag-list");
  if (!el || !_cardFragPending) return;
  const row = (type, f) => `
    <div class="fragment-item" data-type="${escAttr(type)}" data-id="${escAttr(f.id)}">
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label || f.id)}</span>${type === "mood" ? "" : _interactiveTypeBadge(f)}
        ${f.description ? `<div class="frag-desc">${esc(f.description)}</div>` : ""}
      </div>
      <div class="frag-toggle-wrapper" data-action="toggle">
        <label class="tog">
          <input type="checkbox" ${f.enabled === false ? "" : "checked"}>
          <span class="tog-slider"></span>
        </label>
      </div>
    </div>`;
  const moods = _cardFragPending.mood.map((f) => row("mood", f)).join("");
  const interactive = _cardFragPending.interactive.map((f) => row("interactive", f)).join("");
  el.innerHTML =
    moods || interactive
      ? `${interactive ? `<div class="frag-divider">Interactive</div>${interactive}` : ""}
         ${moods ? `<div class="frag-divider">Mood</div>${moods}` : ""}`
      : '<div class="card-frag-empty">No fragments on this character yet</div>';
  if (!el.dataset.wired) {
    el.dataset.wired = "1";
    el.addEventListener("click", (e) => {
      const item = e.target.closest("[data-id]");
      if (!item) return;
      if (e.target.closest("[data-action='toggle']")) return; // handled on change
      const { type, id } = item.dataset;
      if (type === "mood") showCardMoodFragmentModal(id);
      else showCardInteractiveFragmentModal(id);
    });
    el.addEventListener("change", (e) => {
      const item = e.target.closest("[data-id]");
      if (!item || e.target.type !== "checkbox") return;
      const f = _cardFragPending[item.dataset.type].find((x) => x.id === item.dataset.id);
      if (f) f.enabled = e.target.checked;
    });
  }
}

function _wireCardFragModal(type, isEdit, fragId) {
  $("card-frag-cancel").addEventListener("click", closeSubModal);
  if (isEdit) {
    $("card-frag-delete").addEventListener("click", () => {
      _cardFragPending[type] = _cardFragPending[type].filter((f) => f.id !== fragId);
      closeSubModal();
      renderCardFragmentsTab();
    });
  }
  $("card-frag-save").addEventListener("click", async () => {
    const d = type === "mood" ? _readMoodFragForm() : _readInteractiveFragForm();
    const validation = type === "mood" ? validate.validateMoodFragment(d) : validate.validateInteractiveFragment(d);
    if (!validation.valid) {
      toast(validation.error, true);
      return;
    }
    // Card decisions bypass fragment routes, so validate them before saving.
    if (d.field_type === "decision") {
      if (_showDecisionProblems(decisionDraftProblems())) return;
      try {
        await api.post("/decisions/validate", d);
      } catch (e) {
        if (!(e.status === 422 && _showDecisionProblems(e.message))) toast(e.message, true);
        return;
      }
    }
    const globals = type === "mood" ? S.moodFragments : S.interactiveFragments;
    if (!isEdit && (globals.some((g) => g.id === d.id) || _cardFragPending[type].some((f) => f.id === d.id))) {
      toast(`A ${type === "mood" ? "mood" : "interactive"} fragment with this ID already exists`, true);
      return;
    }
    const existing = isEdit ? _cardFragPending[type].find((f) => f.id === fragId) : null;
    d.enabled = existing ? existing.enabled !== false : true;
    if (existing && d.field_type !== "state") {
      for (const key of STATE_SETTING_KEYS) delete existing[key];
    }
    if (existing) Object.assign(existing, d);
    else _cardFragPending[type].push(d);
    closeSubModal();
    renderCardFragmentsTab();
  });
}

function _showCardFragModal(type, kind, fragId, blank, formHtml) {
  const f = fragId ? _cardFragPending[type].find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  if (type === "interactive") _openDecisionDraft(f || blank);
  showSubModal(`
    <h2>${isEdit ? "Edit" : "New"} Character ${kind} Fragment</h2>
    ${formHtml(f || blank, isEdit)}
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" id="card-frag-delete">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" id="card-frag-cancel">Cancel</button>
      <button class="btn btn-accent" id="card-frag-save">${isEdit ? "Save" : "Add"}</button>
    </div>`);
  _wireCardFragModal(type, isEdit, fragId);
}

export function showCardMoodFragmentModal(fragId = null) {
  const blank = { id: "", label: "", description: "", prompt_text: "", negative_prompt: "", cooldown_turns: 0 };
  _showCardFragModal("mood", "Mood", fragId, blank, _moodFragFormHtml);
}

export function showCardInteractiveFragmentModal(fragId = null) {
  const blank = {
    id: "",
    label: "",
    description: "",
    field_type: "string",
    required: false,
    injection_label: "",
    cooldown_turns: 0,
  };
  _showCardFragModal("interactive", "Interactive", fragId, blank, _interactiveFragFormHtml);
}
