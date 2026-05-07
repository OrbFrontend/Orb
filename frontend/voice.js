import { api } from "./api.js";
import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";

function clampVolume(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0.75;
  return Math.max(0, Math.min(1, n));
}

function formatTime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const m = Math.floor(s / 60);
  const r = String(s % 60).padStart(2, "0");
  return `${m}:${r}`;
}

async function persistVoiceSettings(payload) {
  try {
    S.settings = await api.put("/settings", payload);
  } catch (e) {
    toast("Failed to save voice setting", true);
  }
}

export function renderVoicePanel() {
  const el = $("voice-panel-content");
  if (!el) return;

  const extracted = S.ttsExtractedText || "";
  const volumePct = Math.round(clampVolume(S.ttsVolume) * 100);
  const hasNowPlaying = S.speakingMsgId || S.ttsLoading;
  const duration = S.ttsDuration || 0;
  const current = S.ttsCurrentTime || 0;

  el.innerHTML = `
    <div class="voice-block">
      <div class="voice-row">
        <span>Volume</span>
        <span>${volumePct}%</span>
      </div>
      <input class="voice-range" type="range" min="0" max="100" value="${volumePct}" oninput="setTtsVolume(this.value)">
      <label class="voice-check-row">
        <input type="checkbox" ${S.ttsAutoSpeak ? "checked" : ""} onchange="setTtsAutoSpeak(this.checked)">
        <span>Auto-speak new messages</span>
      </label>
    </div>

    <div class="voice-block">
      <div class="voice-section-title">Speech Extraction</div>
      <div class="voice-help">Algorithmic regex extraction runs locally with no LLM call. Action beats and nonverbal tags are preserved when the selected backend supports them.</div>
    </div>

    ${
      hasNowPlaying
        ? `<div class="voice-block">
             <div class="voice-section-title">Now Playing</div>
             <div class="voice-now-label">${esc(S.ttsPlayingLabel || `Message #${S.speakingMsgId}`)}</div>
             <div class="voice-progress-row">
               <span>${formatTime(current)}</span>
               <progress value="${current}" max="${duration || 1}"></progress>
               <span>${duration ? formatTime(duration) : "--:--"}</span>
             </div>
             <button class="btn btn-sm" onclick="stopSpeaking()">Stop</button>
           </div>`
        : ""
    }

    ${
      extracted
        ? `<details class="voice-block" ${S.ttsDebugExpanded ? " open" : ""} ontoggle="setTtsDebugExpanded(this.open)">
             <summary class="voice-section-title" style="cursor:pointer">Extracted Speech via ${esc(S.ttsExtractionMethod || "regex")}</summary>
             <pre class="voice-debug-text">${esc(extracted)}</pre>
           </details>`
        : `<div class="voice-block">
             <div class="voice-section-title">Extracted Speech</div>
             <div class="voice-help">No speech generated yet.</div>
           </div>`
    }
  `;
}

export function toggleVoicePanel() {
  const panel = $("voice-panel");
  const toolsPanel = $("tools-panel");
  const inspector = $("inspector");
  const btn = $("voice-panel-btn");
  const toolsBtn = $("tools-panel-btn");
  const inspectorBtn = $("inspector-toggle");
  if (!panel || !toolsPanel || !inspector || !btn) return;

  const wasOpen = panel.classList.contains("open");
  const switching = !wasOpen && (toolsPanel.classList.contains("open") || inspector.classList.contains("open"));

  if (wasOpen) {
    panel.classList.remove("open");
    btn.classList.remove("btn-active");
  } else {
    toolsPanel.classList.remove("open");
    inspector.classList.remove("open");
    toolsBtn?.classList.remove("btn-active");
    inspectorBtn?.classList.remove("btn-active");
    const open = () => {
      panel.classList.add("open");
      btn.classList.add("btn-active");
      renderVoicePanel();
    };
    if (switching) setTimeout(open, 180);
    else open();
  }
}

export async function setTtsVolume(value) {
  S.ttsVolume = clampVolume(Number(value) / 100);
  if (window.setCurrentTtsVolume) window.setCurrentTtsVolume(S.ttsVolume);
  renderVoicePanel();
  await persistVoiceSettings({ tts_volume: S.ttsVolume });
}

export async function setTtsAutoSpeak(checked) {
  S.ttsAutoSpeak = !!checked;
  renderVoicePanel();
  await persistVoiceSettings({ tts_auto_speak: S.ttsAutoSpeak ? 1 : 0 });
}

export function setTtsDebugExpanded(open) {
  S.ttsDebugExpanded = !!open;
}
