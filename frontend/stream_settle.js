// Stop keeps the stream open until the server saves and settles. Drop it only if /stop fails, finds no registered
// stream, or times out. After disconnect, ask /stop again before refetching to wait for server cleanup.
//
// Maximum stream wait after server settlement.
const CLOSE_GRACE_MS = 2000;
// Allow the server's 15-second settlement wait plus time for delivery.
const STOP_TIMEOUT_MS = 20000;

// Only these events can move a live stream. Finished is terminal, including for
// late /stop replies and close-grace timers.
const TRANSITIONS = {
  open: { stop: "stopping", disconnect: "disconnected", finish: "finished" },
  stopping: { disconnect: "stopping-and-disconnected", finish: "finished" },
  disconnected: { stop: "stopping-and-disconnected", finish: "finished" },
  "stopping-and-disconnected": { finish: "finished" },
  finished: {},
};

/**
 * Start one operation. *requestStop(convId, signal)* posts /stop and resolves to its
 * `{active, settled}` body; it rejects when the request fails.
 */
export function createStreamOperation({
  convId,
  requestStop,
  closeGraceMs = CLOSE_GRACE_MS,
  stopTimeoutMs = STOP_TIMEOUT_MS,
}) {
  const controller = new AbortController();
  let state = { phase: "open" };
  let stopReply = null;
  let cancelPendingStop = null;

  function move(event) {
    const phase = TRANSITIONS[state.phase][event];
    if (!phase) return false;
    // Retain the last live phase so callers can still report a stopped result
    // after settlement. These facts no longer allow any live transition.
    state = phase === "finished" ? { phase, from: state.phase } : { phase };
    return true;
  }

  async function boundedStop() {
    const requestController = new AbortController();
    let timer;
    let cancel;
    const timeout = new Promise((_, reject) => {
      cancel = () => {
        requestController.abort();
        reject(new Error("Stop request cancelled"));
      };
      timer = setTimeout(() => {
        requestController.abort();
        reject(new Error("Stop request timed out"));
      }, stopTimeoutMs);
    });
    cancelPendingStop = cancel;
    try {
      return await Promise.race([Promise.resolve().then(() => requestStop(convId, requestController.signal)), timeout]);
    } finally {
      clearTimeout(timer);
      if (cancelPendingStop === cancel) cancelPendingStop = null;
    }
  }

  const op = {
    convId,
    signal: controller.signal,
    get stopping() {
      const phase = state.from ?? state.phase;
      return phase === "stopping" || phase === "stopping-and-disconnected";
    },
    // The browser dropped the stream, so the server may still be saving.
    get disconnected() {
      const phase = state.from ?? state.phase;
      return phase === "disconnected" || phase === "stopping-and-disconnected";
    },
    // The stream loop has ended.
    get finished() {
      return state.phase === "finished";
    },

    /** Drop the connection; the server treats it as a stop. */
    disconnect() {
      if (!move("disconnect")) return;
      controller.abort();
    },

    /** Ask the server to stop. Repeated calls are one stop. */
    stop() {
      if (!move("stop")) return;
      stopReply = boundedStop().then(
        (reply) => {
          if (op.finished) return reply;
          if (reply?.active && reply.settled) {
            // Settled server-side; the close is on its way.
            setTimeout(() => op.disconnect(), closeGraceMs);
          } else {
            op.disconnect();
          }
          return reply;
        },
        () => {
          op.disconnect();
          return null;
        },
      );
    },

    /**
     * Call after the stream loop ends. Repeated calls share one confirmation,
     * resolving to true when the server has confirmed nothing more is written.
     */
    settle() {
      if (!move("finish")) return state.settlement;
      // Cache the promise before cancellation can reenter settle(). First finish,
      // then cancel the old /stop, then confirm cleanup only after disconnect.
      state.settlement = Promise.resolve().then(async () => {
        // A completed stream already confirms persistence; its pending /stop
        // must not hold Send disabled until the HTTP deadline.
        cancelPendingStop?.();
        if (op.disconnected) {
          try {
            return !!(await boundedStop())?.settled;
          } catch (_) {
            return false;
          }
        }
        // Let the cancelled request finish without leaving a dangling promise.
        if (stopReply) await stopReply;
        return true;
      });
      return state.settlement;
    },
  };
  return op;
}

/** What an operation starts from: the ids already on screen. */
export function streamAnchor(messages) {
  return { knownIds: new Set(messages.filter((m) => m.id != null).map((m) => m.id)) };
}

/**
 * The reply this operation saved, or null: an assistant row that was not on
 * screen when it started. A group turn also names its exchange and speaker, so
 * a reply another request saved is never adopted as this one's.
 */
export function settledReply(messages, anchor, { exchangeId = null, memberId = null } = {}) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant" || m.id == null || anchor.knownIds.has(m.id)) continue;
    if (exchangeId != null && m.exchange_id !== exchangeId) continue;
    if (memberId != null && m.speaker_member_id !== memberId) continue;
    return m;
  }
  return null;
}
