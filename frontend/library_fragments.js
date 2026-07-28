// Mood fragments and interactive fragments: their sidebar lists, edit modals, and
// CRUD + reorder. Split out of library.js; the public surface is re-exported
// from library.js.
import { api } from "./api.js";
import { renderView } from "./extension_renderer.js";
import {
  parseStructuredConfigValue,
  schemaConfigDefaults,
  schemaDefaultValue,
  setConfigDraftPath,
} from "./fragment_type_config.js";
import { closeModal, closeSubModal, confirmDelete, showModal, showSubModal } from "./modal.js";
import { S } from "./state.js";
import { $, boolFlag, esc, escAttr, escHandlerArg, toast } from "./utils.js";
import { validate } from "./validate.js";

const _dragAndDropContainers = new WeakSet();

// ── Mood Fragments
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
  // Add button sits with the global list, above the "From character" divider.
  const addBtn = `<button class="btn btn-block btn-sm" onclick="showMoodFragmentModal()" style="margin-top:6px">+ Add Mood Fragment</button>`;
  if ((!S.moodFragments || S.moodFragments.length === 0) && !cardHtml) {
    $("frag-list").innerHTML =
      `<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No mood fragments</div>${addBtn}`;
    return;
  }

  const html = (S.moodFragments || [])
    .map((f) => {
      // Handle both boolean and numeric (0/1) enabled values from backend
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

// Shared field markup for the global modal and the card-scoped sub-modal (same
// element ids — the two are never open at once).
function _moodFragFormHtml(d, isEdit) {
  return `
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="frag-id" value="${escAttr(d.id)}" ${isEdit ? "disabled" : ""} placeholder="e.g. dramatic"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="frag-label" value="${escAttr(d.label)}" placeholder="Terse"></div>
    </div>
    <div class="field"><label>Description <span style="font-size:10px;color:var(--text-muted)">(tells the Director when to activate — sent in its tool schema)</span></label>
      <input id="frag-desc" value="${escAttr(d.description)}" placeholder="Short, clipped sentences. Minimal description."></div>
    <div class="field"><label>Prompt Text <span style="font-size:10px;color:var(--text-muted)">(injected into the writer context when this mood is active)</span></label>
      <textarea id="frag-text" rows="4" placeholder="Write tersely. Short sentences. No flowery language.">${esc(d.prompt_text)}</textarea></div>
    <div class="field">
      <label>Negative Prompt <span style="font-size:10px;color:var(--text-muted)">(injected if this fragment is removed next turn)</span></label>
      <textarea id="frag-neg" rows="3" placeholder="Stop using short, clipped sentences.">${esc(d.negative_prompt || "")}</textarea>
    </div>`;
}

function _readMoodFragForm() {
  return {
    id: $("frag-id").value.trim(),
    label: $("frag-label").value.trim(),
    description: $("frag-desc").value.trim(),
    prompt_text: $("frag-text").value.trim(),
    negative_prompt: $("frag-neg").value.trim(),
  };
}

export function showMoodFragmentModal(fragId = null) {
  const f = fragId ? S.moodFragments.find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  const d = f || { id: "", label: "", description: "", prompt_text: "", negative_prompt: "" };

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
    // Update local state optimistically
    const frag = S.moodFragments.find((f) => f.id === id);
    if (frag) frag.enabled = newEnabled;
    renderMoodFragments();
    toast(newEnabled ? "Mood fragment enabled" : "Mood fragment disabled");
  } catch (e) {
    toast(e.message, true);
  }
}

// ── Interactive Fragments (unchanged)
export async function loadInteractiveFragments() {
  try {
    const [fragments] = await Promise.all([
      api.get("/interactive-fragments"),
      loadFragmentTypeCatalog({ render: false }),
    ]);
    S.interactiveFragments = fragments;
    renderInteractiveFragments();
  } catch (error) {
    console.error("Failed to load interactive fragments:", error);
    throw error;
  }
}

export async function loadFragmentTypeCatalog({ render = true } = {}) {
  let catalog;
  try {
    catalog = await api.get("/interactive-fragment-types");
  } catch (error) {
    console.error("Failed to load interactive fragment types:", error);
    return false;
  }
  if (catalog.runtime_generation < S.extensionRuntimeGeneration) return false;
  S.extensionRuntimeGeneration = catalog.runtime_generation;
  S.fragmentTypes = Array.isArray(catalog.types) ? catalog.types : [];
  if (render) {
    renderInteractiveFragments();
    const select = document.getElementById("interactive-frag-type");
    if (select) {
      const selectedType = select._fragmentTypeId || "string";
      _fragmentTypeConfigInvalid = new Set();
      _mountFragmentTypeSelect(selectedType);
      _mountFragmentTypeConfig(_fragmentType(selectedType), _fragmentTypeConfigDraft);
      updateInteractiveFragmentExample(selectedType);
    }
  }
  return true;
}

function _fragmentType(typeId) {
  return (S.fragmentTypes || []).find((entry) => entry.id === typeId) || null;
}

function _fragmentTypeAvailable(fragment) {
  if (fragment?.type_available === false) return false;
  return Boolean(_fragmentType(fragment?.field_type));
}

function _fragmentTypeLabel(typeId) {
  const descriptor = _fragmentType(typeId);
  if (descriptor) return descriptor.label || typeId;
  const owner = String(typeId || "").includes(":") ? String(typeId).split(":", 1)[0] : String(typeId || "");
  return `Unavailable — ${owner || "unknown provider"}`;
}

export function renderInteractiveFragments() {
  const el = document.getElementById("interactive-frag-list");
  if (!el) return;
  setupDragAndDrop(el);
  const cardHtml = _cardInteractiveSidepanelHtml();
  // Add button sits with the global list, above the "From character" divider.
  const addBtn = `<button class="btn btn-block btn-sm" onclick="showInteractiveFragmentModal()" style="margin-top:6px">+ Add Interactive Fragment</button>`;
  if ((!S.interactiveFragments || S.interactiveFragments.length === 0) && !cardHtml) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No interactive fragments</div>${addBtn}`;
    return;
  }

  // Sort by sort_order then by id
  const sorted = [...(S.interactiveFragments || [])].sort((a, b) => {
    const orderA = a.sort_order || 0;
    const orderB = b.sort_order || 0;
    if (orderA !== orderB) return orderA - orderB;
    return a.id.localeCompare(b.id);
  });

  const html = sorted
    .map((f) => {
      const enabled = boolFlag(f.enabled);
      const toggleId = `interactive-frag-toggle-${f.id}`;
      const userBadge =
        f.field_type === "feedback"
          ? ` <span class="frag-type-badge" title="Feedback fragment">F</span>`
          : f.field_type === "direction_note"
            ? ` <span class="frag-type-badge" title="Direction-note fragment">D</span>`
            : !_fragmentTypeAvailable(f)
              ? ` <span class="frag-type-badge" title="${escAttr(f.type_diagnostic || "Fragment type unavailable")}">!</span>`
              : "";
      // Feedback and direction-note fragments are gated by their own feature switch;
      // grey them out (and explain why on hover) when that switch is off.
      const feedbackDisabled = f.field_type === "feedback" && !S.feedbackEnabled;
      const directionNoteDisabled = f.field_type === "direction_note" && !S.directionNotesRecord;
      const featureDisabled = feedbackDisabled || directionNoteDisabled;
      const itemTitle = feedbackDisabled
        ? "Editor Feedback feature is disabled — enable it in Agents panel to use this fragment"
        : directionNoteDisabled
          ? "Direction Notes recording is off -- turn on Writing in the Agents panel to use this fragment"
          : !_fragmentTypeAvailable(f)
            ? f.type_diagnostic || `Fragment type ${f.field_type} is unavailable`
            : f.description;
      return `
    <div class="fragment-item${featureDisabled ? " frag-feature-disabled" : ""}" draggable="true" data-id="${escAttr(f.id)}" title="${escAttr(itemTitle)}" onclick="showInteractiveFragmentModal('${escHandlerArg(f.id)}')">
      <div class="frag-drag-handle" onclick="event.stopPropagation()">⋮⋮</div>
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
    })
    .join("");

  // Card items live in .frag-card-list (not .fragment-item), so the drag/reorder
  // machinery below never sees them.
  el.innerHTML = html + addBtn + cardHtml;
}

