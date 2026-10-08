// Shared client state; keep new keys initialized here.

export const S = {
  operations: new Map(),
  docOperation: null,
  conversationViewToken: 0,
  conversationLoading: false,
  documentViewToken: 0,
  documentSessions: new Map(),
  activeWorldIds: new Set(),
  attachmentInvalidations: new Map(),
  inspectedReasoningSelected: 0,
  inspectedReasoning: {},
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
  // The solo card's Scenario and Creator's Note, shown above the opening line. Carries the conversation it was read for
  // so a repaint mid-switch cannot paint the outgoing character's framing over the incoming one's.
  sceneIntro: null,

  personas: [],

  settings: {}, // the server's settings row; settings_store.js writes it, the views below read it
  localMlFeatures: {}, // last /local-ml/status features map; other cards gate on it
  // Chat endpoints and Judge endpoints are separate pools.
  endpoints: [],
  judgeEndpoints: [],
  activeEndpointId: null,
  modelConfigs: [],
  activeModelConfigId: null,
  agentEndpointId: null,
  agentModelConfigs: [],
  agentModelConfigId: null,
  personaAvatarVersion: 0, // bumped on a persona avatar save; busts the image URL

  messages: [],
  editingMsgId: null,
  forkEditMsgId: null, // message being edited and forked
  magicInputMsgId: null,
  editingPendingUserMsg: false, // pending user message is being edited
  pendingUserMsgEdit: null, // edited content for the pending user message
  queuedEdits: {}, // edits saved after the current stream ends
  renderWindowStart: 0, // first rendered message index

  liveGroupReplies: 0, // group replies shown live before an expressive speaker began buffering
  isStreaming: false,
  proseRewriteMsgId: null,
  streamingBodyEl: null,
  streamCutoffIndex: null,
  streamOp: null, // the stoppable stream this tab is running (stream_settle.js)
  streamingContent: null,
  expressionBuffering: false, // this turn holds replies for playback, from the first speaker with expressions on
  expressionPlayback: null, // saved reply parts and the reader's current position
  pendingUserMsg: null,
  attachments: [],
  generationStep: null, // empty while waiting; null when idle
  pendingGenerationStep: null,
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
  lastState: null, // the current turn's state changes, rejections, and dropped corrections
  // In-flight decisions; the Inspector reads stored records after the reply exists.
  lastDecisions: null,
  reasoningPassActive: 0,
  reasoningPassSelected: 0,
  reasoningUserOverride: false,
  reasoningOpen: true,
  toolCallsOpen: false,
  injectionBlockOpen: false,
  decisionsOpen: true,
  feedbackOpen: true,
  stateChangesOpen: true,
  contextSizeOpen: true,
  inlineInspectorOpen: true, // the in-chat Inspector blocks, shared by every reply
  inlineReasoningOpen: true, // the in-chat Reasoning blocks, apart from the panel's section
  inspectedMsgId: null, // message shown in the Inspector
  inspectedDirectorData: null, // director data for the inspected message
  reasoningByPass: {}, // accumulated reasoning by pass id
  inspectorTab: "main", // Inspector tab: "main" | "state"
  toolsTab: "main",

  documents: [], // sidebar document rows
  activeDocId: null,
  documentMode: false, // show the document editor instead of chat
  docStreaming: false,
  docDirty: false, // unsaved editor changes
  docAuditResults: null,
  docAuditBusy: false, // document audit or patch is in flight

  hasMultipleTabs: false, // another app tab is open

  workflowInspectorCardRenderers: [], // Inspector cards by workflow
  workflowToolsPanelRenderers: [], // Tools-panel cards by workflow
  workflowMessageButtonRenderers: [], // Message buttons by workflow
  workflowEventHandlers: {}, // Custom SSE handlers by event name
  workflowAttachmentRenderers: {}, // Attachment renderers by workflow id
  workflowAttachmentPlacements: {}, // Attachment placements by workflow id
  workflowRerollParams: {}, // Extra reroll parameters by workflow id
  workflowRerollSuccess: {}, // Success callbacks after a reroll creates a sibling
  workflowRegenerateSettled: {}, // Callbacks when a regenerate ends, on any outcome
  workflowPipelines: [], // Registered workflow pipelines
  workflowState: {}, // Opaque workflow state
  workflowPhases: {}, // Status labels by workflow channel
  workflowTextEffects: [], // Registered text effects
  workflowClickHandlers: [], // Registered text click handlers

  workflowManifest: [], // fetched workflow metadata

  rejectedWorkflowAtts: [],
  conversationStates: new Map(), // per-conversation views; conversationState() fills it
};

