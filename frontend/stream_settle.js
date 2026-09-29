// Stop and settlement for one generation request.
//
// Stop keeps the stream open: the server stops generating, saves what it keeps,
// and closes the stream, and POST /stop answers once that has happened. The
// browser drops the connection only when the stop could not be delivered, found
// nothing registered yet (a request still on its way), or timed out. After a
// dropped connection, /stop is asked once more so the refetch waits for the
// server's cleanup. Imports nothing, so it can be tested without a DOM.

// How long a stream may stay open after the server said it settled.
const CLOSE_GRACE_MS = 2000;
// Allow the server's 15-second settlement wait plus time for delivery.
const STOP_TIMEOUT_MS = 20000;

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
  let stopReply = null;

  async function boundedStop() {
    const requestController = new AbortController();
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        requestController.abort();
        reject(new Error("Stop request timed out"));
      }, stopTimeoutMs);
    });
    try {
      return await Promise.race([Promise.resolve().then(() => requestStop(convId, requestController.signal)), timeout]);
    } finally {
      clearTimeout(timer);
    }
  }

  const op = {
    convId,
    signal: controller.signal,
    stopping: false,
    // The browser dropped the stream, so the server may still be saving.
    disconnected: false,
    // The stream loop has ended.
    finished: false,

    /** Drop the connection; the server treats it as a stop. */
    disconnect() {
      if (op.disconnected) return;
      op.disconnected = true;
      controller.abort();
    },

    /** Ask the server to stop. Repeated calls are one stop. */
    stop() {
      if (op.stopping || op.finished) return;
      op.stopping = true;
      stopReply = boundedStop().then(
        (reply) => {
          if (op.finished) return reply;
          if (reply?.active && reply.settled) {
            // Settled server-side; the close is on its way.
            setTimeout(() => {
              if (!op.finished) op.disconnect();
            }, closeGraceMs);
          } else {
            op.disconnect();
          }
          return reply;
        },
        () => {
          if (!op.finished) op.disconnect();
          return null;
        },
      );
    },

    /**
     * Call once the stream loop has ended. Resolves to true when the server
     * has confirmed nothing more will be written for this operation.
     */
    async settle() {
      op.finished = true;
      if (op.disconnected) {
        try {
          return !!(await boundedStop())?.settled;
        } catch (_) {
          return false;
        }
      }
      // A clean close means the server had finished; a pending /stop still
      // gets its answer so no request is left dangling.
      if (stopReply) await stopReply;
      return true;
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
