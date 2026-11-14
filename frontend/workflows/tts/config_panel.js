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
  toast,
} from "/static/workflow_api.js";
import { formatTime } from "./widget.js";

const WORKFLOW_ID = "tts";
const CHANNEL = "tts";
const PREVIEW_CHANNEL = "tts-preview";
const PREVIEW_START_GRACE_MS = 1500;

const BACKEND_FIELDS = {
  edge: ["voice", "language", "rate", "pitch"],
  kokoro: ["voice", "api_url", "language", "rate"],
  openai: ["voice", "api_url", "api_key", "model", "rate"],
  spark: ["clone"],
  spark_remote: ["voice", "api_url", "language", "rate", "pitch"],
  fish: ["voice", "api_url", "rate"],
  zonos: ["clone", "api_url", "language", "delivery"],
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
  registerAction(WORKFLOW_ID, "voicePick", (el) => enrollFile(el.files?.[0]));
  registerAction(WORKFLOW_ID, "voiceDrop", (el, ev) => onVoiceDrop(el, ev));
  registerAction(WORKFLOW_ID, "voiceClear", () => clearVoiceReference());
  registerAction(WORKFLOW_ID, "cloneSetup", (el) => runCloneSetup(el.dataset.setup));
  registerAction(WORKFLOW_ID, "cloneGpu", (el) => saveCloneGpu(el));
  registerAction(WORKFLOW_ID, "apiUrlInput", () => renderCloneControl());
  registerAction(WORKFLOW_ID, "cloneMode", (el) => selectCloneMode(el.dataset.mode));
  registerAction(WORKFLOW_ID, "referenceText", (el) => {
    cloned.referenceText = el.value;
  });
  registerAction(WORKFLOW_ID, "playExcerpt", () => playExcerpt());
  registerAction(WORKFLOW_ID, "strengthInput", (el) => showStrength(el.value));
  onChannel(PREVIEW_CHANNEL, onPreviewEvent);
}

const CLONE_FEATURES = [
  { id: "spark_tts_codec", label: "voice codec" },
  { id: "spark_tts_llm", label: "voice model" },
];
const ADVANCED_FEATURES = [
  { id: "spark_tts_reference", label: "reference reader" },
  { id: "speech_recognizer", label: "speech recognizer" },
];
const ZONOS_FEATURES = [
  { id: "zonos_tts", label: "voice model" },
  { id: "zonos_reference", label: "reference reader" },
];
const SEPARATOR_FEATURES = [{ id: "vocal_separator", label: "vocal separator" }];
const SEMANTIC_RATE = 50;

let memberId = null;
let cardId = null;
let mlStatus = null;
let mlPending = false;
let setupBusy = "";
let setupStep = "";
let enrolling = "";
let cloned = emptyClone();
let referenceNote = "";
let loadedProfile = null;
let previewRaf = null;
let previewPending = 0;
let previewWhat = "Preview";

function emptyClone() {
  return { tokens: [], name: "", mode: "basic", referenceTokens: [], referenceText: "", codes: [], zonosName: "" };
}

function cloneFromProfile(profile) {
  return {
    tokens: profile.speaker_tokens || [],
    name: profile.speaker_ref_name || "",
    mode: profile.clone_mode === "advanced" ? "advanced" : "basic",
    referenceTokens: profile.reference_tokens || [],
    referenceText: profile.reference_text || "",
    codes: profile.zonos_reference_codes || [],
    zonosName: profile.zonos_ref_name || "",
  };
}

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
  return `<div class="tool-card-desc">Generates audio for dialogue.</div>
    <button class="btn btn-sm tool-card-btn" data-wf-action="tts:openSettings">Settings</button>`;
}

