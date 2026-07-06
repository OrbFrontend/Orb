import { initAudioPlayer } from "./audio_transport.js";
import {
  applyCompression,
  cancelCompression,
  cancelEdit,
  cancelEditPending,
  cancelForkEdit,
  cancelTitleEdit,
  clearRefineDiff,
  continueFromUser,
  createCheckpoint,
  deleteConversationFromModal,
  deleteMessage,
  generateCompressionSummary,
  handleMagicKey,
  handleTitleEditKey,
  hideAvatarPopup,
  initAutoscroll,
  initChatKeyNav,
  initChatSwipeNav,
  initWorkflowMutationListener,
  loadConversations,
  loadWorkflowManifest,
  newConvForChar,
  regenerate,
  renderMessages,
  saveEdit,
  saveEditPending,
  saveForkEdit,
  saveInspectorOpenStates,
  saveTitleEdit,
  selectChar,
  selectConversation,
  selectReasoningPass,
  selectWorkflowPipelinePass,
  sendMessage,
  setInspectorTab,
  setToolsTab,
  showAvatarPopup,
  showCompressModal,
  showConvHistoryModal,
  startEdit,
  startEditPending,
  startEditTitle,
  startForkEdit,
  stopGeneration,
  submitMagicRewrite,
  superRegenerate,
  switchBranch,
  toggleInspector,
  toggleMagicInput,
  toggleReasoningPass,
} from "./chat.js";
import { initComposer, triggerAttachImage } from "./chat_composer.js";
import {
  addAltGreeting,
  charTagInput,
  charTagKeydown,
  charTagRemoveChip,
  clearExpressions,
  createCharacter,
  deleteCharacter,
  deleteInteractiveFragment,
  deleteMoodFragment,
  exportCharacter,
  handleExpressionsZip,
  handleImportFile,
  importInternetChar,
  loadCharacters,
  loadInteractiveFragments,
  loadMoodFragments,
  loadMoreInternet,
  onCharBrowserSearch,
  randomizeInternet,
  refreshCharacters,
  renderCharacters,
  saveCharEdit,
  saveImportedChar,
  saveInteractiveFragment,
  saveMoodFragment,
  searchInternet,
  setCharBrowserSort,
  setCharBrowserView,
  setInternetSource,
  showCharacterBrowserModal,
  showCharCreateModal,
  showCharEditModal,
  showInteractiveFragmentModal,
  showMoodFragmentModal,
  toggleInteractiveFragmentEnabled,
  toggleMoodFragmentEnabled,
  toggleTagSelection,
  triggerAvatarCrop,
  triggerImport,
  updateInteractiveFragmentExample,
} from "./library.js";
import {
  closeLorebook,
  collapseWorlds,
  createWorld,
  deleteWorld,
  expandWorlds,
  lbAddEntry,
  lbBackToList,
  lbChipInput,
  lbChipKeydown,
  lbDeleteEntry,
  lbDiscardChanges,
  lbDraftChange,
  lbEntrySearch,
  lbImportJson,
  lbRemoveChip,
  lbSaveEntry,
  lbSelectEntry,
  lbToggleConstant,
  lbToggleEntry,
  loadWorlds,
  onWorldSearch,
  openLorebook,
  renameWorld,
  renderWorldsSidebar,
  showCreateWorldModal,
  showRenameWorldModal,
  toggleWorldEnabled,
} from "./lorebooks.js";
import { closeMobileHeaderActions, initMobileUi, toggleMobileHeaderActions, toggleMobileSidebar } from "./mobile.js";
import {
  closeCropModal,
  closeModal,
  closeSubModal,
  runConfirmCb,
  runSubConfirmCb,
  showConfirmModal,
  switchTab,
} from "./modal.js";
import {
  applyPreset,
  deletePreset,
  doCreateSnapshot,
  downloadPreset,
  handlePresetImportFile,
  onPresetDomainChange,
  refreshPresetLibrary,
  restorePreset,
  showPresetsModal,
  showSnapshotModal,
  triggerPresetImport,
} from "./presets.js";
import {
  activatePersona,
  applyTheme,
  deletePersona,
  downloadLocalMlModel,
  editPersona,
  initTheme,
  initThemeList,
  loadSettings,
  onHybridInput,
  toggleLocalMlEnabled,
  saveLengthGuardConfig,
  savePersona,
  saveSetting,
  saveUserProfile,
  setAgentEnabled,
  setDirectionNotesInject,
  setDirectionNotesRecord,
  setPersonaCharacterLock,
  setPersonaConversationLock,
  showAddPhraseGroupModal,
  showPersonaEditModal,
  showPhraseBankModal,
  showUserModal,
  toggleAgenticLorebook,
  toggleAuditType,
  toggleDirectorIndividualFragments,
  toggleFeedbackEnabled,
  toggleHideUntilBaked,
  toggleLengthGuard,
  toggleLengthGuardEnforce,
  togglePreventPromptOverrides,
  toggleShowEditorDiff,
  toggleToolEnabled,
  toggleToolsPanel,
  toggleWorkflowEnabled,
  toggleWorkflowsGlobal,
} from "./settings.js";
import {
  addUserDirectionNote,
  deleteDirectionNote,
  editDirectionNote,
  saveDirectionNote,
  saveUserDirectionNote,
  toggleDirectionNotesPanel,
} from "./direction_notes_panel.js";
import { scoreSlop } from "./slop_score.js";
import { S } from "./state.js";
import { initTabLock, setLockStateChangeCallback } from "./tabLock.js";
import { $ } from "./utils.js";
import { loadWorkflowModules } from "./workflow_loader.js";
import { initWorkflowTextInteraction } from "./workflow_text_interaction.js";

