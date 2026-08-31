// Shared reader for server-sent event streams.

const FRAME_SEP = "\n\n";
const EVENT_PREFIX = "event: ";
const DATA_PREFIX = "data: ";

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
  } finally {
    if (signal) signal.removeEventListener("abort", onAbort);
    try {
      reader.releaseLock();
    } catch {}
  }
}

function parseFrame(frame) {
  if (!frame || frame.startsWith(":")) return null; // keepalive
  let event = null;
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith(EVENT_PREFIX)) event = line.slice(EVENT_PREFIX.length);
    else if (line.startsWith(DATA_PREFIX)) data = line.slice(DATA_PREFIX.length);
    else if (line === "event:") event = "";
    else if (line === "data:") data = "";
  }
  if (event === null) return null; // Ignore frames without an event name.
  return { event, data };
}

export function unescapeSSE(data) {
  return data.replace(/\\n/g, "\n");
}

export function streamPost(path, body, signal) {
  return fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}