function settingsBodyHtml() {
  return `<h2>Text-to-Speech</h2>
    <div class="tts-settings">
      <section class="tts-section">
        <div class="modal-heading" role="heading" aria-level="3">Playback</div>
        <label class="tts-setting-toggle">
          <input type="checkbox" id="tts-cfg-autoplay"${cfg.auto_play ? " checked" : ""} data-wf-action="tts:cfgGlobal" data-wf-on="change">
          <span class="tts-toggle-body"><span class="tts-toggle-label">Play new speech automatically</span><span class="tts-note">Start audio as soon as a reply finishes generating.</span></span>
        </label>
        <label class="tts-field">Volume
          <div class="tts-range"><input type="range" min="0" max="1" step="0.05" id="tts-cfg-volume" value="${cfg.volume}" data-wf-action="tts:cfgGlobal" data-wf-on="change"><output for="tts-cfg-volume" id="tts-cfg-volume-value">${Math.round(cfg.volume * 100)}%</output></div>
        </label>
      </section>
      <section class="tts-section">
        <div class="modal-heading" role="heading" aria-level="3">Message interaction</div>
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
        <div class="modal-heading" role="heading" aria-level="3">Voice profile · this character only</div>
        <div id="tts-profile-content" class="tts-note">Loading voice settings…</div>
      </section>
    </div>
    <div class="modal-actions tts-settings-actions" id="tts-settings-actions">${settingsActionsHtml(false)}</div>`;
}

function openSettings() {
  // Refresh status each time the modal opens.
  mlStatus = null;
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
  cloned = cloneFromProfile(profile);
  referenceNote = "";
  el.innerHTML = profileFormHtml(profile, backends, cast);
  setProfileActions(true);
  applyFieldVisibility(profile.backend);
  loadedProfile = readForm();
  ensureMlStatus(); // a profile that opens on `spark` gates its control on this
  loadVoices(profile.voice_id);
  if (BACKEND_FIELDS[profile.backend]?.includes("model")) loadModels(profile.model);
}

function opt(value, label, selected) {
  return `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label)}</option>`;
}

function field(name, inner) {
  return `<div class="tts-pf-field" data-field="${name}">${inner}</div>`;
}

