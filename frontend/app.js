// ─────────────────────────────────────────────
//  STATE
// ─────────────────────────────────────────────
const S = {
  conversations: [],
  activeConvId: null,
  activeCharId: null,
  messages: [],
  fragments: [],
  characters: [],
  settings: {},
  directorState: null,
  lastDirectorData: null,
  isStreaming: false,
  agentEnabled: true,
  enabledTools: { set_writing_styles: true, rewrite_user_prompt: false, fix_writer_output: false },
  editingMsgId: null,
  abortController: null,
};

// ─────────────────────────────────────────────
//  UTILS
// ─────────────────────────────────────────────
const $ = id => document.getElementById(id);

function esc(s) {
  return s
    ? s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    : '';
}

function formatProse(t) {
  return '<p>' +
    t.replace(/&/g, '&amp;')
     .replace(/</g, '&lt;')
     .replace(/>/g, '&gt;')
     .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
     .replace(/\*(.+?)\*/g, '<em>$1</em>')
     .replace(/\n\n+/g, '</p><p>')
     .replace(/\n/g, '<br>') +
    '</p>';
}

function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function scrollToBottom() {
  const el = $('chat-messages');
  setTimeout(() => el.scrollTop = el.scrollHeight, 50);
}

function avatarUrl(id) {
  return '/api/characters/' + id + '/avatar';
}

function convUrl(convId, ...parts) {
  return '/conversations/' + convId + (parts.length ? '/' + parts.join('/') : '');
}

function formatRelativeDate(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso);
  if (diff < 60_000)      return 'just now';
  if (diff < 3_600_000)   return Math.floor(diff / 60_000) + 'm ago';
  if (diff < 86_400_000)  return Math.floor(diff / 3_600_000) + 'h ago';
  if (diff < 604_800_000) return Math.floor(diff / 86_400_000) + 'd ago';
  return new Date(iso).toLocaleDateString();
}

