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
  newConversationHere,
  newConvForChar,
  refreshConversationMessages,
  regenerate,
  renderMessages,
  rewriteMessageProse,
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
import { initGroupSetup } from "./group_setup.js";
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
  deactivateLinkedWorlds,
  deleteWorld,
  expandWorlds,
  initWorldProposalActions,
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
  setWorldProposalRefresh,
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
  togglePreventPromptOverrides,
  toggleShowChatAvatars,
  toggleShowEditorDiff,
  toggleToolEnabled,
  toggleToolsPanel,
  toggleWorkflowEnabled,
  toggleWorkflowsGlobal,
} from "./settings.js";
import { scoreSlop } from "./slop_score.js";
import { S } from "./state.js";
import { initTabLock } from "./tabLock.js";
import { $ } from "./utils.js";
import { loadWorkflowModules } from "./workflow_loader.js";
import { initWorkflowTextInteraction } from "./workflow_text_interaction.js";

function toggleSection(header) {
  header.querySelector(".arrow").classList.toggle("collapsed");
  header.nextElementSibling.classList.toggle("collapsed");
}
window.toggleSection = toggleSection;

function toggleBurger() {
  $("burger-dropdown").classList.toggle("open");
}
function closeBurger() {
  $("burger-dropdown").classList.remove("open");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#burger-btn") && !e.target.closest("#burger-dropdown")) closeBurger();
});

document.addEventListener("click", (e) => {
  const item = e.target.closest("[data-chat-action]");
  if (!item) return;
  closeBurger();
  closeMobileHeaderActions();
  if (item.dataset.chatAction === "inspector") toggleInspector();
  else document.dispatchEvent(new CustomEvent(`${item.dataset.chatAction}-request`));
});

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

Object.assign(window, {
  closeModal,
  closeSubModal,
  switchTab,
  showConfirmModal,
  runConfirmCb,
  runSubConfirmCb,
  applyTheme,
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
  toggleShowChatAvatars,
  toggleWorkflowsGlobal,
  toggleWorkflowEnabled,
  scoreSlop,
  showPhraseBankModal,
  showAddPhraseGroupModal,
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
  showMoodFragmentModal,
  saveMoodFragment,
  deleteMoodFragment,
  toggleMoodFragmentEnabled,
  showInteractiveFragmentModal,
  saveInteractiveFragment,
  deleteInteractiveFragment,
  toggleInteractiveFragmentEnabled,
  updateInteractiveFragmentExample,
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
  closeCropModal,
  newConvForChar,
  newConversationHere,
  selectConversation,
  deleteConversationFromModal,
  showConvHistoryModal,
  showCompressModal,
  createCheckpoint,
  generateCompressionSummary,
  cancelCompression,
  applyCompression,
  startEditTitle,
  saveTitleEdit,
  cancelTitleEdit,
  handleTitleEditKey,
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
  rewriteMessageProse,
  superRegenerate,
  toggleMagicInput,
  handleMagicKey,
  submitMagicRewrite,
  continueFromUser,
  sendMessage,
  stopGeneration,
  toggleInspector,
  selectReasoningPass,
  toggleReasoningPass,
  clearRefineDiff,
  saveInspectorOpenStates,
  setInspectorTab,
  setToolsTab,
  selectWorkflowPipelinePass,
  toggleSection,
  toggleMobileSidebar,
  toggleMobileHeaderActions,
  closeMobileHeaderActions,
  toggleBurger,
  closeBurger,
  triggerAttachImage,
  showAvatarPopup,
  hideAvatarPopup,
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
  S,
});

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
initGroupSetup();

if (!S.activeConvId) {
  renderMessages();
}

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
    $("frag-list").innerHTML =
      '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">Failed to load mood fragments</div>';
  }

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

  setWorldProposalRefresh(refreshConversationMessages);
  initWorldProposalActions();
  try {
    await deactivateLinkedWorlds();
  } catch (e) {
    console.error("Failed to deactivate linked worlds:", e);
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
}

initAll();
