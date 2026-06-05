// Public entrypoint for the chat UI. The implementation was split out of this
// (formerly ~3200-line) file into focused modules; this barrel re-exports the
// same public surface so existing importers — app.js, settings.js, library.js,
// and workflow modules loading "/static/chat.js" — keep working unchanged.
//
//   chat_core.js          message normalization, toolbar/icons, renderMessages,
//                         context-size counter
//   chat_workflow.js      workflow attachment widgets, swipe/regen/reroll/
//                         rehydrate/delete, viewport access tracking, cross-tab
//                         mutation listener (registers its window.* handlers on
//                         import)
//   chat_inspector.js     reasoning rail, pipeline passes, phase pills, avatar
//   chat_stream.js        generation phases, SSE stream, send/regen/magic
//   chat_messages.js      per-message edit/fork/inspect/delete/branch + nav
//   chat_conversations.js conversation lifecycle, compression, title editing

export { renderMessages } from "./chat_core.js";

export { initWorkflowMutationListener, refreshConversationMessages } from "./chat_workflow.js";

export {
  clearRefineDiff,
  clearWorkflowPhase,
  hideAvatarPopup,
  loadWorkflowManifest,
  renderInspector,
  renderInspectorSecondary,
  saveInspectorOpenStates,
  selectReasoningPass,
  selectWorkflowPipelinePass,
  setInspectorTab,
  setToolsTab,
  setWorkflowPhase,
  showAvatarPopup,
  toggleInspector,
  toggleReasoningPass,
} from "./chat_inspector.js";

export {
  continueFromUser,
  handleMagicKey,
  regenerate,
  sendMessage,
  stopGeneration,
  submitMagicRewrite,
  superRegenerate,
  toggleMagicInput,
} from "./chat_stream.js";

export {
  cancelEdit,
  cancelEditPending,
  cancelForkEdit,
  clearInspectedMessage,
  deleteMessage,
  handleChatKeyNav,
  initAutoscroll,
  initChatKeyNav,
  initChatSwipeNav,
  inspectMessage,
  saveEdit,
  saveEditPending,
  saveForkEdit,
  startEdit,
  startEditPending,
  startForkEdit,
  switchBranch,
} from "./chat_messages.js";

export {
  applyCompression,
  cancelCompression,
  cancelTitleEdit,
  deleteConversationFromModal,
  generateCompressionSummary,
  handleTitleEditKey,
  loadConversations,
  newConvForChar,
  resetChatUI,
  saveTitleEdit,
  selectChar,
  selectConversation,
  showCompressModal,
  showConvHistoryModal,
  startEditTitle,
} from "./chat_conversations.js";
