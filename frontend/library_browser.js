import { api } from "./api.js";
import { _avatarBust, showCharEditModal } from "./library.js";
import { setModalCloseCallback, showModal } from "./modal.js";
import { charactersView, S } from "./state.js";
import {
  $,
  avatarCell,
  avatarUrl,
  convActivity,
  esc,
  escAttr,
  escHandlerArg,
  formatRelativeDate,
  toast,
} from "./utils.js";
import { validate } from "./validate.js";

let _browserViewMode = "grid"; // grid, list, or internet
let _browserSearchQuery = "";
let _browserCharacters = [];
let _browserSortBy = "time-added"; // name, time-added, most-recent-chat, or most-chats
let _browserConversations = [];
let _browserLoading = false; // true while the cache is loading
const _browserSelectedTags = new Set();
let _browserTopTags = []; // most-used tags
const _tagBit = new Map(); // tag -> data-tagmask bit
let _filterApplied = false;

let _openToken = 0;

let _hydration = null;

const BROWSER_CHUNK = 60;

const IDLE_RESERVE_MS = 8;

const NO_BUDGET = { timeRemaining: () => 0 };
const onIdle =
  typeof requestIdleCallback === "function"
    ? (fn) => requestIdleCallback(fn, { timeout: 200 })
    : (fn) => setTimeout(() => fn(NO_BUDGET), 0);

let _internetSource = "characterhub";
let _internetQuery = "";
let _internetPage = 1;
let _internetResults = [];
let _internetLoading = false;
let _internetHasMore = false;

