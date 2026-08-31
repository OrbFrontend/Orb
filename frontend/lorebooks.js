import { api } from "./api.js";
import { createChipInput } from "./chips.js";
import { closeModal, isModalOpen, showConfirmModal, showModal } from "./modal.js";
import { charactersView } from "./state.js";
import { $, boolFlag, downloadBlob, esc, toast } from "./utils.js";
import { changesetRowHtml, isOpen, operationEditHtml, readOperationEdit } from "./world_proposals.js";

let _worlds = [];
let _worldSearch = "";
let _worldsExpanded = false; // show all Worlds instead of recent ones
const RECENT_LIMIT = 5; // Worlds shown before search is needed
let _lorebookOpen = false;

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && _lorebookOpen && !isModalOpen()) closeLorebook();
});
let _focusWorldId = null;
const _entries = {}; // world id -> entries
let _entrySearch = ""; // entry name/content filter
let _selectedEntryId = null;
let _dirty = false;

const _emptyDraft = () => ({
  name: "",
  content: "",
  keywords: [],
  priority: 100,
  case_insensitive: true,
  constant: false,
  at_depth: false,
  enabled: true,
  use_regex: false,
  selective: false,
  secondary_keys: [],
});

const _draftFromEntry = (e) => ({
  name: e.name,
  content: e.content || "",
  keywords: [...(e.keywords || [])],
  priority: e.priority ?? 100,
  case_insensitive: boolFlag(e.case_insensitive),
  constant: boolFlag(e.constant),
  at_depth: boolFlag(e.at_depth),
  enabled: boolFlag(e.enabled),
  use_regex: boolFlag(e.use_regex),
  selective: boolFlag(e.selective),
  secondary_keys: [...(e.secondary_keys || [])],
});

let _draft = _emptyDraft();

const _keywordChips = createChipInput({
  wrapId: "lb-chip-wrap",
  inputId: "lb-chip-text",
  placeholder: "Add keyword…",
  disabledPlaceholder: "Keywords disabled — Constant entry",
  getItems: () => _draft.keywords,
  setItems: (v) => {
    _draft.keywords = v;
  },
  onChange: _markDirty,
  isDisabled: () => _draft.constant,
});

const _secondaryChips = createChipInput({
  wrapId: "lb-sec-chip-wrap",
  inputId: "lb-sec-chip-text",
  placeholder: "Add secondary keyword…",
  getItems: () => _draft.secondary_keys,
  setItems: (v) => {
    _draft.secondary_keys = v;
  },
  onChange: _markDirty,
});

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

const _isWorldEnabled = (w) => boolFlag(w.enabled);
const _worldRecencyTs = (w) => Date.parse(w.updated_at || w.created_at || "") || 0;
const _byRecency = (a, b) => _worldRecencyTs(b) - _worldRecencyTs(a);

function _visibleWorlds() {
  const q = _worldSearch.trim().toLowerCase();
  const active = _worlds.filter(_isWorldEnabled).sort(_byRecency);
  const inactive = _worlds.filter((w) => !_isWorldEnabled(w)).sort(_byRecency);
  if (q) {
    const match = (w) => w.name.toLowerCase().includes(q);
    return { shown: [...active.filter(match), ...inactive.filter(match)], hidden: 0 };
  }
  if (_worldsExpanded) return { shown: [...active, ...inactive], hidden: 0 };
  const recent = inactive.slice(0, Math.max(0, RECENT_LIMIT - active.length));
  const shown = [...active, ...recent];
  return { shown, hidden: _worlds.length - shown.length };
}

function _worldItemHtml(w) {
  const initials = w.name.slice(0, 2).toUpperCase();
  const enabled = _isWorldEnabled(w);
  const active = _lorebookOpen && _focusWorldId === w.id;
  const toggleId = `world-toggle-${w.id}`;
  const clickHandler = active ? "closeLorebook()" : `openLorebook('${w.id}')`;
  const pending = _pendingCount(w.id);
  const dynamic = boolFlag(w.dynamic_enabled);
  const haloClass = dynamic ? " avatar-halo" : "";
  const haloTitle = dynamic ? ' title="Dynamic World — the Agent may propose new lore from what happens in play"' : "";
  return `
  <div class="world-item${active ? " active" : ""}">
    <div class="world-item-main" onclick="${clickHandler}">
      <div class="world-avatar${haloClass}"${haloTitle}>${esc(initials)}</div>
      <span class="world-name">${esc(w.name)}</span>
      ${pending ? `<span class="world-pending" title="World changes awaiting review">${pending}</span>` : ""}
    </div>
    <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
      <label class="tog" for="${toggleId}">
        <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
               onchange="toggleWorldEnabled('${w.id}', this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
  </div>`;
}

