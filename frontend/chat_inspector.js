import { api } from "./api.js";
import { renderContextSize, renderMessages } from "./chat_core.js";
import { currentDecisionsHtml } from "./chat_decisions.js";
import { CHEVRON_RIGHT_ICON } from "./icons.js";
import {
  buildFeedbackHtml,
  buildStateHtml,
  injectionHtml,
  latencyHtml,
  moodsHtml,
  REASONING_BOTTOM_THRESHOLD,
  REASONING_PASSES,
  refreshInlineInspector,
  rememberInspection,
  renderLiveInspector,
  toolCallsHtml,
} from "./message_inspector.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { preserveScroll } from "./scroll_follow.js";
import { effectiveWorkflowEnabled, restingCooldowns, S } from "./state.js";
import { renderStatePanel } from "./state_panel.js";
import { $, convUrl, esc, escAttr, escHandlerArg, sentenceTail } from "./utils.js";

export {
  buildFeedbackHtml,
  buildStateHtml,
  feedbackRows,
  REASONING_PASSES,
  saveInspectorOpenStates,
} from "./message_inspector.js";

let inspectionRequest = 0;

export async function inspectMessage(msgId) {
  if (!S.activeConvId) return;
  const conversationId = S.activeConvId;
  const request = ++inspectionRequest;
  S.inspectedMsgId = msgId;
  S.inspectedDirectorData = null;
  renderInspector();
  const isCurrent = () =>
    request === inspectionRequest && S.activeConvId === conversationId && S.inspectedMsgId === msgId;
  try {
    const data = await api.get(convUrl(conversationId, "messages", msgId, "director-log"));
    if (!isCurrent()) return;
    S.inspectedDirectorData = data;
    S.reasoningDirector = S.inspectedDirectorData.reasoning_director || "";
    S.reasoningWriter = S.inspectedDirectorData.reasoning_writer || "";
    S.reasoningEditor = S.inspectedDirectorData.reasoning_editor || "";
    const highestPassIdx = S.reasoningEditor ? 2 : S.reasoningWriter ? 1 : 0;
    S.reasoningPassActive = highestPassIdx;
    S.reasoningPassSelected = highestPassIdx;
    S.reasoningUserOverride = false;
    rememberInspection(msgId, data);
    renderInspector();
    refreshInlineInspector(msgId);
  } catch (_e) {
    if (!isCurrent()) return;
    S.inspectedDirectorData = null;
    renderInspector();
  }
}

export function clearInspectedMessage() {
  S.inspectedMsgId = null;
  S.inspectedDirectorData = null;
  renderInspector();
}

const withReasoningScroll = (mutate) =>
  preserveScroll(() => document.getElementById("reasoning-box"), REASONING_BOTTOM_THRESHOLD, mutate);

export function appendReasoningDelta(box, delta) {
  if (!box) return;
  preserveScroll(
    () => box,
    REASONING_BOTTOM_THRESHOLD,
    () => box.appendChild(document.createTextNode(delta)),
  );
}

export function _advanceReasoningPass(targetIdx) {
  if (targetIdx <= S.reasoningPassActive) return false;
  S.reasoningPassActive = targetIdx;
  if (!S.reasoningUserOverride) {
    const targetKey = REASONING_PASSES[targetIdx]?.key;
    const targetEnabled = targetKey && S.reasoningEnabled[targetKey] !== false;
    if (targetEnabled) S.reasoningPassSelected = targetIdx;
  }
  return _refreshReasoningSection();
}

