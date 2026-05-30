// Tools-panel "Secondary" card. Two sections: the global config slot
// (volume / click-to-speak / karaoke) read and written through the workflow
// config route, and the active conversation's per-character voice profile read
// and written through the on-demand trigger.

import { S } from "/static/state.js";
import { api } from "/static/api.js";
import { convUrl, esc } from "/static/utils.js";
import { playAudio } from "/static/audio_player.js";
import { showModal } from "/static/modal.js";
import { renderMessages } from "/static/chat.js";

const WORKFLOW_ID = "tts";

// Which profile fields each backend honors. Backend, voice, and the enable
// toggle are always shown; everything here is shown only for the listed
// backends.
const BACKEND_FIELDS = {
  edge: ["language", "rate", "pitch"],
  kokoro: ["api_url", "language", "rate"],
  openai: ["api_url", "api_key", "model", "rate"],
  fish: ["api_url", "rate"],
  elevenlabs: ["api_key", "model"],
};

// Filled into an empty api_url when the user first selects a self-hosted or
// cloud backend, so the common case needs no typing.
const DEFAULT_API_URL = {
  openai: "https://api.openai.com",
  fish: "http://localhost:8080",
  kokoro: "http://localhost:9200",
};

const LANGUAGES = [
  ["en", "English"],
  ["de", "German"],
  ["es", "Spanish"],
  ["fr", "French"],
  ["ja", "Japanese"],
  ["ko", "Korean"],
  ["zh", "Chinese"],
  ["pt", "Portuguese"],
  ["ru", "Russian"],
  ["it", "Italian"],
];

let cfg = { auto_play: false, volume: 0.75, click_granularity: "block", click_play_scope: "unit", show_karaoke: true };

export function initConfigPanel(sharedConfig) {
  cfg = sharedConfig;
  window.ttsOpenSettings = openSettings;
  window.ttsCfgGlobal = saveGlobal;
  window.ttsBackendChange = onBackendChange;
  window.ttsVoiceReload = loadVoices;
  window.ttsProfileSave = saveProfile;
  window.ttsPreview = preview;
}

function triggerUrl() {
  return convUrl(S.activeConvId, "workflows", WORKFLOW_ID, "trigger");
}

// Tools-panel card: one compact entry that opens the settings in a modal, so a
// shipped workflow occupies a single row in the shared panel rather than
// spilling its whole form into it.
export function configPanelRenderer() {
  return `<div class="tool-card">
    <div class="tool-card-header">
      <span class="tool-card-name">Text-to-Speech</span>
      <button class="tts-settings-btn" onclick="window.ttsOpenSettings()">Settings</button>
    </div>
    <div class="tool-card-desc">Generate and play spoken audio for assistant replies.</div>
  </div>`;
}

function settingsBodyHtml() {
  return `<h2>Text-to-Speech</h2>
    <div class="tts-config">
      <div class="tts-config-section">
        <div class="tts-config-heading">Speech</div>
        <label class="tts-config-row"><input type="checkbox" id="tts-cfg-autoplay"${cfg.auto_play ? " checked" : ""} onchange="window.ttsCfgGlobal()"> Play new speech automatically</label>
        <label class="tts-config-row">Volume <input type="range" min="0" max="1" step="0.05" id="tts-cfg-volume" value="${cfg.volume}" onchange="window.ttsCfgGlobal()"></label>
        <label class="tts-config-row"><input type="checkbox" id="tts-cfg-karaoke"${cfg.show_karaoke ? " checked" : ""} onchange="window.ttsCfgGlobal()"> Highlight words as they're spoken</label>
        <label class="tts-config-row">Click to speak
          <select id="tts-cfg-granularity" onchange="window.ttsCfgGlobal()">
            <option value="none"${cfg.click_granularity === "none" ? " selected" : ""}>Off</option>
            <option value="message"${cfg.click_granularity === "message" ? " selected" : ""}>Whole message</option>
            <option value="block"${cfg.click_granularity === "block" ? " selected" : ""}>Block</option>
          </select>
        </label>
        <label class="tts-config-row">Click plays
          <select id="tts-cfg-playscope" onchange="window.ttsCfgGlobal()">
            <option value="unit"${cfg.click_play_scope === "unit" ? " selected" : ""}>Clicked unit</option>
            <option value="whole"${cfg.click_play_scope === "whole" ? " selected" : ""}>Whole reply</option>
          </select>
        </label>
      </div>
      <div class="tts-config-section" id="tts-profile">Loading voice settings...</div>
    </div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`;
}

