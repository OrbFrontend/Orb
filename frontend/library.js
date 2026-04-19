import { S } from './state.js';
import { $, esc, toast, avatarUrl } from './utils.js';
import { api } from './api.js';
import { showModal, closeModal, switchTab, showConfirmModal, showCropModal } from './modal.js';
import { resetChatUI, loadConversations } from './chat.js';

// Pending avatar for the character create modal (cleared on submit or cancel)
let _pendingAvatar = null;
// Stable ID and source format carried over from an imported card (cleared on submit)
let _pendingImportId = null;
let _pendingImportSourceFormat = null;
// Per-card cache-bust timestamps so the browser re-fetches updated avatars
const _avatarBust = new Map();

// Character browser modal state
let _browserViewMode = 'grid'; // 'grid' or 'list'
let _browserSearchQuery = '';
let _browserCharacters = [];
let _browserSortBy = 'time-added'; // 'name', 'time-added', 'most-recent-chat', 'most-chats'
let _browserConversations = [];

// ── Fragments
export async function loadFragments() {
  try {
    S.fragments = await api.get('/fragments');
    renderFragments();
  } catch (error) {
    console.error('Failed to load fragments:', error);
    throw error;
  }
}

export function renderFragments() {
  if (!S.fragments || S.fragments.length === 0) {
    $('frag-list').innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No fragments</div>';
    return;
  }
  
  const html = S.fragments.map(f => {
    // Handle both boolean and numeric (0/1) enabled values from backend
    const enabled = f.enabled === true || f.enabled === 1;
    const toggleId = `frag-toggle-${f.id}`;
    return `
    <div class="fragment-item" style="cursor:pointer" title="${esc(f.description)}" onclick="showFragmentModal('${f.id}')">
      <div style="flex:1; min-width:0;">
        <span class="frag-label">${esc(f.label)}</span>
        <span class="frag-id">${esc(f.id)}</span>
      </div>
      <div class="frag-toggle-wrapper" onclick="event.stopPropagation()">
        <label class="frag-toggle" for="${toggleId}">
          <input type="checkbox" id="${toggleId}" ${enabled ? 'checked' : ''}
                 onchange="toggleFragmentEnabled('${f.id}', this.checked)">
          <span class="frag-toggle-slider"></span>
        </label>
      </div>
    </div>`;
  }).join('');
  
  $('frag-list').innerHTML = html;
}

