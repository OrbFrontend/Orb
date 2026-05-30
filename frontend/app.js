import {
  applyCompression,
  cancelCompression,
  cancelEdit,
  cancelEditPending,
  cancelTitleEdit,
  clearRefineDiff,
  continueFromUser,
  deleteConversationFromModal,
  deleteMessage,
  generateCompressionSummary,
  handleChatKeyNav,
  handleMagicKey,
  handleTitleEditKey,
  initChatSwipeNav,
  initWorkflowMutationListener,
  hideAvatarPopup,
  loadConversations,
  loadWorkflowManifest,
  newConvForChar,
  regenerate,
  renderMessages,
  resetChatUI,
  saveEdit,
  saveEditPending,
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
  stopGeneration,
  submitMagicRewrite,
  superRegenerate,
  switchBranch,
  toggleInspector,
  toggleMagicInput,
  toggleReasoningPass,
  saveInspectorOpenStates,
} from "./chat.js";
import {
  addAltGreeting,
  charTagInput,
  charTagKeydown,
  charTagRemoveChip,
  createCharacter,
  deleteCharacter,
  deleteDirectorFragment,
  deleteMoodFragment,
  exportCharacter,
  handleImportFile,
  importInternetChar,
  loadCharacters,
  loadMoreInternet,
  loadDirectorFragments,
  loadMoodFragments,
  onCharBrowserSearch,
  refreshCharacters,
  renderCharacters,
  saveCharEdit,
  saveDirectorFragment,
  saveImportedChar,
  saveMoodFragment,
  searchInternet,
  setCharBrowserSort,
  setCharBrowserView,
  setInternetSource,
  showCharacterBrowserModal,
  showCharCreateModal,
  showCharEditModal,
  showDirectorFragmentModal,
  showMoodFragmentModal,
  toggleDirectorFragmentEnabled,
  toggleMoodFragmentEnabled,
  toggleTagSelection,
  triggerAvatarCrop,
  triggerImport,
} from "./library.js";
import {
  closeLorebook,
  createWorld,
  deleteWorld,
  lbAddEntry,
  lbChipInput,
  lbChipKeydown,
  lbDeleteEntry,
  lbDiscardChanges,
  lbDraftChange,
  lbImportJson,
  lbRemoveChip,
  lbSaveEntry,
  lbSelectEntry,
  lbToggleConstant,
  lbToggleEntry,
  loadWorlds,
  openLorebook,
  renameWorld,
  renderWorldsSidebar,
  showCreateWorldModal,
  showRenameWorldModal,
  toggleWorldEnabled,
} from "./lorebooks.js";
import { closeMobileHeaderActions, initMobileUi, toggleMobileHeaderActions, toggleMobileSidebar } from "./mobile.js";
import { closeCropModal, closeModal, runConfirmCb, showConfirmModal, switchTab } from "./modal.js";
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
  showAddPhraseGroupModal,
  showPersonaEditModal,
  showPhraseBankModal,
  showUserModal,
  toggleHideUntilBaked,
  toggleLengthGuard,
  toggleLengthGuardEnforce,
  togglePreventPromptOverrides,
  toggleShowEditorDiff,
  toggleToolEnabled,
  toggleToolsPanel,
} from "./settings.js";
import { S } from "./state.js";
import { initTabLock, setLockStateChangeCallback } from "./tabLock.js";
import { $, formatBytes } from "./utils.js";
import { validate } from "./validate.js";
import { loadWorkflowModules } from "./workflow_loader.js";
import { initWorkflowTextInteraction } from "./workflow_text_interaction.js";
import { initAudioPlayer } from "./audio_transport.js";

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

function triggerAttachImage() {
  $("attach-image-input").click();
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#burger-btn") && !e.target.closest("#burger-dropdown")) closeBurger();
});

// Attachments handling
function handleAttachmentSelect(e) {
  const files = Array.from(e.target.files);
  if (files.length === 0) return;

  const validation = validate.validateImageFiles(files, 10, 10 * 1024 * 1024, 20 * 1024 * 1024);
  if (!validation.valid) {
    toast(validation.error, true);
    e.target.value = "";
    return;
  }

  for (const file of files) {
    const fileValidation = validate.validateImageFile(file, 10 * 1024 * 1024, [
      "image/png",
      "image/jpeg",
      "image/webp",
      "image/gif",
    ]);
    if (!fileValidation.valid) {
      toast(fileValidation.error, true);
      continue;
    }
    const reader = new FileReader();
    reader.onload = (event) => {
      const b64 = event.target.result.split(",")[1]; // strip data:image/...;base64,
      S.attachments.push({
        b64,
        mime: file.type,
        filename: file.name,
        size: file.size,
      });
      updateAttachmentPreview();
    };
    reader.readAsDataURL(file);
  }
  e.target.value = ""; // allow re-selecting same file
}