function setupDragAndDrop(container) {
  if (_dragAndDropContainers.has(container)) return;
  _dragAndDropContainers.add(container);
  let dragged = null;

  container.addEventListener("dragstart", (e) => {
    if (!e.target.classList.contains("fragment-item") && !e.target.closest(".fragment-item")) return;
    const item = e.target.classList.contains("fragment-item") ? e.target : e.target.closest(".fragment-item");
    dragged = item;
    item.classList.add("dragging");
    e.dataTransfer.setData("text/plain", item.dataset.id);
    e.dataTransfer.effectAllowed = "move";
  });

  container.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const afterElement = getDragAfterElement(container, e.clientY);
    const draggable = document.querySelector(".dragging");
    if (draggable) {
      if (afterElement == null) {
        container.appendChild(draggable);
      } else {
        container.insertBefore(draggable, afterElement);
      }
    }
  });

  container.addEventListener("drop", (e) => {
    e.preventDefault();
    if (dragged) {
      dragged.classList.remove("dragging");
      dragged = null;
      updateFragmentOrder(container);
    }
  });

  container.addEventListener("dragend", (_e) => {
    if (dragged) {
      dragged.classList.remove("dragging");
      dragged = null;
    }
  });

  function getDragAfterElement(container, y) {
    const draggableElements = [...container.querySelectorAll(".fragment-item:not(.dragging)")];
    return draggableElements.reduce(
      (closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
          return { offset: offset, element: child };
        } else {
          return closest;
        }
      },
      { offset: Number.NEGATIVE_INFINITY },
    ).element;
  }

  function updateFragmentOrder(container) {
    const items = container.querySelectorAll(".fragment-item");
    const updatedOrder = Array.from(items).map((item, index) => ({
      id: item.dataset.id,
      sort_order: index,
    }));
    // Update local state
    updatedOrder.forEach(({ id, sort_order }) => {
      const frag = S.interactiveFragments.find((f) => f.id === id);
      if (frag) frag.sort_order = sort_order;
    });
    // Update each fragment individually
    Promise.all(updatedOrder.map(({ id, sort_order }) => api.put(`/interactive-fragments/${id}`, { sort_order })))
      .then(() => {
        toast("Interactive fragments reordered");
      })
      .catch((e) => {
        console.error("Reorder failed", e);
        toast("Failed to save order", true);
      });
  }
}

