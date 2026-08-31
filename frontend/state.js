// Shared client state; keep new keys initialized here.

export const S = {
  conversations: [],
  activeConvId: null,
  activeCharId: null,
  _selectCharLock: false,

  allCharacters: [], // all characters; used for id lookups
  characters: [], // recent characters shown in the sidebar

  moodFragments: [],
  interactiveFragments: [],
  cardMoodFragments: [],
  cardInteractiveFragments: [],

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
  agentEnabled: true,
  enabledTools: {},
  lengthGuardEnabled: false,
  lengthGuardMaxWords: 240,
  lengthGuardMaxParagraphs: 4,
  lengthGuardEnforce: false,
  agenticLorebookEnabled: false,
  feedbackEnabled: false,
  directorIndividualFragments: false,
  directionNotesRecord: false, // global direction-note switch
  directionNotesInject: "off", // where direction notes are injected
  hideUntilBaked: false, // keep the streaming reply out of the DOM until final
  preventPromptOverrides: false, // ignore character-card prompt overrides
  showEditorDiff: true, // show editor-pass diff highlights
  reasoningEnabled: { director: false, writer: false, editor: false, scripter: false },
  reasoningPrefill: { director: "", writer: "", editor: "" },
  editorAuditToggles: {
    banned_phrases: true,
    repetitive_openers: true,
    repetitive_templates: true,
    contrastive_negation: true,
    phrase_repetition: true,
    structural_repetition: true,
    anti_echo: true,
  },

  messages: [],
  editingMsgId: null,
  forkEditMsgId: null, // message being edited and forked
  magicInputMsgId: null,
  editingPendingUserMsg: false, // pending user message is being edited
  pendingUserMsgEdit: null, // edited content for the pending user message
  queuedEdits: {}, // edits saved after the current stream ends
  renderWindowStart: 0, // first rendered message index

  isStreaming: false,
  proseRewriteMsgId: null,
  streamingBodyEl: null,
  streamCutoffIndex: null,
  abortController: null,
  streamingContent: null,
  pendingUserMsg: null,
  attachments: [],
  wasAborted: false,
  generationPhase: null,
  hideStreamingBox: false,
  contextSize: null,
  pendingRefineDiff: null, // writer/editor diff for the current stream
  editorDraftBaseline: null, // writer text before the editor pass
  turnError: null, // current turn error
  worldProposalArrived: false,

  groupCast: null,
  pinnedSpeakerId: null,
  consumedSpeakerId: null,
  speakingPlan: null,
  currentSpeaker: null,
  currentExchangeId: null,
  completedExchangeMessageIds: [],
  castSetupBusy: false,

  directorState: null,
  lastDirectorData: null,
  reasoningDirector: "",
  reasoningWriter: "",
  reasoningEditor: "", // includes editor feedback reasoning
  lastFeedback: null, // editor feedback for the current turn
  lastDirectionNotes: null, // direction notes recorded for the current turn
  reasoningPassActive: 0,
  reasoningPassSelected: 0,
  reasoningUserOverride: false,
  reasoningOpen: true,
  toolCallsOpen: false,
  injectionBlockOpen: false,
  contextSizeOpen: true,
  inspectedMsgId: null, // message shown in the Inspector
  inspectedDirectorData: null, // director data for the inspected message
  reasoningByPass: {}, // accumulated reasoning by pass id
  inspectorTab: "main",
  toolsTab: "main",

  documents: [], // sidebar document rows
  activeDocId: null,
  documentMode: false, // show the document editor instead of chat
  docStreaming: false,
  docAbortController: null,
  docDirty: false, // unsaved editor changes
  docAuditResults: null,
  docAuditBusy: false, // document audit or patch is in flight

  hasMultipleTabs: false, // another app tab is open

  workflowInspectorCardRenderers: [], // Inspector cards by workflow
  workflowToolsPanelRenderers: [], // Tools-panel cards by workflow
  workflowMessageButtonRenderers: [], // Message buttons by workflow
  workflowEventHandlers: {}, // Custom SSE handlers by event name
  workflowAttachmentRenderers: {}, // Attachment renderers by workflow id
  workflowRerollParams: {}, // Extra reroll parameters by workflow id
  workflowPipelines: [], // Registered workflow pipelines
  workflowState: {}, // Opaque workflow state
  workflowPhases: {}, // Status labels by workflow channel
  workflowTextEffects: [], // Registered text effects
  workflowClickHandlers: [], // Registered text click handlers

  workflowManifest: [], // fetched workflow metadata

  rejectedWorkflowAtts: [],
};

export function effectiveWorkflowEnabled(wid) {
  const g = S.settings?.workflows_globally_enabled;
  const globalOn = g === undefined ? true : Boolean(g);
  const map = (S.settings && typeof S.settings.workflow_enabled === "object" && S.settings.workflow_enabled) || {};
  const localOn = wid in map ? Boolean(map[wid]) : true;
  return globalOn && localOn;
}

export {
  registerClickHandler,
  registerTextEffect,
  registerWorkflowEventHandler,
  registerWorkflowInspectorCard,
  registerWorkflowMessageButton,
  registerWorkflowPipeline,
  registerWorkflowToolsPanelCard,
} from "./workflow_registry.js";

export function charactersView() {
  return S.allCharacters.length ? S.allCharacters : S.characters;
}

export function moodFragmentsView() {
  return S.cardMoodFragments.length ? S.moodFragments.concat(S.cardMoodFragments) : S.moodFragments;
}

export function interactiveFragmentsView() {
  return S.cardInteractiveFragments.length
    ? S.interactiveFragments.concat(S.cardInteractiveFragments)
    : S.interactiveFragments;
}

const TOPICS = new Set([
  "messages",
  "conversations",
  "settings",
  "workflow-phase",
  "characters",
  "personas",
  "documents",
  "attachments",
  "tabs",
  "cast",
]);

const _subscribers = new Map(); // topic -> listeners

export function subscribe(topic, fn) {
  if (!TOPICS.has(topic)) {
    console.error("subscribe: unknown topic", topic);
    return () => {};
  }
  let set = _subscribers.get(topic);
  if (!set) {
    set = new Set();
    _subscribers.set(topic, set);
  }
  set.add(fn);
  return () => set.delete(fn);
}

export function notify(topic, detail) {
  if (!TOPICS.has(topic)) {
    console.error("notify: unknown topic", topic);
    return;
  }
  const set = _subscribers.get(topic);
  if (!set) return;
  for (const fn of [...set]) {
    try {
      fn(detail);
    } catch (e) {
      console.error(`subscriber for "${topic}" threw:`, e);
    }
  }
}
