import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { initDragReorder } from "./drag_reorder.js";
import { GRIP_ICON } from "./icons.js";
import { closeModal, confirmDelete, showModal } from "./modal.js";
import { $, boolFlag, esc, escAttr, toast } from "./utils.js";

const PREFIX_NOTE = "Every pass reads this; editing it re-reads the chat.";

let _layouts = [];
let _activeId = "default";
let _viewId = null;
let _blocks = [];
let _note = "";
let _dragReady = false;

export async function loadPromptLayout() {
  const { layouts, active_id } = await api.get("/prompt-layouts");
  _layouts = layouts;
  _activeId = active_id;
  if (!_layouts.some((l) => l.id === _viewId)) _viewId = _activeId;
  _blocks = await api.get(`/prompt-layouts/${_viewId}/blocks`);
  render();
}

const _byId = (id) => _blocks.find((b) => b.id === id);
const _isMarker = (b) => b.kind !== "custom";

function _ordered(region) {
  return _blocks.filter((b) => b.region === region).sort((a, b) => a.sort_order - b.sort_order);
}

function _rowHtml(b) {
  const marker = _isMarker(b);
  const label = b.label || (marker ? b.kind : "Untitled block");
  const role =
    !marker && b.region === "prefix" && b.role !== "system" ? `<span class="pl-role">${esc(b.role)}</span>` : "";
  const template = marker && b.content !== b.default_content ? `<span class="pl-role">edited</span>` : "";
  const toggle = b.toggleable
    ? `<div class="frag-toggle-wrapper">
        <label class="tog" for="pl-toggle-${escAttr(b.id)}">
          <input type="checkbox" id="pl-toggle-${escAttr(b.id)}" ${boolFlag(b.enabled) ? "checked" : ""}
                 data-wf-action="prompt-layout:toggle" data-wf-on="change" data-block-id="${escAttr(b.id)}">
          <span class="tog-slider"></span>
        </label>
      </div>`
    : "";
  const classes = ["fragment-item", "pl-block", marker && !b.toggleable ? "pl-fixed" : ""].filter(Boolean).join(" ");
  return `<div class="${classes}" data-id="${escAttr(b.id)}" data-wf-action="prompt-layout:open" data-block-id="${escAttr(b.id)}">
      <button type="button" class="frag-drag-handle" title="Drag, or use the arrow keys, to reorder" aria-label="Reorder ${escAttr(label)}">${GRIP_ICON}</button>
      <div class="pl-name">${marker ? '<span class="pl-marker" aria-label="Built-in">◆</span>' : ""}<span class="frag-label">${esc(label)}</span>${role}${template}</div>
      ${toggle}
    </div>`;
}

function render() {
  const el = $("prompt-layout-list");
  if (!el) return;
  const options = _layouts
    .map(
      (l) =>
        `<option value="${escAttr(l.id)}" ${l.id === _viewId ? "selected" : ""}>${esc(l.name)}${l.id === _activeId ? " (active)" : ""}</option>`,
    )
    .join("");
  const activeButton =
    _viewId === _activeId
      ? ""
      : `<button class="btn btn-sm btn-block" data-wf-action="prompt-layout:activate">Use this layout</button>`;
  el.innerHTML = `
    <div class="pl-header">
      <select class="theme-select" aria-label="Prompt layout" data-wf-action="prompt-layout:view" data-wf-on="change">${options}</select>
      <div class="pl-header-actions">
        <button class="btn btn-sm" data-wf-action="prompt-layout:new" title="New layout">New</button>
        <button class="btn btn-sm" data-wf-action="prompt-layout:duplicate" title="Duplicate this layout">Copy</button>
        <button class="btn btn-sm" data-wf-action="prompt-layout:rename" title="Rename this layout">Rename</button>
        <button class="btn btn-sm" data-wf-action="prompt-layout:delete" title="Delete this layout" ${_viewId === _activeId || _layouts.length < 2 ? "disabled" : ""}>Delete</button>
      </div>
    </div>
    ${activeButton}
    ${_note ? `<div class="pl-note" role="status">${esc(_note)}</div>` : ""}
    <div class="pl-blocks">
      ${_ordered("prefix").map(_rowHtml).join("")}
      <div class="fragment-item pl-divider" data-id="">
        <div class="pl-divider-rule"><span>Writer chat history</span></div>
      </div>
      ${_ordered("writer").map(_rowHtml).join("")}
    </div>
    <button class="btn btn-block btn-sm" data-wf-action="prompt-layout:add" style="margin-top:6px">+ Add Block</button>`;
  if (!_dragReady) {
    _dragReady = true;
    initDragReorder(el, { itemSelector: ".fragment-item", handleSelector: ".frag-drag-handle", onReorder: saveOrder });
  }
}

