// Mood fragments and interactive fragments: their sidebar lists, edit modals, and
// CRUD + reorder. Split out of library.js; the public surface is re-exported
// from library.js.
import { api } from "./api.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";
import { validate } from "./validate.js";

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
  if (!S.moodFragments || S.moodFragments.length === 0) {
    $("frag-list").innerHTML =
      '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No mood fragments</div>';
    return;
  }

  const html = S.moodFragments
    .map((f) => {
      // Handle both boolean and numeric (0/1) enabled values from backend
      const enabled = f.enabled === true || f.enabled === 1;
      const toggleId = `frag-toggle-${f.id}`;
      return `
    <div class="fragment-item" style="cursor:pointer" title="${esc(f.description)}" onclick="showMoodFragmentModal('${f.id}')">
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label)}</span>
      </div>
      <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
        <label class="frag-toggle" for="${toggleId}">
          <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                 onchange="toggleMoodFragmentEnabled('${f.id}', this.checked)">
          <span class="frag-toggle-slider"></span>
        </label>
      </div>
    </div>`;
    })
    .join("");

  $("frag-list").innerHTML = html;
}

export function showMoodFragmentModal(fragId = null) {
  const f = fragId ? S.moodFragments.find((x) => x.id === fragId) : null;
  const isEdit = !!f;
  const d = f || { id: "", label: "", description: "", prompt_text: "", negative_prompt: "" };

  showModal(`
    <h2>${isEdit ? "Edit Mood Fragment" : "New Mood Fragment"}</h2>
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="frag-id" value="${esc(d.id)}" ${isEdit ? "disabled" : ""} placeholder="e.g. dramatic"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="frag-label" value="${esc(d.label)}" placeholder="Terse"></div>
    </div>
    <div class="field"><label>Description</label>
      <input id="frag-desc" value="${esc(d.description)}" placeholder="Short, clipped sentences. Minimal description."></div>
    <div class="field"><label>Prompt Text</label>
      <textarea id="frag-text" rows="4" placeholder="Write tersely. Short sentences. No flowery language.">${esc(d.prompt_text)}</textarea></div>
    <div class="field">
      <label>Negative Prompt <span style="font-size:10px;color:var(--text-muted)">(injected if this fragment is removed next turn)</span></label>
      <textarea id="frag-neg" rows="3" placeholder="Stop using short, clipped sentences.">${esc(d.negative_prompt || "")}</textarea>
    </div>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteMoodFragment('${esc(d.id)}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveMoodFragment(${isEdit})">${isEdit ? "Save" : "Create"}</button>
    </div>`);
}

export async function saveMoodFragment(isEdit) {
  const d = {
    id: $("frag-id").value.trim(),
    label: $("frag-label").value.trim(),
    description: $("frag-desc").value.trim(),
    prompt_text: $("frag-text").value.trim(),
    negative_prompt: $("frag-neg").value.trim(),
  };
  const validation = validate.validateMoodFragment(d);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    if (isEdit) await api.put("/fragments/" + d.id, d);
    else await api.post("/fragments", d);
    closeModal();
    await loadMoodFragments();
    toast("Mood fragment saved");
  } catch (e) {
    toast(e.message, true);
  }
}