// Opens the settings modal. The per-character section is filled after the modal
// mounts, and refetched on every open -- so it always reflects the active
// conversation's character without needing a re-render hook.
function openSettings() {
  showModal(settingsBodyHtml());
  setTimeout(populateProfile, 0);
}

function saveGlobal() {
  const autoplay = document.getElementById("tts-cfg-autoplay");
  const volume = document.getElementById("tts-cfg-volume");
  const granularity = document.getElementById("tts-cfg-granularity");
  const playscope = document.getElementById("tts-cfg-playscope");
  const karaoke = document.getElementById("tts-cfg-karaoke");
  const prevGranularity = cfg.click_granularity;
  if (autoplay) cfg.auto_play = autoplay.checked;
  if (volume) cfg.volume = parseFloat(volume.value);
  if (granularity) cfg.click_granularity = granularity.value;
  if (playscope) cfg.click_play_scope = playscope.value;
  if (karaoke) cfg.show_karaoke = karaoke.checked;
  // Clickable-word marking is applied per render and not torn down live, so a
  // granularity change must repaint to add or clear the affordance on the
  // already-rendered messages.
  if (cfg.click_granularity !== prevGranularity) renderMessages();
  // The config slot is replaced wholesale on write, so every key must be sent
  // or an omitted one reverts to its default.
  api
    .put("/workflows/" + WORKFLOW_ID + "/config", {
      config: {
        auto_play: cfg.auto_play,
        volume: cfg.volume,
        click_granularity: cfg.click_granularity,
        click_play_scope: cfg.click_play_scope,
        show_karaoke: cfg.show_karaoke,
      },
    })
    .catch((e) => console.warn("tts config save failed", e));
}

async function populateProfile() {
  let el = document.getElementById("tts-profile");
  if (!el) return;
  if (!S.activeConvId) {
    el.innerHTML = `<div class="tts-config-note">Open a conversation to set its character's voice.</div>`;
    return;
  }
  let profile;
  let backends;
  try {
    const [pr, bk] = await Promise.all([
      api.post(triggerUrl(), { action: "get_profile" }),
      api.post(triggerUrl(), { action: "list_backends" }),
    ]);
    profile = pr && pr.profile;
    backends = (bk && bk.backends) || [];
  } catch (e) {
    console.warn("tts: profile load failed", e);
    el = document.getElementById("tts-profile");
    if (el) el.innerHTML = `<div class="tts-config-note">Could not load voice settings.</div>`;
    return;
  }
  el = document.getElementById("tts-profile");
  if (!el) return;
  if (!profile) {
    el.innerHTML = `<div class="tts-config-note">This conversation has no character.</div>`;
    return;
  }
  el.innerHTML = profileFormHtml(profile, backends);
  applyFieldVisibility(profile.backend);
  loadVoices(profile.voice_id);
  if (BACKEND_FIELDS[profile.backend]?.includes("model")) loadModels(profile.model);
}

function opt(value, label, selected) {
  return `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label)}</option>`;
}

function field(name, inner) {
  return `<div class="tts-pf-field" data-field="${name}">${inner}</div>`;
}