export function showFragmentModal(fragId = null) {
  const f      = fragId ? S.fragments.find(x => x.id === fragId) : null;
  const isEdit = !!f;
  const d      = f || { id: '', label: '', description: '', prompt_text: '', negative_prompt: '' };

  showModal(`
    <h2>${isEdit ? 'Edit' : 'New'} Fragment</h2>
    <div class="field-row">
      <div class="field"><label>ID</label>
        <input id="frag-id" value="${esc(d.id)}" ${isEdit ? 'disabled' : ''} placeholder="e.g. dramatic"></div>
      <div class="field"><label>Label</label>
        <input id="frag-label" value="${esc(d.label)}"></div>
    </div>
    <div class="field"><label>Description</label>
      <input id="frag-desc" value="${esc(d.description)}"></div>
    <div class="field"><label>Prompt Text</label>
      <textarea id="frag-text" rows="4">${esc(d.prompt_text)}</textarea></div>
    <div class="field">
      <label>Negative Prompt <span style="font-size:10px;color:var(--text-muted)">(injected if this fragment is removed next turn)</span></label>
      <textarea id="frag-neg" rows="3">${esc(d.negative_prompt || '')}</textarea>
    </div>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteFragment('${esc(d.id)}')">Delete</button>` : ''}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveFragment(${isEdit})">${isEdit ? 'Save' : 'Create'}</button>
    </div>`);
}

export async function saveFragment(isEdit) {
  const d = {
    id:              $('frag-id').value.trim(),
    label:           $('frag-label').value.trim(),
    description:     $('frag-desc').value.trim(),
    prompt_text:     $('frag-text').value.trim(),
    negative_prompt: $('frag-neg').value.trim(),
  };
  if (!d.id || !d.label || !d.prompt_text) { toast('Fill in required fields', true); return; }
  try {
    if (isEdit) await api.put('/fragments/' + d.id, d);
    else        await api.post('/fragments', d);
    closeModal();
    await loadFragments();
    toast('Fragment saved');
  } catch (e) { toast(e.message, true); }
}

export async function deleteFragment(id) {
  showConfirmModal({
    title: 'Delete Fragment',
    message: 'Are you sure you want to delete this fragment?',
    confirmText: 'Delete',
  }, async () => {
    try {
      await api.del('/fragments/' + id);
      await loadFragments();
      toast('Fragment deleted');
    } catch (e) { toast(e.message, true); }
  });
}

export async function toggleFragmentEnabled(id, newEnabled) {
  try {
    await api.put('/fragments/' + id, { enabled: newEnabled });
    // Update local state optimistically
    const frag = S.fragments.find(f => f.id === id);
    if (frag) frag.enabled = newEnabled;
    renderFragments();
    toast(newEnabled ? 'Fragment enabled' : 'Fragment disabled');
  } catch (e) {
    toast(e.message, true);
  }
}

// ── Characters

/**
 * Build a sorted list of characters showing only the top N most recently
 * talked-to characters (ordered by most recent conversation time).
 * Characters without conversations are sorted by their last update time
 * and included only if there are fewer than `limit` characters total.
 */
function filterRecentCharacters(characters, conversations, limit = 5) {
  // Map each character_card_id to its most recent conversation timestamp
  const recentMap = new Map();
  for (const conv of conversations) {
    const cardId = conv.character_card_id;
    if (!cardId) continue;
    const ts = conv.updated_at || conv.created_at;
    const existing = recentMap.get(cardId);
    if (!existing || ts > existing) {
      recentMap.set(cardId, ts);
    }
  }

  // Tag each character with its "activity" timestamp for sorting
  const tagged = characters.map(char => {
    const convTime = recentMap.get(char.id);
    const activityTime = convTime || (char.updated_at || char.created_at || '');
    return { char, activityTime, hasConversation: !!convTime };
  });

  // Sort by activity time descending (conversations beat updates)
  tagged.sort((a, b) => b.activityTime.localeCompare(a.activityTime));

  // Return only the top N
  return tagged.slice(0, limit).map(t => t.char);
}

export async function loadCharacters() {
  const [characters, conversations] = await Promise.all([
    api.get('/characters'),
    S.conversations || api.get('/conversations'),
  ]);
  // Store full list for efficient refreshes
  S.allCharacters = characters;
  S.characters = filterRecentCharacters(characters, conversations || []);
  renderCharacters();
}

/**
 * Efficiently refresh the character list by re-filtering from the full character
 * set using already-loaded conversations (no API calls). Called after sending a
 * message to promote the active character to the top of the recent list.
 */
export function refreshCharacters() {
  const source = S.allCharacters || S.characters;
  if (!source || source.length === 0) return;
  S.characters = filterRecentCharacters(source, S.conversations || []);
  renderCharacters();
}

export function renderCharacters() {
  if (!S.characters.length) {
    $('char-list').innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No characters yet.</div>';
    return;
  }
  $('char-list').innerHTML = S.characters.map(c => {
    const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : '';
    const av   = c.has_avatar ? `<img src="${avatarUrl(c.id)}${bust}" onerror="this.parentElement.textContent='👤'">` : '👤';
    const meta    = esc((c.tags || []).slice(0, 2).join(', ') || c.source_format || '');
    const isActive = S.activeCharId === c.id;
    return `<div class="char-item${isActive ? ' active' : ''}" onclick="selectChar('${c.id}', 'recent')">
      <div class="char-avatar-sm">${av}</div>
      <div class="char-item-info">
        <div class="char-item-name">${esc(c.name)}</div>
        <div class="char-item-meta">${meta}</div>
      </div>
      <div class="char-item-actions">
        <button onclick="event.stopPropagation();showCharEditModal('${c.id}')" title="Edit character">✏</button>
        <button class="del-btn" onclick="event.stopPropagation();deleteCharacter('${c.id}')">✕</button>
      </div>
    </div>`;
  }).join('');
}

export function triggerImport() { $('import-file-input').click(); }

export async function handleImportFile(inp) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = '';
  try {
    toast('Importing...');
    const r = await api.upload('/characters/import', f);
    showCharEditModal(r);
  } catch (e) {
    toast('Import failed: ' + e.message, true);
  }
}

export async function deleteCharacter(id) {
  showConfirmModal({
    title: 'Delete Character',
    message: 'Are you sure you want to delete this character card?',
    confirmText: 'Delete',
    extraHtml: `
      <div class="field">
        <label class="modal-checkbox-label">
          <input type="checkbox" id="delete-conversations-checkbox">
          Also delete all conversations associated with this character
        </label>
      </div>`,
  }, () => performDeleteCharacter(id));
}

async function performDeleteCharacter(id) {
  const deleteConversations = document.getElementById('delete-conversations-checkbox')?.checked || false;
  const url = '/characters/' + id + (deleteConversations ? '?delete_conversations=true' : '');
  try {
    await api.del(url);
    if (S.activeCharId === id) resetChatUI();
    await loadCharacters();
    await loadConversations();
    closeModal();
    toast('Deleted');
  } catch (e) { toast(e.message, true); }
}

// ── Alternate greetings helpers (used by both create and edit modals)

export function addAltGreeting(prefix) {
  const container = $(`${prefix}-ag-list`);
  if (!container) return;
  const row = document.createElement('div');
  row.className = 'alt-greeting-row';
  row.innerHTML = `<textarea rows="3"></textarea><button class="btn btn-sm" onclick="this.parentElement.remove()" title="Remove">✕</button>`;
  container.appendChild(row);
}

function _readAltGreetings(prefix) {
  const container = $(`${prefix}-ag-list`);
  if (!container) return [];
  return [...container.querySelectorAll('textarea')]
    .map(t => t.value.trim())
    .filter(Boolean);
}

// ── Avatar crop helpers

export function triggerAvatarCrop(prefix, cardId) {
  showCropModal(({ b64, mime }) => {
    _pendingAvatar = { b64, mime };
    const el = $(`${prefix}-avatar-preview`);
    if (el) el.innerHTML = `<img src="data:${mime};base64,${b64}">`;
  });
}

// ── Export

export function exportCharacter(id, name) {
  const a = document.createElement('a');
  a.href = `/api/characters/${id}/export`;
  a.download = (name || 'character') + '.png';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ── Shared tab template for create / edit modals
function charFormTabs(prefix, d, isEdit) {
  const agHtml = (d.alternate_greetings || []).map(g => `
    <div class="alt-greeting-row">
      <textarea rows="3">${esc(g)}</textarea>
      <button class="btn btn-sm" onclick="this.parentElement.remove()" title="Remove">✕</button>
    </div>`).join('');

  return `
    <div class="tabs">
      <div class="tab active" onclick="switchTab(this,'${prefix}-tp')">Persona</div>
      <div class="tab" onclick="switchTab(this,'${prefix}-ts')">Scenario</div>
      <div class="tab" onclick="switchTab(this,'${prefix}-tm')">Messages</div>
      ${isEdit ? `<div class="tab" onclick="switchTab(this,'${prefix}-ta')">Advanced</div>` : ''}
    </div>
    <div id="${prefix}-tp" class="tab-content active">
      <div class="field"><label>Description</label><textarea id="${prefix}-desc" rows="5">${esc(d.description || '')}</textarea></div>
      <div class="field"><label>Personality</label><textarea id="${prefix}-personality" rows="4">${esc(d.personality || '')}</textarea></div>
    </div>
    <div id="${prefix}-ts" class="tab-content">
      <div class="field"><label>Scenario</label><textarea id="${prefix}-scenario" rows="7">${esc(d.scenario || '')}</textarea></div>
    </div>
    <div id="${prefix}-tm" class="tab-content">
      <div class="field"><label>First Message</label><textarea id="${prefix}-first-mes" rows="5">${esc(d.first_mes || '')}</textarea></div>
      <div class="field"><label>Example Messages</label><textarea id="${prefix}-mes-example" rows="4">${esc(d.mes_example || '')}</textarea></div>
      <div class="field">
        <label>Alternate Greetings</label>
        <div id="${prefix}-ag-list">${agHtml}</div>
        <button class="btn btn-sm" style="margin-top:4px" onclick="addAltGreeting('${prefix}')">+ Add</button>
      </div>
    </div>
    ${isEdit ? `
    <div id="${prefix}-ta" class="tab-content">
      <div class="field"><label>System Prompt Override</label><textarea id="${prefix}-sysprompt" rows="3">${esc(d.system_prompt || '')}</textarea></div>
      <div class="field"><label>Post-History Instructions</label><textarea id="${prefix}-posthist" rows="3">${esc(d.post_history_instructions || '')}</textarea></div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:8px">Source: ${esc(d.source_format)} · ID: ${esc(d.id)}</div>
    </div>` : ''}`;
}

export function showCharCreateModal() {
  _pendingAvatar = null;
  showModal(`
    <div style="display:flex;gap:16px;align-items:flex-start;margin-bottom:16px;">
      <div id="cc-avatar-preview" class="char-avatar-lg" onclick="triggerAvatarCrop('cc')"
           title="Click to set avatar" style="cursor:pointer">👤</div>
      <div style="flex:1">
        <div class="field" style="margin-bottom:4px">
          <input id="cc-name" placeholder="Character name…" style="font-size:18px;font-weight:600;width:100%">
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:6px">New character · click portrait to set avatar</div>
      </div>
    </div>
    ${charFormTabs('cc', {}, false)}
    <div class="modal-actions">
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="createCharacter()">Create</button>
    </div>`);
}

export async function createCharacter() {
  const n = $('cc-name').value.trim();
  if (!n) { toast('Name required', true); return; }
  try {
    const payload = {
      name:               n,
      description:        $('cc-desc').value.trim(),
      personality:        $('cc-personality').value.trim(),
      scenario:           $('cc-scenario').value.trim(),
      first_mes:          $('cc-first-mes').value.trim(),
      mes_example:        $('cc-mes-example').value.trim(),
      alternate_greetings: _readAltGreetings('cc'),
    };
    if (_pendingAvatar) {
      payload.avatar_b64  = _pendingAvatar.b64;
      payload.avatar_mime = _pendingAvatar.mime;
    }
    _pendingAvatar = null;
    const created = await api.post('/characters', payload);
    closeModal();
    await loadCharacters();
    toast('Created');
  } catch (e) { toast(e.message, true); }
}

export async function showCharEditModal(idOrData) {
  _pendingAvatar = null;
  const isNew = typeof idOrData === 'object';
  const c = isNew ? idOrData : await api.get('/characters/' + idOrData);

  let av;
  if (isNew && c.avatar_b64) {
    _pendingAvatar = { b64: c.avatar_b64, mime: c.avatar_mime || 'image/png' };
    _pendingImportId = c.id || null;
    _pendingImportSourceFormat = c.source_format || null;
    av = `<img src="data:${_pendingAvatar.mime};base64,${_pendingAvatar.b64}">`;
  } else {
    const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : '';
    av = c.has_avatar ? `<img src="${avatarUrl(c.id)}${bust}">` : '👤';
  }

  const tags = (c.tags || []).map(t => `<span class="char-tag">${esc(t)}</span>`).join('');

  showModal(`
    <div style="display:flex;gap:16px;align-items:flex-start;margin-bottom:16px;">
      <div id="ce-avatar-preview" class="char-avatar-lg" onclick="triggerAvatarCrop('ce')"
           title="Click to change avatar" style="cursor:pointer">${av}</div>
      <div style="flex:1">
        <div class="field" style="margin-bottom:4px">
          <input id="ce-name" value="${esc(c.name)}" style="font-size:18px;font-weight:600;width:100%">
        </div>
        ${c.creator ? `<div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">by ${esc(c.creator)}</div>` : ''}
        ${tags ? `<div class="char-tags">${tags}</div>` : ''}
      </div>
    </div>
    ${charFormTabs('ce', c, true)}
    <div class="modal-actions">
      ${!isNew ? `<button class="btn btn-danger btn-sm" onclick="deleteCharacter('${c.id}')">Delete</button>` : ''}
      <div style="flex:1"></div>
      ${!isNew ? `<button class="btn btn-sm" onclick="exportCharacter('${c.id}','${esc(c.name)}')">Export PNG</button>` : ''}
      <button class="btn" onclick="closeModal()">Cancel</button>
      ${isNew
        ? `<button class="btn btn-accent" onclick="saveImportedChar()">Save</button>`
        : `<button class="btn btn-accent" onclick="saveCharEdit('${c.id}')">Save</button>`}
    </div>`);
}

export async function saveCharEdit(id) {
  const d = {
    name:                      $('ce-name').value.trim(),
    description:               $('ce-desc').value.trim(),
    personality:               $('ce-personality').value.trim(),
    scenario:                  $('ce-scenario').value.trim(),
    first_mes:                 $('ce-first-mes').value.trim(),
    mes_example:               $('ce-mes-example').value.trim(),
    system_prompt:             $('ce-sysprompt').value.trim(),
    post_history_instructions: $('ce-posthist').value.trim(),
    alternate_greetings:       _readAltGreetings('ce'),
  };
  if (_pendingAvatar) {
    d.avatar_b64  = _pendingAvatar.b64;
    d.avatar_mime = _pendingAvatar.mime;
  }
  const avatarChanged = !!_pendingAvatar;
  _pendingAvatar = null;
  if (!d.name) { toast('Name required', true); return; }
  try {
    await api.put('/characters/' + id, d);
    if (avatarChanged) {
      _avatarBust.set(id, Date.now());
      if (S.activeCharId === id) {
        const av = document.getElementById('chat-avatar');
        if (av) av.innerHTML = `<img src="${avatarUrl(id)}?v=${_avatarBust.get(id)}" onerror="this.parentElement.textContent='📜'">`;
      }
    }
    closeModal();
    await loadCharacters();
    toast('Saved');
  } catch (e) { toast(e.message, true); }
}

export async function saveImportedChar() {
  const d = {
    name:                      $('ce-name').value.trim(),
    description:               $('ce-desc').value.trim(),
    personality:               $('ce-personality').value.trim(),
    scenario:                  $('ce-scenario').value.trim(),
    first_mes:                 $('ce-first-mes').value.trim(),
    mes_example:               $('ce-mes-example').value.trim(),
    system_prompt:             $('ce-sysprompt').value.trim(),
    post_history_instructions: $('ce-posthist').value.trim(),
    alternate_greetings:       _readAltGreetings('ce'),
  };
  if (_pendingAvatar) {
    d.avatar_b64  = _pendingAvatar.b64;
    d.avatar_mime = _pendingAvatar.mime;
  }
  if (_pendingImportId)           d.id            = _pendingImportId;
  if (_pendingImportSourceFormat) d.source_format = _pendingImportSourceFormat;
  _pendingAvatar = null;
  _pendingImportId = null;
  _pendingImportSourceFormat = null;
  if (!d.name) { toast('Name required', true); return; }
  try {
    const created = await api.post('/characters', d);
    closeModal();
    await loadCharacters();
    toast(`Imported "${created.name}"`);
  } catch (e) {
    if (e.status === 409) toast('Character already in your library', true);
    else toast(e.message, true);
  }
}

// ── Character Browser Modal

export async function showCharacterBrowserModal() {
  try {
    _browserCharacters = await api.get('/characters');
  } catch (e) {
    _browserCharacters = S.characters || [];
    console.error('Failed to load characters for browser:', e);
  }
  // Load conversations for sorting
  try {
    _browserConversations = await api.get('/conversations');
  } catch (e) {
    _browserConversations = [];
    console.error('Failed to load conversations for browser:', e);
  }
  _browserSortBy = 'time-added';
  _browserViewMode = S.characterBrowserView || 'grid';
  _browserSearchQuery = '';
  renderCharacterBrowser();
  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Character Library</h2>
        <div style="font-size:11px;color:var(--text-muted)">${_browserCharacters.length} character${_browserCharacters.length !== 1 ? 's' : ''}</div>
      </div>
      <div class="modal-title-actions">
        <div class="view-toggle" id="char-browser-view-toggle">
          <button class="view-toggle-btn active" data-view="grid" onclick="setCharBrowserView('grid')">⊞ Grid</button>
          <button class="view-toggle-btn" data-view="list" onclick="setCharBrowserView('list')">☰ List</button>
        </div>
      </div>
    </div>
    <div class="char-browser-search-row">
      <div class="char-browser-search">
        <input type="text" id="char-browser-search" placeholder="Search characters by name..." oninput="onCharBrowserSearch()">
        <span class="search-icon">🔍</span>
      </div>
      <select id="char-browser-sort" class="char-browser-sort" onchange="setCharBrowserSort(this.value)">
        <option value="name" ${_browserSortBy === 'name' ? 'selected' : ''}>Name</option>
        <option value="time-added" ${_browserSortBy === 'time-added' ? 'selected' : ''}>Date Added</option>
        <option value="most-recent-chat" ${_browserSortBy === 'most-recent-chat' ? 'selected' : ''}>Most Recent Chat</option>
        <option value="most-chats" ${_browserSortBy === 'most-chats' ? 'selected' : ''}>Most Chats</option>
      </select>
    </div>
    <div id="char-browser-content"></div>`);
}

