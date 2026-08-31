import { S } from "./state.js";

const RESERVED_PASS_IDS = new Set(["director", "writer", "editor"]);

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