// The re-read note only matters for the layout the chat actually uses.
function _setNote(text) {
  if (_viewId === _activeId) _note = text;
}

async function saveOrder(container) {
  const prefix = [];
  const writer = [];
  let below = false;
  for (const row of container.querySelectorAll(".fragment-item")) {
    if (row.classList.contains("pl-divider")) below = true;
    else (below ? writer : prefix).push(row.dataset.id);
  }
  const crossed = [...prefix.map((id) => [id, "prefix"]), ...writer.map((id) => [id, "writer"])].filter(
    ([id, region]) => _byId(id)?.region !== region,
  );
  if (crossed.some(([id]) => !_byId(id)?.movable)) {
    toast("Only custom blocks and Chat instructions can cross the chat history", true);
    render();
    return;
  }
  if (crossed.some(([, region]) => region === "prefix")) _setNote(PREFIX_NOTE);
  try {
    _blocks = await api.put(`/prompt-layouts/${_viewId}/order`, { prefix, writer });
  } catch (e) {
    toast(e.message, true);
  }
  render();
}

async function toggleBlock(id, enabled) {
  const block = _byId(id);
  if (!block) return;
  try {
    Object.assign(block, await api.patch(`/prompt-blocks/${id}`, { enabled }));
    if (block.region === "prefix") _setNote(PREFIX_NOTE);
  } catch (e) {
    toast(e.message, true);
  }
  render();
}

function openBlock(id) {
  const b = id ? _byId(id) : { kind: "custom", label: "", content: "", role: "system", region: "writer" };
  if (!b) return;
  const marker = _isMarker(b);
  const roleField =
    !marker && b.region === "prefix"
      ? `<div class="field"><label for="pl-role">Role</label>
          <select id="pl-role">${["system", "user", "assistant"]
            .map((r) => `<option value="${r}" ${b.role === r ? "selected" : ""}>${r}</option>`)
            .join("")}</select></div>`
      : "";
  const body = marker
    ? `<div class="field"><label for="pl-content">Template</label>
        <textarea id="pl-content" rows="6">${esc(b.content)}</textarea></div>`
    : `<div class="field"><label for="pl-label">Name</label><input id="pl-label" value="${escAttr(b.label)}"></div>
       <div class="field"><label for="pl-content">Text</label><textarea id="pl-content" rows="8">${esc(b.content)}</textarea></div>
       ${roleField}`;
  showModal(`
    <h2>${marker ? esc(b.label) : id ? "Edit Block" : "New Block"}</h2>
    ${body}
    <div class="modal-actions">
      ${id && !marker ? `<button class="btn btn-danger" data-wf-action="prompt-layout:deleteBlock" data-block-id="${escAttr(id)}">Delete</button>` : ""}
      ${marker ? `<button class="btn" data-wf-action="prompt-layout:resetBlock" data-block-id="${escAttr(id)}">Reset</button>` : ""}
      <button class="btn" data-wf-action="modal:close">Cancel</button>
      <button class="btn btn-accent" data-wf-action="prompt-layout:saveBlock" data-block-id="${escAttr(id || "")}">${id ? "Save" : "Create"}</button>
    </div>`);
}

