// Character Library browser modal: grid/list views with search + tag filtering
// + sorting, plus the Internet browse panel (CharacterHub / Character Archive
// search, randomize, import). Split out of library.js; the public surface is
// re-exported from library.js. Reads the shared avatar cache-bust map and the
// character-edit modal from library.js.
import { api } from "./api.js";
import { _avatarBust, showCharEditModal } from "./library.js";
import { closeModal, setModalCloseCallback, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, avatarUrl, esc, escAttr, escHandlerArg, formatRelativeDate, toast } from "./utils.js";
import { validate } from "./validate.js";

// Character browser modal state
let _browserViewMode = "grid"; // 'grid', 'list', or 'internet'
let _browserSearchQuery = "";
let _browserCharacters = [];
let _browserSortBy = "time-added"; // 'name', 'time-added', 'most-recent-chat', 'most-chats'
let _browserConversations = [];
const _browserSelectedTags = new Set();
let _browserTopTags = []; // top 15 most popular tags

// Internet character browse state
let _internetSource = "characterhub";
let _internetQuery = "";
let _internetPage = 1;
let _internetResults = [];
let _internetLoading = false;
let _internetHasMore = false;

// ── Character Browser Modal

export async function showCharacterBrowserModal() {
  try {
    _browserCharacters = await api.get("/characters");
  } catch (e) {
    _browserCharacters = S.characters || [];
    console.error("Failed to load characters for browser:", e);
  }
  // Load conversations for sorting
  try {
    _browserConversations = await api.get("/conversations");
  } catch (e) {
    _browserConversations = [];
    console.error("Failed to load conversations for browser:", e);
  }
  computeTopTags();
  _browserSelectedTags.clear();
  _browserSortBy = S.characterBrowserSort || "time-added";
  _browserViewMode = _browserViewMode === "internet" ? "internet" : S.characterBrowserView || "grid";
  _browserSearchQuery = "";
  renderCharacterBrowser();
  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Character Library</h2>
        <div style="font-size:11px;color:var(--text-muted)">${_browserCharacters.length} character${_browserCharacters.length !== 1 ? "s" : ""}</div>
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
      <div class="char-tags">
        ${_browserTopTags.map((tag) => `<button class="char-tag ${_browserSelectedTags.has(tag) ? "active" : ""}" data-tag="${escAttr(tag)}" onclick="toggleTagSelection('${escHandlerArg(tag)}')">${esc(tag)}</button>`).join("")}
      </div>
    </div>
    <div id="char-browser-content"></div>`);
}

export function setCharBrowserView(mode) {
  _browserViewMode = mode;
  S.characterBrowserView = mode;
  api.put("/settings", { character_library_view: mode }).catch((e) => console.error("Failed to save view mode", e));
  document.querySelectorAll("#char-browser-view-toggle .view-toggle-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === mode);
  });

  const isInternet = mode === "internet";
  const searchRow = document.querySelector(".char-browser-search-row");
  const tagsRow = document.querySelector(".char-browser-tags-row");
  if (searchRow) searchRow.style.display = isInternet ? "none" : "";
  if (tagsRow) tagsRow.style.display = isInternet ? "none" : "";

  const container = $("char-browser-content");
  if (container) container.style.minHeight = "";

  if (isInternet) {
    renderInternetPanel();
    return;
  }

  // renderCharBrowserItems renders the full set, captures the natural height for
  // minHeight, then applies the active filter in place.
  renderCharBrowserItems();
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
  // Filter in place — never rebuild the grid on a keystroke. Rebuilding tore
  // down and recreated every avatar <img>, which flickered; toggling visibility
  // on the existing nodes is flicker-free and instant.
  applyBrowserFilter();
}

export function setCharBrowserSort(sortBy) {
  _browserSortBy = sortBy;
  S.characterBrowserSort = sortBy;
  api.put("/settings", { character_library_sort: sortBy }).catch((e) => console.error("Failed to save sort mode", e));
  // Update dropdown UI
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
  // Update button visual via data-tag attribute
  const button = document.querySelector(`.char-tag[data-tag="${tag}"]`);
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
  // sort by count descending, then alphabetically
  const sorted = Array.from(counts.entries()).sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    return a[0].localeCompare(b[0]);
  });
  _browserTopTags = sorted.slice(0, 15).map((entry) => entry[0]);
}

function computeConversationStats() {
  const map = new Map();
  for (const conv of _browserConversations) {
    const cardId = conv.character_card_id;
    if (!cardId) continue;
    const entry = map.get(cardId) || { count: 0, recentTimestamp: "" };
    entry.count += 1;
    const ts = conv.updated_at || conv.created_at;
    if (ts && (!entry.recentTimestamp || ts > entry.recentTimestamp)) {
      entry.recentTimestamp = ts;
    }
    map.set(cardId, entry);
  }
  return map;
}

function applySort(characters) {
  const stats = computeConversationStats();
  const sortBy = _browserSortBy;
  const collator = new Intl.Collator(undefined, { sensitivity: "base" });
  return [...characters].sort((a, b) => {
    switch (sortBy) {
      case "name":
        return collator.compare(a.name, b.name);
      case "time-added": {
        // Use created_at descending (newest first)
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

// Show/hide already-rendered cards to match the active search + tag filter,
// without rebuilding the DOM. The match data rides on each card as data-name
// (lowercased) and data-tags (JSON), so filtering is a cheap visibility toggle
// — no avatar <img> is destroyed/recreated, so there is no flicker.
function applyBrowserFilter() {
  const container = $("char-browser-content");
  if (!container) return;
  const items = container.querySelectorAll("[data-char-item]");
  if (!items.length) return;

  const query = _browserSearchQuery;
  const hasTagFilter = _browserSelectedTags.size > 0;
  let visible = 0;

  for (const el of items) {
    let show = true;
    if (query && !(el.dataset.name || "").includes(query)) {
      show = false;
    }
    if (show && hasTagFilter) {
      let tags;
      try {
        tags = JSON.parse(el.dataset.tags || "[]");
      } catch {
        tags = [];
      }
      for (const tag of _browserSelectedTags) {
        if (!tags.includes(tag)) {
          show = false;
          break;
        }
      }
    }
    // Inline display overrides the card's stylesheet display rule (which would
    // win over the [hidden] UA rule); "" restores the stylesheet value.
    el.style.display = show ? "" : "none";
    if (show) visible++;
  }

  const emptyEl = container.querySelector("[data-browser-empty]");
  if (emptyEl) emptyEl.style.display = visible === 0 ? "" : "none";
}

function renderCharBrowserItems() {
  const container = $("char-browser-content");
  if (!container) return;

  // Render the full character set once (sorted). Search/tag filtering is then
  // applied in place by applyBrowserFilter, so typing never rebuilds the grid.
  const sorted = applySort(_browserCharacters);

  if (sorted.length === 0) {
    container.style.minHeight = "";
    container.innerHTML = `<div class="char-browser-empty">No characters available</div>`;
    return;
  }

  const wrapClass = _browserViewMode === "grid" ? "char-browser-grid" : "char-browser-list";
  const renderItem = _browserViewMode === "grid" ? renderCharBrowserCard : renderCharBrowserListItem;
  container.innerHTML =
    `<div class="${wrapClass}">${sorted.map(renderItem).join("")}</div>` +
    `<div class="char-browser-empty" data-browser-empty style="display:none">No characters match your filters</div>`;

  // Capture the natural full-set height so the panel doesn't collapse/jump when
  // a filter hides most cards.
  container.style.minHeight = container.offsetHeight + "px";

  applyBrowserFilter();
}

// Match-data attributes shared by the grid card and the list item, read by
// applyBrowserFilter. data-name is lowercased for case-insensitive search.
function charItemMatchAttrs(c) {
  return `data-char-item data-name="${escAttr((c.name || "").toLowerCase())}" data-tags="${escAttr(JSON.stringify(c.tags || []))}"`;
}

function renderCharBrowserCard(c) {
  const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
  const av = c.has_avatar
    ? `<img src="${avatarUrl(c.id)}${bust}" loading="lazy" onerror="this.parentElement.textContent='👤'">`
    : "👤";
  return `
    <div class="char-browser-card" ${charItemMatchAttrs(c)} onclick="selectChar('${c.id}', 'library');closeModal()">
      <div class="char-browser-avatar">${av}</div>
      <div class="char-browser-card-name">${esc(c.name)}</div>
    </div>`;
}

function renderCharBrowserListItem(c) {
  const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
  const av = c.has_avatar
    ? `<img src="${avatarUrl(c.id)}${bust}" loading="lazy" onerror="this.parentElement.textContent='👤'">`
    : "👤";
  const notes = c.creator_notes || (c.tags && c.tags.length ? c.tags.slice(0, 6).join(", ") : "");
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

// ── Internet character browse

function renderInternetPanel() {
  const container = $("char-browser-content");
  if (!container) return;
  container.innerHTML = `
    <div class="char-browser-internet">
      <div class="internet-controls">
        <select id="internet-source" onchange="setInternetSource(this.value)">
          <option value="characterhub" ${_internetSource === "characterhub" ? "selected" : ""}>CharacterHub</option>
          <option value="chararc" ${_internetSource === "chararc" ? "selected" : ""}>Character Archive</option>
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
  const av = item.avatar_url
    ? `<img src="${escAttr(item.avatar_url)}" loading="lazy" decoding="async" onerror="this.parentElement.textContent='👤'">`
    : "👤";
  const fullPath = escHandlerArg(item.full_path || "");
  const topics = (item.topics || []).slice(0, 12);
  const updated = item.date_updated ? "Updated: " + formatRelativeDate(item.date_updated) : "";
  const tooltipParts = [item.name, item.tagline, updated, topics.length ? "Tags: " + topics.join(", ") : ""].filter(
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
    toast("Search failed: " + e.message, true);
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
    toast("Randomize failed: " + e.message, true);
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
    toast("Import failed: " + e.message, true);
  }
}

function renderCharacterBrowser() {
  setTimeout(() => {
    if (_browserViewMode === "internet") {
      const searchRow = document.querySelector(".char-browser-search-row");
      const tagsRow = document.querySelector(".char-browser-tags-row");
      if (searchRow) searchRow.style.display = "none";
      if (tagsRow) tagsRow.style.display = "none";
      renderInternetPanel();
      return;
    }
    renderCharBrowserItems();
  }, 0);
}