// ── Sidebar toggle
function toggleSection(header) {
  header.querySelector(".arrow").classList.toggle("collapsed");
  header.nextElementSibling.classList.toggle("collapsed");
}
window.toggleSection = toggleSection;

// ── Burger menu
function toggleBurger() {
  $("burger-dropdown").classList.toggle("open");
}
function closeBurger() {
  $("burger-dropdown").classList.remove("open");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#burger-btn") && !e.target.closest("#burger-dropdown")) closeBurger();
});

// ── Expose to inline handlers
Object.assign(window, {
  // modal
  closeModal,
  closeSubModal,
  switchTab,
  showConfirmModal,
  runConfirmCb,
  runSubConfirmCb,
  // theme
  applyTheme,
  // settings / user
  saveSetting,
  onHybridInput,
  showUserModal,
  saveUserProfile,
  showPersonaEditModal,
  savePersona,
  deletePersona,
  editPersona,
  activatePersona,
  setPersonaConversationLock,
  setPersonaCharacterLock,
  // tools
  toggleToolsPanel,
  setAgentEnabled,
  toggleToolEnabled,
  toggleLengthGuard,
  saveLengthGuardConfig,
  toggleLengthGuardEnforce,
  toggleAgenticLorebook,
  toggleFeedbackEnabled,
  toggleDirectorIndividualFragments,
  setDirectionNotesRecord,
  setDirectionNotesInject,
  toggleDirectionNotesPanel,
  addUserDirectionNote,
  editDirectionNote,
  saveDirectionNote,
  saveUserDirectionNote,
  deleteDirectionNote,
  toggleShowEditorDiff,
  toggleAuditType,
  toggleHideUntilBaked,
  togglePreventPromptOverrides,
  toggleWorkflowsGlobal,
  toggleWorkflowEnabled,
  downloadLocalMlModel,
  toggleLocalMlEnabled,
  scoreSlop,
  // phrase bank
  showPhraseBankModal,
  showAddPhraseGroupModal,
  // presets / backups
  showPresetsModal,
  showSnapshotModal,
  onPresetDomainChange,
  doCreateSnapshot,
  triggerPresetImport,
  handlePresetImportFile,
  downloadPreset,
  applyPreset,
  restorePreset,
  deletePreset,
  refreshPresetLibrary,
  // mood fragments
  showMoodFragmentModal,
  saveMoodFragment,
  deleteMoodFragment,
  toggleMoodFragmentEnabled,
  // interactive fragments
  showInteractiveFragmentModal,
  saveInteractiveFragment,
  deleteInteractiveFragment,
  toggleInteractiveFragmentEnabled,
  updateInteractiveFragmentExample,
  // characters
  selectChar,
  triggerImport,
  handleImportFile,
  deleteCharacter,
  showCharCreateModal,
  createCharacter,
  showCharEditModal,
  saveCharEdit,
  saveImportedChar,
  addAltGreeting,
  triggerAvatarCrop,
  exportCharacter,
  handleExpressionsZip,
  clearExpressions,
  showCharacterBrowserModal,
  setCharBrowserView,
  onCharBrowserSearch,
  setCharBrowserSort,
  toggleTagSelection,
  searchInternet,
  loadMoreInternet,
  setInternetSource,
  importInternetChar,
  randomizeInternet,
  refreshCharacters,
  charTagKeydown,
  charTagInput,
  charTagRemoveChip,
  // crop modal
  closeCropModal,
  // conversations
  newConvForChar,
  selectConversation,
  deleteConversationFromModal,
  showConvHistoryModal,
  showCompressModal,
  createCheckpoint,
  generateCompressionSummary,
  cancelCompression,
  applyCompression,
  // title edit
  startEditTitle,
  saveTitleEdit,
  cancelTitleEdit,
  handleTitleEditKey,
  // messages
  startEdit,
  cancelEdit,
  saveEdit,
  startForkEdit,
  cancelForkEdit,
  saveForkEdit,
  startEditPending,
  cancelEditPending,
  saveEditPending,
  deleteMessage,
  switchBranch,
  regenerate,
  superRegenerate,
  toggleMagicInput,
  handleMagicKey,
  submitMagicRewrite,
  continueFromUser,
  sendMessage,
  stopGeneration,
  // inspector
  toggleInspector,
  selectReasoningPass,
  toggleReasoningPass,
  clearRefineDiff,
  saveInspectorOpenStates,
  setInspectorTab,
  setToolsTab,
  selectWorkflowPipelinePass,
  // ui
  toggleSection,
  toggleMobileSidebar,
  toggleMobileHeaderActions,
  closeMobileHeaderActions,
  toggleBurger,
  closeBurger,
  triggerAttachImage,
  showAvatarPopup,
  hideAvatarPopup,
  // worlds / lorebook
  showCreateWorldModal,
  createWorld,
  showRenameWorldModal,
  renameWorld,
  toggleWorldEnabled,
  deleteWorld,
  openLorebook,
  closeLorebook,
  onWorldSearch,
  expandWorlds,
  collapseWorlds,
  lbEntrySearch,
  lbSelectEntry,
  lbToggleEntry,
  lbAddEntry,
  lbBackToList,
  lbDeleteEntry,
  lbSaveEntry,
  lbDiscardChanges,
  lbDraftChange,
  lbToggleConstant,
  lbChipKeydown,
  lbChipInput,
  lbRemoveChip,
  lbImportJson,
  // state
  S,
});

