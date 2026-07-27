import { initAudioPlayer } from "./audio_transport.js";
import {
  applyBranchSwitchRefresh,
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
  addUserDirectionNote,
  deleteDirectionNote,
  editDirectionNote,
  renderDirectionNotesPanel,
  saveDirectionNote,
  saveUserDirectionNote,
  toggleDirectionNotesPanel,
} from "./direction_notes_panel.js";
import {
  collapseDocs,
  createDocument,
  deleteDocument,
  docGenerate,
  docRedo,
  docStop,
  docUndo,
  expandDocs,
  initDocumentMode,
  loadDocuments,
  onDocSearch,
  openDocument,
  renameActiveDocument,
  renameDocument,
  setDocAssisted,
  setDocProbs,
  toggleDocumentMode,
} from "./document.js";
import {
  initExtensionCommands,
  renderCommandSlots,
  renderComposerMenu,
  renderMobileActionsMenu,
} from "./extension_commands.js";
import { initExtensionManager, loadExtensionCatalog } from "./extension_manager.js";
import {
  addAltGreeting,
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
  lbDeleteEntry,
  lbDiscardChanges,
  lbDraftChange,
  lbEntrySearch,
  lbImportJson,
  lbSaveEntry,
  lbSelectEntry,
  lbToggleConstant,
  lbToggleEntry,
  loadWorlds,
  onWorldSearch,
  openLorebook,
  renameWorld,
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
  toggleLocalMlEnabled,
  togglePreventPromptOverrides,
  toggleShowEditorDiff,
  toggleToolEnabled,
  toggleToolsPanel,
  toggleWorkflowEnabled,
  toggleWorkflowsGlobal,
} from "./settings.js";
import { personaMenuLabel } from "./settings_personas.js";
import { scoreSlop } from "./slop_score.js";
import { S } from "./state.js";
import { initTabLock } from "./tabLock.js";
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
//
// The item list is a host command model rather than fixed markup: Orb's own
// entries and the enabled extensions' `composer.menu` placements render through
// one path in extension_commands.js. The shell supplies the built-ins here
// because it already imports every feature they call; extension_commands.js
// must not reach up into the chat layer to find them.
//
// `visible` and function labels exist so an entry that used to be toggled by
// someone reaching into the DOM (the dormant Notes button, the live persona
// label) is now just part of the model.
const BUILTIN_COMMANDS = {
  "composer.menu": [
    { id: "new-conversation", glyph: "✚", label: "New conversation", run: () => newConvForChar(S.activeCharId) },
    { id: "conversations", glyph: "📋", label: "Conversations", run: () => showConvHistoryModal() },
    { id: "compress", glyph: "📦", label: "Compress History", run: () => showCompressModal() },
    { id: "checkpoint", glyph: "🔖", label: "Create Checkpoint", run: () => createCheckpoint() },
    { id: "attach-image", glyph: "🖼️", label: "Attach Image", run: () => triggerAttachImage() },
  ],
  "mobile.chat_actions": [
    { id: "user", glyph: "👤", label: () => personaMenuLabel(), run: () => showUserModal() },
    { id: "workflow", glyph: "✨", label: "Workflow", run: () => toggleToolsPanel() },
    { id: "inspector", glyph: "🔍", label: "Inspector", run: () => toggleInspector() },
    {
      id: "direction-notes",
      glyph: "📝",
      label: "Notes",
      visible: () => S.directionNotesRecord || S.directionNotesInject !== "off",
      run: () => toggleDirectionNotesPanel(),
    },
  ],
};

function toggleBurger() {
  // Repaint on open rather than on every catalog change: the model is cheap to
  // build and this way a stale menu is impossible by construction.
  renderComposerMenu();
  $("burger-dropdown").classList.toggle("open");
}
function closeBurger() {
  $("burger-dropdown").classList.remove("open");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#burger-btn") && !e.target.closest("#burger-dropdown")) closeBurger();
});

// ── Image lightbox: click a generated image to pop it out full-screen; click
// anywhere or press Escape to dismiss. Built as DOM nodes (not innerHTML) so the
// data: src and alt need no escaping.
document.addEventListener("click", (e) => {
  const src = e.target.closest(".workflow-artifact-image");
  if (!src) return;
  const box = document.createElement("div");
  box.className = "image-lightbox";
  const big = document.createElement("img");
  big.src = src.src;
  big.alt = src.alt;
  box.appendChild(big);
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
  };
  const close = () => {
    box.remove();
    document.removeEventListener("keydown", onKey);
  };
  box.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(box);
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
  // document mode
  toggleDocumentMode,
  setDocAssisted,
  setDocProbs,
  createDocument,
  openDocument,
  deleteDocument,
  renameDocument,
  renameActiveDocument,
  onDocSearch,
  expandDocs,
  collapseDocs,
  docGenerate,
  docStop,
  docUndo,
  docRedo,
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
initWorkflowMutationListener();

// On a fresh load with no conversation selected, render the JS empty state so
// the homepage stats grid appears (index.html ships a static empty state).
if (!S.activeConvId) {
  renderMessages();
}

// Load data independently to prevent failures from blocking other loads
async function initAll() {
  initMobileUi({ closeBurger, renderMobileActions: renderMobileActionsMenu });

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

  initDocumentMode();
  try {
    await loadDocuments();
  } catch (e) {
    console.error("Failed to load documents:", e);
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

  // Extensions load last and never gate anything: the catalog is metadata for
  // an Orb-owned panel, and community entries are declarative data that the
  // loader above has already refused to import(). A failure here costs the
  // Extensions sidebar, not the app.
  // The shell owns this wiring because it is the only module that may import
  // both the chat feature layer (for the refetches an effect maps to) and the
  // extension layer. extension_commands.js receives callables, so it never
  // needs an upward import of its own.
  initExtensionCommands({
    builtins: BUILTIN_COMMANDS,
    refetch: {
      // A branch activation lands the frontend exactly where the built-in
      // switch-branch button does — same refetch, same Inspector cleanup —
      // because both go through the one helper chat_messages.js exports.
      conversation: async () => {
        await applyBranchSwitchRefresh(null, { refetchMessages: true });
      },
      directionNotes: async () => {
        await renderDirectionNotesPanel();
      },
      characters: () => {
        void loadCharacters();
      },
      catalog: async () => {
        await loadExtensionCatalog();
      },
    },
  });
  initExtensionManager();
  try {
    await loadExtensionCatalog();
  } catch (e) {
    console.error("Failed to load extension catalog:", e);
  }
  renderCommandSlots();
}

// Start initialization
initAll();