function _buildReasoningHtml() {
  const streamIdx = S.reasoningPassActive;
  const selectedIdx = S.reasoningPassSelected;
  const dotsHtml = REASONING_PASSES.map((p, i) => {
    const hasText = !!S[`reasoning${p.key.charAt(0).toUpperCase()}${p.key.slice(1)}`];
    const isStreaming = i === streamIdx;
    const isSelected = i === selectedIdx;
    const lit = hasText || isStreaming;
    const enabled = S.reasoningEnabled[p.key] !== false;
    const dotStyle = [
      `background:${lit ? p.color : "var(--bg-elevated)"}`,
      `color:${lit ? "#fff" : "var(--text-muted)"}`,
      `border:2px solid ${isSelected ? "var(--accent)" : lit ? p.color : "var(--border)"}`,
      isSelected ? "box-shadow:0 0 0 2px var(--accent)" : "",
      !enabled ? "opacity:0.4" : "",
    ]
      .filter(Boolean)
      .join(";");
    const lineColor = i < streamIdx ? REASONING_PASSES[i + 1].color : "var(--border)";
    const checkId = `reasoning-enabled-${p.key}`;
    return `<div class="reasoning-dot-col">
        <button class="reasoning-dot" onclick="selectReasoningPass(${i})" style="${dotStyle}">${i + 1}</button>
        <label class="reasoning-enabled-label" for="${checkId}">
          <input type="checkbox" id="${checkId}" ${enabled ? "checked" : ""} onchange="toggleReasoningPass('${p.key}')">
          <span>on</span>
        </label>
      </div>${i < 2 ? `<div class="reasoning-rail-line" style="background:${lineColor}"></div>` : ""}`;
  }).join("");

  const selectedPass = REASONING_PASSES[selectedIdx];
  const currentText = S[`reasoning${selectedPass.key.charAt(0).toUpperCase()}${selectedPass.key.slice(1)}`] || "";
  const openAttr = S.reasoningOpen ? " open" : "";

  const key = selectedPass.key;
  const prefillHtml =
    _passTextMode(key) && S.reasoningEnabled[key] !== false
      ? `<textarea class="reasoning-box reasoning-prefill" id="reasoning-prefill" data-pass="${key}" rows="3"
         placeholder="Prefill this pass's reasoning… (macros resolved)"
       >${esc(S.reasoningPrefill[key] || "")}</textarea>`
      : "";

  // With the Inspector in the chat, the text lives under each reply and the
  // panel keeps only the controls for the next turn.
  const boxHtml = S.inspectorInline ? "" : `<div class="reasoning-box" id="reasoning-box">${esc(currentText)}</div>`;
  return `<details class="inspector-block reasoning-section" id="reasoning-section" data-inspect-section="reasoning"${openAttr}>
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4 style="margin:0;display:inline">Reasoning</h4>
    </summary>
    <div style="margin-top:8px">
      <div class="reasoning-stepper">
        ${dotsHtml}
        <span class="reasoning-pass-label">${esc(selectedPass.label)}</span>
      </div>
      ${boxHtml}
      ${prefillHtml}
    </div>
  </details>`;
}

function _passTextMode(key) {
  const separate = !S.agentSameAsWriter && !!S.agentEndpointId;
  const id = key === "writer" || !separate ? S.activeEndpointId : S.agentEndpointId;
  return S.endpoints.find((e) => e.id === id)?.completion_mode === "text";
}

document.addEventListener("input", (e) => {
  if (e.target.id !== "reasoning-prefill") return;
  S.reasoningPrefill[e.target.dataset.pass] = e.target.value;
  S.reasoningUserOverride = true;
});
document.addEventListener("change", (e) => {
  if (e.target.id === "reasoning-prefill")
    api.put("/settings", { reasoning_prefill_passes: { ...S.reasoningPrefill } });
});
/** Rebuild the reasoning views. Returns whether any reasoning box now holds the full text. */
function _refreshReasoningSection() {
  const existing = document.getElementById("reasoning-section");
  if (existing) {
    withReasoningScroll(() => {
      existing.outerHTML = _buildReasoningHtml();
    });
  }
  const live = renderLiveInspector();
  return Boolean(existing) || live;
}

// The streaming reply's pass tabs pick the same pass as the panel's dots.
document.addEventListener("click", (e) => {
  const tab = e.target.closest?.(".msg-reasoning-live button[data-inspect-pass]");
  if (tab) selectReasoningPass(Number(tab.dataset.inspectPass));
});

export function selectReasoningPass(idx) {
  S.reasoningPassSelected = idx;
  S.reasoningUserOverride = true;
  _refreshReasoningSection();
}

const _workflowPipelineSelected = new Map();

function _pipelineSelectedPassId(pipeline) {
  if (!pipeline.passes?.length) return null;
  const cur = _workflowPipelineSelected.get(pipeline.id);
  if (cur && pipeline.passes.some((p) => p.id === cur)) return cur;
  return pipeline.passes[0].id;
}

