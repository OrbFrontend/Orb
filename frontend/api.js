export const api = {
  async _req(path, opts = {}) {
    const r = await fetch(`/api${path}`, opts);
    if (!r.ok) {
      const body = await r.text();
      const err = new Error(body);
      err.status = r.status;
      throw err;
    }
    return r.json();
  },
  get(p, opts = {}) {
    return this._req(p, opts);
  },
  post(p, b, opts = {}) {
    return this._req(p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b),
      ...opts,
    });
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
