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
// Keep previews separate from chat playback.
const PREVIEW_CHANNEL = "tts-preview";
// Give failed preview decodes time to report before showing an error.
const PREVIEW_START_GRACE_MS = 1500;

// Backend-specific settings. `clone` is the built-in Spark control.
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
  registerAction(WORKFLOW_ID, "voicePick", (el) => enrollFile(el.files?.[0]));
  registerAction(WORKFLOW_ID, "voiceDrop", (el, ev) => onVoiceDrop(el, ev));
  registerAction(WORKFLOW_ID, "voiceClear", () => clearVoiceReference());
  registerAction(WORKFLOW_ID, "cloneSetup", () => runCloneSetup());
  registerAction(WORKFLOW_ID, "cloneGpu", (el) => saveCloneGpu(el));
  registerAction(WORKFLOW_ID, "cloneMode", (el) => selectCloneMode(el.dataset.mode));
  registerAction(WORKFLOW_ID, "referenceText", (el) => {
    cloned.referenceText = el.value;
  });
  registerAction(WORKFLOW_ID, "playExcerpt", () => playExcerpt());
  onChannel(PREVIEW_CHANNEL, onPreviewEvent);
}

// The codec is enough for enrollment, so list it before the voice model.
const CLONE_FEATURES = [
  { id: "spark_tts_codec", label: "voice codec" },
  { id: "spark_tts_llm", label: "voice model" },
];
// Advanced cloning adds what reads an excerpt out of the clip. Speaking an
// advanced voice needs neither; only enrolling one does.
const ADVANCED_FEATURES = [
  { id: "spark_tts_reference", label: "reference reader" },
  { id: "speech_recognizer", label: "speech recognizer" },
];
// Spark-TTS reads 50 semantic tokens per second of audio.
const SEMANTIC_RATE = 50;

let memberId = null;
let cardId = null; // the card the open profile belongs to; the clone API is keyed on it
let mlStatus = null; // last /local-ml/status; null means "not asked yet, assume fine"
let mlPending = false;
let setupBusy = false;
let setupStep = ""; // the download in flight, said where the button was pressed
let enrolling = ""; // the file being enrolled, shown in the drop zone until the route answers
// Keep the server-owned voice tokens outside the editable form. The mode and
// transcript are editable, but live here too: the clone control re-renders on
// every status change, and a textarea redrawn from the DOM would lose edits.
let cloned = emptyClone();
let referenceNote = ""; // why the last upload has no usable reference, from the route
let loadedProfile = null;
let previewRaf = null;
let previewPending = 0; // start deadline while a preview is decoding, 0 once it plays
let previewWhat = "Preview"; // what the preview channel is playing, for the status line

function emptyClone() {
  return { tokens: [], name: "", mode: "basic", referenceTokens: [], referenceText: "" };
}

