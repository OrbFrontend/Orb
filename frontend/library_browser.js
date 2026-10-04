import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { selectChar } from "./chat.js";
import {
  CHAT_ICON,
  DOWNLOAD_ICON,
  GLOBE_ICON,
  GRID_ICON,
  HEART_ICON,
  LIST_ICON,
  STAR_ICON,
  WRENCH_ICON,
} from "./icons.js";
import { showCharEditModal } from "./library.js";
import { matchesFilter, tagsAttrFor, topTags } from "./library_filter.js";
import { renderLibraryManager } from "./library_manager.js";
import { avatarBust, loadCharacters } from "./library_sidebar.js";
import { closeModal, setModalCloseCallback, showModal } from "./modal.js";
import { charactersView, S } from "./state.js";
import { $, avatarCell, avatarUrl, convActivity, esc, escAttr, formatRelativeDate, toast } from "./utils.js";
import { validate } from "./validate.js";

// The view toggle, in order. Manager holds auto-tagging, duplicate finding, and character card generation.
const VIEWS = [
  { mode: "grid", label: "Grid", icon: GRID_ICON },
  { mode: "list", label: "List", icon: LIST_ICON },
  { mode: "internet", label: "Internet", icon: GLOBE_ICON },
  { mode: "manager", label: "Manager", icon: WRENCH_ICON },
];

let _browserViewMode = "grid"; // grid, list, internet, or manager
let _browserSearchQuery = "";
let _browserCharacters = [];
let _browserSortBy = "time-added"; // name, time-added, most-recent-chat, or most-chats
let _browserConversations = [];
let _browserLoading = false; // true while the cache is loading
const _browserSelectedTags = new Set();
let _browserTopTags = []; // the chip row: the library's most-used tags
let _filterApplied = false;

let _openToken = 0;

let _hydration = null;

const BROWSER_CHUNK = 60;

const TOP_TAGS = 100;

const IDLE_RESERVE_MS = 8;

const NO_BUDGET = { timeRemaining: () => 0 };
const onIdle =
  typeof requestIdleCallback === "function"
    ? (fn) => requestIdleCallback(fn, { timeout: 200 })
    : (fn) => setTimeout(() => fn(NO_BUDGET), 0);

const INTERNET_SOURCES = [
  { id: "characterhub", label: "Chub" },
  { id: "chararc", label: "Bernkastel" },
  // `login` names the sign-in field of a site with accounts.
  { id: "botbooru", label: "Botbooru", login: "Username" },
  { id: "wyvern", label: "Wyvern", login: "Email" },
];
const currentSource = () => INTERNET_SOURCES.find((src) => src.id === _internetSource);

let _internetSource = "characterhub";
// Per source: the server's account answer ({ supported, username, expired }), cached for the page's lifetime.
const _sourceAccounts = new Map();
let _internetQuery = "";
let _internetPage = 1;
let _internetResults = [];
let _internetLoading = false;
let _internetHasMore = false;

export async function showCharacterBrowserModal({ view } = {}) {
  const token = ++_openToken;
  _browserCharacters = charactersView();
  _browserConversations = S.conversations || [];
  _browserLoading = _browserCharacters.length === 0;

  computeTopTags();
  _browserSelectedTags.clear();
  _filterApplied = false;
  _browserSortBy = S.characterBrowserSort || "time-added";
  _browserViewMode = view || (_browserViewMode === "internet" ? "internet" : S.characterBrowserView || "grid");
  _browserSearchQuery = "";

  showModal(
    `
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
        <input type="text" id="char-browser-search" placeholder="Search characters by name..." data-wf-action="browser:search" data-wf-on="input">
        <span class="search-icon">🔍</span>
      </div>
      <select id="char-browser-sort" class="char-browser-sort" data-wf-action="browser:sort" data-wf-on="change">
        <option value="name" ${_browserSortBy === "name" ? "selected" : ""}>Name</option>
        <option value="time-added" ${_browserSortBy === "time-added" ? "selected" : ""}>Date Added</option>
        <option value="most-recent-chat" ${_browserSortBy === "most-recent-chat" ? "selected" : ""}>Most Recent Chat</option>
        <option value="most-chats" ${_browserSortBy === "most-chats" ? "selected" : ""}>Most Chats</option>
      </select>
    </div>
    <div class="char-browser-tags-row">
      <div class="char-tags" id="char-browser-tags">${browserTagsHtml()}</div>
    </div>
    <div id="char-browser-content"></div>`,
    { size: "wide" },
  );
  wireBrowserChrome();
  renderCharacterBrowser();

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
        `<button class="char-tag ${_browserSelectedTags.has(tag) ? "active" : ""}" data-tag="${escAttr(tag)}">${esc(tag)}</button>`,
    )
    .join("");
}

