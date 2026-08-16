import { api } from "./api.js";
import { createChipInput } from "./chips.js";
import { closeModal, isModalOpen, showConfirmModal, showModal } from "./modal.js";
import { charactersView } from "./state.js";
import { $, boolFlag, downloadBlob, esc, toast } from "./utils.js";
import { changesetRowHtml, isOpen, operationEditHtml, readOperationEdit } from "./world_proposals.js";

// ── Module state
let _worlds = [];
let _worldSearch = "";
let _worldsExpanded = false; // when true, show every world instead of just the recent few
const RECENT_LIMIT = 5; // worlds shown by default before the user needs to search
let _lorebookOpen = false;

document.addEventListener("keydown", (e) => {
  // Yield while a modal is up. modal.js binds Escape on this same node and its
  // handler runs on this same event, so without the check one keypress was
  // handled twice — dismissing a confirm dialog *and* closing the drawer under
  // it, which is never what the second half of that gesture meant.
  if (e.key === "Escape" && _lorebookOpen && !isModalOpen()) closeLorebook();
});
let _focusWorldId = null;
const _entries = {}; // worldId -> entry[]
let _entrySearch = ""; // substring filter over entry names and content
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

// The keyword chip editor. Reads/writes the live `_draft.keywords`, marks the
// entry dirty on any change, and disables (Constant entries take no keywords).
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

// Secondary keys (V3 `selective`): a primary keyword must hit *and* one of these.
// Only rendered while Selective is on.
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
const _isWorldEnabled = (w) => boolFlag(w.enabled);
const _worldRecencyTs = (w) => Date.parse(w.updated_at || w.created_at || "") || 0;
const _byRecency = (a, b) => _worldRecencyTs(b) - _worldRecencyTs(a);

// Decide which worlds to render.
//   • No search: every active world (most recent first) plus the most recent
//     inactive ones, capped so active + recent = RECENT_LIMIT. The active count
//     overrides the cap, so 6 active worlds all show even though that exceeds 5.
//     Once expanded, every world shows (active first, then the rest by recency).
//   • Searching: every name match, with active worlds floated to the top.
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
  // A Dynamic World wears the same halo as a character with an expression pack:
  // both carry a capability of their own, and both say so before being opened.
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

  // The search box only earns its space once the list outgrows the default view.
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

// ── Activate and prioritize a world (called when loading a character with a linked lorebook)
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
  // Move to top of list. Re-found by id rather than reusing the row above:
  // _mergeWorld swaps in a new object, so the old reference is no longer in the
  // array and an identity lookup would splice out the wrong world.
  const [w] = _worlds.splice(
    _worlds.findIndex((x) => x.id === worldId),
    1,
  );
  _worlds.unshift(w);
  renderWorldsSidebar();
}

// A linked lorebook is on loan to the character in play — selectConversation
// turns it on, switching away turns it off. A page load has nobody in play, so
// anything left enabled by the last session would inject that character's lore
// into whatever chat is opened next. Called once at boot, before loadWorlds()
// so the sidebar paints the swept state; floating lorebooks are global and are
// left exactly as the user left them.
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

// ── World CRUD
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
  // Repaint from `_worlds`, not from the click, and on failure too: a rejected
  // write has to put the switch back rather than leave the row claiming a state
  // the server never accepted.
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
        // Backend sets character_cards.world_id to NULL on delete; mirror that locally
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

// ── Lorebook drawer
//
// Every way out of the open editor funnels through _guardDirty — including the
// ones the drawer does not itself cover: Escape, ✕, and clicking a different
// World in the sidebar. Those are the exits a user reaches fastest, so a new one
// that skips the guard drops an unsaved draft without a word.
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

// The unguarded open, for callers that just created the World they are opening:
// there is no draft to lose, and they keep a promise that resolves once loaded.
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

// The unconditional close, for when there is nothing left to save: the World was
// deleted, or it vanished from under the drawer.
function _closeDrawer() {
  _lorebookOpen = false;
  _dirty = false;
  $("lorebook-drawer")?.classList.add("hidden");
  renderWorldsSidebar();
}

function _getWorld(worldId) {
  return _worlds.find((w) => w.id === worldId);
}

// Fold a server response back into the cached row. Merged rather than replaced:
// `/worlds` decorates each row with `pending_changesets`, which the mutation
// routes do not return, so overwriting would blank the sidebar's badge.
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
  // Archived overlay rows are history, not lore: they stay out of the entry list
  // and are reachable through the History tab's undo instead.
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
          // The layer is the one thing a user must never have to guess: an entry
          // the Agent wrote reads differently from one they wrote themselves.
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

  // Bound in JS rather than inline on*= — the frontend layer check ratchets those.
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
  // Editing an Agent-managed entry is allowed, but it should never be mistaken
  // for editing lore the user wrote.
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

export function lbToggleConstant(checked) {
  _draft.constant = checked;
  _markDirty();
  renderLorebookDrawer();
}

// Re-renders because the secondary-keyword chip field only exists while on.
// Bound in renderLorebookDrawer, so it needs no window bridge.
function lbToggleSelective(checked) {
  _draft.selective = checked;
  _markDirty();
  renderLorebookDrawer();
}

// Keyword chips are handled by the shared chips.js widget (`_keywordChips`, above);
// its listeners are (re)attached on each `.render()`.

// ── Entry search
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