// ── Init
initTheme();
initThemeList();
initComposer();
initChatKeyNav();
initAutoscroll();
initChatSwipeNav();
initWorkflowTextInteraction();
initAudioPlayer();
initTabLock();
// Re-render messages when tab lock state changes to update toolbar buttons
setLockStateChangeCallback((hasMultipleTabs) => {
  if (S.activeConvId && !S.isStreaming) {
    renderMessages();
  }
});
initWorkflowMutationListener();

// On a fresh load with no conversation selected, render the JS empty state so
// the homepage stats grid appears (index.html ships a static empty state).
if (!S.activeConvId) {
  renderMessages();
}

// Load data independently to prevent failures from blocking other loads
async function initAll() {
  initMobileUi({ closeBurger });

  try {
    await loadSettings();
  } catch (e) {
    console.error("Failed to load settings:", e);
  }

  try {
    await loadInteractiveFragments();
  } catch (e) {
    console.error("Failed to load interactive fragments:", e);
  }

  try {
    await loadMoodFragments();
  } catch (e) {
    console.error("Failed to load mood fragments:", e);
    // Show empty state but don't crash
    $("frag-list").innerHTML =
      '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">Failed to load mood fragments</div>';
  }

  // Load conversations before characters so we can filter by recent activity
  try {
    await loadConversations();
  } catch (e) {
    console.error("Failed to load conversations:", e);
  }

  try {
    await loadCharacters();
  } catch (e) {
    console.error("Failed to load characters:", e);
  }

  try {
    await loadWorlds();
  } catch (e) {
    console.error("Failed to load worlds:", e);
  }

  try {
    await loadWorkflowManifest();
  } catch (e) {
    console.error("Failed to load workflow manifest:", e);
  }

  try {
    await loadWorkflowModules();
  } catch (e) {
    console.error("Failed to load workflow modules:", e);
  }
}

// Start initialization
initAll();
