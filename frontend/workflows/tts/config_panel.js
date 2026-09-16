import {
  api,
  channelState,
  closeModal,
  convUrl,
  esc,
  getActiveConvId,
  getGroupCast,
  onChannel,
  playAudio,
  registerAction,
  requestRepaint,
  showModal,
  stopChannel,
} from "/static/workflow_api.js";
import { formatTime } from "./widget.js";

const WORKFLOW_ID = "tts";
const CHANNEL = "tts";
// Previews play on their own channel and report progress on the panel's status line, not the chat dock.
const PREVIEW_CHANNEL = "tts-preview";
// Playback start is silent when decoding fails, so a preview that never starts is called unplayable.
const PREVIEW_START_GRACE_MS = 1500;

// Which rows each backend shows. "voice" is the voice picker; "clone" is the
// built-in Spark cloner's upload control, which replaces it.
//
// `spark` (built-in) deliberately shows NEITHER rate nor pitch: Spark-TTS
// accepts prosody attributes only on its control path, and cloning bypasses
// that path, so those sliders would move and change nothing. `spark_remote` is
// the sidecar under its old field set, kept for profiles written before the
// built-in existed.
const BACKEND_FIELDS = {
  edge: ["voice", "language", "rate", "pitch"],
  kokoro: ["voice", "api_url", "language", "rate"],
  openai: ["voice", "api_url", "api_key", "model", "rate"],
  spark: ["clone"],
  spark_remote: ["voice", "api_url", "language", "rate", "pitch"],
  fish: ["voice", "api_url", "rate"],
  elevenlabs: ["voice", "api_key", "model"],
};

const DEFAULT_API_URL = {
  openai: "https://api.openai.com",
  fish: "http://localhost:8080",
  kokoro: "http://localhost:9200",
  spark_remote: "http://localhost:9300",
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
  registerAction(WORKFLOW_ID, "openSettings", () => openSettings());
  registerAction(WORKFLOW_ID, "closeSettings", () => closeModal());
  registerAction(WORKFLOW_ID, "cfgGlobal", () => saveGlobal());
  registerAction(WORKFLOW_ID, "backendChange", () => onBackendChange());
  registerAction(WORKFLOW_ID, "voiceReload", () => loadVoices());
  registerAction(WORKFLOW_ID, "profileSave", () => saveProfile());
  registerAction(WORKFLOW_ID, "preview", () => preview());
  registerAction(WORKFLOW_ID, "profileMember", (el) => selectMember(el));
  registerAction(WORKFLOW_ID, "voiceUpload", () => uploadVoiceReference());
  registerAction(WORKFLOW_ID, "voiceClear", () => clearVoiceReference());
  onChannel(PREVIEW_CHANNEL, onPreviewEvent);
}

let memberId = null;
let cardId = null; // the card the open profile belongs to; the clone API is keyed on it
// The enrolled voice is not an editable form control — it is written by the
// upload route and read back — so it lives here and readForm() carries it
// through unchanged, which is what stops a plain Save from wiping it.
let cloned = { tokens: [], name: "" };
let loadedProfile = null;
let previewRaf = null;
let previewPending = 0; // start deadline while a preview is decoding, 0 once it plays

function triggerUrl() {
  return convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger");
}

function castWithCards() {
  return (getGroupCast() || []).filter((member) => member.card_id);
}

function profileTarget() {
  return memberId ? { speaker_member_id: memberId } : {};
}

function selectMember(select) {
  const next = select.value;
  if (next === memberId) return;
  const dirty = loadedProfile && JSON.stringify(readForm()) !== JSON.stringify(loadedProfile);
  if (dirty && !window.confirm("Discard your unsaved changes to this voice?")) {
    select.value = memberId;
    return;
  }
  memberId = next;
  populateProfile();
}

function memberPickerHtml(cast) {
  const options = cast
    .map(
      (member) =>
        `<option value="${esc(member.id)}"${member.id === memberId ? " selected" : ""}>${esc(member.name)}</option>`,
    )
    .join("");
  return `<label class="tts-field">Cast member
      <select id="tts-pf-member" data-wf-action="tts:profileMember" data-wf-on="change">${options}</select>
    </label>`;
}

function query(action, extra) {
  return api.post(`/workflows/${WORKFLOW_ID}/query`, { action, ...extra });
}