export async function showCharacterBrowserModal() {
  const token = ++_openToken;
  _browserCharacters = charactersView();
  _browserConversations = S.conversations || [];
  _browserLoading = _browserCharacters.length === 0;

  computeTopTags();
  _browserSelectedTags.clear();
  _filterApplied = false;
  _browserSortBy = S.characterBrowserSort || "time-added";
  _browserViewMode = _browserViewMode === "internet" ? "internet" : S.characterBrowserView || "grid";
  _browserSearchQuery = "";

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Character Library</h2>
        <div id="char-browser-count" style="font-size:11px;color:var(--text-muted)">${browserCountLabel()}</div>
      </div>
      <div class="modal-title-actions">
        <div class="view-toggle" id="char-browser-view-toggle">
          <button class="view-toggle-btn${_browserViewMode === "grid" ? " active" : ""}" data-view="grid" onclick="setCharBrowserView('grid')">⊞ Grid</button>
          <button class="view-toggle-btn${_browserViewMode === "list" ? " active" : ""}" data-view="list" onclick="setCharBrowserView('list')">☰ List</button>
          <button class="view-toggle-btn${_browserViewMode === "internet" ? " active" : ""}" data-view="internet" onclick="setCharBrowserView('internet')">🌐 Internet</button>
        </div>
      </div>
    </div>
    <div class="char-browser-search-row">
      <div class="char-browser-search">
        <input type="text" id="char-browser-search" placeholder="Search characters by name..." oninput="onCharBrowserSearch()">
        <span class="search-icon">🔍</span>
      </div>
      <select id="char-browser-sort" class="char-browser-sort" onchange="setCharBrowserSort(this.value)">
        <option value="name" ${_browserSortBy === "name" ? "selected" : ""}>Name</option>
        <option value="time-added" ${_browserSortBy === "time-added" ? "selected" : ""}>Date Added</option>
        <option value="most-recent-chat" ${_browserSortBy === "most-recent-chat" ? "selected" : ""}>Most Recent Chat</option>
        <option value="most-chats" ${_browserSortBy === "most-chats" ? "selected" : ""}>Most Chats</option>
      </select>
    </div>
    <div class="char-browser-tags-row">
      <div class="char-tags" id="char-browser-tags">${browserTagsHtml()}</div>
    </div>
    <div id="char-browser-content"></div>`);
  renderCharacterBrowser();

  if (!_browserLoading) return;
  let characters = [];
  let conversations = [];
  try {
    [characters, conversations] = await Promise.all([api.get("/characters"), api.get("/conversations")]);
  } catch (e) {
    console.error("Failed to load characters for browser:", e);
  }
  if (token !== _openToken) return;
  _browserLoading = false;
  _browserCharacters = characters;
  _browserConversations = conversations;
  const countEl = $("char-browser-count");
  if (!countEl) return;
  countEl.textContent = browserCountLabel();
  computeTopTags();
  const tagsEl = $("char-browser-tags");
  if (tagsEl) tagsEl.innerHTML = browserTagsHtml();
  renderCharacterBrowser();
}

function browserCountLabel() {
  if (_browserLoading) return "Loading…";
  const n = _browserCharacters.length;
  return `${n} character${n !== 1 ? "s" : ""}`;
}

function browserTagsHtml() {
  return _browserTopTags
    .map(
      (tag) =>
        `<button class="char-tag ${_browserSelectedTags.has(tag) ? "active" : ""}" data-tag="${escAttr(tag)}" onclick="toggleTagSelection('${escHandlerArg(tag)}')">${esc(tag)}</button>`,
    )
    .join("");
}

function renderCharacterBrowser() {
  const isInternet = _browserViewMode === "internet";
  const searchRow = document.querySelector(".char-browser-search-row");
  const tagsRow = document.querySelector(".char-browser-tags-row");
  if (searchRow) searchRow.style.display = isInternet ? "none" : "";
  if (tagsRow) tagsRow.style.display = isInternet ? "none" : "";
  if (isInternet) renderInternetPanel();
  else renderCharBrowserItems();
}

export function setCharBrowserView(mode) {
  _browserViewMode = mode;
  S.characterBrowserView = mode;
  api.put("/settings", { character_library_view: mode }).catch((e) => console.error("Failed to save view mode", e));
  document.querySelectorAll("#char-browser-view-toggle .view-toggle-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === mode);
  });

  const container = $("char-browser-content");
  if (container) container.style.minHeight = "";
  renderCharacterBrowser();
}

export function onCharBrowserSearch() {
  const input = $("char-browser-search");
  const query = input.value.trim().toLowerCase();
  const validation = validate.validateBrowseSearch(query);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  _browserSearchQuery = query;
  applyBrowserFilter();
}

export function setCharBrowserSort(sortBy) {
  _browserSortBy = sortBy;
  S.characterBrowserSort = sortBy;
  api.put("/settings", { character_library_sort: sortBy }).catch((e) => console.error("Failed to save sort mode", e));
  const select = document.getElementById("char-browser-sort");
  if (select) select.value = sortBy;
  renderCharBrowserItems();
}

export function toggleTagSelection(tag) {
  if (_browserSelectedTags.has(tag)) {
    _browserSelectedTags.delete(tag);
  } else {
    _browserSelectedTags.add(tag);
  }
  const button = [...document.querySelectorAll("#char-browser-tags .char-tag")].find((b) => b.dataset.tag === tag);
  if (button) {
    button.classList.toggle("active", _browserSelectedTags.has(tag));
  }
  applyBrowserFilter();
}

function computeTopTags() {
  const counts = new Map();
  for (const c of _browserCharacters) {
    const tags = c.tags || [];
    for (const tag of tags) {
      counts.set(tag, (counts.get(tag) || 0) + 1);
    }
  }
  const sorted = Array.from(counts.entries()).sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    return a[0].localeCompare(b[0]);
  });
  _browserTopTags = sorted.slice(0, 15).map((entry) => entry[0]);
  _tagBit.clear();
  _browserTopTags.forEach((tag, i) => {
    _tagBit.set(tag, 1 << i);
  });
}

function tagMaskFor(tags) {
  let mask = 0;
  for (const tag of tags) {
    const bit = _tagBit.get(tag);
    if (bit !== undefined) mask |= bit;
  }
  return mask;
}

function computeConversationStats() {
  const map = new Map();
  for (const conv of _browserConversations) {
    const cardIds = conv.character_card_id ? [conv.character_card_id] : conv.group_card_ids || [];
    for (const cardId of cardIds) {
      const entry = map.get(cardId) || { count: 0, recentTimestamp: "" };
      entry.count += 1;
      const ts = convActivity(conv);
      if (ts && (!entry.recentTimestamp || ts > entry.recentTimestamp)) entry.recentTimestamp = ts;
      map.set(cardId, entry);
    }
  }
  return map;
}

function applySort(characters) {
  const sortBy = _browserSortBy;
  const stats = sortBy === "most-recent-chat" || sortBy === "most-chats" ? computeConversationStats() : new Map();
  const collator = new Intl.Collator(undefined, { sensitivity: "base" });
  return [...characters].sort((a, b) => {
    switch (sortBy) {
      case "name":
        return collator.compare(a.name, b.name);
      case "time-added": {
        const aTime = a.created_at || "";
        const bTime = b.created_at || "";
        return bTime.localeCompare(aTime);
      }
      case "most-recent-chat": {
        const aStat = stats.get(a.id);
        const bStat = stats.get(b.id);
        const aTs = aStat?.recentTimestamp || a.updated_at || a.created_at || "";
        const bTs = bStat?.recentTimestamp || b.updated_at || b.created_at || "";
        return bTs.localeCompare(aTs);
      }
      case "most-chats": {
        const aCount = stats.get(a.id)?.count || 0;
        const bCount = stats.get(b.id)?.count || 0;
        return bCount - aCount;
      }
      default:
        return 0;
    }
  });
}

function applyBrowserFilter() {
  const query = _browserSearchQuery;
  const selectedMask = tagMaskFor(_browserSelectedTags);
  const filterOn = !!query || selectedMask !== 0;
  if (filterOn) flushHydration();
  if (!filterOn && !_filterApplied) return;

  const container = $("char-browser-content");
  if (!container) return;
  const items = container.querySelectorAll("[data-char-item]");
  if (!items.length) return;

  let visible = 0;
  for (const el of items) {
    const show =
      (!query || (el.dataset.name || "").includes(query)) &&
      (selectedMask === 0 || (Number(el.dataset.tagmask) & selectedMask) === selectedMask);
    const next = show ? "" : "none";
    if (el.style.display !== next) el.style.display = next;
    if (show) visible++;
  }

  const emptyEl = container.querySelector("[data-browser-empty]");
  if (emptyEl) emptyEl.style.display = visible === 0 ? "" : "none";
  _filterApplied = filterOn;
}

function renderCharBrowserItems() {
  const container = $("char-browser-content");
  if (!container) return;
  _hydration = null;

  const sorted = applySort(_browserCharacters);

  if (sorted.length === 0) {
    container.style.minHeight = "";
    container.innerHTML = `<div class="char-browser-empty">${_browserLoading ? "Loading…" : "No characters available"}</div>`;
    return;
  }

  const wrapClass = _browserViewMode === "grid" ? "char-browser-grid" : "char-browser-list";
  const renderItem = _browserViewMode === "grid" ? renderCharBrowserCard : renderCharBrowserListItem;
  const head = sorted.slice(0, BROWSER_CHUNK);
  container.innerHTML =
    `<div class="${wrapClass}">${head.map(renderItem).join("")}</div>` +
    `<div class="char-browser-empty" data-browser-empty style="display:none">No characters match your filters</div>`;

  container.style.minHeight = `${Math.min(container.offsetHeight, Math.round(window.innerHeight * 0.85))}px`;

  if (sorted.length > head.length) hydrateRest(container.firstElementChild, sorted, renderItem, head.length);

  _filterApplied = false;
  applyBrowserFilter();
}

function hydrateRest(wrap, sorted, renderItem, start) {
  _hydration = { wrap, sorted, renderItem, next: start };
  const step = () => {
    onIdle((deadline) => {
      const run = _hydration;
      if (!run || run.wrap !== wrap) return;
      if (!wrap.isConnected) {
        _hydration = null;
        return;
      }
      do {
        appendChunk(run, Math.min(run.next + BROWSER_CHUNK, run.sorted.length));
      } while (run.next < run.sorted.length && deadline.timeRemaining() > IDLE_RESERVE_MS);
      if (_filterApplied) applyBrowserFilter();
      if (run.next < run.sorted.length) step();
      else _hydration = null;
    });
  };
  step();
}

function appendChunk(run, end) {
  run.wrap.insertAdjacentHTML(
    "beforeend",
    run.sorted
      .slice(run.next, end)
      .map((c) => run.renderItem(c))
      .join(""),
  );
  run.next = end;
}

function flushHydration() {
  const run = _hydration;
  _hydration = null;
  if (!run?.wrap.isConnected || run.next >= run.sorted.length) return;
  appendChunk(run, run.sorted.length);
}

function charItemMatchAttrs(c) {
  return `data-char-item data-name="${escAttr((c.name || "").toLowerCase())}" data-tagmask="${tagMaskFor(c.tags || [])}"`;
}

function renderCharBrowserCard(c) {
  const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
  const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "", { attrs: 'loading="lazy"' });
  return `
    <div class="char-browser-card" ${charItemMatchAttrs(c)} onclick="selectChar('${c.id}', 'library');closeModal()">
      <div class="char-browser-avatar">${av}</div>
      <div class="char-browser-card-name">${esc(c.name)}</div>
    </div>`;
}

function renderCharBrowserListItem(c) {
  const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
  const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "", { attrs: 'loading="lazy"' });
  const notes = c.creator_notes || (c.tags?.length ? c.tags.slice(0, 6).join(", ") : "");
  const tags = notes ? `<div class="char-browser-list-tags">${esc(notes)}</div>` : "";
  return `
    <div class="char-browser-list-item" ${charItemMatchAttrs(c)} onclick="selectChar('${c.id}', 'library');closeModal()">
      <div class="char-browser-list-avatar">${av}</div>
      <div class="char-browser-list-info">
        <div class="char-browser-list-name">${esc(c.name)}</div>
        ${tags}
      </div>
    </div>`;
}

function renderInternetPanel() {
  const container = $("char-browser-content");
  if (!container) return;
  container.innerHTML = `
    <div class="char-browser-internet">
      <div class="internet-controls">
        <select id="internet-source" onchange="setInternetSource(this.value)">
          <option value="characterhub" ${_internetSource === "characterhub" ? "selected" : ""}>CharacterHub</option>
          <option value="chararc" ${_internetSource === "chararc" ? "selected" : ""}>Character Archive</option>
          <option value="botbooru" ${_internetSource === "botbooru" ? "selected" : ""}>Botbooru</option>
          <option value="wyvern" ${_internetSource === "wyvern" ? "selected" : ""}>Wyvern</option>
        </select>
        <input id="internet-search-input" type="text"
               placeholder="Search characters…"
               value="${esc(_internetQuery)}"
               onkeydown="if(event.key==='Enter')searchInternet()">
        <button class="btn" onclick="searchInternet()">Search</button>
        <button class="btn" onclick="randomizeInternet()" title="Show a random selection">🎲 Randomize</button>
      </div>
      <div id="internet-results">${renderInternetResultsBody()}</div>
    </div>`;
}

function renderInternetResultsBody() {
  if (_internetLoading && _internetResults.length === 0) {
    return `<div class="internet-loading">Loading…</div>`;
  }
  if (!_internetLoading && _internetResults.length === 0) {
    return `<div class="char-browser-empty">${_internetQuery ? "No results" : "Type a query and press Enter to search."}</div>`;
  }
  const cards = _internetResults.map((it) => renderInternetResultCard(it)).join("");
  const more = _internetHasMore
    ? `<button class="btn internet-load-more" onclick="loadMoreInternet()" ${_internetLoading ? "disabled" : ""}>${_internetLoading ? "Loading…" : "Load More"}</button>`
    : "";
  return `<div class="char-browser-grid">${cards}</div>${more}`;
}

function renderInternetResultCard(item) {
  const av = avatarCell(item.avatar_url ? escAttr(item.avatar_url) : "", { attrs: 'loading="lazy" decoding="async"' });
  const fullPath = escHandlerArg(item.full_path || "");
  const topics = (item.topics || []).slice(0, 12);
  const updated = item.date_updated ? `Updated: ${formatRelativeDate(item.date_updated)}` : "";
  const tooltipParts = [item.name, item.tagline, updated, topics.length ? `Tags: ${topics.join(", ")}` : ""].filter(
    Boolean,
  );
  const tooltip = tooltipParts.map(esc).join("\n");
  return `
    <div class="char-browser-card internet-result-card">
      <div class="char-browser-avatar" title="${tooltip}">${av}</div>
      <div class="char-browser-card-name">${esc(item.name || "")}</div>
      <div class="internet-result-meta">${esc(item.tagline || "")}</div>
      <button class="internet-import-btn" onclick="importInternetChar('${fullPath}')">Import</button>
    </div>`;
}

function refreshInternetResults() {
  const el = $("internet-results");
  if (el) el.innerHTML = renderInternetResultsBody();
}

export async function searchInternet(nextPage = false) {
  if (_internetLoading) return;
  const input = $("internet-search-input");
  if (input) _internetQuery = input.value.trim();

  if (!nextPage) {
    _internetPage = 1;
    _internetResults = [];
    _internetHasMore = false;
  }

  _internetLoading = true;
  refreshInternetResults();

  try {
    const data = await api.get(
      `/characters/browse?source=${encodeURIComponent(_internetSource)}&q=${encodeURIComponent(_internetQuery)}&page=${_internetPage}`,
    );
    const results = Array.isArray(data?.results) ? data.results : [];
    if (!nextPage) _internetResults = results;
    else _internetResults = [..._internetResults, ...results];
    _internetHasMore = !!data?.has_more;
  } catch (e) {
    toast(`Search failed: ${e.message}`, true);
  } finally {
    _internetLoading = false;
    refreshInternetResults();
  }
}

export function loadMoreInternet() {
  if (_internetLoading || !_internetHasMore) return;
  _internetPage += 1;
  searchInternet(true);
}

export async function randomizeInternet() {
  if (_internetLoading) return;
  const input = $("internet-search-input");
  if (input) _internetQuery = input.value.trim();

  _internetPage = 1;
  _internetResults = [];
  _internetHasMore = false;
  _internetLoading = true;
  refreshInternetResults();

  try {
    const data = await api.get(
      `/characters/randomize?source=${encodeURIComponent(_internetSource)}&q=${encodeURIComponent(_internetQuery)}`,
    );
    _internetResults = Array.isArray(data?.results) ? data.results : [];
    _internetHasMore = !!data?.has_more;
  } catch (e) {
    toast(`Randomize failed: ${e.message}`, true);
  } finally {
    _internetLoading = false;
    refreshInternetResults();
  }
}

export function setInternetSource(val) {
  _internetSource = val;
  _internetQuery = "";
  _internetResults = [];
  _internetPage = 1;
  _internetHasMore = false;
  renderInternetPanel();
}

export async function importInternetChar(fullPath) {
  try {
    toast("Fetching card…");
    const r = await api.post("/characters/import-url", { source: _internetSource, full_path: fullPath });
    setModalCloseCallback(async () => {
      _browserViewMode = "internet";
      await showCharacterBrowserModal();
    });
    showCharEditModal(r);
  } catch (e) {
    toast(`Import failed: ${e.message}`, true);
  }
}