const flag = (value, fallback) =>
  typeof value === "boolean" ? value : typeof value === "number" ? value !== 0 : fallback;
const record = (value) => (value && typeof value === "object" ? value : {});

// Read-only views of S.settings: a write throws, so every change goes through settings_store.js.
const settingViews = {
  activePersonaId: (s) => s.active_persona_id || null,
  characterBrowserView: (s) => s.character_library_view || "grid",
  characterBrowserSort: (s) => s.character_library_sort || "time-added",
  agentSameAsWriter: (s) => flag(s.agent_same_as_writer, true),
  agentEnabled: (s) => flag(s.enable_agent, true),
  enabledTools: (s) => Object.freeze({ ...record(s.enabled_tools) }),
  lengthGuardEnabled: (s) => Boolean(s.length_guard_enabled),
  lengthGuardMaxWords: (s) => s.length_guard_max_words || 240,
  lengthGuardMaxParagraphs: (s) => s.length_guard_max_paragraphs || 4,
  lengthGuardEnforce: (s) => Boolean(s.length_guard_enforce),
  agenticLorebookEnabled: (s) => Boolean(s.agentic_lorebook_enabled),
  directorIndividualFragments: (s) => Boolean(s.director_individual_fragments),
  hideUntilBaked: (s) => flag(s.hide_streaming_until_baked, false), // keep the streaming reply out of the DOM until final
  preventPromptOverrides: (s) => flag(s.prevent_prompt_overrides, false), // ignore character-card prompt overrides
  showEditorDiff: (s) => flag(s.show_editor_diff, true), // show editor-pass diff highlights
  showChatAvatars: (s) => flag(s.show_chat_avatars, false), // portrait gutter on chat messages
  inspectorInline: (s) => Boolean(s.inspector_inline), // each reply's Inspector sections above its text in the chat
  reasoningEnabled: (s) =>
    Object.freeze({
      director: false,
      writer: false,
      editor: false,
      scripter: false,
      ...record(s.reasoning_enabled_passes),
    }),
  reasoningPrefill: (s) =>
    Object.freeze({ director: "", writer: "", editor: "", ...record(s.reasoning_prefill_passes) }),
  editorAuditToggles: (s) =>
    Object.freeze({
      banned_phrases: true,
      repetitive_openers: true,
      repetitive_templates: true,
      contrastive_negation: true,
      phrase_repetition: true,
      structural_repetition: true,
      anti_echo: true,
      negated_narration: false,
      subject_fixation: false,
      ...record(s.editor_audit_toggles),
    }),
};
for (const [key, view] of Object.entries(settingViews)) {
  Object.defineProperty(S, key, { enumerable: true, get: () => view(S.settings) });
}

// UI reads the selected conversation; asynchronous operations retain this object.
const conversationKeys = `
  conversationLoading workflowPhases castSetupBusy messages queuedEdits editingMsgId forkEditMsgId
  magicInputMsgId editingPendingUserMsg pendingUserMsgEdit renderWindowStart liveGroupReplies isStreaming
  proseRewriteMsgId streamingBodyEl streamCutoffIndex streamOp streamingContent expressionBuffering
  expressionPlayback pendingUserMsg attachments generationStep pendingGenerationStep hideStreamingBox
  contextSize pendingRefineDiff editorDraftBaseline turnError worldProposalArrived groupCast pinnedSpeakerId
  consumedSpeakerId speakingPlan currentSpeaker currentExchangeId completedExchangeMessageIds directorState
  lastDirectorData reasoningDirector reasoningWriter reasoningEditor lastFeedback lastState lastDecisions
  reasoningPassActive reasoningPassSelected reasoningUserOverride inspectedMsgId inspectedDirectorData
  inspectedReasoning inspectedReasoningSelected reasoningByPass sceneIntro cardMoodFragments
  cardInteractiveFragments rejectedWorkflowAtts activeWorldIds
`
  .trim()
  .split(/\s+/);
const defaults = Object.fromEntries(conversationKeys.map((key) => [key, S[key]]));
// Deep copies: the idle view is written while no chat is selected, and must
// never seed the defaults a later conversation starts from.
const idleView = structuredClone(defaults);

