export const S = {
  conversations: [],
  activeConvId: null,
  activeCharId: null,
  messages: [],
  moodFragments: [],
  interactiveFragments: [],
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
  agenticLorebookEnabled: false,
  editingMsgId: null,
  forkEditMsgId: null, // user message whose "Edit & Fork" textarea is open (creates a sibling + new reply)
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
  reasoningEditor: "", // also carries the feedback sub-step's reasoning (folded into the editor channel)
  lastFeedback: null, // {values: {...}} from the editor feedback sub-step for the current/streamed turn (null when none)
  lastDirectionNotes: null, // {notes: [...]} recorded by the direction-note sub-step this turn (null when none)
  feedbackEnabled: false,
  directorIndividualFragments: false,
  directionNotesRecord: false, // master Writing switch; a fragment also needs its own enabled + timing
  directionNotesInject: "off", // injection target: off | director | writer | both (read side, independent of recording)
  reasoningPassActive: 0,
  reasoningPassSelected: 0,
  reasoningUserOverride: false,
  reasoningOpen: true,
  toolCallsOpen: false,
  injectionBlockOpen: false,
  contextSizeOpen: true,
  reasoningEnabled: { director: false, writer: false, editor: false, scripter: false },
  pendingRefineDiff: null, // {original, ops} set on writer_rewrite, cleared on next stream
  showEditorDiff: true, // when false, editor-pass diff highlights + "clear diff" button are suppressed
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
  hideUntilBaked: false, // when true, in-flight streaming message is kept detached from DOM until stream finalizes
  preventPromptOverrides: false, // when true, character card system_prompt and post_history_instructions are ignored
  autoscrollEnabled: true, // whether to auto-scroll chat to bottom during streaming
  _programmaticScroll: false, // true while scrollToBottom() is executing — suppresses scroll listener
  renderWindowStart: 0, // index into S.messages of the first message rendered; older messages are backfilled lazily on scroll-up. 0 means full history is in view.
  hasMultipleTabs: false, // true if multiple tabs of the app are open
  editingPendingUserMsg: false, // true when the pending (not-yet-persisted) user message is in edit mode
  pendingUserMsgEdit: null, // stores edited content for the id-less pending user message to apply after streaming
  queuedEdits: {}, // { [msgId]: content } edits to persisted messages saved mid-stream, applied after the stream (the /edit route blocks on the stream lock)
  inspectedMsgId: null, // when set, Inspector shows director data for this message instead of current state
  inspectedDirectorData: null, // fetched director log data for the inspected message

  // ── Document mode (free-form LLM-assisted writing; orthogonal to chat).
  documents: [], // sidebar list rows {id, title, created_at, updated_at}
  activeDocId: null,
  documentMode: false, // when true, #app.document-mode hides the chat UI
  docStreaming: false,
  docAbortController: null,
  docDirty: false, // unsaved editor edits pending a flush

  // Workflow slot registries -- pushed into at module load by workflow JS files.
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
  reasoningByPass: {}, // {[pass_id]: accumulatedText}, per-workflow-pipeline reasoning buffer
  inspectorTab: "main", // "main" | "secondary"
  toolsTab: "main", // "main" | "secondary"

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

const RESERVED_PASS_IDS = new Set(["director", "writer", "editor"]);

// Registers a workflow's reasoning pipeline so its pass dots and reasoning box render
// in the Inspector Secondary tab and so the SSE router can route `reasoning` events
// by `data.pass`. Validates per the pass id namespace rule: each pass id must start
// with `<workflow_id>:`, must not equal a reserved built-in pass name, and must not
// contain a second `:`. Failure path: console.error and skip registration so the
// missing rail is visible during development. Idempotent on workflow id.
export function registerWorkflowPipeline(entry) {
  if (!entry || typeof entry.id !== "string" || !entry.id) {
    console.error("registerWorkflowPipeline: missing or empty workflow id", entry);
    return;
  }
  const id = entry.id;
  const passes = Array.isArray(entry.passes) ? entry.passes : [];
  const prefix = `${id}:`;
  for (const p of passes) {
    if (!p || typeof p.id !== "string") {
      console.error("registerWorkflowPipeline: pass id missing for workflow", id, p);
      return;
    }
    if (RESERVED_PASS_IDS.has(p.id)) {
      console.error("registerWorkflowPipeline: pass id", p.id, "is a reserved built-in (workflow", `${id})`);
      return;
    }
    if (!p.id.startsWith(prefix)) {
      console.error("registerWorkflowPipeline: pass id", p.id, "must start with", prefix);
      return;
    }
    if (p.id.indexOf(":", prefix.length) !== -1) {
      console.error("registerWorkflowPipeline: pass id", p.id, "contains a second ':'");
      return;
    }
  }
  for (const p of passes) {
    if (!(p.id in S.reasoningByPass)) S.reasoningByPass[p.id] = "";
  }
  const existing = S.workflowPipelines.findIndex((e) => e.id === id);
  const record = { id, label: entry.label || id, passes };
  if (existing >= 0) S.workflowPipelines[existing] = record;
  else S.workflowPipelines.push(record);
}

// Registers a transient text-effect driver. The id gates body word-segmentation
// -- a registered effect needs `.seg` spans to paint. Idempotent on id;
// console.error and skip on a missing id.
export function registerTextEffect(entry) {
  if (!entry || typeof entry.id !== "string" || !entry.id) {
    console.error("registerTextEffect: missing or empty effect id", entry);
    return;
  }
  const record = { id: entry.id, label: entry.label || entry.id };
  const existing = S.workflowTextEffects.findIndex((e) => e.id === entry.id);
  if (existing >= 0) S.workflowTextEffects[existing] = record;
  else S.workflowTextEffects.push(record);
}

// Registers a clickable-text claimant. `claims(seg)` decides which word units
// this workflow wants (defaults to all); `priority` breaks contention when
// several workflows claim the same unit (higher wins, registration order on
// ties); `onClick(seg, msgId)` is the action. Idempotent on id; console.error
// and skip on a missing id or non-function onClick.
export function registerClickHandler(entry) {
  if (!entry || typeof entry.id !== "string" || !entry.id) {
    console.error("registerClickHandler: missing or empty handler id", entry);
    return;
  }
  if (typeof entry.onClick !== "function") {
    console.error("registerClickHandler: onClick must be a function (handler", `${entry.id})`);
    return;
  }
  const record = {
    id: entry.id,
    label: entry.label || entry.id,
    priority: Number.isInteger(entry.priority) ? entry.priority : 0,
    claims: typeof entry.claims === "function" ? entry.claims : () => true,
    onClick: entry.onClick,
  };
  const existing = S.workflowClickHandlers.findIndex((e) => e.id === entry.id);
  if (existing >= 0) S.workflowClickHandlers[existing] = record;
  else S.workflowClickHandlers.push(record);
}

// Production registrars. Each entry carries its owning workflowId so the framework
// suppresses it for a disabled workflow at the read site. Validation matches the
// registrars above (console.error and skip on a bad arg). Idempotent on workflowId:
// re-registering the same workflow replaces its entry rather than duplicating it.
function _registerWorkflowArrayEntry(arr, workflowId, render, where) {
  if (typeof workflowId !== "string" || !workflowId) {
    console.error(`${where}: missing or empty workflowId`, workflowId);
    return;
  }
  if (typeof render !== "function") {
    console.error(`${where}: render must be a function (workflow ${workflowId})`);
    return;
  }
  const record = { workflowId, render };
  const existing = arr.findIndex((e) => e.workflowId === workflowId);
  if (existing >= 0) arr[existing] = record;
  else arr.push(record);
}

export function registerWorkflowInspectorCard(workflowId, render) {
  _registerWorkflowArrayEntry(S.workflowInspectorCardRenderers, workflowId, render, "registerWorkflowInspectorCard");
}

export function registerWorkflowToolsPanelCard(workflowId, render) {
  _registerWorkflowArrayEntry(S.workflowToolsPanelRenderers, workflowId, render, "registerWorkflowToolsPanelCard");
}

export function registerWorkflowMessageButton(workflowId, render) {
  _registerWorkflowArrayEntry(S.workflowMessageButtonRenderers, workflowId, render, "registerWorkflowMessageButton");
}

// Keyed by event name (last writer per event wins), so the workflowId rides along
// for the read-site gate rather than acting as the map key.
export function registerWorkflowEventHandler(workflowId, event, handler) {
  if (typeof workflowId !== "string" || !workflowId) {
    console.error("registerWorkflowEventHandler: missing or empty workflowId", workflowId);
    return;
  }
  if (typeof event !== "string" || !event) {
    console.error(`registerWorkflowEventHandler: missing or empty event (workflow ${workflowId})`);
    return;
  }
  if (typeof handler !== "function") {
    console.error(`registerWorkflowEventHandler: handler must be a function (workflow ${workflowId})`);
    return;
  }
  S.workflowEventHandlers[event] = { workflowId, handler };
}