async function saveBlock(id) {
  const data = { content: $("pl-content").value };
  const block = id ? _byId(id) : null;
  if (!block || !_isMarker(block)) data.label = $("pl-label").value;
  if ($("pl-role")) data.role = $("pl-role").value;
  try {
    if (block) Object.assign(block, await api.patch(`/prompt-blocks/${id}`, data));
    else _blocks.push(await api.post(`/prompt-layouts/${_viewId}/blocks`, data));
    if ((block?.region || "writer") === "prefix") _setNote(PREFIX_NOTE);
    closeModal();
    render();
  } catch (e) {
    toast(e.message, true);
  }
}

function deleteBlock(id) {
  const block = _byId(id);
  confirmDelete("Block", `Delete "${esc(block?.label || "Untitled block")}"?`, async () => {
    try {
      await api.del(`/prompt-blocks/${id}`);
      _blocks = _blocks.filter((b) => b.id !== id);
      closeModal();
      render();
    } catch (e) {
      toast(e.message, true);
    }
  });
}

function nameModal(title, value, action) {
  showModal(
    `<h2>${esc(title)}</h2>
    <div class="field"><label for="pl-layout-name">Name</label><input id="pl-layout-name" value="${escAttr(value)}"></div>
    <div class="modal-actions">
      <button class="btn" data-wf-action="modal:close">Cancel</button>
      <button class="btn btn-accent" data-wf-action="prompt-layout:${action}">Save</button>
    </div>`,
    { size: "narrow" },
  );
}

async function createLayout(copyFrom) {
  const name = $("pl-layout-name").value.trim();
  if (!name) return toast("A layout needs a name", true);
  try {
    const layout = await api.post("/prompt-layouts", { name, copy_from: copyFrom });
    _viewId = layout.id;
    _note = "";
    closeModal();
    await loadPromptLayout();
  } catch (e) {
    toast(e.message, true);
  }
}

async function renameLayout() {
  const name = $("pl-layout-name").value.trim();
  if (!name) return toast("A layout needs a name", true);
  try {
    await api.patch(`/prompt-layouts/${_viewId}`, { name });
    closeModal();
    await loadPromptLayout();
  } catch (e) {
    toast(e.message, true);
  }
}

const _viewed = () => _layouts.find((l) => l.id === _viewId);

function deleteLayout() {
  confirmDelete("Layout", `Delete "${esc(_viewed()?.name || "")}" and its blocks?`, async () => {
    try {
      await api.del(`/prompt-layouts/${_viewId}`);
      _viewId = _activeId;
      await loadPromptLayout();
    } catch (e) {
      toast(e.message, true);
    }
  });
}

registerActions("prompt-layout", {
  view: async (el) => {
    _viewId = el.value;
    _note = "";
    await loadPromptLayout();
  },
  activate: async () => {
    try {
      await api.put("/prompt-layouts/active", { id: _viewId });
      _activeId = _viewId;
      _setNote(PREFIX_NOTE);
      await loadPromptLayout();
    } catch (e) {
      toast(e.message, true);
    }
  },
  new: () => nameModal("New Layout", "", "createNew"),
  duplicate: () => nameModal("Copy Layout", `${_viewed()?.name || "Layout"} copy`, "createCopy"),
  rename: () => nameModal("Rename Layout", _viewed()?.name || "", "saveName"),
  createNew: () => createLayout(null),
  createCopy: () => createLayout(_viewId),
  saveName: () => renameLayout(),
  delete: () => deleteLayout(),
  add: () => openBlock(null),
  open: (el, e) => {
    if (!e.target.closest(".frag-toggle-wrapper, .frag-drag-handle")) openBlock(el.dataset.blockId);
  },
  toggle: (el) => toggleBlock(el.dataset.blockId, el.checked),
  saveBlock: (el) => saveBlock(el.dataset.blockId),
  deleteBlock: (el) => deleteBlock(el.dataset.blockId),
  resetBlock: (el) => {
    $("pl-content").value = _byId(el.dataset.blockId)?.default_content ?? "";
  },
});
