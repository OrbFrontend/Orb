import { api } from "./api.js";
import { $, esc, toast } from "./utils.js";
import { showConfirmModal, showModal, closeModal } from "./modal.js";

// ── Module state
let _worlds = [];
let _lorebookOpen = false;
let _focusWorldId = null;
let _entries = {}; // worldId -> entry[]
let _selectedEntryId = null;
let _dirty = false;
let _draft = { name: "", content: "", keywords: [], priority: 100, case_insensitive: true, enabled: true };

// ── Worlds API
export async function loadWorlds() {
  try {
    _worlds = await api.get("/worlds");
    renderWorldsSidebar();
  } catch (e) {
    console.error("Failed to load worlds:", e);
  }
}

async function _loadEntries(worldId) {
  _entries[worldId] = await api.get(`/worlds/${worldId}/entries`);
}

// ── Sidebar rendering
export function renderWorldsSidebar() {
  const el = $("worlds-list");
  if (!el) return;
  if (!_worlds.length) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No worlds yet</div>`;
    return;
  }
  el.innerHTML = _worlds
    .map((w) => {
      const initials = w.name.slice(0, 2).toUpperCase();
      const enabled = w.enabled === true || w.enabled === 1;
      const active = _lorebookOpen && _focusWorldId === w.id;
      const toggleId = `world-toggle-${w.id}`;
      const clickHandler = active ? 'closeLorebook()' : `openLorebook('${w.id}')`;
      return `
      <div class="world-item${active ? " active" : ""}">
        <div class="world-item-main" onclick="${clickHandler}">
          <div class="world-avatar">${esc(initials)}</div>
          <span class="world-name">${esc(w.name)}</span>
        </div>
        <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
          <label class="frag-toggle" for="${toggleId}">
            <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                   onchange="toggleWorldEnabled('${w.id}', this.checked)">
            <span class="frag-toggle-slider"></span>
          </label>
        </div>
      </div>`;
    })
    .join("");
}

// ── Activate and prioritize a world (called when loading a character with a linked lorebook)
export async function activateAndPrioritizeWorld(worldId) {
  const idx = _worlds.findIndex((w) => w.id === worldId);
  if (idx === -1) return;
  const world = _worlds[idx];
  const enabled = world.enabled === true || world.enabled === 1;
  if (!enabled) {
    try {
      const updated = await api.put(`/worlds/${worldId}`, { enabled: true });
      _worlds[idx] = { ...world, ...updated };
    } catch (e) {
      console.error("Failed to enable world:", e);
      return;
    }
  }
  // Move to top of list
  const [w] = _worlds.splice(idx, 1);
  _worlds.unshift(w);
  renderWorldsSidebar();
}

// ── World CRUD
export async function showCreateWorldModal() {
  showModal(`
    <h2>New World</h2>
    <div class="field">
      <label>Name</label>
      <input id="world-name-inp" placeholder="e.g. Hamlet" autofocus>
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="createWorld()">Create</button>
    </div>`);
  setTimeout(() => $("world-name-inp")?.focus(), 50);
}

export async function createWorld() {
  const name = $("world-name-inp")?.value?.trim();
  if (!name) { toast("Name is required", true); return; }
  try {
    const w = await api.post("/worlds", { name });
    _worlds.push(w);
    closeModal();
    renderWorldsSidebar();
    openLorebook(w.id);
  } catch (e) {
    toast("Failed to create world", true);
  }
}

export async function toggleWorldEnabled(worldId, enabled) {
  try {
    const updated = await api.put(`/worlds/${worldId}`, { enabled });
    const idx = _worlds.findIndex((w) => w.id === worldId);
    if (idx !== -1) _worlds[idx] = { ..._worlds[idx], ...updated };
  } catch (e) {
    toast("Failed to update world", true);
  }
}

export async function deleteWorld(worldId) {
  showConfirmModal(
    { title: "Delete Lorebook", message: "⚠️ Delete this lorebook and all its entries?", confirmText: "Delete", confirmClass: "btn-danger" },
    async () => {
      try {
        await api.del(`/worlds/${worldId}`);
        _worlds = _worlds.filter((w) => w.id !== worldId);
        delete _entries[worldId];
        if (_focusWorldId === worldId) closeLorebook();
        renderWorldsSidebar();
      } catch (e) {
        toast("Failed to delete lorebook", true);
      }
    },
  );
}

// ── Lorebook drawer
export async function openLorebook(worldId) {
  _focusWorldId = worldId;
  _lorebookOpen = true;
  _selectedEntryId = null;
  _dirty = false;
  _draft = { name: "", content: "", keywords: [], priority: 100, case_insensitive: true, enabled: true };
  renderWorldsSidebar();
  try {
    await _loadEntries(worldId);
  } catch (e) {
    toast("Failed to load entries", true);
  }
  renderLorebookDrawer();
  $("lorebook-drawer")?.classList.remove("hidden");
}

export function closeLorebook() {
  _lorebookOpen = false;
  _dirty = false;
  $("lorebook-drawer")?.classList.add("hidden");
  renderWorldsSidebar();
}

function _getWorld(worldId) {
  return _worlds.find((w) => w.id === worldId);
}

function _getEntry(entryId) {
  for (const entries of Object.values(_entries)) {
    const e = entries.find((e) => e.id === entryId);
    if (e) return e;
  }
  return null;
}

function renderLorebookDrawer() {
  const drawer = $("lorebook-drawer");
  if (!drawer) return;

  const world = _getWorld(_focusWorldId);
  if (!world) { closeLorebook(); return; }

  const entries = _entries[_focusWorldId] || [];
  const activeCount = entries.filter((e) => e.enabled === true || e.enabled === 1).length;

  const entryListHtml = entries
    .map((e) => {
      const enabled = e.enabled === true || e.enabled === 1;
      const sel = _selectedEntryId === e.id;
      const toggleId = `lb-entry-toggle-${e.id}`;
      const dirtyDot = _dirty && _selectedEntryId === e.id ? `<span class="lb-dirty-dot"></span>` : "";
      return `
      <div class="lb-entry-item${sel ? " active" : ""}${!enabled ? " lb-disabled" : ""}" onclick="lbSelectEntry(${e.id})">
        ${dirtyDot}
        <span class="lb-entry-name">${esc(e.name)}</span>
        <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
          <label class="frag-toggle" for="${toggleId}">
            <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                   onchange="lbToggleEntry(${e.id}, this.checked)">
            <span class="frag-toggle-slider"></span>
          </label>
        </div>
      </div>`;
    })
    .join("");

  const editorHtml = _selectedEntryId ? _buildEditorHtml() : `<div class="lb-empty-state">Select an entry to edit</div>`;

  drawer.innerHTML = `
    <div class="lb-header">
      <span class="lb-header-title">Lorebook</span>
      <button class="btn btn-sm lb-close-btn" onclick="closeLorebook()">✕</button>
    </div>
    <div class="lb-body">
      <div class="lb-entry-list">
        <div class="lb-world-header">
          <span class="lb-world-name">${esc(world.name)}</span>
          <span class="lb-active-count">${activeCount} active</span>
        </div>
        <div class="lb-entries-scroll">
          ${entryListHtml}
        </div>
        <div class="lb-entry-list-footer">
          <button class="btn btn-sm btn-block" onclick="lbAddEntry()">+ New Entry</button>
          <button class="btn btn-sm btn-block" style="color:var(--red);margin-top:4px" onclick="deleteWorld('${_focusWorldId}')">Delete Lorebook</button>
        </div>
      </div>
      <div class="lb-editor" id="lb-editor">
        ${editorHtml}
      </div>
    </div>`;

  if (_selectedEntryId) _renderKeywordChips();
}

function _buildEditorHtml() {
  const unsavedBadge = _dirty ? `<span class="lb-unsaved-badge">Unsaved changes</span>` : "";
  const discardBtn = _dirty ? `<button class="btn btn-sm" onclick="lbDiscardChanges()">Discard</button>` : "";
  return `
    <div class="lb-editor-inner">
      <div class="lb-editor-header">
        <input id="lb-entry-name" class="lb-entry-name-input" value="${esc(_draft.name)}"
               oninput="lbDraftChange('name', this.value)">
        <div class="lb-editor-header-right">
          ${unsavedBadge}
          <span class="lb-priority-label">Priority</span>
          <input id="lb-priority" class="lb-priority-input" type="number" value="${_draft.priority}"
                 oninput="lbDraftChange('priority', parseInt(this.value) || 0)">
        </div>
      </div>
      <div class="lb-editor-keywords">
        <div class="lb-field-label">Trigger Keywords</div>
        <div class="lb-chip-wrap" id="lb-chip-wrap" onclick="document.getElementById('lb-chip-text')?.focus()"></div>
        <div class="lb-keyword-footer">
          <label class="lb-case-check">
            <input type="checkbox" id="lb-case-insensitive" ${_draft.case_insensitive ? "checked" : ""}
                   onchange="lbDraftChange('case_insensitive', this.checked)">
            <span>Case-insensitive</span>
          </label>
          <span class="lb-keyword-hint">Enter or , to add · Backspace to remove</span>
        </div>
      </div>
      <div class="lb-editor-content">
        <div class="lb-field-label">Injected Content</div>
        <textarea id="lb-content" class="lb-content-textarea"
                  oninput="lbDraftChange('content', this.value)">${esc(_draft.content)}</textarea>
      </div>
      <div class="lb-editor-actions">
        <button class="btn btn-sm" style="color:var(--red)" onclick="lbDeleteEntry()">Delete</button>
        <div style="display:flex;gap:6px;margin-left:auto">
          ${discardBtn}
          <button class="btn btn-sm${_dirty ? " btn-accent" : ""}" onclick="lbSaveEntry()">Save</button>
        </div>
      </div>
    </div>`;
}

function _renderKeywordChips() {
  const wrap = $("lb-chip-wrap");
  if (!wrap) return;
  const chips = _draft.keywords;
  wrap.innerHTML =
    chips.map((c, i) => `<span class="lb-chip">${esc(c)}<button class="lb-chip-remove" onclick="lbRemoveChip(${i})">×</button></span>`).join("") +
    `<input id="lb-chip-text" class="lb-chip-text" placeholder="${chips.length ? "" : "Add keyword…"}" onkeydown="lbChipKeydown(event)" oninput="lbChipInput(this)">`;
}

// ── Dirty state — surgical DOM updates to avoid losing input focus
function _markDirty() {
  if (_dirty) return;
  _dirty = true;

  // Unsaved badge
  const headerRight = document.querySelector(".lb-editor-header-right");
  if (headerRight && !headerRight.querySelector(".lb-unsaved-badge")) {
    const badge = document.createElement("span");
    badge.className = "lb-unsaved-badge";
    badge.textContent = "Unsaved changes";
    headerRight.insertBefore(badge, headerRight.firstChild);
  }

  // Discard button
  const actions = document.querySelector(".lb-editor-actions > div");
  if (actions && !actions.querySelector(".lb-discard-btn")) {
    const saveBtn = actions.querySelector(".btn:last-child");
    const btn = document.createElement("button");
    btn.className = "btn btn-sm lb-discard-btn";
    btn.textContent = "Discard";
    btn.onclick = lbDiscardChanges;
    actions.insertBefore(btn, saveBtn);
    saveBtn?.classList.add("btn-accent");
  }

  // Dirty dot in entry list
  const active = document.querySelector(".lb-entry-item.active");
  if (active && !active.querySelector(".lb-dirty-dot")) {
    const dot = document.createElement("span");
    dot.className = "lb-dirty-dot";
    active.insertBefore(dot, active.firstChild);
  }
}

export function lbDraftChange(field, value) {
  _draft[field] = value;
  _markDirty();
}

// ── Keyword chip handlers
export function lbChipKeydown(e) {
  const input = e.target;
  if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
    e.preventDefault();
    const val = input.value.replace(/,$/, "").trim();
    if (val && !_draft.keywords.includes(val)) {
      _draft.keywords = [..._draft.keywords, val];
      _markDirty();
      _renderKeywordChips();
      setTimeout(() => $("lb-chip-text")?.focus(), 0);
    }
    return;
  }
  if (e.key === "Backspace" && !input.value && _draft.keywords.length) {
    _draft.keywords = _draft.keywords.slice(0, -1);
    _markDirty();
    _renderKeywordChips();
    setTimeout(() => $("lb-chip-text")?.focus(), 0);
  }
}

export function lbChipInput(input) {
  if (input.value.endsWith(",")) {
    const val = input.value.slice(0, -1).trim();
    if (val && !_draft.keywords.includes(val)) {
      _draft.keywords = [..._draft.keywords, val];
      _markDirty();
      _renderKeywordChips();
      setTimeout(() => $("lb-chip-text")?.focus(), 0);
    }
  }
}

export function lbRemoveChip(i) {
  _draft.keywords = _draft.keywords.filter((_, j) => j !== i);
  _markDirty();
  _renderKeywordChips();
  setTimeout(() => $("lb-chip-text")?.focus(), 0);
}

// ── Entry selection with dirty guard
export function lbSelectEntry(entryId) {
  if (_selectedEntryId === entryId) return;
  if (_dirty) {
    showConfirmModal(
      { title: "Unsaved changes", message: "Discard changes to this entry and continue?", confirmText: "Discard & continue", confirmClass: "btn-danger" },
      () => _doSelectEntry(entryId),
    );
    return;
  }
  _doSelectEntry(entryId);
}

function _doSelectEntry(entryId) {
  _selectedEntryId = entryId;
  _dirty = false;
  const entry = _getEntry(entryId);
  if (entry) {
    _draft = {
      name: entry.name,
      content: entry.content || "",
      keywords: [...(entry.keywords || [])],
      priority: entry.priority ?? 100,
      case_insensitive: entry.case_insensitive === true || entry.case_insensitive === 1,
      enabled: entry.enabled === true || entry.enabled === 1,
    };
  }
  renderLorebookDrawer();
}

// ── Toggle entry enabled from list
export async function lbToggleEntry(entryId, enabled) {
  const worldId = _focusWorldId;
  try {
    const updated = await api.put(`/worlds/${worldId}/entries/${entryId}`, { enabled });
    const idx = (_entries[worldId] || []).findIndex((e) => e.id === entryId);
    if (idx !== -1) _entries[worldId][idx] = { ..._entries[worldId][idx], ...updated };
    if (_selectedEntryId === entryId) _draft.enabled = enabled;
    const activeCount = (_entries[worldId] || []).filter((e) => e.enabled === true || e.enabled === 1).length;
    const countEl = document.querySelector(".lb-active-count");
    if (countEl) countEl.textContent = `${activeCount} active`;
  } catch (e) {
    toast("Failed to update entry", true);
  }
}

// ── Save
export async function lbSaveEntry() {
  if (!_selectedEntryId) return;
  const worldId = _focusWorldId;
  try {
    const updated = await api.put(`/worlds/${worldId}/entries/${_selectedEntryId}`, {
      name: _draft.name,
      content: _draft.content,
      keywords: _draft.keywords,
      case_insensitive: _draft.case_insensitive,
      priority: _draft.priority,
      enabled: _draft.enabled,
    });
    const idx = (_entries[worldId] || []).findIndex((e) => e.id === _selectedEntryId);
    if (idx !== -1) _entries[worldId][idx] = { ..._entries[worldId][idx], ...updated };
    _dirty = false;
    renderLorebookDrawer();
    toast("Entry saved");
  } catch (e) {
    toast("Failed to save entry", true);
  }
}

// ── Discard
export function lbDiscardChanges() {
  const entry = _getEntry(_selectedEntryId);
  if (entry) {
    _draft = {
      name: entry.name,
      content: entry.content || "",
      keywords: [...(entry.keywords || [])],
      priority: entry.priority ?? 100,
      case_insensitive: entry.case_insensitive === true || entry.case_insensitive === 1,
      enabled: entry.enabled === true || entry.enabled === 1,
    };
  }
  _dirty = false;
  renderLorebookDrawer();
}

// ── Delete entry
export function lbDeleteEntry() {
  if (!_selectedEntryId) return;
  const worldId = _focusWorldId;
  showConfirmModal(
    { title: "Delete Entry", message: "Delete this lorebook entry?", confirmText: "Delete", confirmClass: "btn-danger" },
    async () => {
      try {
        await api.del(`/worlds/${worldId}/entries/${_selectedEntryId}`);
        _entries[worldId] = (_entries[worldId] || []).filter((e) => e.id !== _selectedEntryId);
        _selectedEntryId = null;
        _dirty = false;
        renderLorebookDrawer();
        toast("Entry deleted");
      } catch (e) {
        toast("Failed to delete entry", true);
      }
    },
  );
}

// ── Add new entry
export async function lbAddEntry() {
  const worldId = _focusWorldId;
  try {
    const entry = await api.post(`/worlds/${worldId}/entries`, {
      name: "New Entry",
      content: "",
      keywords: [],
      case_insensitive: true,
      priority: 100,
      enabled: true,
    });
    if (!_entries[worldId]) _entries[worldId] = [];
    _entries[worldId].push(entry);
    _doSelectEntry(entry.id);
  } catch (e) {
    toast("Failed to create entry", true);
  }
}