// ─────────────────────────────────────────────
//  API
// ─────────────────────────────────────────────
const api = {
  async _req(path, opts = {}) {
    const r = await fetch('/api' + path, opts);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  get(p)    { return this._req(p); },
  post(p, b){ return this._req(p, { method: 'POST',   headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }); },
  put(p, b) { return this._req(p, { method: 'PUT',    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }); },
  del(p)    { return this._req(p, { method: 'DELETE' }); },
  upload(p, file) {
    const fd = new FormData();
    fd.append('file', file);
    return this._req(p, { method: 'POST', body: fd });
  },
};

// ─────────────────────────────────────────────
//  MODAL
// ─────────────────────────────────────────────
function showModal(html) {
  $('modal-root').innerHTML =
    `<div class="modal-overlay" onclick="if(event.target===this)closeModal()">
       <div class="modal">${html}</div>
     </div>`;
}
function closeModal() { $('modal-root').innerHTML = ''; }

function switchTab(tab, contentId) {
  tab.parentElement.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  tab.classList.add('active');
  tab.closest('.modal').querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
  $(contentId).classList.add('active');
}

// ─────────────────────────────────────────────
//  SIDEBAR TOGGLE
// ─────────────────────────────────────────────
function toggleSection(header) {
  header.querySelector('.arrow').classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

// ─────────────────────────────────────────────
//  THEME
// ─────────────────────────────────────────────
const THEMES = ['dark','halloween','dark_forest','ocean_depths','ghostly','pastel_neon','vintage_wood','newspaper'];

function applyTheme(name) {
  if (!THEMES.includes(name)) name = 'dark';
  $('theme-link').href = '/static/themes/' + name + '.css';
  localStorage.setItem('ar-theme', name);
  const sel = $('theme-select');
  if (sel) sel.value = name;
}

(function initTheme() {
  applyTheme(localStorage.getItem('ar-theme') || 'dark');
})();

// ─────────────────────────────────────────────
//  SETTINGS
// ─────────────────────────────────────────────
const SETTING_FIELDS = [
  { k: 'endpoint_url',       l: 'Endpoint URL',    t: 'text'     },
  { k: 'api_key',            l: 'API Key',          t: 'password' },
  { k: 'model_name',         l: 'Model Name',       t: 'text'     },
  { k: 'system_prompt',      l: 'System Prompt',    t: 'textarea' },
  { k: 'temperature',        l: 'Temperature',      t: 'number', s: '0.05', mn: '0',  mx: '2'    },
  { k: 'max_tokens',         l: 'Max Tokens',       t: 'number', s: '64',   mn: '64', mx: '8192' },
  { k: 'top_p',              l: 'Top P',            t: 'number', s: '0.05', mn: '0',  mx: '1'    },
  { k: 'min_p',              l: 'Min P',            t: 'number', s: '0.01', mn: '0',  mx: '1'    },
  { k: 'top_k',              l: 'Top K',            t: 'number', s: '1',    mn: '0',  mx: '200'  },
  { k: 'repetition_penalty', l: 'Rep. Penalty',     t: 'number', s: '0.05', mn: '1',  mx: '2'    },
];

async function loadSettings() {
  S.settings = await api.get('/settings');
  if (S.settings.enabled_tools) S.enabledTools = { ...S.enabledTools, ...S.settings.enabled_tools };
  if (typeof S.settings.enable_agent === 'number') S.agentEnabled = S.settings.enable_agent !== 0;
  renderSettings();
  renderToolsPanel();
  updateUserBtn();
}

function renderSettings() {
  $('settings-form').innerHTML = SETTING_FIELDS.map(f => {
    const v = S.settings[f.k] ?? '';
    if (f.t === 'textarea') {
      return `<div class="field"><label>${f.l}</label>
                <textarea data-key="${f.k}" onchange="saveSetting(this)">${v}</textarea>
              </div>`;
    }
    const attrs = f.s ? `step="${f.s}" min="${f.mn}" max="${f.mx}"` : '';
    return `<div class="field"><label>${f.l}</label>
              <input type="${f.t}" value="${v}" data-key="${f.k}" ${attrs} onchange="saveSetting(this)">
            </div>`;
  }).join('');
}

async function saveSetting(el) {
  let v = el.value;
  if (el.type === 'number') v = parseFloat(v);
  try {
    S.settings = await api.put('/settings', { [el.dataset.key]: v });
    toast('Settings saved');
  } catch (e) { toast('Failed: ' + e.message, true); }
}

// ─────────────────────────────────────────────
//  USER PROFILE
// ─────────────────────────────────────────────
function updateUserBtn() {
  $('user-profile-btn').textContent = '👤 ' + (S.settings.user_name || 'User');
}

function showUserModal() {
  showModal(`
    <h2>User Profile</h2>
    <div class="field">
      <label>Name <span style="font-size:10px;color:var(--text-muted)">(replaces {{user}} in character cards)</span></label>
      <input id="user-name-input" value="${esc(S.settings.user_name || '')}" placeholder="e.g. Alex">
    </div>
    <div class="field">
      <label>Description <span style="font-size:10px;color:var(--text-muted)">(injected into system prompt)</span></label>
      <textarea id="user-desc-input" rows="5" placeholder="Describe yourself — appearance, personality, background...">${esc(S.settings.user_description || '')}</textarea>
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveUserProfile()">Save</button>
    </div>`);
}

async function saveUserProfile() {
  const name = $('user-name-input').value.trim();
  const desc = $('user-desc-input').value.trim();
  try {
    S.settings = await api.put('/settings', { user_name: name || 'User', user_description: desc });
    updateUserBtn();
    closeModal();
    toast('User profile saved');
  } catch (e) { toast('Failed: ' + e.message, true); }
}

// ─────────────────────────────────────────────
//  FRAGMENTS
// ─────────────────────────────────────────────
async function loadFragments() {
  S.fragments = await api.get('/fragments');
  renderFragments();
}

function renderFragments() {
  $('frag-list').innerHTML = S.fragments.map(f =>
    `<div class="fragment-item" style="cursor:pointer" title="${esc(f.description)}" onclick="showFragmentModal('${f.id}')">
       <span class="frag-label">${esc(f.label)}</span>
       <span class="frag-id">${esc(f.id)}</span>
     </div>`
  ).join('');
}

function showFragmentModal(fragId = null) {
  const f    = fragId ? S.fragments.find(x => x.id === fragId) : null;
  const isEdit = !!f;
  const d    = f || { id: '', label: '', description: '', prompt_text: '', negative_prompt: '' };

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
      <label>Negative Prompt <span style="font-size:10px;color:var(--text-muted)">(injected when deactivated)</span></label>
      <textarea id="frag-neg" rows="3">${esc(d.negative_prompt || '')}</textarea>
    </div>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger btn-sm" onclick="deleteFragment('${esc(d.id)}');closeModal()">Delete</button>` : ''}
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveFragment(${isEdit})">${isEdit ? 'Save' : 'Create'}</button>
    </div>`);
}

async function saveFragment(isEdit) {
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

async function deleteFragment(id) {
  if (!confirm('Delete this fragment?')) return;
  try {
    await api.del('/fragments/' + id);
    await loadFragments();
    toast('Fragment deleted');
  } catch (e) { toast(e.message, true); }
}

// ─────────────────────────────────────────────
//  CHARACTERS
// ─────────────────────────────────────────────
async function loadCharacters() {
  S.characters = await api.get('/characters');
  renderCharacters();
}

function renderCharacters() {
  if (!S.characters.length) {
    $('char-list').innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No characters yet.</div>';
    return;
  }
  $('char-list').innerHTML = S.characters.map(c => {
    const av    = c.has_avatar ? `<img src="${avatarUrl(c.id)}" onerror="this.parentElement.textContent='👤'">` : '👤';
    const meta  = esc((c.tags || []).slice(0, 2).join(', ') || c.source_format || '');
    const isActive = S.activeCharId === c.id;
    return `<div class="char-item${isActive ? ' active' : ''}" onclick="selectChar('${c.id}')">
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

function triggerImport() { $('import-file-input').click(); }

async function handleImportFile(inp) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = '';
  try {
    toast('Importing...');
    const r = await api.upload('/characters/import', f);
    await loadCharacters();
    toast(`Imported "${r.name}"`);
    showCharEditModal(r.id);
  } catch (e) { toast('Import failed: ' + e.message, true); }
}

async function deleteCharacter(id) {
  if (!confirm('Delete this character card?')) return;
  try {
    await api.del('/characters/' + id);
    if (S.activeCharId === id) resetChatUI();
    await loadCharacters();
    await loadConversations();
    toast('Deleted');
  } catch (e) { toast(e.message, true); }
}

// Shared template for the character form tabs (used by create and edit modals)
function charFormTabs(prefix, d, isEdit) {
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
    </div>
    ${isEdit ? `
    <div id="${prefix}-ta" class="tab-content">
      <div class="field"><label>System Prompt Override</label><textarea id="${prefix}-sysprompt" rows="3">${esc(d.system_prompt || '')}</textarea></div>
      <div class="field"><label>Post-History Instructions</label><textarea id="${prefix}-posthist" rows="3">${esc(d.post_history_instructions || '')}</textarea></div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:8px">Source: ${esc(d.source_format)} · ID: ${esc(d.id)}</div>
    </div>` : ''}`;
}

function showCharCreateModal() {
  showModal(`
    <div style="display:flex;gap:16px;align-items:flex-start;margin-bottom:16px;">
      <div class="char-avatar-lg">👤</div>
      <div style="flex:1">
        <div class="field" style="margin-bottom:4px">
          <input id="cc-name" placeholder="Character name…" style="font-size:18px;font-weight:600;width:100%">
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:6px">New character</div>
      </div>
    </div>
    ${charFormTabs('cc', {}, false)}
    <div class="modal-actions">
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="createCharacter()">Create</button>
    </div>`);
}

async function createCharacter() {
  const n = $('cc-name').value.trim();
  if (!n) { toast('Name required', true); return; }
  try {
    await api.post('/characters', {
      name:        n,
      description: $('cc-desc').value.trim(),
      personality: $('cc-personality').value.trim(),
      scenario:    $('cc-scenario').value.trim(),
      first_mes:   $('cc-first-mes').value.trim(),
      mes_example: $('cc-mes-example').value.trim(),
    });
    closeModal();
    await loadCharacters();
    toast('Created');
  } catch (e) { toast(e.message, true); }
}

async function showCharEditModal(id) {
  const c    = await api.get('/characters/' + id);
  const av   = c.has_avatar ? `<img src="${avatarUrl(c.id)}">` : '👤';
  const tags = (c.tags || []).map(t => `<span class="char-tag">${esc(t)}</span>`).join('');

  showModal(`
    <div style="display:flex;gap:16px;align-items:flex-start;margin-bottom:16px;">
      <div class="char-avatar-lg">${av}</div>
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
      <button class="btn btn-danger btn-sm" onclick="deleteCharacter('${c.id}');closeModal()">Delete</button>
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveCharEdit('${c.id}')">Save</button>
    </div>`);
}

async function saveCharEdit(id) {
  const d = {
    name:                     $('ce-name').value.trim(),
    description:              $('ce-desc').value.trim(),
    personality:              $('ce-personality').value.trim(),
    scenario:                 $('ce-scenario').value.trim(),
    first_mes:                $('ce-first-mes').value.trim(),
    mes_example:              $('ce-mes-example').value.trim(),
    system_prompt:            $('ce-sysprompt').value.trim(),
    post_history_instructions:$('ce-posthist').value.trim(),
  };
  if (!d.name) { toast('Name required', true); return; }
  try {
    await api.put('/characters/' + id, d);
    closeModal();
    await loadCharacters();
    toast('Saved');
  } catch (e) { toast(e.message, true); }
}

// ─────────────────────────────────────────────
//  CONVERSATIONS
// ─────────────────────────────────────────────
async function loadConversations() {
  S.conversations = await api.get('/conversations');
}

function resetChatUI() {
  S.activeCharId = null;
  S.activeConvId = null;
  S.messages = [];
  $('chat-title-text').textContent = 'Select a character';
  $('chat-avatar').textContent = '📜';
  $('chat-input').disabled = true;
  $('send-btn').disabled = true;
  renderMessages();
}

async function selectChar(id) {
  if (S.activeCharId === id) return;
  S.activeCharId = id;
  renderCharacters();

  const existing = S.conversations.find(c => c.character_card_id === id);
  if (existing) {
    await selectConversation(existing.id);
  } else {
    try {
      const conv = await api.post('/conversations', { character_card_id: id });
      await loadConversations();
      await selectConversation(conv.id);
    } catch (e) { toast(e.message, true); }
  }
}

async function newConvForChar(id) {
  try {
    const conv = await api.post('/conversations', { character_card_id: id });
    await loadConversations();
    S.activeCharId = id;
    renderCharacters();
    await selectConversation(conv.id);
  } catch (e) { toast(e.message, true); }
}

async function selectConversation(id) {
  S.activeConvId = id;
  const conv = S.conversations.find(c => c.id === id);

  if (conv?.character_card_id && S.activeCharId !== conv.character_card_id) {
    S.activeCharId = conv.character_card_id;
    renderCharacters();
  }

  $('chat-title-text').textContent = conv ? (conv.title || conv.character_name) : '';

  const av = $('chat-avatar');
  if (conv?.character_card_id) {
    av.innerHTML = `<img src="${avatarUrl(conv.character_card_id)}" onerror="this.parentElement.textContent='📜'">`;
  } else {
    av.textContent = '📜';
  }

  $('chat-input').disabled = false;
  $('send-btn').disabled = false;

  S.messages      = await api.get(convUrl(id, 'messages'));
  S.directorState = await api.get(convUrl(id, 'director'));
  S.editingMsgId  = null;

  renderMessages();
  renderInspector();
  scrollToBottom();
}

async function deleteConversation(id) {
  if (!confirm('Delete?')) return;
  try {
    await api.del('/conversations/' + id);
    if (S.activeConvId === id) {
      S.activeConvId = null;
      S.messages = [];
      $('chat-input').disabled = true;
      $('send-btn').disabled = true;
      renderMessages();
    }
    await loadConversations();
  } catch (e) { toast(e.message, true); }
}

async function deleteConversationFromModal(id) {
  if (!confirm('Delete?')) return;
  try {
    await api.del('/conversations/' + id);
    if (S.activeConvId === id) {
      S.activeConvId = null;
      S.messages = [];
      $('chat-input').disabled = true;
      $('send-btn').disabled = true;
      renderMessages();
    }
    await showConvHistoryModal();
  } catch (e) { toast(e.message, true); }
}

async function showConvHistoryModal() {
  if (!S.activeCharId) { toast('Select a character first', true); return; }
  await loadConversations();
  const convs = S.conversations.filter(c => c.character_card_id === S.activeCharId);
  if (!convs.length) { toast('No conversations yet', true); return; }

  const char     = S.characters.find(c => c.id === S.activeCharId);
  const charName = char ? char.name : 'Character';

  const items = convs.map(c => {
    const isActive = c.id === S.activeConvId;
    const preview  = esc((c.last_message_preview || '').substring(0, 80));
    const title    = esc(c.title || c.character_name || 'Untitled');
    const ts       = c.updated_at || c.created_at;
    return `<div class="conv-history-item${isActive ? ' active-conv' : ''}" onclick="closeModal();selectConversation('${c.id}')">
      <div class="conv-history-meta">
        <span class="conv-history-title">${title}</span>
        <span class="conv-history-date">${formatRelativeDate(ts)}</span>
        <button class="conv-history-delete" title="Delete conversation" onclick="event.stopPropagation();deleteConversationFromModal('${c.id}')">&#x2715;</button>
      </div>
      ${preview
        ? `<div class="conv-history-preview">${preview}</div>`
        : `<div class="conv-history-preview" style="color:var(--text-muted);font-style:italic">No messages yet</div>`}
    </div>`;
  }).join('');

  showModal(`
    <h2>Conversations — ${esc(charName)}</h2>
    <div style="margin:-8px -24px 0;max-height:60vh;overflow-y:auto;">${items}</div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`);
}

// ─────────────────────────────────────────────
//  MESSAGES
// ─────────────────────────────────────────────
function getCharName() {
  const c = S.conversations.find(c => c.id === S.activeConvId);
  return c?.character_name || 'Assistant';
}

function renderMessages() {
  const ct = $('chat-messages');
  if (!S.activeConvId) {
    ct.innerHTML = '<div class="empty-state"><div class="icon">📜</div><div>Select a character to begin</div></div>';
    return;
  }
  if (!S.messages.length) {
    ct.innerHTML = '<div class="empty-state"><div class="icon">📜</div><div>Start writing to begin the scene</div></div>';
    return;
  }
  ct.innerHTML = S.messages.map(m => {
    const isEditing = S.editingMsgId !== null && S.editingMsgId === m.id;
    const bc        = m.branch_count || 1;
    const bi        = m.branch_index || 0;

    const branchHtml = bc > 1 ? `
      <span class="swipe-nav">
        <button onclick="event.stopPropagation();switchBranch(${m.prev_branch_id})" ${!m.prev_branch_id ? 'disabled' : ''}>◀</button>
        <span class="swipe-counter">${bi + 1}/${bc}</span>
        <button onclick="event.stopPropagation();switchBranch(${m.next_branch_id})" ${!m.next_branch_id ? 'disabled' : ''}>▶</button>
      </span>` : '';

    const toolbar = isEditing ? '' : `
      <div class="msg-toolbar">
        <button onclick="startEdit(${m.id})" title="Edit">✏️ Edit</button>
        ${m.role === 'assistant' ? `<button onclick="regenerate(${m.id})" title="Regenerate">🔄 Regen</button>` : ''}
        <button onclick="deleteMessage(${m.id})" title="Delete message and all children" style="color:var(--red)">✕ Del</button>
      </div>`;

    const body = isEditing ? `
      <div class="msg-edit-area">
        <textarea id="edit-textarea-${m.id}" rows="5">${esc(m.content)}</textarea>
        <div class="msg-edit-actions">
          <button class="btn btn-sm" onclick="cancelEdit()">Cancel</button>
          <button class="btn btn-sm btn-accent" onclick="saveEdit(${m.id},'${m.role}')">
            Save${m.role === 'user' ? ' & Regen' : ''}
          </button>
        </div>
      </div>` : `<div class="msg-body">${formatProse(m.content)}</div>`;

    return `<div class="message ${m.role}" data-msg-id="${m.id}">
      <div class="msg-role">${m.role === 'user' ? 'You' : esc(getCharName())} ${branchHtml}</div>
      ${body}${toolbar}
    </div>`;
  }).join('');
}

function startEdit(msgId)  { S.editingMsgId = msgId; renderMessages(); scrollToBottom(); }
function cancelEdit()      { S.editingMsgId = null;  renderMessages(); }

async function deleteMessage(msgId) {
  if (S.isStreaming) return;
  if (!confirm('Delete this message and all its children?')) return;
  try {
    S.messages        = await api.del(convUrl(S.activeConvId, 'messages', msgId));
    S.lastDirectorData = null;
    renderMessages(); renderInspector(); scrollToBottom();
    toast('Message deleted');
  } catch (e) { toast(e.message, true); }
}

async function switchBranch(msgId) {
  if (!msgId || S.isStreaming) return;
  try {
    S.messages        = await api.post(convUrl(S.activeConvId, 'messages', msgId, 'switch-branch'), {});
    S.lastDirectorData = null;
    renderMessages(); renderInspector(); scrollToBottom();
  } catch (e) { toast(e.message, true); }
}

// ─────────────────────────────────────────────
//  STREAMING HELPERS
// ─────────────────────────────────────────────
function setStreaming(active) {
  S.isStreaming = active;
  $('send-btn').style.display = active ? 'none' : 'flex';
  $('stop-btn').style.display = active ? 'flex' : 'none';
}

function stopGeneration() {
  if (S.abortController) S.abortController.abort();
}

/** Creates the "Director analyzing…" badge and appends it if agent is enabled. */
function createDirectorBadge(container) {
  const badge = document.createElement('div');
  badge.className = 'director-badge';
  badge.id        = 'active-director-badge';
  badge.innerHTML = '<span class="dot"></span> Director analyzing scene...';
  if (S.agentEnabled) container.appendChild(badge);
  return badge;
}

/** Creates the streaming assistant message div (with typing indicator). */
function createStreamingDiv() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `<div class="msg-role">${esc(getCharName())}</div>
    <div class="msg-body" id="streaming-body">
      <span class="typing-indicator"><span></span><span></span><span></span></span>
    </div>`;
  return div;
}

/** Refreshes state and re-renders everything after a stream completes. */
async function afterStream() {
  S.abortController = null;
  setStreaming(false);
  $('send-btn').disabled = false;
  S.messages      = await api.get(convUrl(S.activeConvId, 'messages'));
  S.directorState = await api.get(convUrl(S.activeConvId, 'director'));
  renderMessages(); renderInspector(); scrollToBottom();
}

/** Processes an SSE stream from a fetch response into the given msgDiv. */
async function processSSEStream(resp, container, msgDiv, signal) {
  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', fullResponse = '', rewrittenResponse = null, firstToken = true, currentEvent = null;

  if (signal) signal.addEventListener('abort', () => reader.cancel());

  while (true) {
    const { done, value } = await reader.read();
    if (done || signal?.aborted) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ') && currentEvent) {
        const data = line.slice(6);
        handleSSEEvent(currentEvent, data, container, msgDiv,
          () => {
            if (firstToken) { firstToken = false; container.appendChild(msgDiv); $('streaming-body').innerHTML = ''; }
            fullResponse += data.replace(/\\n/g, '\n');
            $('streaming-body').innerHTML = formatProse(fullResponse);
            scrollToBottom();
          },
          (text) => {
            rewrittenResponse = text;
            $('streaming-body').innerHTML = formatProse(text);
            scrollToBottom();
          }
        );
        currentEvent = null;
      }
    }
  }
  return rewrittenResponse ?? fullResponse;
}

function handleSSEEvent(event, data, container, msgDiv, onToken, onRewrite) {
  switch (event) {
    case 'director_start':
      S.lastDirectorData = null;
      renderInspector();
      break;
    case 'director_done': {
      const b = $('active-director-badge'); if (b) b.remove();
      try { S.lastDirectorData = JSON.parse(data); renderInspector(); } catch (_) {}
      break;
    }
    case 'prompt_rewritten':
      try {
        const d = JSON.parse(data);
        const lastUser = [...S.messages].reverse().find(m => m.role === 'user' && !m.id)
                      || [...S.messages].reverse().find(m => m.role === 'user');
        if (lastUser) lastUser.content = d.rewritten_message;
        renderMessages();
      } catch (_) {}
      break;
    case 'token':
      onToken();
      break;
    case 'writer_rewrite':
      try { onRewrite(JSON.parse(data).rewritten_text); } catch (_) {}
      break;
    case 'error':
      toast('Error: ' + data, true);
      break;
  }
}

function agentPayload() {
  return { enable_agent: S.agentEnabled };
}

// ─────────────────────────────────────────────
//  SEND MESSAGE
// ─────────────────────────────────────────────
async function sendMessage() {
  const inp     = $('chat-input');
  const content = inp.value.trim();
  if (!content || !S.activeConvId || S.isStreaming) return;

  setStreaming(true);
  inp.value = ''; inp.style.height = 'auto';
  $('send-btn').disabled = true;

  S.messages.push({ role: 'user', content, id: null, branch_count: 1, branch_index: 0, prev_branch_id: null, next_branch_id: null });
  renderMessages(); scrollToBottom();

  const ct     = $('chat-messages');
  createDirectorBadge(ct); scrollToBottom();
  const msgDiv = createStreamingDiv();

  S.abortController = new AbortController();
  try {
    const resp = await fetch('/api' + convUrl(S.activeConvId, 'send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, ...agentPayload() }),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    const b = $('active-director-badge'); if (b) b.remove();
    if (e.name !== 'AbortError') toast('Connection error: ' + e.message, true);
  }
  await afterStream();
}

// ─────────────────────────────────────────────
//  REGENERATE
// ─────────────────────────────────────────────
async function regenerate(msgId) {
  if (S.isStreaming || !S.activeConvId) return;
  setStreaming(true);
  $('send-btn').disabled = true;

  const ct = $('chat-messages');
  const el = ct.querySelector(`[data-msg-id="${msgId}"]`);
  if (el) el.remove();

  createDirectorBadge(ct);
  const msgDiv = createStreamingDiv();

  S.abortController = new AbortController();
  try {
    const resp = await fetch('/api' + convUrl(S.activeConvId, 'messages', msgId, 'regenerate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(agentPayload()),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    const b = $('active-director-badge'); if (b) b.remove();
    if (e.name !== 'AbortError') toast('Error: ' + e.message, true);
  }
  await afterStream();
}

// ─────────────────────────────────────────────
//  EDIT MESSAGE
// ─────────────────────────────────────────────
async function saveEdit(msgId, role) {
  const ta = $('edit-textarea-' + msgId);
  if (!ta) return;
  const content = ta.value.trim();
  if (!content) { toast('Message cannot be empty', true); return; }

  S.editingMsgId = null;

  if (role === 'assistant') {
    try {
      await api.post(convUrl(S.activeConvId, 'messages', msgId, 'edit'), { content, regenerate: false });
      S.messages = await api.get(convUrl(S.activeConvId, 'messages'));
      renderMessages();
      toast('Message edited');
    } catch (e) { toast(e.message, true); }
    return;
  }

  // User edit → create sibling branch + regenerate via SSE
  setStreaming(true);
  $('send-btn').disabled = true;
  renderMessages();

  const ct = $('chat-messages');
  // Remove all messages after the edited one so generation starts fresh
  const editedEl = ct.querySelector(`[data-msg-id="${msgId}"]`);
  if (editedEl) {
    let next = editedEl.nextElementSibling;
    while (next) { const n = next.nextElementSibling; next.remove(); next = n; }
  }

  createDirectorBadge(ct);
  const msgDiv = createStreamingDiv();

  S.abortController = new AbortController();
  try {
    const resp = await fetch('/api' + convUrl(S.activeConvId, 'messages', msgId, 'edit'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, regenerate: true, ...agentPayload() }),
      signal: S.abortController.signal,
    });
    if (resp.headers.get('content-type')?.includes('text/event-stream')) {
      await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
    } else {
      S.messages = await api.get(convUrl(S.activeConvId, 'messages'));
      renderMessages();
    }
  } catch (e) {
    const b = $('active-director-badge'); if (b) b.remove();
    if (e.name !== 'AbortError') toast('Error: ' + e.message, true);
  }
  await afterStream();
}

// ─────────────────────────────────────────────
//  INSPECTOR
// ─────────────────────────────────────────────
function toggleInspector() { $('inspector').classList.toggle('open'); }

function renderInspector() {
  if (S.isStreaming && S.lastDirectorData === null) {
    $('inspector-content').innerHTML =
      `<div style="color:var(--text-muted);font-size:12px;display:flex;align-items:center;gap:8px">
         <span class="typing-indicator"><span></span><span></span><span></span></span> Director thinking…
       </div>`;
    return;
  }

  const ds        = S.directorState || {};
  const ld        = S.lastDirectorData || {};
  const activeIds = ld.active_styles || ds.active_styles || [];
  const stylesHtml = S.fragments
    .map(f => `<span class="style-tag ${activeIds.includes(f.id) ? 'active' : ''}">${f.id}</span>`)
    .join('');

  const lat = ld.agent_latency_ms || 0;
  const tc  = ld.tool_calls || [];
  const inj = ld.injection_block || '';

  $('inspector-content').innerHTML = `
    <div class="inspector-block"><h4>Active Styles</h4>
      <div>${stylesHtml || '<span style="color:var(--text-muted);font-size:12px">None</span>'}</div>
    </div>
    ${lat ? `<div class="inspector-block"><h4>Agent Latency</h4>
               <div style="font-size:12px;color:var(--text-secondary)">${lat}ms</div></div>` : ''}
    ${tc.length ? `<div class="inspector-block"><h4>Tool Calls</h4>
                    <div class="injection-box">${esc(JSON.stringify(tc, null, 2))}</div></div>` : ''}
    ${inj ? `<div class="inspector-block"><h4>Injection Block</h4>
               <div class="injection-box">${esc(inj)}</div></div>` : ''}`;
}

// ─────────────────────────────────────────────
//  AGENT TOOLS PANEL
// ─────────────────────────────────────────────
const TOOL_DEFS = [
  { id: 'set_writing_styles',    name: 'Style Director',    desc: 'Selects active writing style fragments based on scene context' },
  { id: 'rewrite_user_prompt',   name: 'Prompt Rewriter',   desc: 'Expands vague or lazy messages into richer input' },
  { id: 'fix_writer_output', name: 'Output Auditor',    desc: 'Post-processes the response to fix repetition, slop, and anachronisms' },
];

function toggleToolsPanel() {
  const panel = $('tools-panel');
  const open  = panel.classList.toggle('open');
  $('tools-panel-btn').style.background   = open ? 'var(--accent-glow)' : '';
  $('tools-panel-btn').style.borderColor  = open ? 'var(--accent-dim)'  : '';
  if (open) renderToolsPanel();
}

async function setAgentEnabled(on) {
  S.agentEnabled = on;
  $('tools-panel-btn').style.opacity = on ? '1' : '0.5';
  try {
    S.settings = await api.put('/settings', { enable_agent: on });
  } catch (e) { toast('Failed to save agent state', true); }
}

async function toggleToolEnabled(id, on) {
  S.enabledTools[id] = on;
  renderToolsPanel();
  try {
    S.settings = await api.put('/settings', { enabled_tools: S.enabledTools });
  } catch (e) { toast('Failed to save tool state', true); }
}

function renderToolsPanel() {
  $('agent-enable-chk').checked = S.agentEnabled;
  $('tools-panel-btn').style.opacity = S.agentEnabled ? '1' : '0.5';
  $('tools-list').innerHTML = TOOL_DEFS.map(t => {
    const on = !!S.enabledTools[t.id];
    return `<div class="tool-card ${on ? 'tool-on' : ''}">
      <div class="tool-card-header">
        <span class="tool-card-name">${t.name}</span>
        <label class="tog" onclick="event.stopPropagation()">
          <input type="checkbox" ${on ? 'checked' : ''} onchange="toggleToolEnabled('${t.id}',this.checked)">
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">${t.desc}</div>
    </div>`;
  }).join('');
}

// ─────────────────────────────────────────────
//  BURGER MENU
// ─────────────────────────────────────────────
function toggleBurger() { $('burger-dropdown').classList.toggle('open'); }
function closeBurger()  { $('burger-dropdown').classList.remove('open'); }

document.addEventListener('click', e => {
  if (!e.target.closest('#burger-btn') && !e.target.closest('#burger-dropdown')) closeBurger();
});

// ─────────────────────────────────────────────
//  INPUT EVENTS
// ─────────────────────────────────────────────
$('chat-input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 150) + 'px';
});
$('chat-input').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ─────────────────────────────────────────────
//  INIT
// ─────────────────────────────────────────────
(async () => {
  await loadSettings();
  await loadFragments();
  await loadCharacters();
  await loadConversations();
})();