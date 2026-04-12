import { S } from './state.js';
import { $, esc, toast, avatarUrl } from './utils.js';
import { api } from './api.js';
import { showModal, closeModal, switchTab } from './modal.js';
import { resetChatUI, loadConversations } from './chat.js';

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
  if (!confirm('Delete this fragment?')) return;
  try {
    await api.del('/fragments/' + id);
    await loadFragments();
    toast('Fragment deleted');
  } catch (e) { toast(e.message, true); }
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
export async function loadCharacters() {
  S.characters = await api.get('/characters');
  renderCharacters();
}

export function renderCharacters() {
  if (!S.characters.length) {
    $('char-list').innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No characters yet.</div>';
    return;
  }
  $('char-list').innerHTML = S.characters.map(c => {
    const av      = c.has_avatar ? `<img src="${avatarUrl(c.id)}" onerror="this.parentElement.textContent='👤'">` : '👤';
    const meta    = esc((c.tags || []).slice(0, 2).join(', ') || c.source_format || '');
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

export function triggerImport() { $('import-file-input').click(); }

export async function handleImportFile(inp) {
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

export async function deleteCharacter(id) {
  if (!confirm('Delete this character card?')) return;
  try {
    await api.del('/characters/' + id);
    if (S.activeCharId === id) resetChatUI();
    await loadCharacters();
    await loadConversations();
    toast('Deleted');
  } catch (e) { toast(e.message, true); }
}

// Shared tab template for create / edit modals
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

export function showCharCreateModal() {
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

export async function createCharacter() {
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

export async function showCharEditModal(id) {
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

export async function saveCharEdit(id) {
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