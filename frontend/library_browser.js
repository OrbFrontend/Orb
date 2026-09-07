import { api } from "./api.js";
import { _avatarBust, showCharEditModal } from "./library.js";
import { matchesFilter, tagsAttrFor } from "./library_filter.js";
import { renderLibraryManager } from "./library_manager.js";
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

// The view toggle, in order. Manager is the home for library-wide maintenance
// tools; today it holds the auto-tagger, and further tools land beside it.
const VIEWS = [
  { mode: "grid", label: "⊞ Grid" },
  { mode: "list", label: "☰ List" },
  { mode: "internet", label: "🌐 Internet" },
  { mode: "manager", label: "🛠 Manager" },
];

let _browserViewMode = "grid"; // grid, list, internet, or manager
let _browserSearchQuery = "";
let _browserCharacters = [];
let _browserSortBy = "time-added"; // name, time-added, most-recent-chat, or most-chats
let _browserConversations = [];
let _browserLoading = false; // true while the cache is loading
const _browserSelectedTags = new Set();
let _browserTopTags = []; // the chip row: the curated vocabulary, or derived
let _browserVocabulary = []; // the user's curated tags, empty until they add some
let _browserAssignments = {}; // card id -> auto-assigned tags
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
          ${VIEWS.map((v) => viewButtonHtml(v)).join("")}
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
  wireBrowserChrome();
  renderCharacterBrowser();

  // The vocabulary is fetched on every open, even when the card cache is warm:
  // a run may have finished since, and it is one small request.
  const tagState = api.get("/library/tags").catch((e) => {
    console.error("Failed to load library tags:", e);
    return null;
  });

  let characters = _browserCharacters;
  let conversations = _browserConversations;
  if (_browserLoading) {
    try {
      [characters, conversations] = await Promise.all([api.get("/characters"), api.get("/conversations")]);
    } catch (e) {
      console.error("Failed to load characters for browser:", e);
      characters = [];
      conversations = [];
    }
  }
  applyTagState(await tagState);
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

/** Adopt a ``GET /library/tags`` payload. Tolerates a failed fetch. */
function applyTagState(state) {
  if (!state) return;
  _browserVocabulary = Array.isArray(state.vocabulary) ? state.vocabulary : [];
  _browserAssignments = state.assignments && typeof state.assignments === "object" ? state.assignments : {};
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
        `<button class="char-tag ${_browserSelectedTags.has(tag) ? "active" : ""}" data-tag="${escAttr(tag)}">${esc(tag)}</button>`,
    )
    .join("");
}

function viewButtonHtml({ mode, label }) {
  return `<button class="view-toggle-btn${_browserViewMode === mode ? " active" : ""}" data-view="${mode}">${label}</button>`;
}

/** Delegated listeners for the two controls the modal rebuilds on every open.
 *
 * Both containers outlive their contents (the chip row's innerHTML is replaced
 * once the card cache lands), so one listener each is enough — and it keeps the
 * handlers off the ``window`` bridge, which is the direction the inline-handler
 * ratchet only moves in.
 */
function wireBrowserChrome() {
  $("char-browser-view-toggle")?.addEventListener("click", (e) => {
    const mode = e.target.closest("[data-view]")?.dataset.view;
    if (mode) setCharBrowserView(mode);
  });
  $("char-browser-tags")?.addEventListener("click", (e) => {
    const tag = e.target.closest("[data-tag]")?.dataset.tag;
    if (tag !== undefined) toggleTagSelection(tag);
  });
}

function renderCharacterBrowser() {
  // Search and tags belong to the card list; the other two views own the whole
  // content area.
  const isCardList = _browserViewMode === "grid" || _browserViewMode === "list";
  const searchRow = document.querySelector(".char-browser-search-row");
  const tagsRow = document.querySelector(".char-browser-tags-row");
  if (searchRow) searchRow.style.display = isCardList ? "" : "none";
  if (tagsRow) tagsRow.style.display = isCardList ? "" : "none";
  if (_browserViewMode === "internet") renderInternetPanel();
  else if (_browserViewMode === "manager") renderManagerPanel();
  else renderCharBrowserItems();
}

function renderManagerPanel() {
  const container = $("char-browser-content");
  if (!container) return;
  _hydration = null;
  renderLibraryManager(container, { onVocabularyChange: applyTagState, onRunComplete: refreshAfterRun });
}

/** Re-read the tag state after a run and repaint everything that shows tags. */
async function refreshAfterRun() {
  try {
    applyTagState(await api.get("/library/tags"));
  } catch (e) {
    console.error("Failed to refresh library tags:", e);
    return;
  }
  computeTopTags();
  const tagsEl = $("char-browser-tags");
  if (tagsEl) tagsEl.innerHTML = browserTagsHtml();
}

function setCharBrowserView(mode) {
  _browserViewMode = mode;
  // Only the two card views are sticky. Internet and Manager are somewhere you
  // go on purpose, not where you want the library to open next time — which
  // also settles the long-standing quirk of Internet persisting itself.
  if (mode === "grid" || mode === "list") {
    S.characterBrowserView = mode;
    api.put("/settings", { character_library_view: mode }).catch((e) => console.error("Failed to save view mode", e));
  }
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

function toggleTagSelection(tag) {
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

/** The chip row: the curated vocabulary when there is one, else the old
 * derivation from whatever the imported cards happened to carry.
 *
 * The fallback is what keeps the library unchanged until the user opts in.
 * The derived row is a frequency count over importer noise — that is the
 * problem the Manager tab exists to fix, not a behaviour to take away from
 * someone who has not used it yet.
 */
function computeTopTags() {
  if (_browserVocabulary.length) {
    _browserTopTags = [..._browserVocabulary];
    return;
  }
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
}

/** A card's own tags plus whatever the tagger assigned it.
 *
 * Merged here rather than joined in ``list_character_cards``: that query also
 * feeds the sidebar, group setup and personas, and a LEFT JOIN there would make
 * removing this feature a surgery on the shared card query and its row model.
 */
function mergedTagsFor(c) {
  const auto = _browserAssignments[c.id];
  return auto?.length ? [...(c.tags || []), ...auto] : c.tags || [];
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
  const selected = [..._browserSelectedTags];
  const filterOn = !!query || selected.length > 0;
  if (filterOn) flushHydration();
  if (!filterOn && !_filterApplied) return;

  const container = $("char-browser-content");
  if (!container) return;
  const items = container.querySelectorAll("[data-char-item]");
  if (!items.length) return;

  let visible = 0;
  for (const el of items) {
    const show = matchesFilter(el.dataset.name, el.dataset.tags, query, selected);
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
  return `data-char-item data-name="${escAttr((c.name || "").toLowerCase())}" data-tags="${escAttr(tagsAttrFor(mergedTagsFor(c)))}"`;
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
  const merged = mergedTagsFor(c);
  const notes = c.creator_notes || (merged.length ? merged.slice(0, 6).join(", ") : "");
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