function apiUrlLabel(backend) {
  return backend === "zonos" ? "Server URL (empty: built-in)" : "API URL";
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
      ${field("api_url", `<label class="tts-field"><span id="tts-pf-api_url-label">${apiUrlLabel(p.backend)}</span> <input type="text" id="tts-pf-api_url" value="${esc(p.api_url || "")}" data-wf-action="tts:apiUrlInput" data-wf-on="input"></label>`)}
      ${field("api_key", `<label class="tts-field">API key <input type="password" id="tts-pf-api_key" value="${esc(p.api_key || "")}"></label>`)}
      ${field("model", `<label class="tts-field">Model <select id="tts-pf-model"><option value="${esc(p.model || "")}" selected>${esc(p.model || "(default)")}</option></select></label>`)}
      ${field(
        "voice",
        `<label class="tts-field">Voice
        <span class="tts-control-row"><select id="tts-pf-voice"><option value="${esc(p.voice_id || "")}" selected>${esc(p.voice_id || "(default)")}</option></select><button class="btn btn-sm" type="button" data-wf-action="tts:voiceReload">Reload</button></span>
      </label>`,
      )}
      ${field("clone", `<div id="tts-pf-clone">${cloneControlHtml(p)}</div>`)}
      ${field("delivery", `<div id="tts-pf-delivery-control" class="tts-delivery">${deliveryControlHtml(p)}</div>`)}
      <div class="tts-grid">
        ${field("rate", `<label class="tts-field">Rate <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-rate" value="${esc(p.rate)}"></label>`)}
        ${field("pitch", `<label class="tts-field">Pitch <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-pitch" value="${esc(p.pitch)}"></label>`)}
      </div>
    </div>`;
}

// --- Emotional delivery -------------------------------------------------------

function deliveryBlocker() {
  if (!mlStatus) return "";
  const info = mlFeature("emotion_classifier");
  if (!info.deps_ok) return "Needs local model support, which this install lacks.";
  if (!info.present) return "Needs the Character Expressions model: download it under Settings › Expression Playback.";
  if (!info.enabled) return "Needs the Character Expressions model: turn it on under Settings › Expression Playback.";
  return "";
}

function deliveryControlHtml(p) {
  const blocker = deliveryBlocker();
  const strength = Number.isFinite(p.delivery_strength) ? p.delivery_strength : 1;
  return `<label class="tts-setting-toggle">
      <input type="checkbox" id="tts-pf-delivery"${p.emotion_delivery !== false ? " checked" : ""}${blocker ? " disabled" : ""}>
      <span class="tts-toggle-body"><span class="tts-toggle-label">Emotional delivery</span><span class="tts-note">${esc(blocker || "Each line is spoken with the emotion of its scene.")}</span></span>
    </label>
    <label class="tts-field">Strength
      <div class="tts-range"><input type="range" min="0" max="2" step="0.1" id="tts-pf-strength" value="${esc(strength)}" data-wf-action="tts:strengthInput" data-wf-on="input"><output for="tts-pf-strength" id="tts-pf-strength-value">${strengthLabel(strength)}</output></div>
    </label>`;
}

function strengthLabel(value) {
  return `${Number(value).toFixed(1)}×`;
}

function showStrength(value) {
  const out = document.getElementById("tts-pf-strength-value");
  if (out) out.textContent = strengthLabel(value);
}

function renderDeliveryControl() {
  const el = document.getElementById("tts-pf-delivery-control");
  if (el) el.innerHTML = deliveryControlHtml(readForm());
}

// --- Cloned voice control ----------------------------------------------------

function mlFeature(id) {
  return mlStatus?.features?.[id] || {};
}

/** Return whether one half of the cloner is usable. */
function featureReady(id) {
  if (!mlStatus) return true;
  const info = mlFeature(id);
  return Boolean(info.present && info.enabled && info.deps_ok && info.runtime_ok !== false);
}

/** Fetch local model status for this panel. */
function ensureMlStatus({ refresh = false } = {}) {
  if ((mlStatus && !refresh) || mlPending) return;
  if (!["spark", "zonos"].includes(document.getElementById("tts-pf-backend")?.value)) return;
  mlPending = true;
  api
    .get("/local-ml/status")
    .then((res) => {
      mlStatus = res;
    })
    .catch((e) => console.warn("tts: local-ml status failed", e))
    .finally(() => {
      mlPending = false;
      renderCloneControl();
      renderDeliveryControl();
    });
}

/** What the selected backend's cloned voice needs and how it enrolls. ZONOS3 needs no local models with a server URL set. */
function cloner(form = readForm()) {
  if (form.backend === "zonos") {
    const builtin = !(form.api_url || "").trim();
    return {
      name: "ZONOS3",
      enrolled: cloned.codes.length > 0,
      clip: cloned.zonosName,
      features: builtin ? ZONOS_FEATURES : [],
      enrollWith: builtin ? ZONOS_FEATURES : [],
      engine: builtin ? "zonos_tts" : "",
      runtime: {
        name: "ZONOS3 runtime",
        mb: mlFeature("zonos_tts").runtime_mb || 110,
        body: { runtime: "zonos_server" },
      },
      upload: `backend=zonos&api_url=${encodeURIComponent(form.api_url)}`,
    };
  }
  const advanced = cloned.mode === "advanced";
  return {
    name: advanced ? "Advanced cloning" : "Voice cloning",
    enrolled: cloned.tokens.length > 0,
    clip: cloned.name,
    features: advanced ? [...CLONE_FEATURES, ...ADVANCED_FEATURES] : CLONE_FEATURES,
    enrollWith: CLONE_FEATURES.slice(0, 1),
    engine: "spark_tts_llm",
    runtime: { name: "llama.cpp runtime", mb: 150, body: {} },
    upload: `mode=${cloned.mode}`,
  };
}

/** Whether a clip can be enrolled right now. */
function cloneLive(c = cloner()) {
  return c.enrollWith.every((f) => featureReady(f.id));
}

function runtimeMissing(c = cloner()) {
  return c.features.length > 0 && mlFeature(c.engine).runtime_ok === false;
}

function setupNoticeHtml(c) {
  if (!mlStatus) return "";
  const pending = c.features.filter((f) => !featureReady(f.id));
  const unavailable = pending.find((f) => !mlFeature(f.id).deps_ok);
  if (unavailable) {
    const cmd = mlFeature(unavailable.id).install_cmd || "pip install -r requirements-onnx.txt";
    return `<span class="tts-note">Voice cloning needs the optional ML extras: <code>${esc(cmd)}</code></span>`;
  }
  const missing = pending.filter((f) => !mlFeature(f.id).present);
  const runtime = runtimeMissing(c);
  if (!pending.length && !runtime) return "";
  const names = [
    ...(runtime ? [c.runtime.name] : []),
    ...missing.map((f) => f.label),
    ...pending.filter((f) => mlFeature(f.id).present && !mlFeature(f.id).enabled).map((f) => f.label),
  ];
  const size = missing.reduce((mb, f) => mb + (mlFeature(f.id).size_mb || 0), runtime ? c.runtime.mb : 0);
  const sentence = (setupBusy === "models" && setupStep) || `${c.name} needs ${listPhrase(names)}.`;
  return `<span class="tts-setup">
      <span class="tts-note">${sentence}</span>
      <button class="btn btn-sm" type="button" data-wf-action="tts:cloneSetup"${setupBusy ? " disabled" : ""}>${setupLabel(Boolean(missing.length || runtime), size)}</button>
    </span>`;
}

function listPhrase(items) {
  if (items.length < 3) return items.join(" and ");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function setupLabel(download, sizeMb) {
  if (setupBusy) return download ? "Downloading…" : "Turning on…";
  if (!download) return "Turn on";
  if (sizeMb >= 1000) return `Download (${(sizeMb / 1000).toFixed(1)} GB)`;
  return sizeMb ? `Download (${sizeMb} MB)` : "Download";
}

const ICON_WAVE = `<svg class="tts-drop-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M2 10v3"/><path d="M6 6v11"/><path d="M10 3v18"/><path d="M14 8v7"/><path d="M18 5v13"/><path d="M22 10v3"/></svg>`;

function cloneTabsHtml() {
  const tab = (mode, label) => {
    const on = cloned.mode === mode;
    return `<button type="button" class="tab${on ? " active" : ""}" role="tab" aria-selected="${on}" data-mode="${mode}" data-wf-action="tts:cloneMode">${label}</button>`;
  };
  return `<div class="tabs tts-clone-tabs" role="tablist" aria-label="Cloning mode">${tab("basic", "Basic")}${tab("advanced", "Advanced")}</div>`;
}

function separatorSetupHtml() {
  if (!mlStatus || featureReady("vocal_separator")) return "";
  const info = mlFeature("vocal_separator");
  if (!info.deps_ok) return "";
  const sentence =
    (setupBusy === "separator" && setupStep) ||
    "To clean music out from under the voice, Orb needs the vocal separator.";
  return `<span class="tts-setup">
      <span class="tts-note">${sentence}</span>
      <button class="btn btn-sm" type="button" data-setup="separator" data-wf-action="tts:cloneSetup"${setupBusy ? " disabled" : ""}>${setupLabel(!info.present, info.size_mb)}</button>
    </span>`;
}

function cloneControlHtml(p) {
  const c = cloner(p);
  const zonos = p.backend === "zonos";
  const advanced = !zonos && cloned.mode === "advanced";
  const live = cloneLive(c) && !enrolling;
  let title = `Drop a clip of this character speaking, or <span class="tts-drop-link">choose a file</span>`;
  let note = zonos
    ? "The first minute is used. One speaker."
    : advanced
      ? "Orb picks about ten seconds of clear speech from it and transcribes them. One speaker, no music."
      : "The whole clip is used, up to two minutes. One speaker, no music.";
  if (enrolling) {
    title = `Enrolling ${esc(enrolling)}…`;
    note = zonos
      ? "Preparing the clip and reading the voice on the ZONOS3 server."
      : advanced
        ? "Reading the voice and transcribing an excerpt."
        : "Reading the voice from the whole clip.";
  } else if (c.enrolled) {
    title = esc(c.clip || "Uploaded clip");
    note = `${!c.engine || featureReady(c.engine) ? "Every reply from this character uses it." : "This character speaks once the voice model is ready."} Drop another clip to replace it.`;
  }
  const state = `${c.enrolled && !enrolling ? " tts-drop-set" : ""}${enrolling ? " tts-drop-busy" : live ? "" : " tts-drop-off"}`;
  const remove =
    c.enrolled && !enrolling
      ? `<button class="tts-clone-remove" type="button" data-wf-action="tts:voiceClear">Remove</button>`
      : "";
  const intro = advanced
    ? "Also copies the clip's pacing and accent, more accurate."
    : "Copies the voice's timbre from the whole clip.";
  return `<div class="tts-field tts-clone">
      <span class="tts-clone-head"><label for="tts-pf-voicefile">Cloned voice</label>${remove}</span>
      ${zonos ? "" : `${cloneTabsHtml()}<span class="tts-note">${intro}</span>`}
      ${setupNoticeHtml(c)}
      <label class="tts-drop${state}" data-wf-action="tts:voiceDrop" data-wf-on="dragover dragleave drop">
        <input type="file" class="tts-drop-input" id="tts-pf-voicefile" accept="audio/*,.wav,.flac,.mp3,.m4a,.ogg"${
          live ? "" : " disabled"
        } data-wf-action="tts:voicePick" data-wf-on="change">
        ${ICON_WAVE}
        <span class="tts-drop-text"><span class="tts-drop-label">${title}</span><span class="tts-note">${note}</span></span>
      </label>
      ${zonos && referenceNote ? `<span class="tts-note">${esc(referenceNote)}</span>` : ""}
      ${advanced && c.enrolled && !enrolling ? referenceHtml() : ""}
      ${zonos ? separatorSetupHtml() : ""}
      ${c.engine ? engineRowHtml(c.engine) : ""}
    </div>`;
}

function referenceHtml() {
  const tokens = cloned.referenceTokens;
  if (!tokens.length) {
    const ready = ADVANCED_FEATURES.every((f) => featureReady(f.id));
    const why = referenceNote || (ready ? "This clip was enrolled before advanced cloning was set up." : "");
    if (!why) return "";
    return `<span class="tts-note">${esc(why)} ${ready ? "Drop the clip again to prepare its excerpt." : ""}</span>`;
  }
  const seconds = (tokens.length / SEMANTIC_RATE).toFixed(1);
  const typed = cloned.referenceText.trim();
  const status = typed
    ? "It must say exactly what the excerpt says. Fix mistakes, then save the voice."
    : `${esc(referenceNote || "The excerpt has no transcript yet.")} Until it has one, replies use Basic cloning.`;
  return `<div class="tts-reference">
      <span class="tts-control-row">
        <button class="btn btn-sm" type="button" data-wf-action="tts:playExcerpt">Play excerpt</button>
        <span class="tts-note">${seconds} s taken from the clip</span>
      </span>
      <label class="tts-field" for="tts-pf-reftext">Transcript
        <textarea id="tts-pf-reftext" rows="3" placeholder="Type exactly what the excerpt says" data-wf-action="tts:referenceText" data-wf-on="input">${esc(cloned.referenceText)}</textarea>
      </label>
      <span class="tts-note">${status}</span>
    </div>`;
}

function selectCloneMode(mode) {
  if (mode !== "basic" && mode !== "advanced") return;
  if (cloned.mode === mode) return;
  cloned.mode = mode;
  renderCloneControl();
}

async function playExcerpt() {
  if (!statusLine()) return;
  setStatus("Loading the excerpt…");
  setPreviewTime("");
  try {
    const res = await query("reference_audio", readForm());
    if (!statusLine()) return;
    if (!res?.audio_b64) {
      setStatus(res?.error || "Could not play the excerpt");
      return;
    }
    playPreview(res.audio_b64, res.mime, "Excerpt");
  } catch (e) {
    console.error("tts: excerpt playback failed", e);
    setStatus("Could not play the excerpt");
  }
}

function engineRowHtml(feature) {
  const llm = mlFeature(feature);
  if (!llm.deps_ok || !llm.present || llm.runtime_ok === false || llm.gpu_capable === false) return "";
  const state = llm.state === "loading" ? "loading…" : llm.state || "idle";
  return `<label class="tts-setting-toggle" title="Run the voice model on the GPU. Takes effect on the next spoken line.">
      <input type="checkbox"${llm.gpu ? " checked" : ""} data-feature="${feature}" data-wf-action="tts:cloneGpu" data-wf-on="change">
      <span class="tts-toggle-body">
        <span class="tts-toggle-label">Run on GPU</span>
        <span class="tts-note${llm.error ? " tts-engine-error" : ""}">Voice model ${esc(state)}${llm.error ? `: ${esc(llm.error)}` : ""}</span>
      </span>
    </label>`;
}

async function saveCloneGpu(box) {
  try {
    await api.post(`/local-ml/${box.dataset.feature}/config`, { gpu: box.checked });
    mlFeature(box.dataset.feature).gpu = box.checked;
  } catch (e) {
    console.warn("tts: voice model GPU switch failed", e);
    setStatus(e?.message || "Could not change the GPU setting");
  }
  renderCloneControl(); // a failed write redraws the box as it was
}

function renderCloneControl() {
  const el = document.getElementById("tts-pf-clone");
  if (!el) return;
  el.innerHTML = cloneControlHtml(readForm());
  ensureMlStatus();
}

function onVoiceDrop(el, ev) {
  if (ev.type === "dragleave") {
    el.classList.remove("tts-drop-over");
    return;
  }
  ev.preventDefault();
  if (enrolling) return; // one clip at a time; the zone already says which
  const live = cloneLive();
  if (ev.type === "dragover") {
    if (live) el.classList.add("tts-drop-over");
    return;
  }
  el.classList.remove("tts-drop-over");
  if (!live) {
    setStatus(`Download the ${cloner().enrollWith.find((f) => !featureReady(f.id))?.label || "voice model"} first`);
    return;
  }
  enrollFile(ev.dataTransfer?.files?.[0]);
}

async function runCloneSetup(setup) {
  if (setupBusy) return;
  setupBusy = setup === "separator" ? "separator" : "models";
  renderCloneControl();
  try {
    const c = cloner();
    const features = setup === "separator" ? SEPARATOR_FEATURES : c.features;
    for (const feature of features) {
      if (!mlFeature(feature.id).present) {
        setupStep = `Downloading the ${feature.label} (${mlFeature(feature.id).size_mb || "?"} MB).`;
        renderCloneControl();
        await api.post(`/local-ml/${feature.id}/download`, {});
      }
      if (mlFeature(feature.id).enabled === false) {
        await api.post(`/local-ml/${feature.id}/enabled`, { enabled: true });
      }
      mlStatus = await api.get("/local-ml/status");
      renderCloneControl();
    }
    if (setup !== "separator" && runtimeMissing(c)) {
      setupStep = `Downloading the ${c.runtime.name} (${c.runtime.mb} MB) — this takes a while.`;
      renderCloneControl();
      await api.post("/local-ml/runtime", c.runtime.body);
      mlStatus = await api.get("/local-ml/status");
      renderCloneControl();
    }
    const what = setup === "separator" ? "Vocal separation" : c.name;
    setStatus(features.every((f) => featureReady(f.id)) ? `${what} is ready` : "");
  } catch (e) {
    console.warn("tts: voice cloning setup failed", e);
    setStatus(e?.message || "Could not set up voice cloning");
  } finally {
    setupBusy = "";
    setupStep = "";
    renderCloneControl();
  }
}

async function enrollFile(file) {
  if (!file) return;
  if (!cardId) {
    setStatus("This conversation has no character to give a voice");
    return;
  }
  enrolling = file.name;
  setStatus("");
  setPreviewTime("");
  renderCloneControl();
  try {
    const url = `/characters/${encodeURIComponent(cardId)}/workflows/${WORKFLOW_ID}/upload?${cloner().upload}`;
    const res = await api.upload(url, file);
    enrolling = "";
    referenceNote = res?.reference_note || "";
    applyProfile(res?.profile);
    setStatus("Voice saved");
  } catch (e) {
    console.warn("tts: voice enrollment failed", e);
    setStatus(e?.message || "Voice enrollment failed");
  } finally {
    if (enrolling) {
      enrolling = "";
      renderCloneControl();
    }
  }
}

async function clearVoiceReference() {
  if (!cardId) return;
  if (!window.confirm("Remove this character's cloned voice?")) return;
  try {
    const res = await api.post(triggerUrl(), {
      action: "clear_voice",
      backend: readForm().backend,
      ...profileTarget(),
    });
    if (res?.error) throw new Error(res.error);
    referenceNote = "";
    applyProfile(res?.profile);
    setStatus("Cloned voice removed");
  } catch (e) {
    console.warn("tts: voice clear failed", e);
    setStatus("Could not remove the cloned voice");
  }
}

function applyProfile(profile) {
  if (!profile) return;
  cloned = cloneFromProfile(profile);
  const backend = document.getElementById("tts-pf-backend");
  if (backend && profile.backend) backend.value = profile.backend;
  const voice = document.getElementById("tts-pf-voice");
  if (voice && profile.voice_id) voice.innerHTML = opt(profile.voice_id, profile.voice_id, true);
  const enabled = document.getElementById("tts-pf-enabled");
  if (enabled) enabled.checked = Boolean(profile.enabled);
  applyFieldVisibility(profile.backend || backend?.value || "spark");
  renderCloneControl();
  loadedProfile = readForm();
}

function settingsActionsHtml(hasProfile) {
  return `
    ${hasProfile ? `<button class="btn" type="button" data-wf-action="tts:preview">Preview</button>` : ""}
    <span id="tts-pf-status" class="modal-action-status" aria-live="polite"></span>
    <span id="tts-pf-time" aria-hidden="true"></span>
    <button class="btn" data-wf-action="tts:closeSettings">Close</button>
    ${hasProfile ? `<button class="btn btn-accent" type="button" data-wf-action="tts:profileSave">Save</button>` : ""}`;
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
    backend: val("tts-pf-backend")?.value || "spark",
    voice_id: val("tts-pf-voice")?.value || "",
    speaker_tokens: cloned.tokens,
    speaker_ref_name: cloned.name,
    clone_mode: cloned.mode,
    reference_tokens: cloned.referenceTokens,
    reference_text: cloned.referenceText,
    zonos_reference_codes: cloned.codes,
    zonos_ref_name: cloned.zonosName,
    language: val("tts-pf-language")?.value || "en",
    model: val("tts-pf-model")?.value || "",
    api_url: val("tts-pf-api_url")?.value || "",
    api_key: val("tts-pf-api_key")?.value || "",
    rate: parseFloat(val("tts-pf-rate")?.value) || 1.0,
    pitch: parseFloat(val("tts-pf-pitch")?.value) || 1.0,
    emotion_delivery: val("tts-pf-delivery") ? val("tts-pf-delivery").checked : true,
    delivery_strength: val("tts-pf-strength") ? parseFloat(val("tts-pf-strength").value) : 1.0,
  };
}

function onBackendChange() {
  const backend = document.getElementById("tts-pf-backend")?.value || "spark";
  const apiUrl = document.getElementById("tts-pf-api_url");
  // Another backend's prefilled address is not a ZONOS3 server.
  if (apiUrl && backend === "zonos" && Object.values(DEFAULT_API_URL).includes(apiUrl.value)) apiUrl.value = "";
  if (apiUrl && !apiUrl.value && DEFAULT_API_URL[backend]) apiUrl.value = DEFAULT_API_URL[backend];
  const label = document.getElementById("tts-pf-api_url-label");
  if (label) label.textContent = apiUrlLabel(backend);
  applyFieldVisibility(backend);
  renderCloneControl();
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
    if (res?.error) {
      if (status) status.textContent = res.error;
      return;
    }
    loadedProfile = saved;
    toast("Voice saved");
    closeModal();
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
    setStatus(`Playing ${previewWhat.toLowerCase()}`);
    armPreviewRaf();
    return;
  }
  if (ev.type !== "close" || ev.reason === "superseded") return; // a newer preview owns the line
  cancelPreviewRaf();
  setPreviewTime("");
  setStatus(ev.reason === "ended" ? `${previewWhat} finished` : "");
}

function armPreviewRaf() {
  if (previewRaf == null) previewRaf = requestAnimationFrame(tickPreview);
}

// Only the elapsed/duration readout updates here.
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

function playPreview(b64, mime, what = "Preview") {
  stopChannel(CHANNEL); // a preview should not talk over a message that is playing
  previewWhat = what;
  previewPending = performance.now() + PREVIEW_START_GRACE_MS;
  playAudio({
    channel: PREVIEW_CHANNEL,
    segments: [{ b64, mime: mime || "audio/wav" }],
    volume: cfg.volume,
    source: { label: `Voice ${what.toLowerCase()}`, dock: false },
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
    // A built-in preview is what loads the voice model, or fails to.
    if (readForm().backend === "spark") ensureMlStatus({ refresh: true });
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
