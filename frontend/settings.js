import { S } from './state.js';
import { $, esc, toast } from './utils.js';
import { api } from './api.js';
import { showModal, closeModal } from './modal.js';

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
  renderSettings();
  renderToolsPanel();
  updateUserBtn();
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
  $('user-profile-btn').textContent = '👤 ' + (S.settings.user_name || 'User');
}

export function showUserModal() {
  showModal(`
    <h2>User Profile</h2>
    <div class="field">
      <label>Name</label>
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

// ── Agent Tools Panel
const TOOL_DEFS = [
  { id: 'direct_scene',          name: 'Director',   desc: 'Gives written direction and selects active mood fragments based on scene context' },
  { id: 'rewrite_user_prompt',   name: 'Prompt Rewriter',  desc: 'Expands user\'s vague or lazy messages into richer input' },
  { id: 'refine_apply_patch',    name: 'Output Auditor',   desc: 'Scans for banned phrases, repetitive openers & templates, then surgically patches the draft' },
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
        <button class="btn" onclick="closeModal()">Close</button>
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
  if (!confirm('Delete this phrase group?')) return;
  try {
    await api.del(`/phrase-bank/${groupId}`);
    toast('Phrase group deleted');
    showPhraseBankModal(); // Refresh the modal
  } catch (e) {
    toast('Failed to delete: ' + e.message, true);
  }
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
