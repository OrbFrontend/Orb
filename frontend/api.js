export const api = {
  async _req(path, opts = {}) {
    const r = await fetch("/api" + path, opts);
    if (!r.ok) {
      const body = await r.text();
      const err = new Error(body);
      err.status = r.status;
      throw err;
    }
    return r.json();
  },
  get(p) {
    return this._req(p);
  },
  post(p, b) {
    return this._req(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
  },
  put(p, b) {
    return this._req(p, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
  },
  del(p) {
    return this._req(p, { method: "DELETE" });
  },
  upload(p, file) {
    const fd = new FormData();
    fd.append("file", file);
    return this._req(p, { method: "POST", body: fd });
  },
};

export function stopConversation(convId) {
  fetch(`/api/conversations/${convId}/stop`, { method: "POST" }).catch(() => {});
}

export async function getContextSize(convId) {
  const r = await fetch(`/api/conversations/${convId}/context-size`);
  if (!r.ok) return null;
  return r.json();
}

export function summarizeConversation(convId, { keepCount, customInstructions }, signal) {
  return fetch(`/api/conversations/${convId}/summarize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_count: keepCount, custom_instructions: customInstructions }),
    signal,
  });
}

export function streamPost(path, body, signal) {
  return fetch("/api" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}