// Example placeholders per field_type, shown across the modal's empty inputs
// and refreshed when the Field Type dropdown changes.
const INTERACTIVE_FRAGMENT_EXAMPLES = {
  string: {
    id: "e.g. pacing",
    label: "e.g. Pacing",
    injection_label: "e.g. Pacing",
    description: "Set the pace of the narration, e.g. 'slow', 'fast', 'time-skip'",
    inj_hint: "sent to the writer",
    desc_hint: "tells the Director what to set — sent in its tool schema",
  },
  array: {
    id: "e.g. plot_threads",
    label: "e.g. Plot Threads",
    injection_label: "e.g. Active Threads",
    description: "List the active plot threads, e.g. 'unresolved rivalry', 'looming deadline'",
    inj_hint: "sent to the writer",
    desc_hint: "tells the Director what to list — sent in its tool schema",
  },
  progressive: {
    id: "e.g. trust",
    label: "e.g. Trust",
    injection_label: "e.g. Trust level",
    description:
      "How much the character trusts the user, as a percentage that shifts gradually, e.g. '10%' -> '25%' -> '40%'",
    inj_hint: "sent to the writer",
    desc_hint: "sent in the Director's tool schema, and shown to the writer beside the value",
  },
  direction_note: {
    id: "e.g. trajectory",
    label: "e.g. Trajectory",
    injection_label: "e.g. Direction of travel",
    description:
      "A lasting note the director records and keeps on this branch, e.g. 'where the story is heading and the established facts that pin it'",
    inj_hint: "sent to the writer",
    desc_hint: "tells the Director what to record — sent in its tool schema",
  },
  feedback: {
    id: "e.g. next_actions",
    label: "e.g. Next Actions",
    injection_label: "e.g. What you could do next",
    description:
      "A short out-of-character note shown to you after each reply, e.g. 'suggest what the player could do or say next'",
    inj_hint: "shown to you",
    desc_hint: "tells the model what to write — sent in its tool schema",
  },
};

