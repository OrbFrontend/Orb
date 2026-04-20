import { $ } from "./utils.js";
import { S } from "./state.js";
import {
  initTheme,
  loadSettings,
  saveSetting,
  showUserModal,
  saveUserProfile,
  applyTheme,
  toggleToolsPanel,
  setAgentEnabled,
  toggleToolEnabled,
  toggleLengthGuard,
  saveLengthGuardConfig,
  toggleLengthGuardEnforce,
  showPhraseBankModal,
  showAddPhraseGroupModal,
  showPersonaEditModal,
  savePersona,
  deletePersona,
  editPersona,
  activatePersona,
} from "./settings.js";
import {
  loadFragments,
  showFragmentModal,
  saveFragment,
  deleteFragment,
  toggleFragmentEnabled,
  loadDirectorFragments,
  showDirectorFragmentModal,
  saveDirectorFragment,
  deleteDirectorFragment,
  toggleDirectorFragmentEnabled,
  loadCharacters,
  renderCharacters,
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
  refreshCharacters,
} from "./library.js";
import {
  loadConversations,
  resetChatUI,
  selectChar,
  newConvForChar,
  selectConversation,
  deleteConversationFromModal,
  showConvHistoryModal,
  renderMessages,
  startEdit,
  cancelEdit,
  saveEdit,
  deleteMessage,
  switchBranch,
  regenerate,
  sendMessage,
  stopGeneration,
  toggleInspector,
  selectReasoningPass,
  toggleReasoningPass,
  clearRefineDiff,
  showAvatarPopup,
  hideAvatarPopup,
} from "./chat.js";
import { closeModal, switchTab, showConfirmModal, runConfirmCb, closeCropModal } from "./modal.js";

// ── Sidebar toggle
function toggleSection(header) {
  header.querySelector(".arrow").classList.toggle("collapsed");
  header.nextElementSibling.classList.toggle("collapsed");
}

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
  for (const file of files) {
    if (!file.type.startsWith("image/")) {
      alert("Only image files are allowed.");
      continue;
    }
    if (file.size > 10 * 1024 * 1024) {
      alert("File size exceeds 10 MB limit.");
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

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
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
    sendMessage();
  }
});

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
  // phrase bank
  showPhraseBankModal,
  showAddPhraseGroupModal,
  // fragments
  showFragmentModal,
  saveFragment,
  deleteFragment,
  toggleFragmentEnabled,
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
  refreshCharacters,
  // crop modal
  closeCropModal,
  // conversations
  newConvForChar,
  selectConversation,
  deleteConversationFromModal,
  showConvHistoryModal,
  // messages
  startEdit,
  cancelEdit,
  saveEdit,
  deleteMessage,
  switchBranch,
  regenerate,
  sendMessage,
  stopGeneration,
  // inspector
  toggleInspector,
  selectReasoningPass,
  toggleReasoningPass,
  clearRefineDiff,
  // ui
  toggleSection,
  toggleBurger,
  closeBurger,
  triggerAttachImage,
  updateAttachmentPreview,
  showAvatarPopup,
  hideAvatarPopup,
  // state
  S,
});

// ── Smart autoscroll: only autoscroll during streaming if user is at the bottom
function initAutoscroll() {
  const ct = $("chat-messages");
  if (!ct) return;
  const THRESHOLD = 20; // pixels from bottom to be considered "at the bottom" (increased from 5 to prevent false negatives)
  ct.addEventListener("scroll", () => {
    if (!S.isStreaming || S._programmaticScroll) return;
    const atBottom = ct.scrollHeight - ct.scrollTop - ct.clientHeight <= THRESHOLD;
    S.autoscrollEnabled = atBottom;
  });
}

// ── Init
initTheme();
initAutoscroll();

// Load data independently to prevent failures from blocking other loads
async function initAll() {
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
    await loadFragments();
  } catch (e) {
    console.error("Failed to load fragments:", e);
    // Show empty state but don't crash
    $("frag-list").innerHTML =
      '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">Failed to load fragments</div>';
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
}

// Start initialization
initAll();
