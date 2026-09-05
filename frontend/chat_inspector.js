import { api } from "./api.js";
import { renderContextSize, renderMessages } from "./chat_core.js";
import { USER_NOTE_ID } from "./direction_notes_panel.js";
import { CHEVRON_RIGHT_ICON } from "./icons.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { preserveScroll } from "./scroll_follow.js";
import { effectiveWorkflowEnabled, interactiveFragmentsView, moodFragmentsView, S } from "./state.js";
import { $, esc, escAttr, escHandlerArg, sentenceTail } from "./utils.js";

export const REASONING_PASSES = [
  { key: "director", label: "Director", color: "var(--accent-dim)" },
  { key: "writer", label: "Writer", color: "var(--accent-dim)" },
  { key: "editor", label: "Editor", color: "var(--accent-dim)" },
];

const REASONING_BOTTOM_THRESHOLD = 20;

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
  const existing = document.getElementById("reasoning-section");
  if (!existing) return false;
  _refreshReasoningSection();
  return true;
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

  return `<details class="inspector-block reasoning-section" id="reasoning-section"${openAttr} ontoggle="S.reasoningOpen=this.open;saveInspectorOpenStates()">
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4 style="margin:0;display:inline">Reasoning</h4>
    </summary>
    <div style="margin-top:8px">
      <div class="reasoning-stepper">
        ${dotsHtml}
        <span class="reasoning-pass-label">${esc(selectedPass.label)}</span>
      </div>
      <div class="reasoning-box" id="reasoning-box">${esc(currentText)}</div>
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

function _refreshReasoningSection() {
  const existing = document.getElementById("reasoning-section");
  if (!existing) return;
  preserveScroll(
    () => document.getElementById("reasoning-box"),
    REASONING_BOTTOM_THRESHOLD,
    () => {
      existing.outerHTML = _buildReasoningHtml();
    },
  );
}

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

function _buildSecondaryReasoningHtml() {
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

function _buildSecondaryAgentsHtml() {
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
  renderInspectorSecondary();
}

export function renderInspectorSecondary() {
  const el = $("inspector-secondary-content");
  if (!el) return;
  const reasoning = _buildSecondaryReasoningHtml();
  const cards = _buildSecondaryAgentsHtml();
  if (!reasoning && !cards) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:8px 0;">No workflows registered.</div>`;
    return;
  }
  el.innerHTML = reasoning + cards;
}

export function setInspectorTab(name) {
  S.inspectorTab = name === "secondary" ? "secondary" : "main";
  _applyInspectorTab();
}

function _applyInspectorTab() {
  const main = $("inspector-content");
  const sec = $("inspector-secondary-content");
  const btnMain = $("inspector-tab-main");
  const btnSec = $("inspector-tab-secondary");
  if (!main || !sec || !btnMain || !btnSec) return;
  if (S.inspectorTab === "secondary") {
    main.classList.add("hidden");
    sec.classList.remove("hidden");
    btnMain.classList.remove("tab-button-active");
    btnSec.classList.add("tab-button-active");
    renderInspectorSecondary();
  } else {
    sec.classList.add("hidden");
    main.classList.remove("hidden");
    btnSec.classList.remove("tab-button-active");
    btnMain.classList.add("tab-button-active");
  }
}

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
  const turnActive = !!S.generationPhase;
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

function _buildToolCallsHtml(tc) {
  const openAttr = S.toolCallsOpen ? " open" : "";
  return `<details class="inspector-block"${openAttr} ontoggle="S.toolCallsOpen=this.open;saveInspectorOpenStates()">
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4 style="margin:0;display:inline">Tool Calls</h4>
    </summary>
    <div class="injection-box" style="margin-top:8px">${esc(tc.map((c) => JSON.stringify(c)).join("\n\n"))}</div>
  </details>`;
}

export function feedbackRows(values) {
  if (!values || typeof values !== "object") return [];
  const frags = interactiveFragmentsView();
  return Object.entries(values)
    .filter(([, v]) => v && (Array.isArray(v) ? v.length : true))
    .map(([id, v]) => {
      const frag = frags.find((f) => f.id === id);
      const label = frag?.injection_label || frag?.label || id;
      return { label, value: v };
    });
}

export function buildFeedbackHtml(values) {
  const rows = feedbackRows(values);
  if (!rows.length) return "";
  const body = rows
    .map(({ label, value }) => {
      const valHtml = Array.isArray(value)
        ? `<ul>${value.map((it) => `<li>${esc(String(it))}</li>`).join("")}</ul>`
        : esc(String(value));
      return `<div class="feedback-row">
        <span class="feedback-row-label">${esc(label)}</span>
        <div class="feedback-row-value">${valHtml}</div>
      </div>`;
    })
    .join("");
  return `<div class="inspector-block">
    <h4>Feedback</h4>
    <div class="feedback-card">${body}</div>
  </div>`;
}

