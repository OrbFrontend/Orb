import { S } from './state.js';
import { $, esc, toast } from './utils.js';
import { api } from './api.js';
import { showModal, closeModal, showConfirmModal } from './modal.js';

// ── Theme
const THEMES = ['dark','halloween','dark_forest','ocean_depths','ghostly','pastel_neon','vintage_wood','newspaper'];

export function applyTheme(name) {
  if (!THEMES.includes(name)) name = 'dark';
  $('theme-link').href = '/static/themes/' + name + '.css';
  localStorage.setItem('ar-theme', name);
  const sel = $('theme-select');
  if (sel) sel.value = name;
}

export function initTheme() {
  applyTheme(localStorage.getItem('ar-theme') || 'dark');
}

// ── Settings
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

export async function loadSettings() {
  S.settings = await api.get('/settings');
  S.activePersonaId = S.settings.active_persona_id || null;
  S.characterBrowserView = S.settings.character_library_view || 'grid';
  S.characterBrowserSort = S.settings.character_library_sort || 'time-added';
  if (S.settings.enabled_tools) S.enabledTools = { ...S.enabledTools, ...S.settings.enabled_tools };
  if (typeof S.settings.enable_agent === 'number') S.agentEnabled = S.settings.enable_agent !== 0;
  
  if (S.settings.enabled_tools && 'length_guard' in S.settings.enabled_tools) {
    S.lengthGuardEnabled = Boolean(S.settings.enabled_tools.length_guard);
  } else {
    S.lengthGuardEnabled = false;
  }

  if (S.settings.enabled_tools && 'length_guard_enforce' in S.settings.enabled_tools) {
    S.lengthGuardEnforce = Boolean(S.settings.enabled_tools.length_guard_enforce);
  } else {
    S.lengthGuardEnforce = false;
  }

  if (S.settings.length_guard_max_words) S.lengthGuardMaxWords = S.settings.length_guard_max_words;
  if (S.settings.length_guard_max_paragraphs) S.lengthGuardMaxParagraphs = S.settings.length_guard_max_paragraphs;
  if (S.settings.reasoning_enabled_passes)
    S.reasoningEnabled = { ...S.reasoningEnabled, ...S.settings.reasoning_enabled_passes };
  renderSettings();
  renderToolsPanel();
  await loadPersonas();
  updateUserBtn();
}

export async function loadPersonas() {
  try {
    S.personas = await api.get('/user-personas');
  } catch (e) {
    console.error('Failed to load personas:', e);
    S.personas = [];
  }
}

