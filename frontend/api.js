// The dataset this page loaded from; a preset restore replaces it, and a write
// carrying the old one is refused with `refresh_required`.
let _epoch = null;
let _onRefreshRequired = () => {};

export function onRefreshRequired(fn) {
  _onRefreshRequired = fn;
}

export async function apiFetch(path, opts = {}) {
  const headers = { ...opts.headers, ...(_epoch && { "X-Orb-Epoch": _epoch }) };
  const response = await fetch(path, { ...opts, headers });
  _epoch ||= response.headers?.get("X-Orb-Epoch") || null;
  if (response.status === 409 && response.clone) {
    const body = await response
      .clone()
      .json()
      .catch(() => null);
    if (body?.detail?.code === "refresh_required") _onRefreshRequired();
  }
  return response;
}

function _detail(body) {
  try {
    const parsed = JSON.parse(body);
    const detail = parsed?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (typeof detail?.message === "string" && detail.message) return detail.message;
    if (Array.isArray(detail)) {
      return detail
        .map((entry) => entry?.msg)
        .filter(Boolean)
        .join("; ");
    }
  } catch {}
  return "";
}

export const api = {
  async _req(path, opts = {}) {
    const r = await apiFetch(`/api${path}`, opts);
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
  post(p, b, { signal } = {}) {
    return this._req(p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b),
      signal,
    });
  },
  put(p, b) {
    return this._req(p, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
  },
  del(p, b) {
    const opts = { method: "DELETE" };
    if (b !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(b);
    }
    return this._req(p, opts);
  },
  upload(p, file) {
    const fd = new FormData();
    fd.append("file", file);
    return this._req(p, { method: "POST", body: fd });
  },
};
