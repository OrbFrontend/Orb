import { $ } from './utils.js';
import { S } from './state.js';
import { initTheme, loadSettings, saveSetting, showUserModal, saveUserProfile, applyTheme,
         toggleToolsPanel, setAgentEnabled, toggleToolEnabled,
         toggleLengthGuard, saveLengthGuardConfig, showPhraseBankModal } from './settings.js';
import { loadFragments, showFragmentModal, saveFragment, deleteFragment, toggleFragmentEnabled,
         loadCharacters, renderCharacters, triggerImport, handleImportFile,
         deleteCharacter, showCharCreateModal, createCharacter,
         showCharEditModal, saveCharEdit } from './library.js';
import { loadConversations, resetChatUI, selectChar, newConvForChar,
         selectConversation, deleteConversationFromModal, showConvHistoryModal,
         renderMessages, startEdit, cancelEdit, saveEdit, deleteMessage,
         switchBranch, regenerate, sendMessage, stopGeneration,
         toggleInspector } from './chat.js';
import { closeModal, switchTab } from './modal.js';

// ── Sidebar toggle
function toggleSection(header) {
  header.querySelector('.arrow').classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

// ── Burger menu
function toggleBurger() { $('burger-dropdown').classList.toggle('open'); }
function closeBurger()  { $('burger-dropdown').classList.remove('open'); }

document.addEventListener('click', e => {
  if (!e.target.closest('#burger-btn') && !e.target.closest('#burger-dropdown')) closeBurger();
});

// ── Input events
$('chat-input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 150) + 'px';
});
$('chat-input').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ── Expose to inline handlers
Object.assign(window, {
  // modal
  closeModal, switchTab,
  // theme
  applyTheme,
  // settings / user
  saveSetting, showUserModal, saveUserProfile,
  // tools
  toggleToolsPanel, setAgentEnabled, toggleToolEnabled,
  toggleLengthGuard, saveLengthGuardConfig,
  // phrase bank
  showPhraseBankModal,
  // fragments
  showFragmentModal, saveFragment, deleteFragment, toggleFragmentEnabled,
  // characters
  selectChar, triggerImport, handleImportFile, deleteCharacter,
  showCharCreateModal, createCharacter, showCharEditModal, saveCharEdit,
  // conversations
  newConvForChar, selectConversation, deleteConversationFromModal, showConvHistoryModal,
  // messages
  startEdit, cancelEdit, saveEdit, deleteMessage, switchBranch, regenerate,
  sendMessage, stopGeneration,
  // inspector
  toggleInspector,
  // ui
  toggleSection, toggleBurger, closeBurger,
  // state
  S,
});

// ── Init
initTheme();

// Load data independently to prevent failures from blocking other loads
async function initAll() {
  try {
    await loadSettings();
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
  
  try {
    await loadFragments();
  } catch (e) {
    console.error('Failed to load fragments:', e);
    // Show empty state but don't crash
    $('frag-list').innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">Failed to load fragments</div>';
  }
  
  try {
    await loadCharacters();
  } catch (e) {
    console.error('Failed to load characters:', e);
  }
  
  try {
    await loadConversations();
  } catch (e) {
    console.error('Failed to load conversations:', e);
  }
}

// Start initialization
initAll();