export function renderWorldsSidebar() {
  const el = $("worlds-list");
  if (!el) return;

  const countEl = $("worlds-active-count");
  if (countEl) {
    const activeCount = _worlds.filter(_isWorldEnabled).length;
    countEl.textContent = activeCount;
    countEl.style.display = activeCount > 0 ? "" : "none";
  }

  const searchWrap = $("worlds-search-wrap");
  if (searchWrap) {
    searchWrap.style.display = _worlds.length > RECENT_LIMIT || _worldSearch.trim() ? "" : "none";
  }
  const searchInp = $("worlds-search");
  if (searchInp && searchInp.value !== _worldSearch) searchInp.value = _worldSearch;

  if (!_worlds.length) {
    el.innerHTML = `<div class="worlds-empty">No worlds yet</div>`;
    return;
  }

  const { shown, hidden } = _visibleWorlds();
  if (!shown.length) {
    el.innerHTML = `<div class="worlds-empty">No worlds match “${esc(_worldSearch.trim())}”</div>`;
    return;
  }

  let html = shown.map(_worldItemHtml).join("");
  if (!_worldSearch.trim()) {
    if (hidden > 0) {
      html += `<button type="button" class="worlds-more" onclick="expandWorlds()">+${hidden} more — show all</button>`;
    } else if (_worldsExpanded && _worlds.length > RECENT_LIMIT) {
      html += `<button type="button" class="worlds-more" onclick="collapseWorlds()">Show less</button>`;
    }
  }
  el.innerHTML = html;
}

export function onWorldSearch(value) {
  _worldSearch = value;
  renderWorldsSidebar();
}

export function expandWorlds() {
  _worldsExpanded = true;
  renderWorldsSidebar();
}

export function collapseWorlds() {
  _worldsExpanded = false;
  renderWorldsSidebar();
}

export async function activateAndPrioritizeWorld(worldId) {
  const world = _getWorld(worldId);
  if (!world) return;
  if (!boolFlag(world.enabled)) {
    try {
      _mergeWorld(worldId, await api.put(`/worlds/${worldId}`, { enabled: true }));
    } catch (e) {
      console.error("Failed to enable world:", e);
      return;
    }
  }
  const [w] = _worlds.splice(
    _worlds.findIndex((x) => x.id === worldId),
    1,
  );
  _worlds.unshift(w);
  renderWorldsSidebar();
}

export function reflectConversationWorldActivation(worldIds) {
  const active = new Set(worldIds || []);
  const linked = new Set(
    charactersView()
      .map((card) => card.world_id)
      .filter(Boolean),
  );
  for (const world of _worlds) {
    if (linked.has(world.id)) world.enabled = active.has(world.id) ? 1 : 0;
  }
  for (const worldId of [...active].reverse()) {
    const index = _worlds.findIndex((world) => world.id === worldId);
    if (index >= 0) _worlds.unshift(_worlds.splice(index, 1)[0]);
  }
  renderWorldsSidebar();
}

export async function deactivateLinkedWorlds() {
  await api.post("/worlds/deactivate-linked", {});
}

export async function deactivateWorld(worldId) {
  const world = _getWorld(worldId);
  if (!world) return;
  const enabled = boolFlag(world.enabled);
  if (!enabled) return;
  try {
    _mergeWorld(worldId, await api.put(`/worlds/${worldId}`, { enabled: false }));
    renderWorldsSidebar();
  } catch (e) {
    console.error("Failed to deactivate world:", e);
  }
}

export function showRenameWorldModal(worldId) {
  const world = _getWorld(worldId);
  if (!world) return;
  showModal(`
    <h2>Rename Lorebook</h2>
    <div class="field">
      <label>Name</label>
      <input id="rename-world-inp" value="${esc(world.name)}" autofocus>
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="renameWorld('${worldId}')">Rename</button>
    </div>`);
  setTimeout(() => {
    const inp = $("rename-world-inp");
    if (inp) {
      inp.focus();
      inp.select();
    }
  }, 50);
}