/** Is *state*'s conversation the one on screen? A background turn keeps its data but paints nothing. */
export function isViewing(state) {
  return S.activeConvId === state.activeConvId && !S.documentMode;
}

export function conversationState(cid) {
  if (cid == null) return idleView;
  let state = S.conversationStates.get(cid);
  if (!state) {
    const data = structuredClone(defaults);
    data.activeConvId = cid;
    data.draft = "";
    state = new Proxy(data, { get: (target, key) => (key in target ? target[key] : S[key]) });
    S.conversationStates.set(cid, state);
  }
  return state;
}

/**
 * Discard cached server data once reload can rebuild it; retain active work,
 * drafts, unsaved edits and speaker pins.
 */
export function releaseConversationState(cid) {
  const state = S.conversationStates.get(cid);
  if (!state || cid === S.activeConvId) return;
  const holds =
    state.isStreaming ||
    state.proseRewriteMsgId ||
    state.castSetupBusy ||
    state.draft ||
    state.pinnedSpeakerId != null ||
    Object.keys(state.queuedEdits).length ||
    Object.keys(state.workflowPhases).length ||
    [...S.operations.values()].some((op) => op.target.conversationId === cid);
  if (!holds) S.conversationStates.delete(cid);
}

for (const key of conversationKeys) {
  Object.defineProperty(S, key, {
    enumerable: true,
    get: () => conversationState(S.activeConvId)[key],
    set: (value) => {
      conversationState(S.activeConvId)[key] = value;
    },
  });
}

/** Read live Local ML availability (downloaded, enabled, dependencies installed) from S. */
export function localMlReady(feature) {
  const info = S.localMlFeatures[feature];
  // `runtime_ok` is absent for in-process features: only a feature that reports one can fail it.
  return Boolean(info?.present && info?.enabled && info?.deps_ok && info.runtime_ok !== false);
}

export function effectiveWorkflowEnabled(wid) {
  const g = S.settings?.workflows_globally_enabled;
  const globalOn = g === undefined ? true : Boolean(g);
  const map = (S.settings && typeof S.settings.workflow_enabled === "object" && S.settings.workflow_enabled) || {};
  const localOn = wid in map ? Boolean(map[wid]) : true;
  return globalOn && localOn;
}

/** The live reply stays out of the DOM until it is saved. */
export function streamingHidden() {
  return S.hideUntilBaked || S.expressionBuffering;
}

export function charactersView() {
  return S.allCharacters.length ? S.allCharacters : S.characters;
}

export function moodFragmentsView() {
  return S.cardMoodFragments.length ? S.moodFragments.concat(S.cardMoodFragments) : S.moodFragments;
}

/**
 * A card fragment with a legacy state type, rewritten as an explicit state
 * fragment -- the same mapping the backend applies at its card read boundary.
 * Shared card files keep ``progressive`` and ``direction_note`` indefinitely.
 */
export function upgradeLegacyFragment(f) {
  if (f?.field_type === "progressive") {
    return { ...f, field_type: "state", state_mode: "value", state_update: "before_writer", state_inject: "both" };
  }
  if (f?.field_type === "direction_note") {
    const { direction_note_timing: timing, ...rest } = f;
    const update = timing === "pre_writer" ? "before_writer" : "after_reply";
    return { ...rest, field_type: "state", state_mode: "entries", state_update: update, state_inject: "both" };
  }
  return f;
}

export function interactiveFragmentsView() {
  return S.cardInteractiveFragments.length
    ? S.interactiveFragments.concat(S.cardInteractiveFragments)
    : S.interactiveFragments;
}

/**
 * Return cooldowns read before *msgId*'s turn, from the preceding exchange.
 * The reply stores post-turn state; group speakers share one Director snapshot.
 */
export function restingCooldowns(msgId) {
  const at = S.messages.findIndex((message) => message.id === msgId);
  if (at < 0) return {};
  const exchangeId = S.messages[at].exchange_id ?? null;
  const prior = S.messages
    .slice(0, at)
    .findLast((message) => message.role === "assistant" && (exchangeId == null || message.exchange_id !== exchangeId));
  return prior?.fragment_cooldowns || {};
}

const TOPICS = new Set([
  "operations",
  "messages",
  "conversations",
  "settings",
  "local-ml",
  "endpoints",
  "workflow-phase",
  "characters",
  "personas",
  "documents",
  "attachments",
  "tabs",
  "cast",
  "expression-playback",
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