// ── Entry selection with dirty guard
export function lbSelectEntry(entryId) {
  if (_selectedEntryId === entryId) return;
  _guardDirty(() => _doSelectEntry(entryId), "Discard changes to this entry and continue?", "Discard & continue");
}

// ── Deselect the current entry, returning to the entry list
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

// ── Toggle entry enabled from list
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

/**
 * Repaint one entry row from what is *stored*, never from what was clicked.
 *
 * A row patch rather than a drawer re-render: the editor next door may hold an
 * unsaved draft with a live caret in it, and flipping a switch in the list is no
 * reason to rebuild that. Both outcomes land here — a failed write puts the
 * switch back rather than let it claim a state the server rejected, and a
 * successful one carries the row's dimming along with it.
 */
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

// ── Discard
export function lbDiscardChanges() {
  const entry = _getEntry(_selectedEntryId);
  if (entry) _draft = _draftFromEntry(entry);
  _dirty = false;
  renderLorebookDrawer();
}

// ── Delete entry
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
        // A Dynamic World records the deletion as history, so this is not just
        // one row leaving the list — the History tab has a new row too.
        await _refreshAfterWorldChange(worldId);
        toast("Entry deleted");
      } catch (_e) {
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

// ── Export the focused lorebook as a standalone JSON file
// Authored by default: a lorebook file is the user's own lore, and an export
// that silently baked in Agent-managed state would round-trip as authored.
function lbExportJson(view = "authored") {
  const world = _getWorld(_focusWorldId);
  if (!world) return;
  const suffix = view === "authored" ? "" : ` (${view})`;
  downloadBlob(`${world.name}${suffix}.json`, `/api/worlds/${world.id}/export?view=${view}`);
}

// ══ Dynamic Worlds ═══════════════════════════════════════════════════════════
//
// Every world mutation lives in this module, so the proposal card under a chat
// reply and the drawer's Pending / History lists share one set of actions and
// one cache-refresh path. The card only *renders* (world_proposals.js); this is
// what happens when it is clicked.

const _changesets = {}; // worldId -> changeset[]
let _drawerTab = "entries"; // entries | pending | history

// The two drawer tabs that list changesets instead of the entry editor, as
// `[statuses, emptyText]`. A stale proposal lists under Pending, not History:
// it is still a decision the user owes, not one they have made. `superseded` is
// history like the rest — a re-evaluation that answered "nothing left to
// propose" resolves the original without applying or rejecting it, and leaving
// that state out of both tabs is the one way a proposal can vanish from the app
// entirely. Mirrors the server's own `?status=history` grouping.
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
  // The cached list is authoritative once loaded (it reflects decisions made
  // this session); the world row's count covers the not-yet-opened drawer.
  return cached ? cached.filter(isOpen).length : world?.pending_changesets || 0;
}

// Set by app.js at boot rather than imported: lorebooks.js sits below the chat
// feature modules, and reaching up into them would invert the layering.
let _chatRefresh = null;

export function setWorldProposalRefresh(fn) {
  _chatRefresh = fn;
}

/**
 * Reload every surface a world mutation can invalidate: the drawer and the chat.
 *
 * The repaint happens whether or not the refetch did, so a failed reload leaves
 * a consistent view of what this module last knew rather than a half-updated
 * one. The chat is refetched through *_chatRefresh* because it paints proposal
 * cards from the message rows — it needs the server's view of them, not ours.
 */
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

/**
 * Fetch a changeset the chat card knows about but this module may not.
 *
 * A miss refetches rather than trusting the cache: the list is loaded when a
 * world's drawer opens, so a world opened before the turn that proposed against
 * it holds a list that is merely *older* than the proposal — and an empty list
 * is truthy, so "already loaded" never means "already complete".
 */
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
  // Two surfaces render this flag — the drawer's switch and the sidebar row's
  // halo — and both repaint from `_worlds` on failure too, so a rejected write
  // puts the switch back instead of leaving either surface claiming it landed.
  renderLorebookDrawer();
  renderWorldsSidebar();
}

/**
 * Run one world mutation: say what happened, then reload every stale surface.
 *
 * Every mutation here has the same shape, failure included. A 409 is the
 * revision conflict, and the server's own sentence is the whole remedy — there
 * is no force-apply to offer — so an error is surfaced verbatim, and the
 * refresh runs either way because a losing action still means this client's
 * view is behind. *said* turns the response into the success toast, and is
 * omitted where landing silently reads better.
 */
async function _worldMutation(worldId, request, said) {
  try {
    const result = await request();
    if (said) toast(said(result));
  } catch (e) {
    toast(e.message, true);
  }
  await _refreshAfterWorldChange(worldId);
}

/** One of the decisions the review card offers on a changeset. */
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

// ── Edit-before-apply
//
// The whole edited batch commits together, so the modal collects every
// operation's final wording first and posts once.
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

// ── One delegated listener for every proposal button, anywhere in the app.
// Delegation rather than inline `on*=` handlers: the frontend layer check
// ratchets those down, and the cards are re-rendered constantly.
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
  // The only action that needs the proposal itself rather than just its id, so
  // it is also the only one that can come up empty. It says so: a silent button
  // is indistinguishable from a broken one.
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

// ── Import lorebook from JSON file (always creates a new world)
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
    // Accept a top-level {entries:...} lorebook object or a bare array
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