export function renderSettings() {
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

export async function saveSetting(el) {
  let v = el.value;
  if (el.type === 'number') v = parseFloat(v);
  try {
    S.settings = await api.put('/settings', { [el.dataset.key]: v });
    toast('Settings saved');
  } catch (e) { toast('Failed: ' + e.message, true); }
}

// ── User Profile
export function updateUserBtn() {
  let displayName = 'User';
  if (S.activePersonaId && S.personas.length) {
    const activePersona = S.personas.find(p => p.id === S.activePersonaId);
    if (activePersona) displayName = activePersona.name;
  }
  $('user-profile-btn').textContent = '👤 ' + displayName;
}

export function showUserModal() {
  const personaItems = S.personas.map(p => {
    const isActive = p.id === S.activePersonaId;
    const avatarColor = p.avatar_color || '#E1F5EE';
    const avatarTextColor = isActive ? 'var(--accent)' : '#085041';
    const avatarBg = isActive ? 'var(--accent-glow)' : avatarColor;
    const initials = p.name.charAt(0).toUpperCase();
    return `
      <div class="persona-item${isActive ? ' persona-item-active' : ''}" onclick="activatePersona(${p.id})">
        <div class="persona-avatar" style="background:${avatarBg};color:${avatarTextColor}">${initials}</div>
        <div class="persona-info">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="persona-name">${esc(p.name)}</span>
            ${isActive ? '<span class="persona-active-badge">Active</span>' : ''}
          </div>
          <span class="persona-desc">${esc(p.description || '')}</span>
        </div>
        <button class="btn btn-sm" onclick="event.stopPropagation();editPersona(${p.id})">Edit</button>
      </div>
    `;
  }).join('');

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>User personas</h2>
        <p class="modal-subtitle">Click a persona to activate it.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn" onclick="showPersonaEditModal(null)">+ New persona</button>
      </div>
    </div>
    <div class="persona-list">
      ${personaItems.length ? personaItems : '<p class="modal-subtitle" style="text-align:center;padding:1rem 0">No personas yet. Create one to get started.</p>'}
    </div>
  `);
}

export async function saveUserProfile() {
  const name = $('user-name-input').value.trim();
  const desc = $('user-desc-input').value.trim();
  try {
    S.settings = await api.put('/settings', { user_name: name || 'User', user_description: desc });
    updateUserBtn();
    closeModal();
    toast('User profile saved');
  } catch (e) { toast('Failed: ' + e.message, true); }
}

export function showPersonaEditModal(personaId) {
  const persona = personaId ? S.personas.find(p => p.id === personaId) : null;
  const isEdit = persona !== null;
  showModal(`
    <h2>${isEdit ? 'Edit persona' : 'New persona'}</h2>
    <div class="field">
      <label>Name</label>
      <input id="persona-name-input" type="text" placeholder="e.g. Kai" value="${esc(persona?.name || '')}">
    </div>
    <div class="field">
      <label>Description <span style="font-weight:400;text-transform:none;letter-spacing:0">(injected into system prompt)</span></label>
      <textarea id="persona-desc-input" placeholder="Describe yourself — appearance, personality, background…" rows="4" style="resize:vertical;min-height:90px">${esc(persona?.description || '')}</textarea>
    </div>
    <label class="modal-checkbox-label" style="margin-bottom:1.25rem">
      <input type="checkbox" id="persona-active-checkbox" ${!personaId || personaId === S.activePersonaId ? 'checked' : ''} style="width:14px;height:14px;margin:0;flex-shrink:0">
      <span style="font-size:13px;text-transform:none;letter-spacing:0;font-weight:400">Set as active persona after saving</span>
    </label>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger" onclick="deletePersona(${personaId})">Delete</button>` : ''}
      <button class="btn" onclick="showUserModal()">Cancel</button>
      <button class="btn btn-accent" onclick="savePersona(${personaId || 'null'})">${isEdit ? 'Update' : 'Create'}</button>
    </div>
  `);
}

export async function savePersona(personaId) {
  const name = $('persona-name-input').value.trim();
  const description = $('persona-desc-input').value.trim();
  const setActive = $('persona-active-checkbox').checked;
  if (!name) {
    toast('Name is required', true);
    return;
  }
  try {
    let newId;
    if (personaId && personaId !== 'null') {
      await api.put('/user-personas/' + personaId, { name, description });
      newId = parseInt(personaId, 10);
    } else {
      const result = await api.post('/user-personas', { name, description });
      newId = result.id;
    }
    await loadPersonas();
    if (setActive) {
      await api.put('/settings', { active_persona_id: newId });
      S.activePersonaId = newId;
      updateUserBtn();
    }
    showUserModal();
    toast('Persona saved');
  } catch (e) {
    toast('Failed: ' + e.message, true);
  }
}

export async function deletePersona(personaId) {
  showConfirmModal({
    title: 'Delete Persona',
    message: 'Are you sure you want to delete this persona?',
    confirmText: 'Delete',
  }, async () => {
    try {
      await api.del('/user-personas/' + personaId);
      if (S.activePersonaId === personaId) {
        await api.put('/settings', { active_persona_id: null });
        S.activePersonaId = null;
        updateUserBtn();
      }
      await loadPersonas();
      showUserModal();
      toast('Persona deleted');
    } catch (e) {
      toast('Failed: ' + e.message, true);
    }
  });
}

export async function activatePersona(personaId) {
  if (S.activePersonaId === personaId) return;
  try {
    await api.put('/settings', { active_persona_id: personaId });
    S.activePersonaId = personaId;
    updateUserBtn();
    showUserModal();
  } catch (e) {
    toast('Failed: ' + e.message, true);
  }
}

export async function editPersona(personaId) {
  showPersonaEditModal(personaId);
}

// ── Agent Tools Panel
const TOOL_DEFS = [
  { id: 'direct_scene',          name: 'Director',   desc: 'Gives written direction and selects active mood fragments based on scene context' },
  { id: 'rewrite_user_prompt',   name: 'Prompt Rewriter',  desc: 'Expands user\'s vague or lazy messages into richer input' },
  { id: 'editor_apply_patch',    name: 'Output Auditor',   desc: 'Scans for banned phrases, repetitive openers & templates, then surgically patches the draft' },
];

export function toggleToolsPanel() {
  const panel = $('tools-panel');
  const open  = panel.classList.toggle('open');
  $('tools-panel-btn').style.background  = open ? 'var(--accent-glow)' : '';
  $('tools-panel-btn').style.borderColor = open ? 'var(--accent-dim)'  : '';
  if (open) renderToolsPanel();
}

export async function setAgentEnabled(on) {
  S.agentEnabled = on;
  $('tools-panel-btn').style.opacity = on ? '1' : '0.5';
  try {
    S.settings = await api.put('/settings', { enable_agent: on });
  } catch (e) { toast('Failed to save agent state', true); }
}

export async function toggleToolEnabled(id, on) {
  S.enabledTools[id] = on;
  renderToolsPanel();
  try {
    S.settings = await api.put('/settings', { enabled_tools: S.enabledTools });
  } catch (e) { toast('Failed to save tool state', true); }
}

export async function toggleLengthGuard(on) {
  S.lengthGuardEnabled = on;
  S.enabledTools.length_guard = on;
  renderToolsPanel();
  try {
    S.settings = await api.put('/settings', { enabled_tools: S.enabledTools });
  } catch (e) { toast('Failed to save length guard state', true); }
}

export async function toggleLengthGuardEnforce(on) {
  S.lengthGuardEnforce = on;
  S.enabledTools.length_guard_enforce = on;
  renderToolsPanel();
  try {
    S.settings = await api.put('/settings', { enabled_tools: S.enabledTools });
  } catch (e) { toast('Failed to save length guard enforce state', true); }
}

export async function saveLengthGuardConfig() {
  const words = parseInt($('lg-max-words').value, 10);
  const paras = parseInt($('lg-max-paragraphs').value, 10);
  if (!words || !paras || words < 50 || paras < 1) { toast('Invalid length guard values', true); return; }
  S.lengthGuardMaxWords = words;
  S.lengthGuardMaxParagraphs = paras;
  try {
    S.settings = await api.put('/settings', { length_guard_max_words: words, length_guard_max_paragraphs: paras });
    toast('Length guard saved');
  } catch (e) { toast('Failed to save length guard config', true); }
}

export function renderToolsPanel() {
  $('agent-enable-chk').checked = S.agentEnabled;
  $('tools-panel-btn').style.opacity = S.agentEnabled ? '1' : '0.5';
  const toolCards = TOOL_DEFS.map(t => {
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

  const lgOn = S.lengthGuardEnabled;
  const lgEnforce = S.lengthGuardEnforce;
  const lgConfig = lgOn ? `
    <div class="lg-config">
      <div class="lg-config-row">
        <div class="lg-field">
          <label>Max words</label>
          <input id="lg-max-words" type="number" min="50" max="4000" step="50" value="${S.lengthGuardMaxWords}" onchange="saveLengthGuardConfig()">
        </div>
        <div class="lg-field">
          <label>Max sections</label>
          <input id="lg-max-paragraphs" type="number" min="1" max="20" step="1" value="${S.lengthGuardMaxParagraphs}" onchange="saveLengthGuardConfig()">
        </div>
      </div>
      <label class="lg-enforce-label" title="Always suggest max length and paragraphs to the writer.">
        <input type="checkbox" ${lgEnforce ? 'checked' : ''} onchange="toggleLengthGuardEnforce(this.checked)">
        Enforce
      </label>
    </div>` : '';

  const lengthGuardCard = `<div class="tool-card ${lgOn ? 'tool-on' : ''}">
    <div class="tool-card-header">
      <span class="tool-card-name">Length Guard</span>
      <label class="tog" onclick="event.stopPropagation()">
        <input type="checkbox" ${lgOn ? 'checked' : ''} onchange="toggleLengthGuard(this.checked)">
        <span class="tog-slider"></span>
      </label>
    </div>
    <div class="tool-card-desc">Reigns the model's response length by word count. MAX SECTIONS is suggested to the AI in rewrite pass.</div>
    ${lgConfig}
  </div>`;

  $('tools-list').innerHTML = toolCards + lengthGuardCard;
}

// ── Phrase Bank

export async function showPhraseBankModal() {
  const groups = await api.get('/phrase-bank');
  
  const groupRows = groups.map(g => `
    <div class="phrase-group-item" onclick="editPhraseGroup(${g.id})" data-id="${g.id}">
      <div class="phrase-group-variants">
        ${g.variants.map(v => `<span class="phrase-variant">${esc(v)}</span>`).join(', ')}
      </div>
      <div class="phrase-group-count">${g.variants.length} variant${g.variants.length !== 1 ? 's' : ''}</div>
    </div>
  `).join('');
  
  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Phrase Bank</h2>
        <p class="modal-subtitle">Manage banned/overused phrase groups. Each group contains variants that are considered equivalent. Click a group to edit it.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn btn-accent" onclick="showAddPhraseGroupModal()">+ Add Group</button>
      </div>
    </div>
    
    <div id="phrase-bank-list" class="phrase-bank-list">
      ${groupRows.length ? groupRows : '<div class="phrase-bank-empty">No phrase groups yet</div>'}
    </div>
  `);
}