export async function renameWorld(worldId) {
  const name = $("rename-world-inp")?.value?.trim();
  if (!name) {
    toast("Name is required", true);
    return;
  }
  try {
    _mergeWorld(worldId, await api.put(`/worlds/${worldId}`, { name }));
    closeModal();
    renderWorldsSidebar();
    if (_focusWorldId === worldId) renderLorebookDrawer();
  } catch (_e) {
    toast("Failed to rename lorebook", true);
  }
}

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
  if (!name) {
    toast("Name is required", true);
    return;
  }
  try {
    const w = await api.post("/worlds", { name });
    _worlds.push(w);
    closeModal();
    renderWorldsSidebar();
    _openDrawer(w.id);
  } catch (_e) {
    toast("Failed to create world", true);
  }
}

export async function toggleWorldEnabled(worldId, enabled) {
  try {
    _mergeWorld(worldId, await api.put(`/worlds/${worldId}`, { enabled }));
  } catch (_e) {
    toast("Failed to update world", true);
  }
  renderWorldsSidebar();
}

export async function deleteWorld(worldId) {
  const linked = charactersView().filter((c) => c.world_id === worldId);
  let extraHtml = "";
  if (linked.length) {
    const names = linked.map((c) => `<li>${esc(c.name)}</li>`).join("");
    extraHtml = `
      <p style="margin-bottom:4px">Linked to ${linked.length} character${linked.length === 1 ? "" : "s"}, which will be unlinked:</p>
      <ul style="margin:0;padding-left:20px;max-height:160px;overflow-y:auto">${names}</ul>`;
  }
  showConfirmModal(
    {
      title: "Delete Lorebook",
      message: "⚠️ Delete this lorebook and all its entries?",
      confirmText: "Delete",
      confirmClass: "btn-danger",
      extraHtml,
    },
    async () => {
      try {
        await api.del(`/worlds/${worldId}`);
        _worlds = _worlds.filter((w) => w.id !== worldId);
        delete _entries[worldId];
        for (const c of charactersView()) {
          if (c.world_id === worldId) c.world_id = null;
        }
        if (_focusWorldId === worldId) _closeDrawer();
        renderWorldsSidebar();
      } catch (_e) {
        toast("Failed to delete lorebook", true);
      }
    },
  );
}

function _guardDirty(proceed, message, confirmText) {
  if (!_dirty) {
    proceed();
    return;
  }
  showConfirmModal({ title: "Unsaved changes", message, confirmText, confirmClass: "btn-danger" }, proceed);
}

export async function openLorebook(worldId) {
  _guardDirty(
    () => _openDrawer(worldId),
    "Discard changes to this entry and open another lorebook?",
    "Discard & switch",
  );
}

async function _openDrawer(worldId) {
  _focusWorldId = worldId;
  _lorebookOpen = true;
  _selectedEntryId = null;
  _entrySearch = "";
  _dirty = false;
  _draft = _emptyDraft();
  _drawerTab = "entries";
  renderWorldsSidebar();
  try {
    await Promise.all([_loadEntries(worldId), _loadChangesets(worldId)]);
  } catch (_e) {
    toast("Failed to load entries", true);
  }
  renderLorebookDrawer();
  $("lorebook-drawer")?.classList.remove("hidden");
}

export function closeLorebook() {
  _guardDirty(_closeDrawer, "Discard changes to this entry and close the lorebook?", "Discard & close");
}

function _closeDrawer() {
  _lorebookOpen = false;
  _dirty = false;
  $("lorebook-drawer")?.classList.add("hidden");
  renderWorldsSidebar();
}

function _getWorld(worldId) {
  return _worlds.find((w) => w.id === worldId);
}