function updateAttachmentPreview() {
  const container = $("attachment-preview");
  container.innerHTML = "";
  S.attachments.forEach((att, idx) => {
    const item = document.createElement("div");
    item.className = "attachment-item";
    const img = document.createElement("img");
    img.src = `data:${att.mime};base64,${att.b64}`;
    const info = document.createElement("div");
    info.className = "attachment-info";
    const name = document.createElement("div");
    name.className = "attachment-name";
    name.textContent = att.filename || "image";
    const size = document.createElement("div");
    size.className = "attachment-size";
    size.textContent = formatBytes(att.size);
    info.appendChild(name);
    info.appendChild(size);
    const removeBtn = document.createElement("button");
    removeBtn.className = "attachment-remove";
    removeBtn.innerHTML = "×";
    removeBtn.title = "Remove";
    removeBtn.onclick = () => {
      S.attachments.splice(idx, 1);
      updateAttachmentPreview();
    };
    item.appendChild(img);
    item.appendChild(info);
    item.appendChild(removeBtn);
    container.appendChild(item);
  });
}

// File input change listener
$("attach-image-input").addEventListener("change", handleAttachmentSelect);

// ── Input events
$("chat-input").addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 150) + "px";
});
$("chat-input").addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const validation = validate.validateChatInput(this.value);
    if (!validation.valid) {
      toast(validation.error, true);
      return;
    }
    sendMessage();
  }
});

document.addEventListener("keydown", handleChatKeyNav);

// ── Expose to inline handlers
Object.assign(window, {
  // modal
  closeModal,
  switchTab,
  showConfirmModal,
  runConfirmCb,
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
  // tools
  toggleToolsPanel,
  setAgentEnabled,
  toggleToolEnabled,
  toggleLengthGuard,
  saveLengthGuardConfig,
  toggleLengthGuardEnforce,
  toggleShowEditorDiff,
  toggleHideUntilBaked,
  togglePreventPromptOverrides,
  // phrase bank
  showPhraseBankModal,
  showAddPhraseGroupModal,
  // mood fragments
  showMoodFragmentModal,
  saveMoodFragment,
  deleteMoodFragment,
  toggleMoodFragmentEnabled,
  // director fragments
  showDirectorFragmentModal,
  saveDirectorFragment,
  deleteDirectorFragment,
  toggleDirectorFragmentEnabled,
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
  showCharacterBrowserModal,
  setCharBrowserView,
  onCharBrowserSearch,
  setCharBrowserSort,
  toggleTagSelection,
  searchInternet,
  loadMoreInternet,
  setInternetSource,
  importInternetChar,
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
  updateAttachmentPreview,
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
  lbSelectEntry,
  lbToggleEntry,
  lbAddEntry,
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

// ── Smart autoscroll: disable on upward scroll, re-enable when back at bottom
function initAutoscroll() {
  const ct = $("chat-messages");
  if (!ct) return;
  const THRESHOLD = 20;
  let scrollDebounce = null;

  // Wheel: immediately cut autoscroll on any upward scroll intent
  ct.addEventListener(
    "wheel",
    (e) => {
      if (e.deltaY < 0) S.autoscrollEnabled = false;
    },
    { passive: true },
  );

  // Touch: disable on upward swipe
  let touchStartY = 0;
  ct.addEventListener(
    "touchstart",
    (e) => {
      touchStartY = e.touches[0].clientY;
    },
    { passive: true },
  );
  ct.addEventListener(
    "touchmove",
    (e) => {
      if (e.touches[0].clientY > touchStartY) S.autoscrollEnabled = false;
    },
    { passive: true },
  );

  // Re-enable only once the user has scrolled back to the bottom (debounced to
  // avoid false positives from rapid programmatic scroll events during streaming)
  ct.addEventListener("scroll", () => {
    if (S._programmaticScroll) return;
    clearTimeout(scrollDebounce);
    scrollDebounce = setTimeout(() => {
      const atBottom = ct.scrollHeight - ct.scrollTop - ct.clientHeight <= THRESHOLD;
      if (atBottom) S.autoscrollEnabled = true;
    }, 100);
  });
}

// ── Init
initTheme();
initThemeList();
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

// Load data independently to prevent failures from blocking other loads
async function initAll() {
  initMobileUi({ closeBurger });

  try {
    await loadSettings();
  } catch (e) {
    console.error("Failed to load settings:", e);
  }

  try {
    await loadDirectorFragments();
  } catch (e) {
    console.error("Failed to load director fragments:", e);
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