function _buildWorkflowReasoningHtml() {
  if (!S.workflowPipelines.length) return "";
  return S.workflowPipelines
    .map((pipeline) => {
      const selectedId = _pipelineSelectedPassId(pipeline);
      const dotsHtml = pipeline.passes
        .map((p, i) => {
          const hasText = !!S.reasoningByPass[p.id];
          const isSelected = p.id === selectedId;
          const lit = hasText || isSelected;
          const dotStyle = [
            `background:${lit ? "var(--accent)" : "var(--bg-elevated)"}`,
            `color:${lit ? "#fff" : "var(--text-muted)"}`,
            `border:2px solid ${isSelected ? "var(--accent)" : lit ? "var(--accent)" : "var(--border)"}`,
            isSelected ? "box-shadow:0 0 0 2px var(--accent)" : "",
          ]
            .filter(Boolean)
            .join(";");
          const lineColor = hasText ? "var(--accent)" : "var(--border)";
          return (
            `<div class="reasoning-dot-col">
              <button class="reasoning-dot" onclick="selectWorkflowPipelinePass('${escHandlerArg(pipeline.id)}','${escHandlerArg(p.id)}')" style="${dotStyle}">${i + 1}</button>
              <span class="reasoning-pass-label" style="margin:0">${esc(p.label || p.id)}</span>
            </div>` +
            (i < pipeline.passes.length - 1
              ? `<div class="reasoning-rail-line" style="background:${lineColor}"></div>`
              : "")
          );
        })
        .join("");
      const text = S.reasoningByPass[selectedId] || "";
      return `<div class="workflow-card workflow-pipeline-card" data-pipeline-id="${escAttr(pipeline.id)}">
        <h4>${esc(pipeline.label || pipeline.id)}</h4>
        <div class="reasoning-stepper">${dotsHtml}</div>
        <div class="reasoning-box" id="reasoning-box-${escAttr(pipeline.id)}" data-pass-id="${escAttr(selectedId)}">${esc(text)}</div>
      </div>`;
    })
    .join("");
}

export function _relightWorkflowPipelinePass(pipeline, passId) {
  const card = document.querySelector(`.workflow-pipeline-card[data-pipeline-id="${CSS.escape(pipeline.id)}"]`);
  if (!card) return;
  const idx = pipeline.passes.findIndex((p) => p.id === passId);
  if (idx < 0) return;
  const dot = card.querySelectorAll(".reasoning-dot")[idx];
  if (dot) {
    dot.style.background = "var(--accent)";
    dot.style.color = "#fff";
    dot.style.borderColor = "var(--accent)";
  }
  const line = card.querySelectorAll(".reasoning-rail-line")[idx];
  if (line) line.style.background = "var(--accent)";
}

function _buildWorkflowCardsHtml() {
  if (!S.workflowInspectorCardRenderers.length) return "";
  let html = "";
  for (const { workflowId, render } of S.workflowInspectorCardRenderers) {
    if (!effectiveWorkflowEnabled(workflowId)) continue;
    try {
      const piece = render();
      if (typeof piece === "string" && piece) html += piece;
    } catch (e) {
      console.error("workflow inspector card renderer threw:", e);
    }
  }
  return html;
}

export function selectWorkflowPipelinePass(pipelineId, passId) {
  _workflowPipelineSelected.set(pipelineId, passId);
  renderInspectorWorkflows();
}

/** Workflow pipelines and cards, below the Main tab's own sections. */
export function renderInspectorWorkflows() {
  const el = $("inspector-workflow-content");
  if (el) el.innerHTML = _buildWorkflowReasoningHtml() + _buildWorkflowCardsHtml();
}

const INSPECTOR_TABS = ["main", "state"];

