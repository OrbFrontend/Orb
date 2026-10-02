let _epoch = null;
let readEpoch = () => _epoch;
let writeEpoch = (value) => {
  _epoch = value;
};
let onRefresh = () => {};

export function configureApiConcurrency({ getEpoch, setEpoch, refresh }) {
  readEpoch = getEpoch;
  writeEpoch = setEpoch;
  onRefresh = refresh;
}

export async function apiFetch(path, opts = {}) {
  const epoch = readEpoch();
  const headers = { ...opts.headers, ...(epoch ? { "X-Orb-Epoch": epoch } : {}) };
  const response = await fetch(path, { ...opts, headers });
  const current = response.headers?.get("X-Orb-Epoch");
  if (!readEpoch() && current) writeEpoch(current);
  if (response.status === 409 && response.clone) {
    const body = await response
      .clone()
      .json()
      .catch(() => null);
    if (body?.detail?.code === "refresh_required") {
      onRefresh();
    }
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
  post(p, b) {
    return this._req(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
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
