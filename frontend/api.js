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

export async function speakMessage(convId, msgId) {
  const r = await fetch(`/api/conversations/${convId}/messages/${msgId}/speak`, {
    method: "POST",
  });
  if (!r.ok) {
    const err = new Error(await r.text());
    err.status = r.status;
    throw err;
  }
  const extractedText = decodeURIComponent(r.headers.get("X-Orb-TTS-Extracted-Text") || "");
  const extractionMethod = r.headers.get("X-Orb-TTS-Extraction-Method") || "";
  const blob = await r.blob();
  return {
    audioUrl: URL.createObjectURL(blob),
    extractedText,
    extractionMethod,
  };
}

export async function getTtsBackends() {
  const r = await fetch("/api/tts/backends");
  if (!r.ok) return [];
  return r.json();
}

export async function getTtsVoices(backend, language) {
  const params = new URLSearchParams({ backend });
  if (language) params.set("language", language);
  const r = await fetch(`/api/tts/voices?${params}`);
  if (!r.ok) return [];
  return r.json();
}

export async function getVoiceProfile(cardId) {
  const r = await fetch(`/api/characters/${cardId}/voice-profile`);
  if (!r.ok) return null;
  return r.json();
}

export async function saveVoiceProfile(cardId, profile) {
  const r = await fetch(`/api/characters/${cardId}/voice-profile`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