export function showAddPhraseGroupModal(editId = null, initialVariants = []) {
  const isEdit = editId !== null;
  const variantsHtml = initialVariants.map(v => `
    <div class="variant-row">
      <input type="text" class="variant-input" value="${esc(v)}" placeholder="e.g., a mix of">
      <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
    </div>
  `).join('');
  
  const emptyRow = `<div class="variant-row">
    <input type="text" class="variant-input" placeholder="e.g., a mix of">
    <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
  </div>`;
  
  const deleteButton = isEdit ? `
    <button class="btn btn-danger" onclick="deletePhraseGroup(${editId})" style="margin-right: auto;">Delete</button>
  ` : '';
  
  showModal(`
    <h2>${isEdit ? 'Edit' : 'Add'} Phrase Group</h2>
    <p class="modal-subtitle">Enter variant phrases that are considered equivalent. The first variant is treated as the canonical name.</p>
    
    <div id="variant-list" style="margin-bottom: 15px;">
      ${variantsHtml || emptyRow}
    </div>
    
    <button class="btn btn-sm" onclick="addVariantRow()" style="margin-bottom: 20px;">+ Add Another Variant</button>
    
    <div class="modal-actions">
      ${deleteButton}
      <button class="btn" onclick="showPhraseBankModal()">Cancel</button>
      <button class="btn btn-accent" onclick="savePhraseGroup(${editId || 'null'})">${isEdit ? 'Update' : 'Save'}</button>
    </div>
  `);
}