function _mergeWorld(worldId, updated) {
  const idx = _worlds.findIndex((w) => w.id === worldId);
  if (idx !== -1) _worlds[idx] = { ..._worlds[idx], ...updated };
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
  if (!world) {
    _closeDrawer();
    return;
  }

  const allEntries = _entries[_focusWorldId] || [];
  const liveEntries = allEntries.filter((e) => !boolFlag(e.archived));
  const activeCount = liveEntries.filter((e) => boolFlag(e.enabled)).length;
  const dynamicOn = boolFlag(world.dynamic_enabled);
  const dynamicCount = liveEntries.filter((e) => e.entry_layer === "dynamic").length;
  const pendingCount = _pendingCount(_focusWorldId);

  const q = _entrySearch.trim().toLowerCase();
  const entries = q
    ? liveEntries.filter((e) => (e.name || "").toLowerCase().includes(q) || (e.content || "").toLowerCase().includes(q))
    : liveEntries;

  const entryListHtml = entries.length
    ? entries
        .map((e) => {
          const enabled = boolFlag(e.enabled);
          const sel = _selectedEntryId === e.id;
          const toggleId = `lb-entry-toggle-${e.id}`;
          const dirtyDot = _dirty && _selectedEntryId === e.id ? `<span class="lb-dirty-dot"></span>` : "";
          const layerTag =
            e.entry_layer === "dynamic"
              ? `<span class="lb-layer lb-layer-dynamic" title="Agent-managed">Dynamic</span>`
              : "";
          return `
      <div class="lb-entry-item${sel ? " active" : ""}${!enabled ? " lb-disabled" : ""}" onclick="lbSelectEntry(${e.id})">
        ${dirtyDot}
        <span class="lb-entry-name">${esc(e.name || e.keywords?.[0] || "")}</span>
        ${layerTag}
        <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
          <label class="tog" for="${toggleId}">
            <input type="checkbox" id="${toggleId}" ${enabled ? "checked" : ""}
                   onchange="lbToggleEntry(${e.id}, this.checked)">
            <span class="tog-slider"></span>
          </label>
        </div>
      </div>`;
        })
        .join("")
    : `<div class="lb-empty-state">${q ? "No matching entries" : "No entries yet"}</div>`;

  const editorHtml = _selectedEntryId
    ? _buildEditorHtml()
    : `<div class="lb-empty-state">Select an entry to edit</div>`;

  const tab = (id, label, badge = 0) =>
    `<button type="button" class="lb-tab${_drawerTab === id ? " active" : ""}" data-lb-tab="${id}">${esc(label)}${
      badge ? `<span class="lb-tab-badge">${badge}</span>` : ""
    }</button>`;

  const list = _CHANGESET_TABS[_drawerTab];
  const rightPane = list ? `<div class="wc-list">${_changesetListHtml(_focusWorldId, ...list)}</div>` : editorHtml;

  const prevScrollTop = drawer.querySelector(".lb-entries-scroll")?.scrollTop ?? 0;

  drawer.innerHTML = `
    <div class="lb-header">
      <span class="lb-header-title">Lorebook</span>
      <button class="btn btn-sm lb-close-btn" onclick="closeLorebook()">✕</button>
    </div>
    <div class="lb-body">
      <div class="lb-entry-list">
        <div class="lb-world-header">
          <span class="lb-world-name" title="${esc(world.name)}">${esc(world.name)}</span>
          <button class="btn btn-sm lb-rename-btn" onclick="showRenameWorldModal('${_focusWorldId}')" title="Rename lorebook">✏</button>
          <span class="lb-active-count">${activeCount} active</span>
        </div>
        <div class="lb-dynamic-row">
          <span class="lb-dynamic-label" title="Let the Agent propose world changes from what happens in play">Dynamic World</span>
          ${dynamicCount ? `<button type="button" class="btn btn-sm lb-reset-btn" title="Retire every Agent-managed entry">Reset</button>` : ""}
          <label class="tog" for="lb-dynamic-toggle" title="Let the Agent propose world changes from what happens in play">
            <input type="checkbox" id="lb-dynamic-toggle" ${dynamicOn ? "checked" : ""}>
            <span class="tog-slider"></span>
          </label>
        </div>
        <div class="lb-entry-search">
          <input id="lb-entry-search-inp" class="lb-entry-search-inp" type="text"
                 placeholder="Search entries…" value="${esc(_entrySearch)}"
                 oninput="lbEntrySearch(this.value)">
        </div>
        <div class="lb-entries-scroll">
          ${entryListHtml}
        </div>
        <div class="lb-entry-list-footer">
          <button class="btn btn-sm btn-block" onclick="lbAddEntry()">+ New Entry</button>
          <div style="display:flex;gap:4px;margin-top:4px">
            <button class="btn btn-sm lb-export-btn" style="flex:1;justify-content:center" title="Export your authored entries as JSON">⬇ Export JSON</button>
            <button class="btn btn-sm" style="flex:1;justify-content:center;color:var(--red)" onclick="deleteWorld('${_focusWorldId}')">Delete Lorebook</button>
          </div>
        </div>
      </div>
      <div class="lb-editor" id="lb-editor" data-has-selection="${!!_selectedEntryId}">
        <div class="lb-tabs">
          ${tab("entries", "Entry")}
          ${tab("pending", "Pending", pendingCount)}
          ${tab("history", "History")}
        </div>
        ${rightPane}
      </div>
    </div>`;

  const scrollEl = drawer.querySelector(".lb-entries-scroll");
  if (scrollEl) scrollEl.scrollTop = prevScrollTop;

  const exportBtn = drawer.querySelector(".lb-export-btn");
  if (exportBtn) exportBtn.onclick = () => lbExportJson("authored");
  const dynamicToggle = $("lb-dynamic-toggle");
  if (dynamicToggle) dynamicToggle.onchange = (e) => toggleWorldDynamic(_focusWorldId, e.target.checked);
  const resetBtn = drawer.querySelector(".lb-reset-btn");
  if (resetBtn) resetBtn.onclick = () => resetWorldToAuthored(_focusWorldId);
  for (const el of drawer.querySelectorAll("[data-lb-tab]")) {
    el.onclick = () => lbSelectTab(el.dataset.lbTab);
  }
  const regexCb = $("lb-use-regex");
  if (regexCb) regexCb.onchange = (e) => lbDraftChange("use_regex", e.target.checked);
  const selectiveCb = $("lb-selective");
  if (selectiveCb) selectiveCb.onchange = (e) => lbToggleSelective(e.target.checked);
  const atDepthCb = $("lb-at-depth");
  if (atDepthCb) atDepthCb.onchange = (e) => lbDraftChange("at_depth", e.target.checked);

  if (_selectedEntryId && _drawerTab === "entries") {
    const kwWrap = $("lb-chip-wrap");
    if (kwWrap && !_draft.constant) kwWrap.onclick = () => $("lb-chip-text")?.focus();
    _keywordChips.render();
    if (_draft.selective && !_draft.constant) {
      const secWrap = $("lb-sec-chip-wrap");
      if (secWrap) secWrap.onclick = () => $("lb-sec-chip-text")?.focus();
      _secondaryChips.render();
    }
  }
}