function setInspectorTab(name) {
  S.inspectorTab = name === "state" ? "state" : "main";
  for (const tab of INSPECTOR_TABS) {
    const active = tab === S.inspectorTab;
    $(`inspector-pane-${tab}`)?.classList.toggle("hidden", !active);
    $(`inspector-tab-${tab}`)?.classList.toggle("tab-button-active", active);
  }
  if (S.inspectorTab === "state" && isUtilityPanelOpen("inspector")) renderStatePanel();
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest("[data-inspector-tab]");
  if (tab) setInspectorTab(tab.dataset.inspectorTab);
});
// The State tab asks for Main when it has nothing left to show.
document.addEventListener("inspector-tab-request", (e) => setInspectorTab(e.detail));

export function setToolsTab(name) {
  S.toolsTab = name === "secondary" ? "secondary" : "main";
  _applyToolsTab();
}

function _applyToolsTab() {
  const main = $("tools-pane-main");
  const sec = $("tools-pane-secondary");
  const btnMain = $("tools-tab-main");
  const btnSec = $("tools-tab-secondary");
  if (!main || !sec || !btnMain || !btnSec) return;
  if (S.toolsTab === "secondary") {
    main.classList.add("hidden");
    sec.classList.remove("hidden");
    btnMain.classList.remove("tab-button-active");
    btnSec.classList.add("tab-button-active");
  } else {
    sec.classList.add("hidden");
    main.classList.remove("hidden");
    btnSec.classList.remove("tab-button-active");
    btnMain.classList.add("tab-button-active");
  }
}

function _renderWorkflowPhasesPill() {
  const el = $("gen-text-secondary");
  if (!el) return;
  const entries = Object.entries(S.workflowPhases);
  el.textContent = entries.length ? entries[entries.length - 1][1] : "";
}

export function _syncGenerationStatusVisibility() {
  const el = $("generation-status");
  if (!el) return;
  const turnActive = S.generationStep !== null;
  const pillActive = Object.keys(S.workflowPhases).length > 0;
  el.classList.toggle("hidden", !(turnActive || pillActive));
  el.classList.toggle("pill-only", !turnActive && pillActive);
}

export function setWorkflowPhase(channel, label) {
  if (typeof channel === "string" && channel.startsWith("workflow:")) {
    const wid = channel.split(":")[1];
    if (wid && !effectiveWorkflowEnabled(wid)) return;
  }
  if (label?.trim()) S.workflowPhases[channel] = label;
  else delete S.workflowPhases[channel];
  _renderWorkflowPhasesPill();
  _syncGenerationStatusVisibility();
}

export function clearWorkflowPhase(channel) {
  if (channel === undefined) S.workflowPhases = {};
  else delete S.workflowPhases[channel];
  _renderWorkflowPhasesPill();
  _syncGenerationStatusVisibility();
}

export function workflowPhaseLabel(wid, verb) {
  const entry = S.workflowManifest.find((w) => w.id === wid);
  return `${entry?.display_name || "Workflow"}: ${verb}`;
}

export async function loadWorkflowManifest() {
  try {
    const manifest = await api.get("/workflows");
    if (Array.isArray(manifest)) S.workflowManifest = manifest;
  } catch (e) {
    console.error("Failed to load workflow manifest:", e);
  }
}

export async function toggleReasoningPass(passKey) {
  S.reasoningEnabled[passKey] = !S.reasoningEnabled[passKey];
  _refreshReasoningSection();
  await api.put("/settings", { reasoning_enabled_passes: { ...S.reasoningEnabled } });
}

export function clearRefineDiff() {
  S.pendingRefineDiff = null;
  renderMessages();
}

export function toggleInspector() {
  if (isUtilityPanelOpen("inspector")) {
    closeUtilityPanel("inspector", "inspector-toggle");
  } else {
    openUtilityPanel("inspector", "inspector-toggle", () => {
      renderInspector();
      if (S.inspectorTab === "state") renderStatePanel();
    });
  }
}

export function renderInspector() {
  _renderInspectorMain();
  renderInspectorWorkflows();
  renderLiveInspector();
}

