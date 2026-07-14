// The single shared state bag. Every key is declared here (no key is born by a
// stray write in a feature module) and grouped under a domain banner naming its
// OWNER — the module responsible for that slice's writes and its render. Reads
// are open; cross-module *writes* should announce via notify(topic) (see the bus
// below) rather than reaching into another domain's renderer. The flat shape is
// deliberate: ~600 read/write sites and the plugin ABI depend on `S.<key>`, so
// this stays flat + a pub/sub bus, not a nested tree.
export const S = {
  // ── Conversations & active selection · owner: chat_conversations.js
  conversations: [],
  activeConvId: null,
  activeCharId: null,
  _selectCharLock: false,

  // ── Characters · owner: library.js
  allCharacters: [], // the full character set; canonical source for id lookups (see charactersView)
  characters: [], // recent-filtered subset shown in the sidebar

  // ── Fragments · owner: library_fragments.js
  moodFragments: [],
  interactiveFragments: [],

  // ── Personas · owner: settings_personas.js
  personas: [],
  activePersonaId: null,

  // ── Settings, endpoints & models · owner: settings.js / settings_models.js
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
  directionNotesRecord: false, // master Writing switch; a fragment also needs its own enabled + timing
  directionNotesInject: "off", // injection target: off | director | writer | both (read side, independent of recording)
  hideUntilBaked: false, // when true, in-flight streaming message is kept detached from DOM until stream finalizes
  preventPromptOverrides: false, // when true, character card system_prompt and post_history_instructions are ignored
  retryEnabled: false, // when true, completions that fail with a transient server error are retried
  retryCount: 10, // retries after the initial attempt
  retryDelay: 5, // seconds between attempts
  showEditorDiff: true, // when false, editor-pass diff highlights + "clear diff" button are suppressed
  reasoningEnabled: { director: false, writer: false, editor: false, scripter: false },
  editorAuditToggles: {
    // per-scanner on/off for the Output Auditor; keys match backend AUDIT_TYPES
    banned_phrases: true,
    repetitive_openers: true,
    repetitive_templates: true,
    contrastive_negation: true,
    phrase_repetition: true,
    structural_repetition: true,
    anti_echo: true,
    // The deterministic RP format-consistency normalizer is not listed here — it
    // is not user-toggleable and always runs (see backend editor.py).
  },

  // ── Messages & per-message editing · owner: chat_core.js / chat_messages.js
  messages: [],
  editingMsgId: null,
  forkEditMsgId: null, // user message whose "Edit & Fork" textarea is open (creates a sibling + new reply)
  magicInputMsgId: null,
  editingPendingUserMsg: false, // true when the pending (not-yet-persisted) user message is in edit mode
  pendingUserMsgEdit: null, // stores edited content for the id-less pending user message to apply after streaming
  queuedEdits: {}, // { [msgId]: content } edits to persisted messages saved mid-stream, applied after the stream (the /edit route blocks on the stream lock)
  renderWindowStart: 0, // index into S.messages of the first message rendered; older messages are backfilled lazily on scroll-up. 0 means full history is in view.
  autoscrollEnabled: true, // whether to auto-scroll chat to bottom during streaming
  _programmaticScroll: false, // true while scrollToBottom() is executing — suppresses scroll listener

  // ── Streaming / generation lifecycle · owner: chat_stream.js
  isStreaming: false,
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
  pendingRefineDiff: null, // {original, ops} set on writer_rewrite, cleared on next stream
  editorDraftBaseline: null, // writer's pre-editor text; diff anchor across draft_update/writer_rewrite, reset on next stream

  // ── Reasoning rail & Inspector · owner: chat_inspector.js
  directorState: null,
  lastDirectorData: null,
  reasoningDirector: "",
  reasoningWriter: "",
  reasoningEditor: "", // also carries the feedback sub-step's reasoning (folded into the editor channel)
  lastFeedback: null, // {values: {...}} from the editor feedback sub-step for the current/streamed turn (null when none)
  lastDirectionNotes: null, // {notes: [...]} recorded by the direction-note sub-step this turn (null when none)
  reasoningPassActive: 0,
  reasoningPassSelected: 0,
  reasoningUserOverride: false,
  reasoningOpen: true,
  toolCallsOpen: false,
  injectionBlockOpen: false,
  contextSizeOpen: true,
  inspectedMsgId: null, // when set, Inspector shows director data for this message instead of current state
  inspectedDirectorData: null, // fetched director log data for the inspected message
  reasoningByPass: {}, // {[pass_id]: accumulatedText}, per-workflow-pipeline reasoning buffer
  inspectorTab: "main", // "main" | "secondary"
  toolsTab: "main", // "main" | "secondary"

  // ── Document mode (free-form LLM-assisted writing; orthogonal to chat) · owner: document.js
  documents: [], // sidebar list rows {id, title, created_at, updated_at}
  activeDocId: null,
  documentMode: false, // when true, #app.document-mode hides the chat UI
  docStreaming: false,
  docAbortController: null,
  docDirty: false, // unsaved editor edits pending a flush

  // ── Multi-tab presence · owner: tabLock.js
  hasMultipleTabs: false, // true if multiple tabs of the app are open

  // ── Workflow slot registries · owner: workflow_registry.js (writes) / chat_workflow.js (reads)
  // Pushed into at module load by workflow JS files via the registrars (now in
  // workflow_registry.js, re-exported below so the plugin ABI is unchanged).
  // Built-in chat/settings/index code iterates these; no built-in code knows about specific workflows.
  // The four production registries below carry the owning workflowId so the framework can filter each
  // entry by effectiveWorkflowEnabled(workflowId) at its read site: a disabled workflow's production
  // surfaces vanish while its consumption surfaces (attachment renderer, audio, swipe/delete) stay live.
  workflowInspectorCardRenderers: [], // [{workflowId, render: () => htmlString}], Inspector Secondary cards
  workflowToolsPanelRenderers: [], // [{workflowId, render: () => htmlString}], Agents-panel Secondary card body folded into the workflow's on/off card (the framework owns the name + toggle header; render() returns the body below it)
  workflowMessageButtonRenderers: [], // [{workflowId, render: (msg) => htmlString}], extra per-message toolbar buttons
  workflowEventHandlers: {}, // {[event_name]: {workflowId, handler: (data, msgDiv|null) => void}}, custom SSE dispatch
  workflowAttachmentRenderers: {}, // {[workflow_id]: (ctx) => htmlString} where ctx = {att, buttons:{regen,reroll}, defaultHtml}; defaultHtml is the complete default rendering (media plus the regen/reroll button strip) -- returning it reproduces the framework default exactly. buttons.regen/buttons.reroll are the individual button strings, already contained in defaultHtml, for authors who place the controls themselves; splice defaultHtml OR the buttons, not both (both double the strip). One renderer per workflow_id -- a workflow producing multiple attachment kinds should register as multiple workflow ids rather than branching inside one renderer. Widget renders one row (the active sibling)
  workflowPipelines: [], // [{id, label, passes:[{id,label}]}], pushed by registerWorkflowPipeline
  workflowState: {}, // {[workflow_id]: any}, per-workflow opaque UI state
  workflowPhases: {}, // {[channel]: label}, live status pill text per workflow channel
  workflowTextEffects: [], // [{id, label}], registered text-effect drivers; a non-empty list enables body word-segmentation
  workflowClickHandlers: [], // [{id, label, priority, claims, onClick}], clickable-text-unit claimants

  workflowManifest: [], // fetched from /api/workflows at boot

  // Flat list of workflow-attachment rejection records. Each entry:
  //   {message_id (number), originating_attachment_id (number|null),
  //    filename, workflow_id, mime, reason}
  // originating_attachment_id is null for SSE-path entries (pre-insert
  // rejection, no DB row); for regenerate/reroll routes it carries the
  // root_id of the swipe group the operation targeted. Writers merge
  // per (message_id, originating_attachment_id) tuple; the reader splits
  // into footer chips (null tag) and per-widget chips (root_id tag).
  rejectedWorkflowAtts: [],
};

