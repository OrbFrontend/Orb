// FastAPI puts the human half of a failure in `detail`. Unwrapped once, here, so
// `e.message` is the sentence at every call site — the alternative is the ~40
// existing `toast(e.message, true)` callers each showing a raw `{"detail":"..."}`,
// and a backend that took care to word a provider's rejection well being read
// through a JSON wrapper. The raw body stays on `err.body` for anything that wants it.
function _detail(body) {
  try {
    const parsed = JSON.parse(body);
    const detail = parsed?.detail;
    if (typeof detail === "string" && detail) return detail;
    // A 422 from request validation is a list of {loc, msg}, never a string.
    if (Array.isArray(detail)) {
      return detail
        .map((entry) => entry?.msg)
        .filter(Boolean)
        .join("; ");
    }
  } catch {
    // Not JSON — a proxy error page or an empty body. Fall through to the raw text.
  }
  return "";
}

export const api = {
  async _req(path, opts = {}) {
    const r = await fetch(`/api${path}`, opts);
    if (!r.ok) {
      const body = await r.text();
      const err = new Error(_detail(body) || body);
      err.status = r.status;
      err.body = body;
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