// Helper functions exposed to window
window.addVariantRow = function() {
  const container = document.getElementById('variant-list');
  const row = document.createElement('div');
  row.className = 'variant-row';
  row.innerHTML = `
    <input type="text" class="variant-input" placeholder="e.g., a mix of">
    <button class="btn btn-xs btn-danger" onclick="removeVariantRow(this)">×</button>
  `;
  container.appendChild(row);
  // Focus the new input and scroll it into view
  const input = row.querySelector('.variant-input');
  input.focus();
  // Scroll the modal to show the new row
  row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
};

window.removeVariantRow = function(btn) {
  const rows = document.querySelectorAll('.variant-row');
  if (rows.length > 1) {
    btn.closest('.variant-row').remove();
  } else {
    // If it's the last row, just clear it
    btn.closest('.variant-row').querySelector('.variant-input').value = '';
  }
};

window.editPhraseGroup = async function(groupId) {
  const groups = await api.get('/phrase-bank');
  const group = groups.find(g => g.id === groupId);
  if (group) {
    showAddPhraseGroupModal(groupId, group.variants);
  }
};

window.deletePhraseGroup = async function(groupId) {
  showConfirmModal({
    title: 'Delete Phrase Group',
    message: 'Are you sure you want to delete this phrase group?',
    confirmText: 'Delete',
  }, async () => {
    try {
      await api.del(`/phrase-bank/${groupId}`);
      toast('Phrase group deleted');
      showPhraseBankModal();
    } catch (e) {
      toast('Failed to delete: ' + e.message, true);
    }
  });
};

window.savePhraseGroup = async function(editId) {
  const variantInputs = document.querySelectorAll('.variant-input');
  const variants = Array.from(variantInputs)
    .map(input => input.value.trim())
    .filter(v => v.length > 0);
  
  if (variants.length === 0) {
    toast('At least one variant is required', true);
    return;
  }
  
  try {
    if (editId && editId !== 'null') {
      await api.put(`/phrase-bank/${editId}`, { variants });
      toast('Phrase group updated');
    } else {
      await api.post('/phrase-bank', { variants });
      toast('Phrase group added');
    }
    showPhraseBankModal(); // Refresh the main modal
  } catch (e) {
    toast('Failed to save: ' + e.message, true);
  }
};