function viewButtonHtml({ mode, label, icon }) {
  return `<button class="view-toggle-btn${_browserViewMode === mode ? " active" : ""}" data-view="${mode}">${icon}<span>${label}</span></button>`;
}

/** Delegate on persistent containers so rebuilt chip rows keep their listeners. */
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
  // Search and tags belong to the card list; the other two views own the whole content area.
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
  renderLibraryManager(container, {
    onRunComplete: refreshAfterRun,
    characterCount: _browserCharacters.length,
    onOpenDraft: (card) => {
      setModalCloseCallback(async () => {
        await showCharacterBrowserModal({ view: "manager" });
      });
      return showCharEditModal(card);
    },
  });
}

/** Reload the shared card cache after tagging so the browser and sidebar agree. */
async function refreshAfterRun() {
  try {
    await loadCharacters();
  } catch (e) {
    console.error("Failed to refresh characters after tagging:", e);
    return;
  }
  _browserCharacters = charactersView();
  computeTopTags();
  reconcileSelectedTags();
  const tagsEl = $("char-browser-tags");
  if (tagsEl) tagsEl.innerHTML = browserTagsHtml();
  // No re-render: the Manager tab owns the content area while a run is on, and repainting it here would remount the
  // panel out from under its own callback. Switching back to a card view renders from the refreshed cache.
}