export function currentMoodsHtml() {
  const inspecting = S.inspectedMsgId != null;
  if (!inspecting && !S.isStreaming && !S.messages.some((message) => message.role === "assistant")) return "";
  const data = inspecting ? S.inspectedDirectorData : S.lastDirectorData;
  const known = data?.mood_data_available !== false && Array.isArray(data?.active_moods);
  const history = S.isStreaming ? S.messages.slice(0, S.streamCutoffIndex ?? S.messages.length) : S.messages;
  const lastAssistant = history.findLast((message) => message.role === "assistant");
  // A saved reply stores cooldowns for the following turn. Read the baseline
  // before this reply, including before the whole exchange in group chats.
  const resting = inspecting
    ? restingCooldowns(S.inspectedMsgId)
    : S.isStreaming
      ? lastAssistant?.fragment_cooldowns || {}
      : restingCooldowns(lastAssistant?.id);
  return moodsHtml({ known, activeIds: known ? data.active_moods : [], resting }, { showNone: true });
}

function _renderDirectorPanel({ latency, toolCalls, injection, feedback, stateChanges }) {
  withReasoningScroll(() => {
    $("inspector-content").innerHTML = `
      <div class="inspector-block" id="inspector-context-size"></div>
      ${currentMoodsHtml()}
      ${_buildReasoningHtml()}
      ${currentDecisionsHtml()}
      ${buildFeedbackHtml(feedback)}
      ${buildStateHtml(stateChanges)}
      ${toolCallsHtml(toolCalls)}
      ${injectionHtml(injection)}
      ${latencyHtml(latency)}`;
  });
  renderContextSize();
}