export function updateInteractiveFragmentExample(fieldType) {
  const ex = INTERACTIVE_FRAGMENT_EXAMPLES[fieldType] || INTERACTIVE_FRAGMENT_EXAMPLES.string;
  const set = (elId, placeholder) => {
    const el = document.getElementById(elId);
    if (el) el.placeholder = placeholder;
  };
  set("interactive-frag-id", ex.id);
  set("interactive-frag-label", ex.label);
  set("interactive-frag-inj-label", ex.injection_label);
  set("interactive-frag-desc", ex.description);
  const setHint = (elId, text) => {
    const el = document.getElementById(elId);
    if (el) el.textContent = `(${text})`;
  };
  setHint("interactive-frag-inj-hint", ex.inj_hint);
  setHint("interactive-frag-desc-hint", ex.desc_hint);
  // The recording-timing selector applies only to direction-note fragments.
  const timingRow = document.getElementById("interactive-frag-timing-row");
  if (timingRow) timingRow.style.display = fieldType === "direction_note" ? "" : "none";
}

let _fragmentTypeConfigDraft = {};
let _fragmentTypeEditorInstance = "new";
let _fragmentTypeEditorNonce = 0;
let _fragmentTypeConfigInvalid = new Set();

function _mountFragmentTypeSelect(selectedType) {
  const select = document.getElementById("interactive-frag-type");
  if (!select) return;
  const descriptors = [...(S.fragmentTypes || [])];
  if (!descriptors.some((entry) => entry.id === selectedType)) {
    descriptors.push({ id: selectedType, label: _fragmentTypeLabel(selectedType), unavailable: true });
  }
  select.replaceChildren();
  descriptors.forEach((descriptor, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = descriptor.label || descriptor.id;
    option.disabled = Boolean(descriptor.unavailable);
    option.selected = descriptor.id === selectedType;
    select.appendChild(option);
  });
  select._fragmentTypeDescriptors = descriptors;
  select._fragmentTypeId = selectedType;
  if (select._fragmentTypeChangeHandler) {
    select.removeEventListener?.("change", select._fragmentTypeChangeHandler);
  }
  select._fragmentTypeChangeHandler = () => {
    const descriptor = descriptors[Number(select.value)];
    if (!descriptor) return;
    select._fragmentTypeId = descriptor.id;
    _fragmentTypeConfigDraft = schemaConfigDefaults(descriptor.config_schema);
    _fragmentTypeConfigInvalid = new Set();
    updateInteractiveFragmentExample(descriptor.id);
    _mountFragmentTypeConfig(descriptor, _fragmentTypeConfigDraft);
  };
  select.addEventListener("change", select._fragmentTypeChangeHandler);
}

function _setConfigValue(key, property, field) {
  let value;
  if (Array.isArray(property.enum)) {
    value = property.enum[Number(field.value)];
  } else if (property.type === "boolean") value = field.checked;
  else if (property.type === "integer" || property.type === "number") {
    if (field.value === "") {
      delete _fragmentTypeConfigDraft[key];
      return;
    }
    value = Number(field.value);
  } else if (property.type === "array" && property.items?.type === "string") {
    value = field.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  } else if (property.type === "array" || property.type === "object") {
    const parsed = parseStructuredConfigValue(field.value);
    if (!parsed.ok) {
      _fragmentTypeConfigInvalid.add(key);
      field.setCustomValidity?.("Enter valid JSON");
      return;
    }
    value = parsed.value;
    _fragmentTypeConfigInvalid.delete(key);
    field.setCustomValidity?.("");
  } else value = field.value;
  _fragmentTypeConfigDraft[key] = value;
}