function setCharBrowserView(mode) {
  _browserViewMode = mode;
  // Only the two card views are sticky. Internet and Manager are somewhere you go on purpose, not where you want the
  // library to open next time -- which also settles the long-standing quirk of Internet persisting itself.
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

function onCharBrowserSearch() {
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

function setCharBrowserSort(sortBy) {
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

function computeTopTags() {
  _browserTopTags = topTags(
    _browserCharacters.map((c) => c.tags),
    TOP_TAGS,
  );
}

/** Drop vanished tag selections and adopt current spelling to avoid invisible filters. */
function reconcileSelectedTags() {
  const byKey = new Map(_browserTopTags.map((tag) => [tag.toLowerCase(), tag]));
  const kept = [..._browserSelectedTags].map((tag) => byKey.get(tag.toLowerCase())).filter(Boolean);
  _browserSelectedTags.clear();
  for (const tag of kept) _browserSelectedTags.add(tag);
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
  return `data-char-item data-name="${escAttr((c.name || "").toLowerCase())}" data-tags="${escAttr(tagsAttrFor(c.tags || []))}"`;
}

function renderCharBrowserCard(c) {
  const bust = avatarBust.has(c.id) ? `?v=${avatarBust.get(c.id)}` : "";
  const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "", { attrs: 'loading="lazy"' });
  return `
    <div class="char-browser-card" ${charItemMatchAttrs(c)} data-wf-action="browser:pick" data-char-id="${c.id}">
      <div class="char-browser-avatar">${av}</div>
      <div class="char-browser-card-name">${esc(c.name)}</div>
    </div>`;
}

function renderCharBrowserListItem(c) {
  const bust = avatarBust.has(c.id) ? `?v=${avatarBust.get(c.id)}` : "";
  const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "", { attrs: 'loading="lazy"' });
  const cardTags = c.tags || [];
  const notes = c.creator_notes || (cardTags.length ? cardTags.slice(0, 6).join(", ") : "");
  const tags = notes ? `<div class="char-browser-list-tags">${esc(notes)}</div>` : "";
  return `
    <div class="char-browser-list-item" ${charItemMatchAttrs(c)} data-wf-action="browser:pick" data-char-id="${c.id}">
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
        <select id="internet-source" data-wf-action="browser:source" data-wf-on="change">
          ${INTERNET_SOURCES.map((src) => `<option value="${src.id}" ${_internetSource === src.id ? "selected" : ""}>${src.label}</option>`).join("")}
        </select>
        <input id="internet-search-input" type="text"
               placeholder="Search characters…"
               value="${esc(_internetQuery)}"
               data-wf-action="browser:searchInternetKey" data-wf-on="keydown">
        <button class="btn" data-wf-action="browser:searchInternet">Search</button>
        <button class="btn" data-wf-action="browser:randomize" title="Show a random selection">🎲 Randomize</button>
      </div>
      <div id="internet-account">${renderSourceAccountBody()}</div>
      <div id="internet-results">${renderInternetResultsBody()}</div>
    </div>`;
  if (!_sourceAccounts.has(_internetSource)) loadSourceAccount(_internetSource);
}

/** The sign-in row for a source whose account widens what it lists; empty for the others. */
function renderSourceAccountBody() {
  const account = _sourceAccounts.get(_internetSource);
  if (!account?.supported) return "";
  const { label, login } = currentSource();
  if (account.username) {
    return `
      <div class="internet-account">
        <span>Signed in to ${esc(label)} as <strong>${esc(account.username)}</strong></span>
        <button class="btn btn-sm" data-wf-action="browser:sourceLogout">Sign out</button>
      </div>`;
  }
  const hint = account.expired
    ? `Your ${esc(label)} sign-in has expired. Sign in again to see exclusive cards.`
    : `Sign in to ${esc(label)} to see exclusive cards. Orb keeps the session, not the password.`;
  return `
    <div class="internet-account">
      <span>${hint}</span>
      <div class="internet-account-form">
        <input id="internet-login-user" type="text" placeholder="${login}" autocomplete="username"
               data-wf-action="browser:sourceLoginKey" data-wf-on="keydown">
        <input id="internet-login-pass" type="password" placeholder="Password" autocomplete="current-password"
               data-wf-action="browser:sourceLoginKey" data-wf-on="keydown">
        <button class="btn btn-sm" data-wf-action="browser:sourceLogin">Sign in</button>
      </div>
    </div>`;
}

function refreshSourceAccount() {
  const el = $("internet-account");
  if (el) el.innerHTML = renderSourceAccountBody();
}

async function loadSourceAccount(source) {
  try {
    _sourceAccounts.set(source, await api.get(`/characters/sources/${encodeURIComponent(source)}/account`));
  } catch (e) {
    console.error("Failed to read the card source sign-in:", e);
    return;
  }
  if (source === _internetSource) refreshSourceAccount();
}

/** A sign-in change alters what the source lists, so results already on screen are fetched again. A failure leaves the row
 *  as typed. */
async function changeSourceAccount(request) {
  const button = document.querySelector("#internet-account .btn");
  if (button?.disabled) return;
  if (button) button.disabled = true;
  const source = _internetSource;
  try {
    _sourceAccounts.set(source, await request(source));
  } catch (e) {
    toast(`Sign-in failed: ${e.message}`, true);
    if (button) button.disabled = false;
    return;
  }
  if (source !== _internetSource) return;
  refreshSourceAccount();
  if (_internetResults.length) searchInternet();
}

function loginSource() {
  const username = $("internet-login-user")?.value.trim() || "";
  const password = $("internet-login-pass")?.value || "";
  if (!username || !password) {
    toast(`Enter your ${currentSource().login.toLowerCase()} and password`, true);
    return;
  }
  changeSourceAccount((source) =>
    api.post(`/characters/sources/${encodeURIComponent(source)}/login`, { username, password }),
  );
}

function logoutSource() {
  changeSourceAccount((source) => api.del(`/characters/sources/${encodeURIComponent(source)}/login`));
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
    ? `<button class="btn internet-load-more" data-wf-action="browser:loadMore" ${_internetLoading ? "disabled" : ""}>${_internetLoading ? "Loading…" : "Load More"}</button>`
    : "";
  return `<div class="char-browser-grid">${cards}</div>${more}`;
}

const COMPACT = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const EXACT = new Intl.NumberFormat();

// The tallies a site reports, in tile order. A tile has room for two; the tooltip lists them all.
function internetStats(item) {
  const stats = [];
  if (item.rating != null) {
    const n = item.rating_count;
    stats.push({
      icon: STAR_ICON,
      kind: "rating",
      short: item.rating.toFixed(1),
      full: `Rated ${item.rating.toFixed(1)} by ${EXACT.format(n)} ${n === 1 ? "person" : "people"}`,
    });
  }
  for (const [key, icon, noun] of [
    ["downloads", DOWNLOAD_ICON, "downloads"],
    ["favorites", HEART_ICON, "favorites"],
    ["chats", CHAT_ICON, "chats"],
  ]) {
    const n = item[key];
    if (n != null) stats.push({ icon, kind: key, short: COMPACT.format(n), full: `${EXACT.format(n)} ${noun}` });
  }
  return stats;
}

function renderInternetResultCard(item) {
  const av = avatarCell(item.avatar_url ? escAttr(item.avatar_url) : "", { attrs: 'loading="lazy" decoding="async"' });
  const topics = (item.topics || []).slice(0, 12);
  const updated = item.date_updated ? formatRelativeDate(item.date_updated) : "";
  const stats = internetStats(item);
  const tooltipParts = [
    item.name,
    item.creator ? `by ${item.creator}` : "",
    item.tagline,
    [...stats.map((s) => s.full), item.tokens != null ? `${EXACT.format(item.tokens)} tokens` : ""]
      .filter(Boolean)
      .join(" · "),
    updated ? `Updated: ${updated}` : "",
    topics.length ? `Tags: ${topics.join(", ")}` : "",
  ].filter(Boolean);
  const tooltip = tooltipParts.map(esc).join("\n");
  // A site with no tallies still says how fresh the card is.
  const statRow = stats.length
    ? stats
        .slice(0, 2)
        .map(
          (s) =>
            `<span class="internet-stat internet-stat-${s.kind}" title="${escAttr(s.full)}">${s.icon}${esc(s.short)}</span>`,
        )
        .join("")
    : updated
      ? `<span class="internet-stat">${esc(updated)}</span>`
      : "";
  return `
    <div class="char-browser-card internet-result-card">
      <div class="char-browser-avatar" title="${tooltip}">${av}</div>
      <div class="char-browser-card-name">${esc(item.name || "")}</div>
      <div class="internet-result-meta">
        <div class="internet-result-creator">${item.creator ? `by ${esc(item.creator)}` : ""}</div>
        <div class="internet-result-stats">${statRow}</div>
      </div>
      <button class="internet-import-btn" data-wf-action="browser:importInternet" data-path="${escAttr(item.full_path || "")}">Import</button>
    </div>`;
}

function refreshInternetResults() {
  const el = $("internet-results");
  if (el) el.innerHTML = renderInternetResultsBody();
}

async function searchInternet(nextPage = false) {
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

function loadMoreInternet() {
  if (_internetLoading || !_internetHasMore) return;
  _internetPage += 1;
  searchInternet(true);
}

async function randomizeInternet() {
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

function setInternetSource(val) {
  _internetSource = val;
  _internetQuery = "";
  _internetResults = [];
  _internetPage = 1;
  _internetHasMore = false;
  renderInternetPanel();
}

async function importInternetChar(fullPath) {
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

registerActions("browser", {
  open: () => showCharacterBrowserModal(),
  search: () => onCharBrowserSearch(),
  sort: (el) => setCharBrowserSort(el.value),
  pick: (el) => {
    selectChar(el.dataset.charId, "library");
    closeModal();
  },
  source: (el) => setInternetSource(el.value),
  searchInternet: () => searchInternet(),
  searchInternetKey: (_el, e) => {
    if (e.key === "Enter") searchInternet();
  },
  randomize: () => randomizeInternet(),
  loadMore: () => loadMoreInternet(),
  importInternet: (el) => importInternetChar(el.dataset.path),
  sourceLogin: () => loginSource(),
  sourceLoginKey: (_el, e) => {
    if (e.key === "Enter") loginSource();
  },
  sourceLogout: () => logoutSource(),
});
