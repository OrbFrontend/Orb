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
  toolCallsOpen: false,
  injectionBlockOpen: false,
  contextSizeOpen: true,
  reasoningEnabled: { director: true, writer: false, editor: false, scripter: false },
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
  },
  hideUntilBaked: false, // when true, in-flight streaming message is kept detached from DOM until stream finalizes
  preventPromptOverrides: false, // when true, character card system_prompt and post_history_instructions are ignored
  autoscrollEnabled: true, // whether to auto-scroll chat to bottom during streaming
  _programmaticScroll: false, // true while scrollToBottom() is executing — suppresses scroll listener
  hasMultipleTabs: false, // true if multiple tabs of the app are open
  editingPendingUserMsg: false, // true when the pending (not-yet-persisted) user message is in edit mode
  pendingUserMsgEdit: null, // stores edited content for a pending user message to apply after streaming
  inspectedMsgId: null, // when set, Inspector shows director data for this message instead of current state
  inspectedDirectorData: null, // fetched director log data for the inspected message

  // Workflow slot registries -- pushed into at module load by workflow JS files.
  // Built-in chat/settings/index code iterates these; no built-in code knows about specific workflows.
  workflowInspectorCardRenderers: [], // [() => htmlString], rendered into the Inspector Secondary tab (global panel, no per-message context)
  workflowToolsPanelRenderers: [], // [() => htmlString], rendered into the Agents panel Secondary tab
  workflowMessageButtonRenderers: [], // [(msg) => htmlString], extra per-message toolbar buttons
  workflowEventHandlers: {}, // {[event_name]: (data, msgDiv|null) => void}, custom SSE event dispatch
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
  const prefix = id + ":";
  for (const p of passes) {
    if (!p || typeof p.id !== "string") {
      console.error("registerWorkflowPipeline: pass id missing for workflow", id, p);
      return;
    }
    if (RESERVED_PASS_IDS.has(p.id)) {
      console.error("registerWorkflowPipeline: pass id", p.id, "is a reserved built-in (workflow", id + ")");
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
    console.error("registerClickHandler: onClick must be a function (handler", entry.id + ")");
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
