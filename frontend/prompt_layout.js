import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { initDragReorder } from "./drag_reorder.js";
import { GRIP_ICON } from "./icons.js";
import { closeModal, confirmDelete, showModal } from "./modal.js";
import { $, boolFlag, esc, escAttr, toast } from "./utils.js";

let _layouts = [];
let _activeId = "default";
let _blocks = [];
let _dragReady = false;

export async function loadPromptLayout() {
  const { layouts, active_id } = await api.get("/prompt-layouts");
  _layouts = layouts;
  _activeId = active_id;
  _blocks = await api.get(`/prompt-layouts/${_activeId}/blocks`);
  render();
}

const _byId = (id) => _blocks.find((b) => b.id === id);
const _isMarker = (b) => b.kind !== "custom";

// What each built-in block carries, and what produces it.
const BUILT_IN = {
  main_prompt: {
    about:
      "Your system prompt. A character card's own system prompt replaces it unless Prevent prompt overrides is on.",
    source: "You, or the character card",
  },
  cast: { about: "The group's character cards, as the group's context mode shapes them.", source: "Group chat" },
  constant_lore: { about: "Constant lorebook entries without @ Depth.", source: "Worlds" },
  selected_lore: {
    about: "Lorebook entries chosen this turn by keyword or by the Director.",
    source: "Worlds, Director",
  },
  decisions: { about: "The Judge's guidance for this turn.", source: "Judge" },
  scene_guidance: {
    about: "The Director's guidance for this turn, followed by the current state.",
    source: "Director, State",
  },
  user_message: { about: "Your message this turn.", source: "You" },
  depth_lore: { about: "Constant lorebook entries with @ Depth.", source: "Worlds" },
};

// The role a block takes unless the user picks another: system text above the chat history, the Writer's user turn below.
const _defaultRole = (region) => (region === "prefix" ? "system" : "user");

function _ordered(region) {
  return _blocks.filter((b) => b.region === region).sort((a, b) => a.sort_order - b.sort_order);
}

function _rowHtml(b) {
  const label = b.label || "Untitled block";
  const builtIn = _isMarker(b) ? `<span class="frag-type-badge" title="Built-in block">B</span>` : "";
  const role = b.role !== _defaultRole(b.region) ? `<span class="pl-role">${esc(b.role)}</span>` : "";
  const classes = ["fragment-item", "pl-block", boolFlag(b.enabled) ? "" : "pl-off"].filter(Boolean).join(" ");
  return `<div class="${classes}" data-id="${escAttr(b.id)}" data-wf-action="prompt-layout:open" data-block-id="${escAttr(b.id)}">
      <button type="button" class="frag-drag-handle" title="Drag, or use the arrow keys, to reorder" aria-label="Reorder ${escAttr(label)}">${GRIP_ICON}</button>
      <div class="pl-name"><span class="frag-label">${esc(label)}</span>${builtIn}${role}</div>
      <div class="frag-toggle-wrapper">
        <label class="tog" for="pl-toggle-${escAttr(b.id)}">
          <input type="checkbox" id="pl-toggle-${escAttr(b.id)}" ${boolFlag(b.enabled) ? "checked" : ""}
                 data-wf-action="prompt-layout:toggle" data-wf-on="change" data-block-id="${escAttr(b.id)}">
          <span class="tog-slider"></span>
        </label>
      </div>
    </div>`;
}

function render() {
  const el = $("prompt-layout-list");
  if (!el) return;
  const options = _layouts
    .map((l) => `<option value="${escAttr(l.id)}" ${l.id === _activeId ? "selected" : ""}>${esc(l.name)}</option>`)
    .join("");
  el.innerHTML = `
    <div class="pl-header">
      <select class="theme-select" aria-label="Prompt layout" data-wf-action="prompt-layout:select" data-wf-on="change">${options}</select>
      <div class="pl-header-actions">
        <button class="btn btn-sm" data-wf-action="prompt-layout:new">New</button>
        <button class="btn btn-sm" data-wf-action="prompt-layout:duplicate">Copy</button>
        <button class="btn btn-sm" data-wf-action="prompt-layout:rename">Rename</button>
        <button class="btn btn-sm" data-wf-action="prompt-layout:delete" ${_layouts.length < 2 ? "disabled" : ""}>Delete</button>
      </div>
    </div>
    <div class="pl-blocks">
      ${_ordered("prefix").map(_rowHtml).join("")}
      <div class="fragment-item pl-divider" data-id="">
        <div class="pl-divider-rule"><span>Chat history</span></div>
      </div>
      ${_ordered("writer").map(_rowHtml).join("")}
    </div>
    <button class="btn btn-block btn-sm" data-wf-action="prompt-layout:add" style="margin-top:8px">+ Add Block</button>`;
  if (!_dragReady) {
    _dragReady = true;
    initDragReorder(el, { itemSelector: ".fragment-item", handleSelector: ".frag-drag-handle", onReorder: saveOrder });
  }
}

