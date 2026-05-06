export const S = {
  conversations: [],
  activeConvId: null,
  activeCharId: null,
  messages: [],
  moodFragments: [],
  directorFragments: [],
  characters: [],
  personas: [],
  activePersonaId: null,
  settings: {},
  endpoints: [],
  activeEndpointId: null,
  modelConfigs: [],
  activeModelConfigId: null,
  agentSameAsWriter: true,
  agentEndpointId: null,
  agentModelConfigs: [],
  agentModelConfigId: null,
  directorState: null,
  lastDirectorData: null,
  isStreaming: false,
  streamingBodyEl: null,
  streamCutoffIndex: null,
  agentEnabled: true,
  enabledTools: {},
  lengthGuardEnabled: false,
  lengthGuardMaxWords: 240,
  lengthGuardMaxParagraphs: 4,
  lengthGuardEnforce: false,
  editingMsgId: null,
  magicInputMsgId: null,
  abortController: null,
  streamingContent: null,
  contextSize: null,
  pendingUserMsg: null,
  attachments: [],
  wasAborted: false,
  _selectCharLock: false,
  generationPhase: null,
  hideStreamingBox: false,
  reasoningDirector: "",
  reasoningWriter: "",
  reasoningEditor: "",
  reasoningPassActive: 0,
  reasoningPassSelected: 0,
  reasoningUserOverride: false,
  reasoningOpen: true,
  reasoningEnabled: { director: true, writer: false, editor: false, scripter: false },
  pendingRefineDiff: null, // {original, ops} set on writer_rewrite, cleared on next stream
  showEditorDiff: true, // when false, editor-pass diff highlights + "clear diff" button are suppressed
  hideUntilBaked: false, // when true, in-flight streaming message is kept detached from DOM until stream finalizes
  autoscrollEnabled: true, // whether to auto-scroll chat to bottom during streaming
  _programmaticScroll: false, // true while scrollToBottom() is executing — suppresses scroll listener
  hasMultipleTabs: false, // true if multiple tabs of the app are open
  editingPendingUserMsg: false, // true when the pending (not-yet-persisted) user message is in edit mode
  pendingUserMsgEdit: null, // stores edited content for a pending user message to apply after streaming
  speakingMsgId: null, // message ID currently being spoken (null = idle)
  ttsLoading: false, // true while fetching TTS audio
  ttsError: null, // last TTS error message
};