// Mirrors the backend truth table (backend/workflows/enablement.py): a workflow
// is effective only when the global master and its per-workflow flag are both on,
// each defaulting to on when its value is missing. Reads S.settings directly (the
// single source), so a toggle takes effect at the next render with no refetch, and
// it is safe before loadSettings populates S.settings (defaults to enabled). The
// typeof guard mirrors the backend's defensive coercion: a malformed
// workflow_enabled degrades to enabled rather than throwing at every gate.
export function effectiveWorkflowEnabled(wid) {
  const g = S.settings?.workflows_globally_enabled;
  const globalOn = g === undefined ? true : Boolean(g);
  const map = (S.settings && typeof S.settings.workflow_enabled === "object" && S.settings.workflow_enabled) || {};
  const localOn = wid in map ? Boolean(map[wid]) : true;
  return globalOn && localOn;
}

// The 8 workflow registrars live in workflow_registry.js now; re-exported here so
// the ABI v1 import path `/static/state.js` stays valid (plugin ABI unchanged).
export {
  registerClickHandler,
  registerTextEffect,
  registerWorkflowEventHandler,
  registerWorkflowInspectorCard,
  registerWorkflowMessageButton,
  registerWorkflowPipeline,
  registerWorkflowToolsPanelCard,
} from "./workflow_registry.js";

// ── Selectors

// The character list for id lookups. S.allCharacters (library.js owns it) is the
// canonical full set; while it is still empty (pre-load) fall back to the recent-
// filtered S.characters. Both are always arrays now, so callers drop the old
// hand-rolled `(S.allCharacters || S.characters || [])` / `(S.allCharacters || [])`
// guards and go through this single selector instead.
export function charactersView() {
  return S.allCharacters.length ? S.allCharacters : S.characters;
}

// ── Pub/sub bus
// A ~20-line synchronous fan-out so a module that MUTATES a slice of S can
// announce the change and every interested module re-renders, without the
// mutator importing each renderer. This is the seam that lets later stages
// dissolve the window bridge and the cross-module underscore imports; it is
// infrastructure here — Stage 2 wires no call sites beyond the selector above.
//
// Rule: the bus is for CROSS-module mutate→render pairs. A same-module
// mutate→render pair stays a plain function call.
//
// Topics are enumerated and tiered. The public-for-plugins tier becomes ABI the
// moment the facade re-exports `subscribe`, so its payload shapes are frozen;
// the internal tier is free to change through stages 3–5. Plugins are
// subscribe-only — `notify` is NEVER exposed through the facade.
//   public-for-plugins: messages, conversations, settings, workflow-phase
//   internal:           characters, personas, documents, attachments, tabs
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
]);

const _subscribers = new Map(); // topic -> Set<fn>

// Subscribe `fn(detail)` to a topic; returns an unsubscribe function. An unknown
// topic is a programming error (logged, not silent) so a typo surfaces in dev.
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

// Synchronously fan a change out to a topic's subscribers. Each handler runs in
// its own try/catch so one throwing subscriber can't starve the rest or the
// caller that just mutated S. Iterates a snapshot so a handler may (un)subscribe.
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