// Runs *fn*, showing a refused request as a toast.
async function _attempt(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveOrder(container) {
  const prefix = [];
  const writer = [];
  let below = false;
  for (const row of container.querySelectorAll(".fragment-item")) {
    if (row.classList.contains("pl-divider")) below = true;
    else (below ? writer : prefix).push(row.dataset.id);
  }
  await _attempt(async () => {
    _blocks = await api.put(`/prompt-layouts/${_activeId}/order`, { prefix, writer });
  });
  render();
}

async function toggleBlock(id, enabled) {
  const block = _byId(id);
  if (!block) return;
  await _attempt(async () => Object.assign(block, await api.patch(`/prompt-blocks/${id}`, { enabled })));
  render();
}

function openBlock(id) {
  const b = id ? _byId(id) : { kind: "custom", label: "", content: "", role: _defaultRole("writer"), region: "writer" };
  if (!b) return;
  const custom = !_isMarker(b);
  const nameField = custom
    ? `<div class="field"><label for="pl-label">Name</label><input id="pl-label" value="${escAttr(b.label)}"></div>`
    : "";
  const about = BUILT_IN[b.kind];
  const aboutField = about
    ? `<p class="modal-subtitle pl-about">${esc(about.about)}</p><div class="field"><label>Source</label><div class="pl-source">${esc(about.source)}</div></div>`
    : "";
  const textField =
    b.editable || !id
      ? `<div class="field"><label for="pl-content">Text</label><textarea id="pl-content" rows="8">${esc(b.content)}</textarea></div>`
      : "";
  const roleField = `<div class="field"><label for="pl-role">Role</label>
      <select id="pl-role">${["system", "user", "assistant"]
        .map((r) => `<option value="${r}" ${b.role === r ? "selected" : ""}>${r}</option>`)
        .join("")}</select></div>`;
  showModal(`
    <h2>${custom ? (id ? "Edit Block" : "New Block") : esc(b.label)}</h2>
    ${aboutField}
    ${nameField}
    ${textField}
    ${roleField}
    <div class="modal-actions">
      ${id && custom ? `<button class="btn btn-danger" data-wf-action="prompt-layout:deleteBlock" data-block-id="${escAttr(id)}">Delete</button>` : ""}
      <button class="btn" data-wf-action="modal:close">Cancel</button>
      <button class="btn btn-accent" data-wf-action="prompt-layout:saveBlock" data-block-id="${escAttr(id || "")}">${id ? "Save" : "Create"}</button>
    </div>`);
}

async function saveBlock(id) {
  const block = id ? _byId(id) : null;
  const data = {};
  if ($("pl-content")) data.content = $("pl-content").value;
  if ($("pl-label")) data.label = $("pl-label").value;
  if ($("pl-role")) data.role = $("pl-role").value;
  await _attempt(async () => {
    if (block) Object.assign(block, await api.patch(`/prompt-blocks/${id}`, data));
    else _blocks.push(await api.post(`/prompt-layouts/${_activeId}/blocks`, data));
    closeModal();
    render();
  });
}

function deleteBlock(id) {
  confirmDelete("Block", `Delete "${esc(_byId(id)?.label || "Untitled block")}"?`, () =>
    _attempt(async () => {
      await api.del(`/prompt-blocks/${id}`);
      _blocks = _blocks.filter((b) => b.id !== id);
      closeModal();
      render();
    }),
  );
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

// Saves the name modal through *save*, then reloads the layouts.
async function saveName(save) {
  const name = $("pl-layout-name").value.trim();
  if (!name) return toast("A layout needs a name", true);
  await _attempt(async () => {
    await save(name);
    closeModal();
    await loadPromptLayout();
  });
}

const _activate = (id) => api.put("/prompt-layouts/active", { id });

const createLayout = (copyFrom) =>
  saveName(async (name) => _activate((await api.post("/prompt-layouts", { name, copy_from: copyFrom })).id));

const _active = () => _layouts.find((l) => l.id === _activeId);

function deleteLayout() {
  confirmDelete("Layout", `Delete "${esc(_active()?.name || "")}" and its blocks?`, () =>
    _attempt(async () => {
      await api.del(`/prompt-layouts/${_activeId}`);
      await loadPromptLayout();
    }),
  );
}

registerActions("prompt-layout", {
  select: async (el) => {
    await _attempt(() => _activate(el.value));
    await loadPromptLayout();
  },
  new: () => nameModal("New Layout", "", "createNew"),
  duplicate: () => nameModal("Copy Layout", `${_active()?.name || "Layout"} copy`, "createCopy"),
  rename: () => nameModal("Rename Layout", _active()?.name || "", "saveName"),
  createNew: () => createLayout(null),
  createCopy: () => createLayout(_activeId),
  saveName: () => saveName((name) => api.patch(`/prompt-layouts/${_activeId}`, { name })),
  delete: () => deleteLayout(),
  add: () => openBlock(null),
  open: (el, e) => {
    if (!e.target.closest(".frag-toggle-wrapper, .frag-drag-handle")) openBlock(el.dataset.blockId);
  },
  toggle: (el) => toggleBlock(el.dataset.blockId, el.checked),
  saveBlock: (el) => saveBlock(el.dataset.blockId),
  deleteBlock: (el) => deleteBlock(el.dataset.blockId),
});
