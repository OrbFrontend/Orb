import { api } from "./api.js";
import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";

let _voicePromptSaveTimer = null;

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

  const extracted = S.ttsExtractedText || "No speech generated yet.";
  const lines = extracted.split("\n");
  const shouldCollapse = extracted.length > 180 || lines.length > 2;
  const debugText = S.ttsDebugExpanded || !shouldCollapse ? extracted : lines.slice(0, 2).join("\n");
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
      <label class="voice-check-row">
        <input type="checkbox" ${S.ttsScripterEnabled ? "checked" : ""} onchange="setTtsScripterEnabled(this.checked)">
        <span>Use LLM extraction</span>
      </label>
      <div class="voice-help">${S.ttsScripterEnabled ? "LLM scripter extracts spoken dialogue when regex is not enough." : "Regex extraction runs locally with no LLM call."}</div>
      ${
        S.ttsScripterEnabled
          ? `<label class="voice-label">Global scripter prompt</label>
             <textarea class="voice-textarea" rows="4" oninput="setTtsScripterPrompt(this.value)">${esc(S.ttsScripterPrompt)}</textarea>`
          : ""
      }
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

    <div class="voice-block">
      <div class="voice-section-title">Extracted Speech Debug</div>
      <div class="voice-help">Last generated via ${esc(S.ttsExtractionMethod || "regex")}</div>
      <pre class="voice-debug-text">${esc(debugText)}</pre>
      ${shouldCollapse ? `<button class="btn btn-sm" onclick="toggleTtsDebugExpanded()">${S.ttsDebugExpanded ? "Collapse" : "Show full"}</button>` : ""}
    </div>
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

export async function setTtsScripterEnabled(checked) {
  S.ttsScripterEnabled = !!checked;
  renderVoicePanel();
  await persistVoiceSettings({ tts_scripter_enabled: S.ttsScripterEnabled ? 1 : 0 });
}

export function setTtsScripterPrompt(value) {
  S.ttsScripterPrompt = value;
  clearTimeout(_voicePromptSaveTimer);
  _voicePromptSaveTimer = setTimeout(async () => {
    await persistVoiceSettings({ tts_scripter_prompt: S.ttsScripterPrompt });
  }, 400);
}

export function toggleTtsDebugExpanded() {
  S.ttsDebugExpanded = !S.ttsDebugExpanded;
  renderVoicePanel();
}