export function configPanelRenderer() {
  return `<div class="tool-card-desc">Generate audio for dialogues.</div>
    <button class="btn btn-sm tool-card-btn" data-wf-action="tts:openSettings">Settings</button>`;
}

function settingsBodyHtml() {
  return `<h2>Text-to-Speech</h2>
    <div class="tts-settings">
      <section class="tts-section">
        <div class="tts-heading">Playback</div>
        <label class="tts-setting-toggle">
          <input type="checkbox" id="tts-cfg-autoplay"${cfg.auto_play ? " checked" : ""} data-wf-action="tts:cfgGlobal" data-wf-on="change">
          <span class="tts-toggle-body"><span class="tts-toggle-label">Play new speech automatically</span><span class="tts-note">Start audio as soon as a reply finishes generating.</span></span>
        </label>
        <label class="tts-field">Volume
          <div class="tts-range"><input type="range" min="0" max="1" step="0.05" id="tts-cfg-volume" value="${cfg.volume}" data-wf-action="tts:cfgGlobal" data-wf-on="change"><output for="tts-cfg-volume" id="tts-cfg-volume-value">${Math.round(cfg.volume * 100)}%</output></div>
        </label>
      </section>
      <section class="tts-section">
        <div class="tts-heading">Message interaction</div>
        <div class="tts-grid">
          <label class="tts-field">Click to speak
          <select id="tts-cfg-granularity" data-wf-action="tts:cfgGlobal" data-wf-on="change">
            <option value="none"${cfg.click_granularity === "none" ? " selected" : ""}>Off</option>
            <option value="message"${cfg.click_granularity === "message" ? " selected" : ""}>Whole message</option>
            <option value="block"${cfg.click_granularity === "block" ? " selected" : ""}>Block</option>
          </select>
          </label>
          <label class="tts-field">Click plays
          <select id="tts-cfg-playscope" data-wf-action="tts:cfgGlobal" data-wf-on="change">
            <option value="unit"${cfg.click_play_scope === "unit" ? " selected" : ""}>Clicked unit</option>
            <option value="whole"${cfg.click_play_scope === "whole" ? " selected" : ""}>Whole reply</option>
          </select>
          </label>
        </div>
        <label class="tts-setting-toggle">
          <input type="checkbox" id="tts-cfg-karaoke"${cfg.show_karaoke ? " checked" : ""} data-wf-action="tts:cfgGlobal" data-wf-on="change">
          <span class="tts-toggle-body"><span class="tts-toggle-label">Highlight words as they're spoken</span><span class="tts-note">Show the current word while speech is playing.</span></span>
        </label>
      </section>
      <section class="tts-section" id="tts-profile">
        <div class="tts-heading">Voice profile - This character only</div>
        <div id="tts-profile-content" class="tts-note">Loading voice settings…</div>
      </section>
    </div>
    <div class="modal-actions tts-settings-actions" id="tts-settings-actions">${settingsActionsHtml(false)}</div>`;
}

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
  const volumeValue = document.getElementById("tts-cfg-volume-value");
  if (volumeValue && volume) volumeValue.textContent = `${Math.round(parseFloat(volume.value) * 100)}%`;
  if (cfg.click_granularity !== prevGranularity) requestRepaint();
  api
    .put(`/workflows/${WORKFLOW_ID}/config`, {
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
  let el = document.getElementById("tts-profile-content");
  if (!el) return;
  setProfileActions(false); // every path below that shows a note instead of a form keeps just Close
  if (!getActiveConvId()) {
    el.innerHTML = `<div class="tts-note">Open a conversation to set its character's voice.</div>`;
    return;
  }
  const cast = getGroupCast() ? castWithCards() : null;
  if (cast) {
    if (!cast.length) {
      el.innerHTML = `<div class="tts-note">This scene has no character cards to give a voice.</div>`;
      return;
    }
    if (!cast.some((member) => member.id === memberId)) memberId = cast[0].id;
  } else {
    memberId = null;
  }
  let profile;
  let backends;
  let pr;
  try {
    const [loaded, bk] = await Promise.all([
      api.post(triggerUrl(), { action: "get_profile", ...profileTarget() }),
      query("list_backends"),
    ]);
    pr = loaded;
    profile = pr?.profile;
    backends = bk?.backends || [];
  } catch (e) {
    console.warn("tts: profile load failed", e);
    el = document.getElementById("tts-profile-content");
    if (el) el.innerHTML = `<div class="tts-note">Could not load voice settings.</div>`;
    return;
  }
  el = document.getElementById("tts-profile-content");
  if (!el) return;
  if (!profile) {
    el.innerHTML = `<div class="tts-note">This conversation has no character.</div>`;
    return;
  }
  cardId = pr?.character_id || null;
  cloned = { tokens: profile.speaker_tokens || [], name: profile.speaker_ref_name || "" };
  el.innerHTML = profileFormHtml(profile, backends, cast);
  setProfileActions(true);
  applyFieldVisibility(profile.backend);
  loadedProfile = readForm();
  loadVoices(profile.voice_id);
  if (BACKEND_FIELDS[profile.backend]?.includes("model")) loadModels(profile.model);
  refreshCloneStatus();
}

function opt(value, label, selected) {
  return `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label)}</option>`;
}

function field(name, inner) {
  return `<div class="tts-pf-field" data-field="${name}">${inner}</div>`;
}

function profileFormHtml(p, backends, cast = null) {
  const backendOpts = backends.map((b) => opt(b.id, b.name || b.id, b.id === p.backend)).join("");
  const langOpts = LANGUAGES.map(([code, label]) => opt(code, label, p.language?.startsWith(code))).join("");
  return `
    <div class="tts-profile-fields">
      ${cast ? memberPickerHtml(cast) : ""}
      <label class="tts-setting-toggle">
        <input type="checkbox" id="tts-pf-enabled"${p.enabled ? " checked" : ""}>
        <span class="tts-toggle-body"><span class="tts-toggle-label">Auto-generate speech for this character's replies</span><span class="tts-note">New replies will receive an audio clip automatically.</span></span>
      </label>
      <div class="tts-grid">
        <label class="tts-field">Backend
          <select id="tts-pf-backend" data-wf-action="tts:backendChange" data-wf-on="change">${backendOpts}</select>
        </label>
        ${field("language", `<label class="tts-field">Language <select id="tts-pf-language">${langOpts}</select></label>`)}
      </div>
      ${field("api_url", `<label class="tts-field">API URL <input type="text" id="tts-pf-api_url" value="${esc(p.api_url || "")}"></label>`)}
      ${field("api_key", `<label class="tts-field">API key <input type="password" id="tts-pf-api_key" value="${esc(p.api_key || "")}"></label>`)}
      ${field("model", `<label class="tts-field">Model <select id="tts-pf-model"><option value="${esc(p.model || "")}" selected>${esc(p.model || "(default)")}</option></select></label>`)}
      ${field(
        "voice",
        `<label class="tts-field">Voice
        <span class="tts-control-row"><select id="tts-pf-voice"><option value="${esc(p.voice_id || "")}" selected>${esc(p.voice_id || "(default)")}</option></select><button class="btn btn-sm" type="button" data-wf-action="tts:voiceReload">Reload</button></span>
      </label>`,
      )}
      ${field("clone", cloneControlHtml(p))}
      <div class="tts-grid">
        ${field("rate", `<label class="tts-field">Rate <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-rate" value="${esc(p.rate)}"></label>`)}
        ${field("pitch", `<label class="tts-field">Pitch <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-pitch" value="${esc(p.pitch)}"></label>`)}
      </div>
    </div>`;
}

// Why the built-in cloner cannot run, or "" when it can. Fetched once per
// panel open rather than per render: it is a fact about the install, not about
// the form, and it is what turns an Upload button that would fail into one
// that says which download is missing.
let cloneBlocker = "";

async function refreshCloneStatus() {
  try {
    const res = await query("voice_status");
    cloneBlocker = res?.ready ? "" : res?.reason || "";
  } catch (e) {
    console.warn("tts: voice status failed", e);
    cloneBlocker = "";
  }
  renderCloneStatus();
}

function cloneStatusHtml(p) {
  if (cloneBlocker) {
    return `<span class="tts-note">${esc(cloneBlocker)} Download it in Settings → Local ML.</span>`;
  }
  if (!p.speaker_tokens?.length) {
    return `<span class="tts-note">No voice uploaded yet. Pick a clip of this character speaking — only the first six seconds are used.</span>`;
  }
  const from = p.speaker_ref_name ? ` from ${esc(p.speaker_ref_name)}` : "";
  return `<span class="tts-note">Voice cloned${from}. Every reply from this character uses it.</span>`;
}

function cloneControlHtml(p) {
  return `<div class="tts-field">Cloned voice
      <span class="tts-control-row">
        <input type="file" id="tts-pf-voicefile" accept="audio/*,.wav,.flac,.mp3,.m4a,.ogg">
        <button class="btn btn-sm" type="button" data-wf-action="tts:voiceUpload">Upload</button>
        <button class="btn btn-sm" type="button" data-wf-action="tts:voiceClear"${p.speaker_tokens?.length ? "" : " disabled"}>Clear</button>
      </span>
      <div id="tts-pf-clone-status">${cloneStatusHtml(p)}</div>
    </div>`;
}

// Rate and pitch are impossible for a cloned voice rather than merely unused:
// Spark-TTS takes those attributes on its control path, which cloning bypasses.
// Redrawing the status in place keeps the file input and the rest of the form.
function renderCloneStatus() {
  const el = document.getElementById("tts-pf-clone-status");
  if (el) el.innerHTML = cloneStatusHtml(readForm());
  const clear = document.querySelector('[data-wf-action="tts:voiceClear"]');
  if (clear) clear.disabled = !readForm().speaker_tokens?.length;
}

async function uploadVoiceReference() {
  const input = document.getElementById("tts-pf-voicefile");
  const file = input?.files?.[0];
  if (!file) {
    setStatus("Choose an audio file first");
    return;
  }
  if (!cardId) {
    setStatus("This conversation has no character to give a voice");
    return;
  }
  setStatus("Enrolling voice…");
  setPreviewTime("");
  try {
    const res = await api.upload(`/characters/${encodeURIComponent(cardId)}/voice-reference`, file);
    // The route writes the profile itself (enrolling also selects this backend),
    // so the form is refilled from what was stored rather than from the guess
    // the panel would otherwise make about it.
    applyProfile(res?.profile);
    setStatus(res?.preview_error ? `Voice saved. ${res.preview_error}` : "Voice saved");
    if (res?.preview_b64) playPreview(res.preview_b64, res.mime || "audio/wav");
  } catch (e) {
    console.warn("tts: voice enrollment failed", e);
    setStatus(e?.message || "Voice enrollment failed");
  }
}

async function clearVoiceReference() {
  if (!cardId) return;
  if (!window.confirm("Remove this character's cloned voice?")) return;
  try {
    const res = await api.del(`/characters/${encodeURIComponent(cardId)}/voice-reference`);
    applyProfile(res?.profile);
    setStatus("Cloned voice removed");
  } catch (e) {
    console.warn("tts: voice clear failed", e);
    setStatus("Could not remove the cloned voice");
  }
}

// Push a server-owned profile back into the open form. Only the fields the
// clone flow actually changes, so a half-edited API key beside it survives.
function applyProfile(profile) {
  if (!profile) return;
  cloned = { tokens: profile.speaker_tokens || [], name: profile.speaker_ref_name || "" };
  const backend = document.getElementById("tts-pf-backend");
  if (backend && profile.backend) backend.value = profile.backend;
  const voice = document.getElementById("tts-pf-voice");
  if (voice && profile.voice_id) voice.innerHTML = opt(profile.voice_id, profile.voice_id, true);
  applyFieldVisibility(profile.backend || backend?.value || "edge");
  renderCloneStatus();
  loadedProfile = readForm();
}

// Footer order follows the other modals: the secondary action sits far left with the status
// text, then Close and the primary action on the right. Voice buttons need a profile form.
function settingsActionsHtml(hasProfile) {
  return `
    ${hasProfile ? `<button class="btn" type="button" data-wf-action="tts:preview">Preview</button>` : ""}
    <span id="tts-pf-status" aria-live="polite"></span>
    <span id="tts-pf-time" aria-hidden="true"></span>
    <button class="btn" data-wf-action="tts:closeSettings">Close</button>
    ${hasProfile ? `<button class="btn btn-accent" type="button" data-wf-action="tts:profileSave">Save voice</button>` : ""}`;
}

function setProfileActions(hasProfile) {
  const el = document.getElementById("tts-settings-actions");
  if (el) el.innerHTML = settingsActionsHtml(hasProfile);
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
    speaker_tokens: cloned.tokens,
    speaker_ref_name: cloned.name,
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
  renderCloneStatus();
  loadVoices();
  if (BACKEND_FIELDS[backend]?.includes("model")) loadModels();
}

async function loadVoices(selectId) {
  const sel = document.getElementById("tts-pf-voice");
  if (!sel) return;
  const f = readForm();
  const want = selectId != null ? selectId : sel.value;
  try {
    const res = await query("list_voices", {
      backend: f.backend,
      language: f.language,
      api_url: f.api_url,
      api_key: f.api_key,
      // The built-in cloner has no catalog to list: its one voice is whatever
      // this character has enrolled, which only the form knows.
      speaker_tokens: f.speaker_tokens,
      speaker_ref_name: f.speaker_ref_name,
    });
    const voices = res?.voices || [];
    if (!voices.length) return;
    const live = document.getElementById("tts-pf-voice");
    if (live) live.innerHTML = voices.map((v) => opt(v.id, v.name || v.id, v.id === want)).join("");
  } catch (e) {
    console.warn("tts: voice list failed", e);
  }
}

async function loadModels(selectId) {
  const sel = document.getElementById("tts-pf-model");
  if (!sel) return;
  const f = readForm();
  const want = selectId != null ? selectId : sel.value;
  try {
    const res = await query("list_models", {
      backend: f.backend,
      api_url: f.api_url,
      api_key: f.api_key,
    });
    const models = res?.models || [];
    if (!models.length) return;
    const live = document.getElementById("tts-pf-model");
    if (live) live.innerHTML = models.map((m) => opt(m.id || m, m.name || m.id || m, (m.id || m) === want)).join("");
  } catch (e) {
    console.warn("tts: model list failed", e);
  }
}

async function saveProfile() {
  if (!getActiveConvId()) return;
  const status = document.getElementById("tts-pf-status");
  try {
    const saved = readForm();
    const res = await api.post(triggerUrl(), { action: "set_profile", profile: saved, ...profileTarget() });
    if (!res?.error) loadedProfile = saved;
    if (status) status.textContent = res?.error ? res.error : "Saved";
  } catch (e) {
    console.error("tts: profile save failed", e);
    if (status) status.textContent = "Save failed";
  }
}

function statusLine() {
  return document.getElementById("tts-pf-status");
}

function setStatus(text) {
  const el = statusLine();
  if (el) el.textContent = text;
}

function setPreviewTime(text) {
  const el = document.getElementById("tts-pf-time");
  if (el) el.textContent = text;
}

function onPreviewEvent(ev) {
  if (ev.type === "play") {
    previewPending = 0;
    setStatus("Playing preview");
    armPreviewRaf();
    return;
  }
  if (ev.type !== "close" || ev.reason === "superseded") return; // a newer preview owns the line
  cancelPreviewRaf();
  setPreviewTime("");
  setStatus(ev.reason === "ended" ? "Preview finished" : "");
}

function armPreviewRaf() {
  if (previewRaf == null) previewRaf = requestAnimationFrame(tickPreview);
}

// Only the elapsed/duration readout ticks here; play and close events own the status text.
function tickPreview(now) {
  previewRaf = null;
  if (!statusLine()) {
    stopChannel(PREVIEW_CHANNEL); // the panel showing this preview is gone
    return;
  }
  const st = channelState(PREVIEW_CHANNEL);
  if (st?.playing) {
    setPreviewTime(`${formatTime(st.stream.elapsedSec)} / ${formatTime(st.stream.durationSec)}`);
  } else if (!previewPending) {
    return;
  } else if (now > previewPending) {
    previewPending = 0;
    setStatus("Preview audio could not be played");
    return;
  }
  armPreviewRaf();
}

function playPreview(b64, mime) {
  stopChannel(CHANNEL); // a preview should not talk over a message that is playing
  previewPending = performance.now() + PREVIEW_START_GRACE_MS;
  playAudio({
    channel: PREVIEW_CHANNEL,
    segments: [{ b64, mime: mime || "audio/wav" }],
    volume: cfg.volume,
    source: { label: "Voice preview", dock: false },
  });
  armPreviewRaf();
}

function cancelPreviewRaf() {
  if (previewRaf != null) cancelAnimationFrame(previewRaf);
  previewRaf = null;
}

async function preview() {
  if (!statusLine()) return;
  setStatus("Generating preview…");
  setPreviewTime("");
  try {
    const res = await query("preview", readForm());
    if (!statusLine()) return;
    if (!res?.audio_b64) {
      setStatus(res?.error || "Preview failed");
      return;
    }
    playPreview(res.audio_b64, res.mime);
  } catch (e) {
    console.error("tts: preview failed", e);
    setStatus("Preview failed");
  }
}