function _renderInspectorMain() {
  // Each reply carries its own turn details in the chat; the panel keeps what
  // belongs to the conversation and the controls for the next turn.
  if (S.inspectorInline) {
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       <div class="inspector-block" id="inspector-context-size"></div>
       ${_buildReasoningHtml()}`;
    });
    renderContextSize();
    return;
  }

  if (S.inspectedMsgId == null && S.isStreaming && S.lastDirectorData === null) {
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       <div class="inspector-block" id="inspector-context-size"></div>
       ${currentMoodsHtml()}
       ${_buildReasoningHtml()}
       ${currentDecisionsHtml()}
       <div style="color:var(--text-muted);font-size:12px;display:flex;align-items:center;gap:8px">
         <span class="typing-indicator"><span></span><span></span><span></span></span> Director thinking…
       </div>`;
    });
    renderContextSize();
    return;
  }

  const insp = S.inspectedMsgId && S.inspectedDirectorData ? S.inspectedDirectorData : null;

  if (S.inspectedMsgId != null) {
    const data = insp || {};
    _renderDirectorPanel({
      latency: data.agent_latency_ms || 0,
      toolCalls: data.tool_calls || [],
      injection: data.injection_block || "",
      feedback: data.feedback,
      stateChanges: data.state,
    });
    return;
  }

  const hasDirectorData = S.lastDirectorData && Object.keys(S.lastDirectorData).length > 0;

  if (!hasDirectorData) {
    const fbHtml = buildFeedbackHtml(S.lastFeedback?.values);
    const stateHtml = buildStateHtml(S.lastState);
    const decHtml = currentDecisionsHtml();
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       <div class="inspector-block" id="inspector-context-size"></div>
       ${currentMoodsHtml()}
       ${_buildReasoningHtml()}
       ${decHtml}
       ${fbHtml}
       ${stateHtml}
       ${fbHtml || stateHtml || decHtml ? "" : `<div style="color:var(--text-muted);font-size:12px;">Send a message to see director output</div>`}`;
    });
    renderContextSize();
    return;
  }

  const ld = S.lastDirectorData || {};
  _renderDirectorPanel({
    latency: ld.agent_latency_ms || 0,
    toolCalls: ld.tool_calls || [],
    injection: ld.injection_block || "",
    feedback: S.lastFeedback?.values,
    stateChanges: S.lastState,
  });
}

const _EXPR_TAIL_SENTENCES = 7;
const _EXPR_MIN_INTERVAL_MS = 1000;
const _EXPR_STALE_MS = 5000;
const _EXPR_MIN_GROWTH_CHARS = 40; // don't classify a fragment like "She"
let _exprTimer = null;
let _exprLastCallAt = 0;

export function expressionCharId() {
  if (!S.groupCast) return S.activeCharId;
  if (S.currentSpeaker?.card_id) return S.currentSpeaker.card_id;
  const lastSpoken = [...S.messages].reverse().find((m) => m.role === "assistant" && m.speaker_member_id);
  const member = lastSpoken && S.groupCast.members.find((item) => item.id === lastSpoken.speaker_member_id);
  return (
    member?.character_card_id || S.groupCast.members.find((item) => item.character_card_id)?.character_card_id || null
  );
}

async function _expressionLabels(charId) {
  if (!(S.characters || []).find((c) => c.id === charId)?.has_expressions) return [];
  try {
    return (await api.get(`/characters/${charId}/expressions`)).labels || [];
  } catch {
    return [];
  }
}

async function _bindExpressionChar(img, charId) {
  img._exprCharId = charId;
  img._exprSrc = null;
  img._exprText = null;
  img._exprFullLen = 0;
  _exprLastCallAt = 0;
  const labels = await _expressionLabels(charId);
  if (img._exprCharId !== charId || document.getElementById("avatar-popup")?.classList.contains("hidden")) return;
  img._exprLabels = labels;
  const neutral = labels.includes("neutral") ? `/api/characters/${charId}/expressions/neutral` : null;
  img._exprSrc = neutral;
  img.src = neutral || `/api/characters/${charId}/avatar?t=${Date.now()}`;
}

async function _expressionTick() {
  const img = document.getElementById("avatar-popup-image");
  if (!img) return;
  const charId = expressionCharId();
  if (!charId) return;
  if (charId !== img._exprCharId) {
    await _bindExpressionChar(img, charId);
    return;
  }
  if (!img._exprLabels?.length) return;
  const full = S.isStreaming
    ? S.streamingContent
    : [...S.messages].reverse().find((m) => m.role === "assistant" && m.id)?.content;
  if (!full) return;
  const now = Date.now();
  if (now - _exprLastCallAt < _EXPR_MIN_INTERVAL_MS) return; // fast models: rate floor
  let text = sentenceTail(full, _EXPR_TAIL_SENTENCES, S.isStreaming);
  if (
    (!text || img._exprText === text) &&
    S.isStreaming &&
    now - _exprLastCallAt >= _EXPR_STALE_MS &&
    full.length - (img._exprFullLen || 0) >= _EXPR_MIN_GROWTH_CHARS
  ) {
    text = sentenceTail(full, _EXPR_TAIL_SENTENCES, false);
  }
  if (!text || img._exprText === text) return;
  img._exprText = text;
  img._exprFullLen = full.length;
  _exprLastCallAt = now;
  let label;
  try {
    ({ label } = await api.post("/local-ml/classify-emotion", { text }));
  } catch (_e) {
    clearInterval(_exprTimer);
    _exprTimer = null;
    return;
  }
  const labels = img._exprLabels || [];
  const resolved = labels.includes(label) ? label : labels.includes("neutral") ? "neutral" : null;
  if (!resolved) {
    img.src = `/api/characters/${charId}/avatar`; // no matching expression → plain avatar
    return;
  }
  const next = `/api/characters/${charId}/expressions/${resolved}`;
  if (img._exprSrc !== next) {
    img._exprSrc = next; // swap only on change (ETag handles caching; no ?t= flicker)
    img.src = next;
  }
}

export async function showAvatarPopup() {
  const charId = expressionCharId();
  if (!charId) return;
  const popup = document.getElementById("avatar-popup");
  if (!popup) return;
  if (!popup.classList.contains("hidden")) {
    hideAvatarPopup();
    return;
  }
  const img = document.getElementById("avatar-popup-image");
  if (!img) return;
  const hasExpr = (S.characters || []).find((c) => c.id === charId)?.has_expressions;
  if (!hasExpr) img.src = `/api/characters/${charId}/avatar?t=${Date.now()}`;
  popup.classList.remove("hidden");
  img._exprCharId = null;
  await _bindExpressionChar(img, charId);
  if (popup.classList.contains("hidden")) return;
  _expressionTick();
  _exprTimer = setInterval(_expressionTick, 1000);
}

export function hideAvatarPopup() {
  const popup = document.getElementById("avatar-popup");
  if (popup) popup.classList.add("hidden");
  const img = document.getElementById("avatar-popup-image");
  if (img) img._exprCharId = null;
  clearInterval(_exprTimer);
  _exprTimer = null;
}
