// The workflow registrars: the functions plugin modules call to push their
// production/consumption surfaces into the S.workflow* slots (declared in
// state.js). Split out of state.js so state.js is pure data + bus and this file
// holds the registration behavior. state.js re-exports every function here, so
// the plugin ABI v1 import path (`/static/state.js`) is unchanged; plugins
// migrated to the facade import them from workflow_api.js instead.
//
// This forms a deliberate state.js ⇄ workflow_registry.js pair: state.js
// re-exports these one way, this file `import { S }` the other. It is load-safe
// because every registrar only dereferences S inside its function body at call
// time (module load never touches S here), so the cyclic binding is fully
// resolved before any registrar runs. The layer check allowlists this pair as a
// permanent documented L1 exception.
import { S } from "./state.js";

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
