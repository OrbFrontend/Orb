import { $ } from './utils.js';
import { S } from './state.js';
import { initTheme, loadSettings, saveSetting, showUserModal, saveUserProfile, applyTheme,
         toggleToolsPanel, setAgentEnabled, toggleToolEnabled,
         toggleLengthGuard, saveLengthGuardConfig } from './settings.js';
import { loadFragments, showFragmentModal, saveFragment, deleteFragment,
         loadCharacters, renderCharacters, triggerImport, handleImportFile,
         deleteCharacter, showCharCreateModal, createCharacter,
         showCharEditModal, saveCharEdit } from './library.js';
import { loadConversations, resetChatUI, selectChar, newConvForChar,
         selectConversation, deleteConversationFromModal, showConvHistoryModal,
         renderMessages, startEdit, cancelEdit, saveEdit, deleteMessage,
         switchBranch, regenerate, sendMessage, stopGeneration,
         toggleInspector } from './chat.js';
import { closeModal, switchTab } from './modal.js';

// ── Sidebar toggle ───────────────────────────
function toggleSection(header) {
  header.querySelector('.arrow').classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

// ── Burger menu ──────────────────────────────
function toggleBurger() { $('burger-dropdown').classList.toggle('open'); }
function closeBurger()  { $('burger-dropdown').classList.remove('open'); }

document.addEventListener('click', e => {
  if (!e.target.closest('#burger-btn') && !e.target.closest('#burger-dropdown')) closeBurger();
});

// ── Input events ─────────────────────────────
$('chat-input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 150) + 'px';
});
$('chat-input').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ── Expose to inline handlers ────────────────
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
  // fragments
  showFragmentModal, saveFragment, deleteFragment,
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

// ── Init ─────────────────────────────────────
initTheme();
await loadSettings();
await loadFragments();
await loadCharacters();
await loadConversations();