function _mountGenericTypeConfig(host, descriptor) {
  const schema = descriptor?.config_schema || {};
  const properties = Object.entries(schema.properties || {});
  if (!properties.length) {
    host.replaceChildren();
    return;
  }
  const title = document.createElement("div");
  title.className = "frag-divider";
  title.textContent = `${descriptor.label || descriptor.id} configuration`;
  const required = new Set(schema.required || []);
  const fields = properties.map(([key, property]) => {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const label = document.createElement("span");
    label.textContent = property.description || key;
    if (required.has(key) && !Object.hasOwn(_fragmentTypeConfigDraft, key)) {
      _fragmentTypeConfigDraft[key] = schemaDefaultValue(property);
    }
    const included = required.has(key) || Object.hasOwn(_fragmentTypeConfigDraft, key);
    let field;
    if (Array.isArray(property.enum)) {
      field = document.createElement("select");
      property.enum.forEach((member, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = String(member);
        field.appendChild(option);
      });
      field.value = String(
        Math.max(
          0,
          property.enum.findIndex((member) => Object.is(member, _fragmentTypeConfigDraft[key])),
        ),
      );
    } else if (property.type === "boolean") {
      field = document.createElement("input");
      field.type = "checkbox";
      field.checked = Boolean(_fragmentTypeConfigDraft[key]);
    } else if (property.type === "integer" || property.type === "number") {
      field = document.createElement("input");
      field.type = "number";
      field.step = property.type === "integer" ? "1" : "any";
      if (property.minimum !== undefined) field.min = String(property.minimum);
      if (property.maximum !== undefined) field.max = String(property.maximum);
      field.value = _fragmentTypeConfigDraft[key] ?? "";
    } else if (property.type === "array" && property.items?.type === "string") {
      field = document.createElement("textarea");
      field.rows = 3;
      field.value = Array.isArray(_fragmentTypeConfigDraft[key]) ? _fragmentTypeConfigDraft[key].join("\n") : "";
    } else if (property.type === "array" || property.type === "object") {
      field = document.createElement("textarea");
      field.rows = 4;
      field.value = JSON.stringify(
        Object.hasOwn(_fragmentTypeConfigDraft, key) ? _fragmentTypeConfigDraft[key] : schemaDefaultValue(property),
        null,
        2,
      );
    } else {
      field = document.createElement("input");
      field.type = "text";
      field.value = _fragmentTypeConfigDraft[key] ?? "";
    }
    field.className = "xc-input";
    const fixed = Object.hasOwn(property, "const");
    field.disabled = !included || fixed;
    if (!fixed) {
      field.addEventListener("input", () => _setConfigValue(key, property, field));
      field.addEventListener("change", () => _setConfigValue(key, property, field));
    }
    if (!required.has(key)) {
      const include = document.createElement("input");
      include.type = "checkbox";
      include.checked = included;
      const includeLabel = document.createElement("span");
      includeLabel.textContent = "Include";
      include.addEventListener("change", () => {
        if (include.checked) {
          _fragmentTypeConfigDraft[key] = schemaDefaultValue(property);
          field.disabled = fixed;
          _setConfigFieldDisplay(field, property, _fragmentTypeConfigDraft[key]);
        } else {
          delete _fragmentTypeConfigDraft[key];
          _fragmentTypeConfigInvalid.delete(key);
          field.setCustomValidity?.("");
          field.disabled = true;
        }
      });
      wrap.append(label, include, includeLabel, field);
      return wrap;
    }
    wrap.append(label, field);
    return wrap;
  });
  host.replaceChildren(title, ...fields);
}