export function setCharBrowserView(mode) {
  _browserViewMode = mode;
  S.characterBrowserView = mode;
  document.querySelectorAll('#char-browser-view-toggle .view-toggle-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === mode);
  });
  renderCharBrowserItems();
}

export function onCharBrowserSearch() {
  const input = $('char-browser-search');
  _browserSearchQuery = input.value.trim().toLowerCase();
  renderCharBrowserItems();
}

export function setCharBrowserSort(sortBy) {
  _browserSortBy = sortBy;
  // Update dropdown UI
  const select = document.getElementById('char-browser-sort');
  if (select) select.value = sortBy;
  renderCharBrowserItems();
}

function computeConversationStats() {
  const map = new Map();
  for (const conv of _browserConversations) {
    const cardId = conv.character_card_id;
    if (!cardId) continue;
    const entry = map.get(cardId) || { count: 0, recentTimestamp: '' };
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
  const collator = new Intl.Collator(undefined, { sensitivity: 'base' });
  return [...characters].sort((a, b) => {
    switch (sortBy) {
      case 'name':
        return collator.compare(a.name, b.name);
      case 'time-added':
        // Use created_at descending (newest first)
        const aTime = a.created_at || '';
        const bTime = b.created_at || '';
        return bTime.localeCompare(aTime);
      case 'most-recent-chat':
        const aStat = stats.get(a.id);
        const bStat = stats.get(b.id);
        const aTs = aStat?.recentTimestamp || a.updated_at || a.created_at || '';
        const bTs = bStat?.recentTimestamp || b.updated_at || b.created_at || '';
        return bTs.localeCompare(aTs);
      case 'most-chats':
        const aCount = stats.get(a.id)?.count || 0;
        const bCount = stats.get(b.id)?.count || 0;
        return bCount - aCount;
      default:
        return 0;
    }
  });
}

function getFilteredCharacters() {
  if (!_browserSearchQuery) return _browserCharacters;
  return _browserCharacters.filter(c =>
    c.name.toLowerCase().includes(_browserSearchQuery)
  );
}

function renderCharBrowserItems() {
  const container = $('char-browser-content');
  if (!container) return;
  
  const filtered = getFilteredCharacters();
  const sorted = applySort(filtered);
  
  if (sorted.length === 0) {
    container.innerHTML = `<div class="char-browser-empty">${_browserSearchQuery ? 'No characters match your search' : 'No characters available'}</div>`;
    return;
  }
  
  if (_browserViewMode === 'grid') {
    container.innerHTML = `<div class="char-browser-grid">${sorted.map(c => renderCharBrowserCard(c)).join('')}</div>`;
  } else {
    container.innerHTML = `<div class="char-browser-list">${sorted.map(c => renderCharBrowserListItem(c)).join('')}</div>`;
  }
}

function renderCharBrowserCard(c) {
  const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : '';
  const av = c.has_avatar
    ? `<img src="${avatarUrl(c.id)}${bust}" onerror="this.parentElement.textContent='👤'">`
    : '👤';
  return `
    <div class="char-browser-card" onclick="selectChar('${c.id}', 'library');closeModal()">
      <div class="char-browser-avatar">${av}</div>
      <div class="char-browser-card-name">${esc(c.name)}</div>
    </div>`;
}

function renderCharBrowserListItem(c) {
  const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : '';
  const av = c.has_avatar
    ? `<img src="${avatarUrl(c.id)}${bust}" onerror="this.parentElement.textContent='👤'">`
    : '👤';
  return `
    <div class="char-browser-list-item" onclick="selectChar('${c.id}', 'library');closeModal()">
      <div class="char-browser-list-avatar">${av}</div>
      <div class="char-browser-list-name">${esc(c.name)}</div>
    </div>`;
}

function renderCharacterBrowser() {
  // Initial render happens after modal is shown
  setTimeout(() => renderCharBrowserItems(), 0);
}