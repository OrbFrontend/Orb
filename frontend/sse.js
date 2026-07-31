// The single SSE parser + streaming transport for the whole app. Every streaming
// endpoint — chat send/continue/regenerate/super-regenerate/fork-edit/magic-
// rewrite, conversation summarize, and document generate — reads its bytes
// through here. Before this module there were three bespoke parsers (chat's
// line-split, document's frame-split, summarize's copy of chat's) and three
// bespoke fetch transports; this is the one implementation they converge on.
//
// The parser is transport-only: it knows the wire frame shape and NOTHING about
// event names or payload semantics. Crucially it NEVER unescapes `data`. The
// backend serializer (backend/api/deps.py `_sse_stream`) escapes real newlines
// to a literal `\n` for STRING data, but leaves DICT data as raw json.dumps
// output where an in-token newline is already `\n`-escaped inside the JSON.
// Those are opposite rules, so un-escaping is the consumer's call — see
// `unescapeSSE`, applied per string-channel event, never to a JSON payload.
//
// Frame facts, fixed by that serializer:
//   • Each event is `event: <name>\ndata: <payload>\n\n`.
//   • Silent stretches emit a `: keepalive\n\n` comment frame.
//   • Because string data has its newlines escaped and dict data is single-line
//     json.dumps, a payload never contains a real newline — which makes `\n\n`
//     an unambiguous frame terminator and `\n` an unambiguous line terminator
//     inside a frame.

const FRAME_SEP = "\n\n";
const EVENT_PREFIX = "event: ";
const DATA_PREFIX = "data: ";

// Async generator over the SSE frames of a fetch Response body. Yields
// `{ event, data }` where `data` is the raw payload string (never unescaped).
// Comment frames (keepalives) and frames without an `event:` line are skipped.
//
// Abort handling: a passed AbortSignal cancels the reader. In every in-repo
// caller the same signal is also handed to `fetch`, so an abort rejects the
// pending `read()` with an AbortError that propagates out through the
// for-await; the reader.cancel() here is a belt-and-suspenders backstop for a
// caller that only wires the signal to the parser.
export async function* sseEvents(body, { signal } = {}) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const onAbort = () => {
    reader.cancel().catch(() => {});
  };
  if (signal) signal.addEventListener("abort", onAbort, { once: true });
  let buf = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx = buf.indexOf(FRAME_SEP);
      while (idx !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + FRAME_SEP.length);
        const evt = parseFrame(frame);
        if (evt) yield evt;
        idx = buf.indexOf(FRAME_SEP);
      }
    }
    // A trailing partial frame (stream cut before its blank line) is dropped:
    // the backend always terminates real frames, so a fragment is never a
    // dispatchable event.
  } finally {
    if (signal) signal.removeEventListener("abort", onAbort);
    try {
      reader.releaseLock();
    } catch {
      // Already released / errored — nothing to release.
    }
  }
}

// Parse one raw frame into `{ event, data }`, or null for a comment/keepalive
// or a frame carrying no event name. `data` keeps the exact bytes after the
// `data: ` prefix (its single delimiter space, not any further leading space):
// a token delta like " world" must retain its leading space.
function parseFrame(frame) {
  if (!frame || frame.startsWith(":")) return null; // comment / keepalive
  let event = null;
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith(EVENT_PREFIX)) event = line.slice(EVENT_PREFIX.length);
    else if (line.startsWith(DATA_PREFIX)) data = line.slice(DATA_PREFIX.length);
    else if (line === "event:") event = "";
    else if (line === "data:") data = "";
  }
  if (event === null) return null; // data-only frame → not dispatchable
  return { event, data };
}

// Consumer-side newline un-escaping for STRING data channels (token, error, the
// summarize token stream). Do NOT run a JSON/dict channel (document `probs`)
// through this — its newlines are already `\n`-escaped inside the JSON by
// json.dumps and un-escaping would corrupt the payload.
export function unescapeSSE(data) {
  return data.replace(/\\n/g, "\n");
}

// The streaming sibling of `api._req`: fires a POST and returns the raw Response
// so the caller can read `resp.body` through `sseEvents`. Bypasses the `api`
// helper deliberately — `api._req` would consume the body with `.json()`.
// Non-ok handling is left to the caller: chat streams always return 200 (errors
// arrive in-band as an `error` event), while document/summarize check
// `resp.ok` and read the short error body themselves.
export function streamPost(path, body, signal) {
  return fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}