function _setConfigFieldDisplay(field, property, value) {
  if (Array.isArray(property.enum)) {
    field.value = String(
      Math.max(
        0,
        property.enum.findIndex((member) => Object.is(member, value)),
      ),
    );
  } else if (property.type === "boolean") {
    field.checked = Boolean(value);
  } else if (property.type === "array" && property.items?.type === "string") {
    field.value = Array.isArray(value) ? value.join("\n") : "";
  } else if (property.type === "array" || property.type === "object") {
    field.value = JSON.stringify(value, null, 2);
  } else {
    field.value = value ?? "";
  }
}

function _mountFragmentTypeConfig(descriptor, config) {
  const host = document.getElementById("interactive-frag-type-config");
  if (!host) return;
  if (!descriptor || descriptor.kind === "core" || descriptor.kind === "dedicated") {
    host.replaceChildren();
    return;
  }
  if (!descriptor.config_view) {
    _mountGenericTypeConfig(host, descriptor);
    return;
  }
  renderView(
    host,
    {
      view: descriptor.config_view,
      config,
      data: { fragment: { config } },
      state: {},
      errors: {},
    },
    {
      extensionId: descriptor.owner_id,
      viewId: `fragment-config:${descriptor.local_id}`,
      instanceId: _fragmentTypeEditorInstance,
      digest: descriptor.content_digest || "",
      saveMode: "external",
      onAction: async () => {},
      onSaveState: async () => {},
      onDraftChange: (path, value) => {
        const [root, ...segments] = String(path).split(".");
        if (root === "config" && segments.length) {
          setConfigDraftPath(_fragmentTypeConfigDraft, segments, value);
        }
      },
    },
  );
}

function _initializeFragmentTypeEditor(d) {
  // A cancelled modal must not replay the renderer's old ephemeral draft on a
  // later open while `_fragmentTypeConfigDraft` has reset to persisted data.
  _fragmentTypeEditorNonce += 1;
  _fragmentTypeEditorInstance = `${d.id || "new"}:${_fragmentTypeEditorNonce}`;
  const persisted =
    d.type_config && typeof d.type_config === "object" && !Array.isArray(d.type_config)
      ? structuredClone(d.type_config)
      : {};
  const descriptor = _fragmentType(d.field_type);
  _fragmentTypeConfigDraft = d.id ? persisted : { ...schemaConfigDefaults(descriptor?.config_schema), ...persisted };
  _fragmentTypeConfigInvalid = new Set();
  _mountFragmentTypeSelect(d.field_type);
  _mountFragmentTypeConfig(descriptor, _fragmentTypeConfigDraft);
  updateInteractiveFragmentExample(d.field_type);
}

// Shared field markup for the global modal and the card-scoped sub-modal (same
// element ids — the two are never open at once).
function _interactiveFragFormHtml(d, isEdit) {
  const ex = INTERACTIVE_FRAGMENT_EXAMPLES[d.field_type] || INTERACTIVE_FRAGMENT_EXAMPLES.string;
  return `
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="interactive-frag-id" value="${escAttr(d.id)}" ${isEdit ? "disabled" : ""} placeholder="${escAttr(ex.id)}"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="interactive-frag-label" value="${escAttr(d.label)}" placeholder="${escAttr(ex.label)}"></div>
    </div>
    <div class="field-row">
      <div class="field"><label>Injection Label <span id="interactive-frag-inj-hint" style="font-size:10px;color:var(--text-muted)">(${esc(ex.inj_hint)})</span></label>
        <input id="interactive-frag-inj-label" value="${escAttr(d.injection_label)}" placeholder="${escAttr(ex.injection_label)}"></div>
      <div class="field"><label>Field Type</label>
        <select id="interactive-frag-type"></select>
      </div>
    </div>
    <div class="field" id="interactive-frag-timing-row" style="${d.field_type === "direction_note" ? "" : "display:none"}">
      <label>When recorded <span style="font-size:10px;color:var(--text-muted)">(direction notes only)</span></label>
      <select id="interactive-frag-timing-select">
        <option value="post_turn" ${d.direction_note_timing !== "pre_writer" ? "selected" : ""}>End of turn</option>
        <option value="pre_writer" ${d.direction_note_timing === "pre_writer" ? "selected" : ""}>Before writer</option>
      </select>
    </div>
    <div class="field"><label>Description <span id="interactive-frag-desc-hint" style="font-size:10px;color:var(--text-muted)">(${esc(ex.desc_hint)})</span></label>
      <textarea id="interactive-frag-desc" rows="4" placeholder="${escAttr(ex.description)}">${esc(d.description)}</textarea></div>
    <div class="field-row">
      <div class="field" style="align-self:flex-end;padding-bottom:4px">
        <label class="modal-checkbox-label">
          <input type="checkbox" id="interactive-frag-required" ${d.required ? "checked" : ""}> Required
        </label>
      </div>
    </div>
    <div id="interactive-frag-type-config"></div>`;
}