function _buildEditorHtml() {
  const unsavedBadge = _dirty ? `<span class="lb-unsaved-badge">Unsaved changes</span>` : "";
  const discardBtn = _dirty ? `<button class="btn btn-sm" onclick="lbDiscardChanges()">Discard</button>` : "";
  const entry = _getEntry(_selectedEntryId);
  const layerBanner =
    entry?.entry_layer === "dynamic"
      ? `<div class="lb-layer-banner">Agent-managed entry${
          entry.supersedes_entry_id ? ` — replaces an entry you wrote` : ""
        }. Reset to Authored World retires it.</div>`
      : "";
  return `
    <div class="lb-editor-inner">
      ${layerBanner}
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
      <div class="lb-editor-keywords${_draft.constant ? " lb-keywords-disabled" : ""}">
        <div class="lb-field-label">Trigger Keywords</div>
        <div class="lb-chip-wrap" id="lb-chip-wrap"></div>
        <div class="lb-keyword-footer">
          <label class="lb-case-check">
            <input type="checkbox" id="lb-case-insensitive" ${_draft.case_insensitive ? "checked" : ""}
                   ${_draft.constant ? "disabled" : ""}
                   onchange="lbDraftChange('case_insensitive', this.checked)">
            <span>Case-insensitive</span>
          </label>
          <label class="lb-case-check" title="Treat each keyword as a regular expression">
            <input type="checkbox" id="lb-use-regex" ${_draft.use_regex ? "checked" : ""}
                   ${_draft.constant ? "disabled" : ""}>
            <span>Regex</span>
          </label>
          <label class="lb-case-check" title="Also require one of the secondary keywords to match">
            <input type="checkbox" id="lb-selective" ${_draft.selective ? "checked" : ""}
                   ${_draft.constant ? "disabled" : ""}>
            <span>Selective</span>
          </label>
          <label class="lb-case-check" title="Always inject this entry, regardless of keywords">
            <input type="checkbox" id="lb-constant" ${_draft.constant ? "checked" : ""}
                   onchange="lbToggleConstant(this.checked)">
            <span>Constant</span>
          </label>
          ${
            _draft.constant
              ? `<label class="lb-case-check" title="Inject after the latest message instead of the system prompt — {{roll}} re-rolls every turn">
            <input type="checkbox" id="lb-at-depth" ${_draft.at_depth ? "checked" : ""}>
            <span>@ Depth</span>
          </label>`
              : ""
          }
          <span class="lb-keyword-hint">${_draft.constant ? "Always injected" : "Enter or , to add · Backspace to remove"}</span>
        </div>
      </div>
      ${
        _draft.selective && !_draft.constant
          ? `<div class="lb-editor-keywords">
        <div class="lb-field-label">Secondary Keywords</div>
        <div class="lb-chip-wrap" id="lb-sec-chip-wrap"></div>
        <div class="lb-keyword-footer">
          <span class="lb-keyword-hint">One of these must also match</span>
        </div>
      </div>`
          : ""
      }
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

function _markDirty() {
  if (_dirty) return;
  _dirty = true;

  const headerRight = document.querySelector(".lb-editor-header-right");
  if (headerRight && !headerRight.querySelector(".lb-unsaved-badge")) {
    const badge = document.createElement("span");
    badge.className = "lb-unsaved-badge";
    badge.textContent = "Unsaved changes";
    headerRight.insertBefore(badge, headerRight.firstChild);
  }

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

export function lbToggleConstant(checked) {
  _draft.constant = checked;
  _markDirty();
  renderLorebookDrawer();
}

function lbToggleSelective(checked) {
  _draft.selective = checked;
  _markDirty();
  renderLorebookDrawer();
}

export function lbEntrySearch(value) {
  _entrySearch = value;
  const inp = $("lb-entry-search-inp");
  const caret = inp?.selectionStart ?? value.length;
  renderLorebookDrawer();
  const newInp = $("lb-entry-search-inp");
  if (newInp) {
    newInp.focus();
    newInp.setSelectionRange(caret, caret);
  }
}

export function lbSelectEntry(entryId) {
  if (_selectedEntryId === entryId) return;
  _guardDirty(() => _doSelectEntry(entryId), "Discard changes to this entry and continue?", "Discard & continue");
}

export function lbBackToList() {
  _guardDirty(
    () => {
      _selectedEntryId = null;
      _dirty = false;
      renderLorebookDrawer();
    },
    "Discard changes to this entry and go back?",
    "Discard & go back",
  );
}

function _doSelectEntry(entryId) {
  _selectedEntryId = entryId;
  _dirty = false;
  const entry = _getEntry(entryId);
  if (entry) _draft = _draftFromEntry(entry);
  renderLorebookDrawer();
}

export async function lbToggleEntry(entryId, enabled) {
  const worldId = _focusWorldId;
  try {
    const updated = await api.put(`/worlds/${worldId}/entries/${entryId}`, { enabled });
    const idx = (_entries[worldId] || []).findIndex((e) => e.id === entryId);
    if (idx !== -1) _entries[worldId][idx] = { ..._entries[worldId][idx], ...updated };
    if (_selectedEntryId === entryId) _draft.enabled = enabled;
  } catch (_e) {
    toast("Failed to update entry", true);
  }
  _syncEntryRow(worldId, entryId);
}

function _syncEntryRow(worldId, entryId) {
  const entries = _entries[worldId] || [];
  const entry = entries.find((e) => e.id === entryId);
  const box = $(`lb-entry-toggle-${entryId}`);
  if (entry && box) {
    const on = boolFlag(entry.enabled);
    box.checked = on;
    box.closest(".lb-entry-item")?.classList.toggle("lb-disabled", !on);
  }
  const countEl = document.querySelector(".lb-active-count");
  if (countEl) countEl.textContent = `${entries.filter((e) => boolFlag(e.enabled)).length} active`;
}

export async function lbSaveEntry() {
  if (!_selectedEntryId) return;
  const worldId = _focusWorldId;
  try {
    const updated = await api.put(`/worlds/${worldId}/entries/${_selectedEntryId}`, {
      name: _draft.name,
      content: _draft.content,
      keywords: _draft.keywords,
      case_insensitive: _draft.case_insensitive,
      constant: _draft.constant,
      at_depth: _draft.at_depth,
      use_regex: _draft.use_regex,
      selective: _draft.selective,
      secondary_keys: _draft.secondary_keys,
      priority: _draft.priority,
      enabled: _draft.enabled,
    });
    const idx = (_entries[worldId] || []).findIndex((e) => e.id === _selectedEntryId);
    if (idx !== -1) _entries[worldId][idx] = { ..._entries[worldId][idx], ...updated };
    _dirty = false;
    renderLorebookDrawer();
    toast("Entry saved");
  } catch (_e) {
    toast("Failed to save entry", true);
  }
}

export function lbDiscardChanges() {
  const entry = _getEntry(_selectedEntryId);
  if (entry) _draft = _draftFromEntry(entry);
  _dirty = false;
  renderLorebookDrawer();
}

export function lbDeleteEntry() {
  if (!_selectedEntryId) return;
  const worldId = _focusWorldId;
  showConfirmModal(
    {
      title: "Delete Entry",
      message: "Delete this lorebook entry?",
      confirmText: "Delete",
      confirmClass: "btn-danger",
    },
    async () => {
      try {
        await api.del(`/worlds/${worldId}/entries/${_selectedEntryId}`);
        _entries[worldId] = (_entries[worldId] || []).filter((e) => e.id !== _selectedEntryId);
        _selectedEntryId = null;
        _dirty = false;
        await _refreshAfterWorldChange(worldId);
        toast("Entry deleted");
      } catch (_e) {
        toast("Failed to delete entry", true);
      }
    },
  );
}

export async function lbAddEntry() {
  const worldId = _focusWorldId;
  try {
    const entry = await api.post(`/worlds/${worldId}/entries`, {
      name: "New Entry",
      content: "",
      keywords: [],
      case_insensitive: true,
      constant: false,
      priority: 100,
      enabled: true,
    });
    if (!_entries[worldId]) _entries[worldId] = [];
    _entries[worldId].push(entry);
    _doSelectEntry(entry.id);
  } catch (_e) {
    toast("Failed to create entry", true);
  }
}

function lbExportJson(view = "authored") {
  const world = _getWorld(_focusWorldId);
  if (!world) return;
  const suffix = view === "authored" ? "" : ` (${view})`;
  downloadBlob(`${world.name}${suffix}.json`, `/api/worlds/${world.id}/export?view=${view}`);
}

const _changesets = {}; // world id -> changesets
let _drawerTab = "entries"; // entries, pending, or history

const _CHANGESET_TABS = {
  pending: [["pending", "stale"], "No proposals waiting for review"],
  history: [["applied", "rejected", "reverted", "superseded"], "No decided changes yet"],
};

async function _loadChangesets(worldId) {
  _changesets[worldId] = await api.get(`/worlds/${worldId}/changesets`);
}

function _pendingCount(worldId) {
  const world = _getWorld(worldId);
  const cached = _changesets[worldId];
  return cached ? cached.filter(isOpen).length : world?.pending_changesets || 0;
}

let _chatRefresh = null;

export function setWorldProposalRefresh(fn) {
  _chatRefresh = fn;
}

async function _refreshAfterWorldChange(worldId) {
  try {
    await Promise.all([_loadEntries(worldId), _loadChangesets(worldId)]);
    _worlds = await api.get("/worlds");
  } catch (e) {
    console.error("Failed to refresh world after change:", e);
  }
  renderWorldsSidebar();
  if (_focusWorldId === worldId) renderLorebookDrawer();
  try {
    await _chatRefresh?.();
  } catch (e) {
    console.error("Failed to refresh chat proposals:", e);
  }
}

function _findChangeset(worldId, id) {
  return (_changesets[worldId] || []).find((c) => String(c.id) === String(id)) || null;
}

async function _requireChangeset(worldId, id) {
  const cached = _findChangeset(worldId, id);
  if (cached) return cached;
  await _loadChangesets(worldId);
  return _findChangeset(worldId, id);
}

export async function toggleWorldDynamic(worldId, enabled) {
  try {
    _mergeWorld(worldId, await api.put(`/worlds/${worldId}/dynamic`, { enabled }));
  } catch (_e) {
    toast("Failed to update Dynamic Worlds", true);
  }
  renderLorebookDrawer();
  renderWorldsSidebar();
}

async function _worldMutation(worldId, request, said) {
  try {
    const result = await request();
    if (said) toast(said(result));
  } catch (e) {
    toast(e.message, true);
  }
  await _refreshAfterWorldChange(worldId);
}

const _decideChangeset = (worldId, id, action, said, body = {}) =>
  _worldMutation(worldId, () => api.post(`/worlds/${worldId}/changesets/${id}/${action}`, body), said);

export function resetWorldToAuthored(worldId) {
  showConfirmModal(
    {
      title: "Reset to Authored World",
      message:
        "Retire every Agent-managed entry, restoring this lorebook to the entries you wrote. " +
        "Your own entries are never touched, and this reset can itself be undone from History.",
      confirmText: "Reset",
      confirmClass: "btn-danger",
    },
    () =>
      _worldMutation(
        worldId,
        () => api.post(`/worlds/${worldId}/reset`, {}),
        (r) => (r.reset ? "Reset to your authored world" : "Nothing to reset"),
      ),
  );
}

function _openChangesetEditor(worldId, id, changeset) {
  const ops = changeset.operations || [];
  showModal(`
    <h2>Review world change</h2>
    <div class="field">
      <label>Summary</label>
      <input id="wc-edit-summary" value="${esc(changeset.summary || "")}">
    </div>
    <div class="wc-edit-ops">${ops.map((op, i) => operationEditHtml(op, i)).join("")}</div>
    <div class="modal-actions">
      <button class="btn" id="wc-edit-cancel">Cancel</button>
      <button class="btn" id="wc-edit-save">Save</button>
      <button class="btn btn-accent" id="wc-edit-apply">Save &amp; Apply</button>
    </div>`);

  const collect = () => {
    const edited = [];
    for (const [i, op] of ops.entries()) {
      const row = document.querySelector(`.wc-edit-op[data-wc-index="${i}"]`);
      if (!row) continue;
      const read = (field) => row.querySelector(`[data-wc-field="${field}"]`);
      const values = {
        include: read("include")?.checked ?? true,
        name: read("name")?.value,
        content: read("content")?.value,
        activation: read("activation")?.value,
        keywords: read("keywords")?.value,
      };
      const kept = readOperationEdit(op, values);
      if (kept) edited.push(kept);
    }
    return { summary: $("wc-edit-summary")?.value ?? changeset.summary, operations: edited };
  };

  const bind = (elId, fn) => {
    const el = $(elId);
    if (el) el.onclick = fn;
  };
  bind("wc-edit-cancel", closeModal);
  bind("wc-edit-save", () => {
    const body = collect();
    closeModal();
    return _worldMutation(
      worldId,
      () => api.put(`/worlds/${worldId}/changesets/${id}`, body),
      () => "Proposal updated",
    );
  });
  bind("wc-edit-apply", () => {
    const body = collect();
    closeModal();
    if (!body.operations.length) {
      toast("Nothing left to apply — reject it instead", true);
      return undefined;
    }
    return _decideChangeset(worldId, id, "apply", () => "World updated", body);
  });
}

const _WC_ACTIONS = {
  apply: (world, id) => _decideChangeset(world, id, "apply", () => "World updated"),
  reject: (world, id) => _decideChangeset(world, id, "reject"),
  "re-evaluate": (world, id) => {
    toast("Re-evaluating against the current world…");
    return _decideChangeset(world, id, "re-evaluate", (r) =>
      r.changeset ? "New proposal ready for review" : "Nothing left to propose",
    );
  },
  undo: (world, id) => _decideChangeset(world, id, "undo", () => "Change undone"),
  edit: async (world, id) => {
    let changeset;
    try {
      changeset = await _requireChangeset(world, id);
    } catch (e) {
      toast(e.message, true);
      return;
    }
    if (!changeset) {
      toast("That proposal is no longer there — reload to see the world as it stands", true);
      return;
    }
    _openChangesetEditor(world, id, changeset);
  },
};

export function initWorldProposalActions() {
  document.addEventListener("click", (e) => {
    const btn = e.target?.closest?.("[data-wc-action]");
    if (!btn) return;
    const host = btn.closest("[data-wc-id][data-wc-world]");
    if (!host) return;
    const handler = _WC_ACTIONS[btn.dataset.wcAction];
    if (!handler) return;
    e.preventDefault();
    e.stopPropagation();
    btn.disabled = true;
    Promise.resolve(handler(host.dataset.wcWorld, host.dataset.wcId)).finally(() => {
      btn.disabled = false;
    });
  });
}

export function lbSelectTab(tab) {
  _drawerTab = tab;
  renderLorebookDrawer();
}

function _changesetListHtml(worldId, statuses, emptyText) {
  const rows = (_changesets[worldId] || []).filter((c) => statuses.includes(c.status));
  if (!rows.length) return `<div class="lb-empty-state">${esc(emptyText)}</div>`;
  return rows.map(changesetRowHtml).join("");
}

export function lbImportJson() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      toast("Invalid JSON file", true);
      return;
    }
    const payload = Array.isArray(parsed) ? { entries: parsed } : parsed;
    if (!payload.entries) {
      toast("No entries found in file", true);
      return;
    }
    const worldName = payload.name || file.name.replace(/\.json$/i, "") || "Imported Lorebook";
    if (_worlds.some((w) => w.name === worldName)) {
      toast(`A lorebook named "${worldName}" already exists`, true);
      return;
    }
    try {
      const world = await api.post("/worlds", { name: worldName });
      _worlds.push(world);
      const result = await api.post(`/worlds/${world.id}/import`, payload);
      _entries[world.id] = result.entries;
      renderWorldsSidebar();
      _openDrawer(world.id);
      toast(`Imported ${result.imported} ${result.imported === 1 ? "entry" : "entries"}`);
    } catch (_e) {
      toast("Import failed", true);
    }
  };
  input.click();
}