function profileFormHtml(p, backends) {
  const backendOpts = backends.map((b) => opt(b.id, b.name || b.id, b.id === p.backend)).join("");
  const langOpts = LANGUAGES.map(([code, label]) => opt(code, label, p.language && p.language.startsWith(code))).join(
    "",
  );
  return `
    <div class="tts-config-heading">Voice (this character)</div>
    <label class="tts-config-row"><input type="checkbox" id="tts-pf-enabled"${p.enabled ? " checked" : ""}> Auto-generate speech for this character's replies</label>
    <label class="tts-config-row">Backend
      <select id="tts-pf-backend" onchange="window.ttsBackendChange()">${backendOpts}</select>
    </label>
    ${field("api_url", `<label class="tts-config-row">API URL <input type="text" id="tts-pf-api_url" value="${esc(p.api_url || "")}"></label>`)}
    ${field("api_key", `<label class="tts-config-row">API key <input type="password" id="tts-pf-api_key" value="${esc(p.api_key || "")}"></label>`)}
    ${field("model", `<label class="tts-config-row">Model <select id="tts-pf-model"><option value="${esc(p.model || "")}" selected>${esc(p.model || "(default)")}</option></select></label>`)}
    ${field("language", `<label class="tts-config-row">Language <select id="tts-pf-language">${langOpts}</select></label>`)}
    <label class="tts-config-row">Voice
      <select id="tts-pf-voice"><option value="${esc(p.voice_id || "")}" selected>${esc(p.voice_id || "(default)")}</option></select>
      <button type="button" onclick="window.ttsVoiceReload()">Reload</button>
    </label>
    ${field("rate", `<label class="tts-config-row">Rate <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-rate" value="${esc(p.rate)}"></label>`)}
    ${field("pitch", `<label class="tts-config-row">Pitch <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-pitch" value="${esc(p.pitch)}"></label>`)}
    <div class="tts-config-row">
      <button type="button" onclick="window.ttsProfileSave()">Save voice</button>
      <button type="button" onclick="window.ttsPreview()">Preview</button>
      <span id="tts-pf-status"></span>
    </div>`;
}

function applyFieldVisibility(backend) {
  const shown = BACKEND_FIELDS[backend] || [];
  for (const el of document.querySelectorAll("#tts-profile .tts-pf-field")) {
    el.style.display = shown.includes(el.dataset.field) ? "" : "none";
  }
}

function readForm() {
  const val = (id) => document.getElementById(id);
  return {
    enabled: !!val("tts-pf-enabled")?.checked,
    backend: val("tts-pf-backend")?.value || "edge",
    voice_id: val("tts-pf-voice")?.value || "",
    language: val("tts-pf-language")?.value || "en",
    model: val("tts-pf-model")?.value || "",
    api_url: val("tts-pf-api_url")?.value || "",
    api_key: val("tts-pf-api_key")?.value || "",
    rate: parseFloat(val("tts-pf-rate")?.value) || 1.0,
    pitch: parseFloat(val("tts-pf-pitch")?.value) || 1.0,
  };
}

function onBackendChange() {
  const backend = document.getElementById("tts-pf-backend")?.value || "edge";
  const apiUrl = document.getElementById("tts-pf-api_url");
  if (apiUrl && !apiUrl.value && DEFAULT_API_URL[backend]) apiUrl.value = DEFAULT_API_URL[backend];
  applyFieldVisibility(backend);
  loadVoices();
  if (BACKEND_FIELDS[backend]?.includes("model")) loadModels();
}

async function loadVoices(selectId) {
  const sel = document.getElementById("tts-pf-voice");
  if (!sel || !S.activeConvId) return;
  const f = readForm();
  const want = selectId != null ? selectId : sel.value;
  try {
    const res = await api.post(triggerUrl(), {
      action: "list_voices",
      backend: f.backend,
      language: f.language,
      api_url: f.api_url,
      api_key: f.api_key,
    });
    const voices = (res && res.voices) || [];
    if (!voices.length) return;
    const live = document.getElementById("tts-pf-voice");
    if (live) live.innerHTML = voices.map((v) => opt(v.id, v.name || v.id, v.id === want)).join("");
  } catch (e) {
    console.warn("tts: voice list failed", e);
  }
}

async function loadModels(selectId) {
  const sel = document.getElementById("tts-pf-model");
  if (!sel || !S.activeConvId) return;
  const f = readForm();
  const want = selectId != null ? selectId : sel.value;
  try {
    const res = await api.post(triggerUrl(), {
      action: "list_models",
      backend: f.backend,
      api_url: f.api_url,
      api_key: f.api_key,
    });
    const models = (res && res.models) || [];
    if (!models.length) return;
    const live = document.getElementById("tts-pf-model");
    if (live) live.innerHTML = models.map((m) => opt(m.id || m, m.name || m.id || m, (m.id || m) === want)).join("");
  } catch (e) {
    console.warn("tts: model list failed", e);
  }
}

async function saveProfile() {
  if (!S.activeConvId) return;
  const status = document.getElementById("tts-pf-status");
  try {
    const res = await api.post(triggerUrl(), { action: "set_profile", profile: readForm() });
    if (status) status.textContent = res && res.error ? res.error : "Saved";
  } catch (e) {
    console.error("tts: profile save failed", e);
    if (status) status.textContent = "Save failed";
  }
}

async function preview() {
  if (!S.activeConvId) return;
  const status = document.getElementById("tts-pf-status");
  try {
    const res = await api.post(triggerUrl(), { action: "preview", ...readForm() });
    if (res && res.audio_b64) {
      playAudio({ channel: WORKFLOW_ID, segments: [{ b64: res.audio_b64, mime: res.mime }], volume: cfg.volume });
    } else if (status) {
      status.textContent = (res && res.error) || "Preview failed";
    }
  } catch (e) {
    console.error("tts: preview failed", e);
    if (status) status.textContent = "Preview failed";
  }
}