function _readInteractiveFragForm() {
  return {
    id: document.getElementById("interactive-frag-id").value.trim(),
    label: document.getElementById("interactive-frag-label").value.trim(),
    description: document.getElementById("interactive-frag-desc").value.trim(),
    field_type: document.getElementById("interactive-frag-type")._fragmentTypeId || "string",
    required: document.getElementById("interactive-frag-required").checked,
    injection_label: document.getElementById("interactive-frag-inj-label").value.trim(),
    direction_note_timing: document.getElementById("interactive-frag-timing-select").value,
    type_config: structuredClone(_fragmentTypeConfigDraft),
  };
}

function _fragmentTypeConfigIsValid() {
  if (!_fragmentTypeConfigInvalid.size) return true;
  document.querySelector?.("#interactive-frag-type-config :invalid")?.reportValidity?.();
  toast("Fix the invalid fragment configuration before saving", true);
  return false;
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
    direction_note_timing: "post_turn",
    type_config: {},
  };

  showModal(`
    <h2>${isEdit ? "Edit" : "New"} Interactive Fragment</h2>
    ${_interactiveFragFormHtml(d, isEdit)}
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteInteractiveFragment('${escHandlerArg(d.id)}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveInteractiveFragment(${isEdit})">${isEdit ? "Save" : "Create"}</button>
    </div>`);
  _initializeFragmentTypeEditor(d);
}

export async function saveInteractiveFragment(isEdit) {
  if (!_fragmentTypeConfigIsValid()) return;
  const d = _readInteractiveFragForm();
  const validation = validate.validateInteractiveFragment(d);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    if (isEdit) await api.put(`/interactive-fragments/${d.id}`, d);
    else await api.post("/interactive-fragments", d);
    closeModal();
    await loadInteractiveFragments();
    toast("Interactive fragment saved");
  } catch (e) {
    toast(e.message, true);
  }
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
    toast(newEnabled ? "Interactive fragment enabled" : "Interactive fragment disabled");
  } catch (e) {
    toast(e.message, true);
  }
}

// ── Card-embedded fragments (extensions.orb.fragments on a character card)
//
// Two surfaces share this section:
// 1. Sidepanel: the active character's fragments render read-only below a
//    divider in both fragment lists (S.cardMoodFragments / S.cardInteractiveFragments,
//    stashed by chat_conversations.js on selection). Not draggable, no toggles —
//    editing happens in the character modal.
// 2. Character modal "Fragments" tab: a pending in-memory copy edited via the
//    sub-modal layer and saved with the card PUT/POST (library.js reads it back
//    through readCardFragments()).

function _interactiveTypeBadge(f) {
  return f.field_type === "feedback"
    ? ` <span class="frag-type-badge" title="Feedback fragment">F</span>`
    : f.field_type === "direction_note"
      ? ` <span class="frag-type-badge" title="Direction-note fragment">D</span>`
      : !_fragmentTypeAvailable(f)
        ? ` <span class="frag-type-badge" title="Fragment type unavailable">!</span>`
        : "";
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
      const feedbackDisabled = f.field_type === "feedback" && !S.feedbackEnabled;
      const directionNoteDisabled = f.field_type === "direction_note" && !S.directionNotesRecord;
      const featureDisabled = feedbackDisabled || directionNoteDisabled;
      const itemTitle = feedbackDisabled
        ? "Editor Feedback feature is disabled — enable it in Agents panel to use this fragment"
        : directionNoteDisabled
          ? "Direction Notes recording is off -- turn on Writing in the Agents panel to use this fragment"
          : !_fragmentTypeAvailable(f)
            ? `Fragment type ${f.field_type} is unavailable`
            : f.description || "";
      return `<span${featureDisabled ? ' class="frag-feature-disabled"' : ""} title="${escAttr(itemTitle)}">${esc(f.label)}${_interactiveTypeBadge(f)}</span>`;
    })
    .join("");
  return `<div class="frag-divider">From character</div><div class="frag-card-list">${items}</div>`;
}