export async function deleteMoodFragment(id) {
  showConfirmModal(
    {
      title: "Delete Mood Fragment",
      message: "Are you sure you want to delete this mood fragment?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/fragments/" + id);
        await loadMoodFragments();
        toast("Mood fragment deleted");
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export async function toggleMoodFragmentEnabled(id, newEnabled) {
  try {
    await api.put("/fragments/" + id, { enabled: newEnabled });
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
    S.interactiveFragments = await api.get("/interactive-fragments");
    renderInteractiveFragments();
  } catch (error) {
    console.error("Failed to load interactive fragments:", error);
    throw error;
  }
}

export function renderInteractiveFragments() {
  const el = document.getElementById("interactive-frag-list");
  if (!el) return;
  if (!S.interactiveFragments || S.interactiveFragments.length === 0) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No interactive fragments</div>';
    return;
  }

  // Sort by sort_order then by id
  const sorted = [...S.interactiveFragments].sort((a, b) => {
    const orderA = a.sort_order || 0;
    const orderB = b.sort_order || 0;
    if (orderA !== orderB) return orderA - orderB;
    return a.id.localeCompare(b.id);
  });

  const html = sorted
    .map((f) => {
      const enabled = f.enabled === true || f.enabled === 1;
      const toggleId = `interactive-frag-toggle-${f.id}`;
      const userBadge =
        f.field_type === "feedback" ? ` <span class="frag-type-badge" title="Feedback fragment">F</span>` : "";
      // Feedback fragments are gated by the "Editor Feedback" feature flag; grey
      // them out (and explain why on hover) when that feature is disabled.
      const featureDisabled = f.field_type === "feedback" && !S.feedbackEnabled;
      const itemTitle = featureDisabled
        ? "Editor Feedback feature is disabled — enable it in Settings to use this fragment"
        : esc(f.description);
      return `
    <div class="fragment-item${featureDisabled ? " frag-feature-disabled" : ""}" draggable="true" data-id="${esc(f.id)}" title="${itemTitle}" onclick="showInteractiveFragmentModal('${f.id}')">
      <div class="frag-drag-handle" onclick="event.stopPropagation()">⋮⋮</div>
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label)}</span>${userBadge}
      </div>
      <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
        <label class="frag-toggle" for="${toggleId}">
          <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                 onchange="toggleInteractiveFragmentEnabled('${f.id}', this.checked)">
          <span class="frag-toggle-slider"></span>
        </label>
      </div>
    </div>`;
    })
    .join("");

  el.innerHTML = html;
  setupDragAndDrop(el);
}

function setupDragAndDrop(container) {
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

  container.addEventListener("dragend", (e) => {
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
  },
  array: {
    id: "e.g. plot_threads",
    label: "e.g. Plot Threads",
    injection_label: "e.g. Active Threads",
    description: "List the active plot threads, e.g. 'unresolved rivalry', 'looming deadline'",
  },
  progressive: {
    id: "e.g. tension",
    label: "e.g. Tension",
    injection_label: "e.g. Tension",
    description: "Track a value that evolves each turn, e.g. 'calm' -> 'uneasy' -> 'breaking point'",
  },
  feedback: {
    id: "e.g. next_actions",
    label: "e.g. Next Actions",
    injection_label: "e.g. What you could do next",
    description:
      "A short out-of-character note shown to you after each reply, e.g. 'suggest what the player could do or say next'",
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
  };
  const ex = INTERACTIVE_FRAGMENT_EXAMPLES[d.field_type] || INTERACTIVE_FRAGMENT_EXAMPLES.string;

  showModal(`
    <h2>${isEdit ? "Edit" : "New"} Interactive Fragment</h2>
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="interactive-frag-id" value="${esc(d.id)}" ${isEdit ? "disabled" : ""} placeholder="${esc(ex.id)}"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="interactive-frag-label" value="${esc(d.label)}" placeholder="${esc(ex.label)}"></div>
    </div>
    <div class="field-row">
      <div class="field"><label>Injection Label</label>
        <input id="interactive-frag-inj-label" value="${esc(d.injection_label)}" placeholder="${esc(ex.injection_label)}"></div>
      <div class="field"><label>Field Type</label>
        <select id="interactive-frag-type" onchange="updateInteractiveFragmentExample(this.value)">
          <option value="string" ${d.field_type === "string" ? "selected" : ""}>single</option>
          <option value="array" ${d.field_type === "array" ? "selected" : ""}>list</option>
          <option value="progressive" ${d.field_type === "progressive" ? "selected" : ""}>progressive</option>
          <option value="feedback" ${d.field_type === "feedback" ? "selected" : ""}>feedback (note to you)</option>
        </select>
      </div>
    </div>
    <div class="field"><label>Description <span style="font-size:10px;color:var(--text-muted)">(shown to the LLM in the tool schema)</span></label>
      <textarea id="interactive-frag-desc" rows="4" placeholder="${esc(ex.description)}">${esc(d.description)}</textarea></div>
    <div class="field-row">
      <div class="field" style="align-self:flex-end;padding-bottom:4px">
        <label class="modal-checkbox-label">
          <input type="checkbox" id="interactive-frag-required" ${d.required ? "checked" : ""}> Required
        </label>
      </div>
    </div>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteInteractiveFragment('${esc(d.id)}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveInteractiveFragment(${isEdit})">${isEdit ? "Save" : "Create"}</button>
    </div>`);
}

export async function saveInteractiveFragment(isEdit) {
  const d = {
    id: document.getElementById("interactive-frag-id").value.trim(),
    label: document.getElementById("interactive-frag-label").value.trim(),
    description: document.getElementById("interactive-frag-desc").value.trim(),
    field_type: document.getElementById("interactive-frag-type").value,
    required: document.getElementById("interactive-frag-required").checked,
    injection_label: document.getElementById("interactive-frag-inj-label").value.trim(),
  };
  const validation = validate.validateInteractiveFragment(d);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    if (isEdit) await api.put("/interactive-fragments/" + d.id, d);
    else await api.post("/interactive-fragments", d);
    closeModal();
    await loadInteractiveFragments();
    toast("Interactive fragment saved");
  } catch (e) {
    toast(e.message, true);
  }
}

export async function deleteInteractiveFragment(id) {
  showConfirmModal(
    {
      title: "Delete Interactive Fragment",
      message: "Are you sure you want to delete this interactive fragment?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/interactive-fragments/" + id);
        await loadInteractiveFragments();
        toast("Interactive fragment deleted");
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export async function toggleInteractiveFragmentEnabled(id, newEnabled) {
  try {
    await api.put("/interactive-fragments/" + id, { enabled: newEnabled });
    const frag = S.interactiveFragments.find((f) => f.id === id);
    if (frag) frag.enabled = newEnabled;
    renderInteractiveFragments();
    toast(newEnabled ? "Interactive fragment enabled" : "Interactive fragment disabled");
  } catch (e) {
    toast(e.message, true);
  }
}