function cloneFromProfile(profile) {
  return {
    tokens: profile.speaker_tokens || [],
    name: profile.speaker_ref_name || "",
    mode: profile.clone_mode === "advanced" ? "advanced" : "basic",
    referenceTokens: profile.reference_tokens || [],
    referenceText: profile.reference_text || "",
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
  return `<div class="tool-card-desc">Generate audio for dialogue.</div>
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
      ${field("clone", `<div id="tts-pf-clone">${cloneControlHtml(p)}</div>`)}
      <div class="tts-grid">
        ${field("rate", `<label class="tts-field">Rate <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-rate" value="${esc(p.rate)}"></label>`)}
        ${field("pitch", `<label class="tts-field">Pitch <input type="range" min="0.5" max="2.0" step="0.1" id="tts-pf-pitch" value="${esc(p.pitch)}"></label>`)}
      </div>
    </div>`;
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
  if (document.getElementById("tts-pf-backend")?.value !== "spark") return;
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
    });
}

/** The downloads the open tab needs. */
function cloneFeatures() {
  return cloned.mode === "advanced" ? [...CLONE_FEATURES, ...ADVANCED_FEATURES] : CLONE_FEATURES;
}

/** Describe setup still required before speech can run. */
function setupNoticeHtml() {
  if (!mlStatus) return "";
  const pending = cloneFeatures().filter((f) => !featureReady(f.id));
  const unavailable = pending.find((f) => !mlFeature(f.id).deps_ok);
  if (unavailable) {
    const cmd = mlStatus.install_cmd || "pip install -r requirements-ml.txt";
    return `<span class="tts-note">Voice cloning needs the optional ML extras: <code>${esc(cmd)}</code></span>`;
  }
  const missing = pending.filter((f) => !mlFeature(f.id).present);
  const runtime = mlFeature("spark_tts_llm").runtime_ok === false;
  if (!pending.length && !runtime) return "";
  const names = [
    ...(runtime ? ["llama.cpp runtime"] : []),
    ...missing.map((f) => f.label),
    ...pending.filter((f) => mlFeature(f.id).present && !mlFeature(f.id).enabled).map((f) => f.label),
  ];
  const size = missing.reduce((mb, f) => mb + (mlFeature(f.id).size_mb || 0), runtime ? 150 : 0);
  const what = cloned.mode === "advanced" ? "Advanced cloning" : "Voice cloning";
  const sentence = setupStep || `${what} needs ${listPhrase(names)}.`;
  return `<span class="tts-setup">
      <span class="tts-note">${sentence}</span>
      <button class="btn btn-sm" type="button" data-wf-action="tts:cloneSetup"${setupBusy ? " disabled" : ""}>${setupLabel(Boolean(missing.length || runtime), size)}</button>
    </span>`;
}

function listPhrase(items) {
  if (items.length < 3) return items.join(" and ");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

// The button downloads missing files and enables disabled features.
function setupLabel(download, sizeMb) {
  if (setupBusy) return download ? "Downloading…" : "Turning on…";
  if (!download) return "Turn on";
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

function cloneControlHtml(p) {
  const enrolled = Boolean(p.speaker_tokens?.length);
  const advanced = cloned.mode === "advanced";
  const live = featureReady("spark_tts_codec") && !enrolling;
  let title = `Drop a clip of this character speaking, or <span class="tts-drop-link">choose a file</span>`;
  let note = advanced
    ? "Orb picks about ten seconds of clear speech from it and transcribes them. One speaker, no music."
    : "The whole clip is used, up to two minutes. One speaker, no music.";
  if (enrolling) {
    title = `Enrolling ${esc(enrolling)}…`;
    note = advanced ? "Reading the voice and transcribing an excerpt." : "Reading the voice from the whole clip.";
  } else if (enrolled) {
    title = esc(p.speaker_ref_name || "Uploaded clip");
    note = `${featureReady("spark_tts_llm") ? "Every reply from this character uses it." : "This character speaks once the voice model is ready."} Drop another clip to replace it.`;
  }
  const state = `${enrolled && !enrolling ? " tts-drop-set" : ""}${enrolling ? " tts-drop-busy" : live ? "" : " tts-drop-off"}`;
  const zone = `<label class="tts-drop${state}" data-wf-action="tts:voiceDrop" data-wf-on="dragover dragleave drop">
      <input type="file" class="tts-drop-input" id="tts-pf-voicefile" accept="audio/*,.wav,.flac,.mp3,.m4a,.ogg"${
        live ? "" : " disabled"
      } data-wf-action="tts:voicePick" data-wf-on="change">
      ${ICON_WAVE}
      <span class="tts-drop-text"><span class="tts-drop-label">${title}</span><span class="tts-note">${note}</span></span>
    </label>`;
  const remove =
    enrolled && !enrolling
      ? `<button class="tts-clone-remove" type="button" data-wf-action="tts:voiceClear">Remove</button>`
      : "";
  const intro = advanced
    ? "Also copies the clip's pacing and accent, more accurate."
    : "Copies the voice's timbre from the whole clip.";
  return `<div class="tts-field tts-clone">
      <span class="tts-clone-head"><label for="tts-pf-voicefile">Cloned voice</label>${remove}</span>
      ${cloneTabsHtml()}
      <span class="tts-note">${intro}</span>
      ${setupNoticeHtml()}
      ${zone}
      ${advanced && enrolled && !enrolling ? referenceHtml() : ""}
      ${engineRowHtml()}
    </div>`;
}

/** The excerpt an advanced voice speaks from: play it, and correct its transcript. */
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

/** Play the stored excerpt, rebuilt from its tokens, on the preview channel. */
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

/** Render the GPU switch with the voice model's state as its note. */
function engineRowHtml() {
  const llm = mlFeature("spark_tts_llm");
  if (!llm.deps_ok || !llm.present || llm.runtime_ok === false) return "";
  const state = llm.state === "loading" ? "loading…" : llm.state || "idle";
  return `<label class="tts-setting-toggle" title="Run the voice model on the GPU. Takes effect on the next spoken line.">
      <input type="checkbox"${llm.gpu ? " checked" : ""} data-wf-action="tts:cloneGpu" data-wf-on="change">
      <span class="tts-toggle-body">
        <span class="tts-toggle-label">Run on GPU</span>
        <span class="tts-note${llm.error ? " tts-engine-error" : ""}">Voice model ${esc(state)}${llm.error ? `: ${esc(llm.error)}` : ""}</span>
      </span>
    </label>`;
}

// The next spoken line picks up the new GPU setting.
async function saveCloneGpu(box) {
  try {
    await api.post("/local-ml/spark_tts_llm/config", { gpu: box.checked });
    mlFeature("spark_tts_llm").gpu = box.checked;
  } catch (e) {
    console.warn("tts: voice model GPU switch failed", e);
    setStatus(e?.message || "Could not change the GPU setting");
  }
  renderCloneControl(); // a failed write redraws the box as it was
}

// Cloned voices do not support rate or pitch controls.
function renderCloneControl() {
  const el = document.getElementById("tts-pf-clone");
  if (!el) return;
  el.innerHTML = cloneControlHtml(readForm());
  ensureMlStatus();
}

// Handle dragover, dragleave, and drop on the same target.
function onVoiceDrop(el, ev) {
  if (ev.type === "dragleave") {
    el.classList.remove("tts-drop-over");
    return;
  }
  // Prevent the browser from navigating to the dropped file.
  ev.preventDefault();
  if (enrolling) return; // one clip at a time; the zone already says which
  const live = featureReady("spark_tts_codec");
  if (ev.type === "dragover") {
    if (live) el.classList.add("tts-drop-over");
    return;
  }
  el.classList.remove("tts-drop-over");
  if (!live) {
    setStatus("Download the voice codec first");
    return;
  }
  enrollFile(ev.dataTransfer?.files?.[0]);
}

/** Download missing model files and enable their features. */
async function runCloneSetup() {
  if (setupBusy) return;
  setupBusy = true;
  renderCloneControl();
  try {
    const features = cloneFeatures();
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
      if (feature.id === "spark_tts_codec" && mlFeature("spark_tts_llm").runtime_ok === false) {
        setupStep = "Downloading the llama.cpp runtime (150 MB) — this takes a while.";
        renderCloneControl();
        await api.post("/local-ml/runtime", {});
        mlStatus = await api.get("/local-ml/status");
        renderCloneControl();
      }
    }
    const what = cloned.mode === "advanced" ? "Advanced cloning" : "Voice cloning";
    setStatus(features.every((f) => featureReady(f.id)) ? `${what} is ready` : "");
  } catch (e) {
    console.warn("tts: voice cloning setup failed", e);
    setStatus(e?.message || "Could not set up voice cloning");
  } finally {
    setupBusy = false;
    setupStep = "";
    renderCloneControl();
  }
}

/** Enroll one selected or dropped file. */
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
    const url = `/characters/${encodeURIComponent(cardId)}/voice-reference?mode=${cloned.mode}`;
    const res = await api.upload(url, file);
    // Refill the form from the profile written by the enrollment route.
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
    const res = await api.del(`/characters/${encodeURIComponent(cardId)}/voice-reference`);
    referenceNote = "";
    applyProfile(res?.profile);
    setStatus("Cloned voice removed");
  } catch (e) {
    console.warn("tts: voice clear failed", e);
    setStatus("Could not remove the cloned voice");
  }
}

// Push server-owned clone fields into the open form.
function applyProfile(profile) {
  if (!profile) return;
  cloned = cloneFromProfile(profile);
  const backend = document.getElementById("tts-pf-backend");
  if (backend && profile.backend) backend.value = profile.backend;
  const voice = document.getElementById("tts-pf-voice");
  if (voice && profile.voice_id) voice.innerHTML = opt(profile.voice_id, profile.voice_id, true);
  // Enrollment and clearing also toggle the character state.
  const enabled = document.getElementById("tts-pf-enabled");
  if (enabled) enabled.checked = Boolean(profile.enabled);
  applyFieldVisibility(profile.backend || backend?.value || "edge");
  renderCloneControl();
  loadedProfile = readForm();
}

// Keep footer order consistent with the other modals.
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
    clone_mode: cloned.mode,
    reference_tokens: cloned.referenceTokens,
    reference_text: cloned.referenceText,
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