// Pending copy of the card being edited; null when no character modal is open.
let _cardFragPending = null;

export function initCardFragments(fragments) {
  _cardFragPending = {
    mood: Array.isArray(fragments?.mood) ? structuredClone(fragments.mood) : [],
    interactive: Array.isArray(fragments?.interactive) ? structuredClone(fragments.interactive) : [],
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

// Save/delete against the pending copy only; nothing touches the API until the
// character itself is saved. Sub-modal buttons are wired programmatically.
function _wireCardFragModal(type, isEdit, fragId) {
  $("card-frag-cancel").addEventListener("click", closeSubModal);
  if (isEdit) {
    $("card-frag-delete").addEventListener("click", () => {
      _cardFragPending[type] = _cardFragPending[type].filter((f) => f.id !== fragId);
      closeSubModal();
      renderCardFragmentsTab();
    });
  }
  $("card-frag-save").addEventListener("click", () => {
    if (type === "interactive" && !_fragmentTypeConfigIsValid()) return;
    const d = type === "mood" ? _readMoodFragForm() : _readInteractiveFragForm();
    const validation = type === "mood" ? validate.validateMoodFragment(d) : validate.validateInteractiveFragment(d);
    if (!validation.valid) {
      toast(validation.error, true);
      return;
    }
    const globals = type === "mood" ? S.moodFragments : S.interactiveFragments;
    if (!isEdit && (globals.some((g) => g.id === d.id) || _cardFragPending[type].some((f) => f.id === d.id))) {
      toast(`A ${type === "mood" ? "mood" : "interactive"} fragment with this ID already exists`, true);
      return;
    }
    const existing = isEdit ? _cardFragPending[type].find((f) => f.id === fragId) : null;
    d.enabled = existing ? existing.enabled !== false : true;
    if (existing) Object.assign(existing, d);
    else _cardFragPending[type].push(d);
    closeSubModal();
    renderCardFragmentsTab();
  });
}

export function showCardMoodFragmentModal(fragId = null) {
  const f = fragId ? _cardFragPending.mood.find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  const d = f || { id: "", label: "", description: "", prompt_text: "", negative_prompt: "" };
  showSubModal(`
    <h2>${isEdit ? "Edit" : "New"} Character Mood Fragment</h2>
    ${_moodFragFormHtml(d, isEdit)}
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" id="card-frag-delete">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" id="card-frag-cancel">Cancel</button>
      <button class="btn btn-accent" id="card-frag-save">${isEdit ? "Save" : "Add"}</button>
    </div>`);
  _wireCardFragModal("mood", isEdit, fragId);
}

export function showCardInteractiveFragmentModal(fragId = null) {
  const f = fragId ? _cardFragPending.interactive.find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  const d = f || {
    id: "",
    label: "",
    description: "",
    field_type: "string",
    required: false,
    injection_label: "",
    direction_note_timing: "post_turn",
    type_config: {},
  };
  showSubModal(`
    <h2>${isEdit ? "Edit" : "New"} Character Interactive Fragment</h2>
    ${_interactiveFragFormHtml(d, isEdit)}
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" id="card-frag-delete">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" id="card-frag-cancel">Cancel</button>
      <button class="btn btn-accent" id="card-frag-save">${isEdit ? "Save" : "Add"}</button>
    </div>`);
  _initializeFragmentTypeEditor(d);
  _wireCardFragModal("interactive", isEdit, fragId);
}
