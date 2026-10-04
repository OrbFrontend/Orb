import { api } from "./api.js";
import { responseError } from "./errors.js";
import { sseEvents, streamPost } from "./sse.js";
import { notify, S } from "./state.js";
import { createStreamOperation } from "./stream_settle.js";

/** Reserve ownership before awaiting. A resource target never changes. */
export function begin(kind, target) {
  const op = {
    id: globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    kind,
    target: Object.freeze({ ...target }),
    stop: null,
  };
  S.operations.set(op.id, op);
  notify("operations");
  return op;
}

export function finish(op) {
  if (!op || S.operations.get(op.id) !== op) return;
  S.operations.delete(op.id);
  notify("operations");
}

/** Begin a stream whose Stop posts to *stopPath* naming this operation, so a stale Stop ends nothing newer. */
export function beginStream(kind, target, stopPath) {
  const record = begin(kind, target);
  record.stream = createStreamOperation({
    requestStop: (_, signal) => api.post(`${stopPath}?operation_id=${record.id}`, {}, { signal }),
  });
  record.stop = () => record.stream.stop();
  return record;
}

/**
 * POST *path* for *record* and yield its SSE events. A stream that ends without
 * `done` or `error` is a failure unless stopped; any failure before one drops
 * the connection, so settlement asks the server whether it is still writing.
 */
export async function* streamEvents(record, path, body, what) {
  const op = record.stream;
  let terminal = false;
  try {
    const resp = await streamPost(`${path}?operation_id=${record.id}`, body, op.signal);
    if (!resp.ok) throw await responseError(resp);
    for await (const event of sseEvents(resp.body, { signal: op.signal })) {
      terminal ||= event.event === "done" || event.event === "error";
      yield event;
    }
    if (!terminal && !op.stopping) throw new Error(`${what} stream ended before completion`);
  } catch (error) {
    if (!terminal) op.disconnect();
    throw error;
  }
}

/** Wait for the server to finish the stream, then release the operation. */
export async function settle(record) {
  await record.stream.settle();
  finish(record);
}

export function runningFor(key, id) {
  return [...S.operations.values()].filter((op) => op.target[key] === id);
}

export function ownsView(token, id) {
  return S.conversationViewToken === token && S.activeConvId === id;
}

/** Mark the *selector* items whose chat is generating a reply, in view or not; *keyOf* maps a chat to its item's `data-activity`. */
export function syncActivity(selector, keyOf) {
  const running = new Set(
    [...S.operations.values()].filter((op) => op.kind === "chat").map((op) => op.target.conversationId),
  );
  const busy = new Set((S.conversations || []).filter((conv) => running.has(conv.id)).map(keyOf));
  for (const el of document.querySelectorAll(selector)) el.classList.toggle("busy", busy.has(el.dataset.activity));
}
