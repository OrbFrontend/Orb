// Mood fragments and director fragments: their sidebar lists, edit modals, and
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

// ── Director Fragments (unchanged)
export async function loadDirectorFragments() {
  try {
    S.directorFragments = await api.get("/director-fragments");
    renderDirectorFragments();
  } catch (error) {
    console.error("Failed to load director fragments:", error);
    throw error;
  }
}

export function renderDirectorFragments() {
  const el = document.getElementById("director-frag-list");
  if (!el) return;
  if (!S.directorFragments || S.directorFragments.length === 0) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No director fragments</div>';
    return;
  }

  // Sort by sort_order then by id
  const sorted = [...S.directorFragments].sort((a, b) => {
    const orderA = a.sort_order || 0;
    const orderB = b.sort_order || 0;
    if (orderA !== orderB) return orderA - orderB;
    return a.id.localeCompare(b.id);
  });

  const html = sorted
    .map((f) => {
      const enabled = f.enabled === true || f.enabled === 1;
      const toggleId = `director-frag-toggle-${f.id}`;
      return `
    <div class="fragment-item" draggable="true" data-id="${esc(f.id)}" title="${esc(f.description)}" onclick="showDirectorFragmentModal('${f.id}')">
      <div class="frag-drag-handle" onclick="event.stopPropagation()">⋮⋮</div>
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label)}</span>
      </div>
      <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
        <label class="frag-toggle" for="${toggleId}">
          <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                 onchange="toggleDirectorFragmentEnabled('${f.id}', this.checked)">
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
      const frag = S.directorFragments.find((f) => f.id === id);
      if (frag) frag.sort_order = sort_order;
    });
    // Update each fragment individually
    Promise.all(updatedOrder.map(({ id, sort_order }) => api.put(`/director-fragments/${id}`, { sort_order })))
      .then(() => {
        toast("Director fragments reordered");
      })
      .catch((e) => {
        console.error("Reorder failed", e);
        toast("Failed to save order", true);
      });
  }
}

export function showDirectorFragmentModal(fragId = null) {
  const f = fragId ? S.directorFragments.find((x) => x.id === fragId) : null;
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

  showModal(`
    <h2>${isEdit ? "Edit" : "New"} Director Fragment</h2>
    <div class="field-row">
      <div class="field"><label>ID <span style="font-size:10px;color:var(--text-muted)">(For tool-calling)</span></label>
        <input id="dir-frag-id" value="${esc(d.id)}" ${isEdit ? "disabled" : ""} placeholder="e.g. pacing"></div>
      <div class="field"><label>Label <span style="font-size:10px;color:var(--text-muted)">(For display only)</span></label>
        <input id="dir-frag-label" value="${esc(d.label)}"></div>
    </div>
    <div class="field-row">
      <div class="field"><label>Injection Label</label>
        <input id="dir-frag-inj-label" value="${esc(d.injection_label)}" placeholder="e.g. Pacing"></div>
      <div class="field"><label>Field Type</label>
        <select id="dir-frag-type">
          <option value="string" ${d.field_type === "string" ? "selected" : ""}>single</option>
          <option value="array" ${d.field_type === "array" ? "selected" : ""}>list</option>
          <option value="progressive" ${d.field_type === "progressive" ? "selected" : ""}>progressive</option>
        </select>
      </div>
    </div>
    <div class="field"><label>Description <span style="font-size:10px;color:var(--text-muted)">(shown to the LLM in the tool schema)</span></label>
      <textarea id="dir-frag-desc" rows="4" placeholder="Set the pace of the narration, e.g. &#39;slow&#39;, &#39;fast&#39;, &#39;time-skip&#39;">${esc(d.description)}</textarea></div>
    <div class="field-row">
      <div class="field" style="align-self:flex-end;padding-bottom:4px">
        <label class="modal-checkbox-label">
          <input type="checkbox" id="dir-frag-required" ${d.required ? "checked" : ""}> Required
        </label>
      </div>
    </div>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteDirectorFragment('${esc(d.id)}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveDirectorFragment(${isEdit})">${isEdit ? "Save" : "Create"}</button>
    </div>`);
}

export async function saveDirectorFragment(isEdit) {
  const d = {
    id: document.getElementById("dir-frag-id").value.trim(),
    label: document.getElementById("dir-frag-label").value.trim(),
    description: document.getElementById("dir-frag-desc").value.trim(),
    field_type: document.getElementById("dir-frag-type").value,
    required: document.getElementById("dir-frag-required").checked,
    injection_label: document.getElementById("dir-frag-inj-label").value.trim(),
  };
  const validation = validate.validateDirectorFragment(d);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    if (isEdit) await api.put("/director-fragments/" + d.id, d);
    else await api.post("/director-fragments", d);
    closeModal();
    await loadDirectorFragments();
    toast("Director fragment saved");
  } catch (e) {
    toast(e.message, true);
  }
}

export async function deleteDirectorFragment(id) {
  showConfirmModal(
    {
      title: "Delete Director Fragment",
      message: "Are you sure you want to delete this director fragment?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/director-fragments/" + id);
        await loadDirectorFragments();
        toast("Director fragment deleted");
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export async function toggleDirectorFragmentEnabled(id, newEnabled) {
  try {
    await api.put("/director-fragments/" + id, { enabled: newEnabled });
    const frag = S.directorFragments.find((f) => f.id === id);
    if (frag) frag.enabled = newEnabled;
    renderDirectorFragments();
    toast(newEnabled ? "Director fragment enabled" : "Director fragment disabled");
  } catch (e) {
    toast(e.message, true);
  }
}
