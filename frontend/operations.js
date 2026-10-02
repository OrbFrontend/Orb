import { notify, S } from "./state.js";

/** Reserve ownership before awaiting. A resource target never changes. */
export function begin(kind, target) {
  const op = {
    id: globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    kind,
    target: Object.freeze({ ...target }),
    phase: "running",
    stop: null,
    outcome: null,
  };
  S.operations.set(op.id, op);
  notify("operations");
  return op;
}

export function finish(op, outcome = "settled") {
  if (!op) return;
  if (S.operations.get(op.id) !== op) return;
  op.phase = outcome;
  op.outcome = outcome;
  if (outcome !== "unknown") S.operations.delete(op.id);
  notify("operations");
}

export function runningFor(key, id) {
  return [...S.operations.values()].filter((op) => op.target[key] === id);
}

export function ownsView(token, id, kind = "conversation") {
  return kind === "document"
    ? S.documentViewToken === token && S.activeDocId === id
    : S.conversationViewToken === token && S.activeConvId === id;
}