export function buildDirectionNotesHtml(notes) {
  if (!Array.isArray(notes) || !notes.length) return "";
  const body = notes
    .map((n) => {
      const isUser = n.interactive_fragment_id === USER_NOTE_ID;
      const badge = isUser ? ` <span class="notes-row-user-badge">You</span>` : "";
      return `<div class="feedback-row${isUser ? " user-note" : ""}">
        <span class="feedback-row-label">${esc(n.interactive_fragment_label || "")}${badge}</span>
        <div class="feedback-row-value">${esc(String(n.content))}</div>
      </div>`;
    })
    .join("");
  return `<div class="inspector-block">
    <h4>Direction Notes (this turn)</h4>
    <div class="feedback-card">${body}</div>
  </div>`;
}

function _buildInjectionBlockHtml(inj) {
  const openAttr = S.injectionBlockOpen ? " open" : "";
  return `<details class="inspector-block"${openAttr} ontoggle="S.injectionBlockOpen=this.open;saveInspectorOpenStates()">
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4 style="margin:0;display:inline">Injection Block</h4>
    </summary>
    <div class="injection-box" style="margin-top:8px">${esc(inj)}</div>
  </details>`;
}

export function saveInspectorOpenStates() {
  api
    .put("/settings", {
      inspector_open_states: {
        reasoning: S.reasoningOpen,
        tool_calls: S.toolCallsOpen,
        injection_block: S.injectionBlockOpen,
        context_size: S.contextSizeOpen,
      },
    })
    .catch(() => {});
}

export function clearRefineDiff() {
  S.pendingRefineDiff = null;
  renderMessages();
}

export function toggleInspector() {
  if (isUtilityPanelOpen("inspector")) {
    closeUtilityPanel("inspector", "inspector-toggle");
  } else {
    openUtilityPanel("inspector", "inspector-toggle", renderInspector);
  }
}

export function renderInspector() {
  _renderInspectorMain();
  renderInspectorSecondary();
}

function _renderDirectorPanel({ activeIds, latency, toolCalls, injection, feedback, directionNotes }) {
  const stylesHtml = moodFragmentsView()
    .map((f) => `<span class="style-tag ${activeIds.includes(f.id) ? "active" : ""}">${esc(f.label)}</span>`)
    .join("");
  withReasoningScroll(() => {
    $("inspector-content").innerHTML = `
      <div class="inspector-block" id="inspector-context-size"></div>
      <div class="inspector-block"><h4>Moods</h4>
        <div>${stylesHtml || '<span style="color:var(--text-muted);font-size:12px">None</span>'}</div>
      </div>
      ${_buildReasoningHtml()}
      ${buildFeedbackHtml(feedback)}
      ${buildDirectionNotesHtml(directionNotes)}
      ${toolCalls.length ? _buildToolCallsHtml(toolCalls) : ""}
      ${injection ? _buildInjectionBlockHtml(injection) : ""}
      ${
        latency
          ? `<div class="inspector-block"><h4>Agent Latency</h4>
               <div style="font-size:12px;color:var(--text-secondary)">${latency}ms</div></div>`
          : ""
      }`;
  });
  renderContextSize();
}

function _renderInspectorMain() {
  if (S.isStreaming && S.lastDirectorData === null) {
    const pendingMoodsHtml = moodFragmentsView()
      .map((f) => `<span class="style-tag">${esc(f.label)}</span>`)
      .join("");
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       <div class="inspector-block" id="inspector-context-size"></div>
       <div class="inspector-block"><h4>Moods</h4>
         <div>${pendingMoodsHtml || '<span style="color:var(--text-muted);font-size:12px">None</span>'}</div>
       </div>
       ${_buildReasoningHtml()}
       <div style="color:var(--text-muted);font-size:12px;display:flex;align-items:center;gap:8px">
         <span class="typing-indicator"><span></span><span></span><span></span></span> Director thinking…
       </div>`;
    });
    renderContextSize();
    return;
  }

  const insp = S.inspectedMsgId && S.inspectedDirectorData ? S.inspectedDirectorData : null;

  if (insp) {
    _renderDirectorPanel({
      activeIds: insp.active_moods || [],
      latency: insp.agent_latency_ms || 0,
      toolCalls: insp.tool_calls || [],
      injection: insp.injection_block || "",
      feedback: insp.feedback,
      directionNotes: insp.direction_notes,
    });
    return;
  }

  const hasDirectorData =
    (S.directorState && Object.keys(S.directorState).length > 0) ||
    (S.lastDirectorData && Object.keys(S.lastDirectorData).length > 0);

  if (!hasDirectorData) {
    const fbHtml = buildFeedbackHtml(S.lastFeedback?.values);
    const pnHtml = buildDirectionNotesHtml(S.lastDirectionNotes?.notes);
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       <div class="inspector-block" id="inspector-context-size"></div>
       ${_buildReasoningHtml()}
       ${fbHtml}
       ${pnHtml}
       ${fbHtml || pnHtml ? "" : `<div style="color:var(--text-muted);font-size:12px;">Send a message to see director output</div>`}`;
    });
    renderContextSize();
    return;
  }

  const ds = S.directorState || {};
  const ld = S.lastDirectorData || {};
  _renderDirectorPanel({
    activeIds: ld.active_moods || ds.active_moods || [],
    latency: ld.agent_latency_ms || 0,
    toolCalls: ld.tool_calls || [],
    injection: ld.injection_block || "",
    feedback: S.lastFeedback?.values,
    directionNotes: S.lastDirectionNotes?.notes,
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
