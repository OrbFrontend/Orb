import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { renderMessages } from "./chat_core.js";
import { currentDecisionsHtml } from "./chat_decisions.js";
import { turnLabel } from "./chat_turn.js";
import { expressionLabels, expressionPlaybackCue } from "./expression_playback.js";
import { syncGenerationStatusMarquee } from "./generation_status.js";
import { cardDefTokens } from "./group_cast.js";
import { sectionHtml } from "./inspector_section.js";
import { worldName } from "./lorebooks.js";
import {
  buildFeedbackHtml,
  buildStateHtml,
  injectionHtml,
  latencyHtml,
  moodsHtml,
  moodsOf,
  REASONING_BOTTOM_THRESHOLD,
  REASONING_PASSES,
  refreshInlineInspector,
  rememberedInspection,
  rememberInspection,
  renderLiveInspector,
  shownInjection,
  toolCallsHtml,
} from "./message_inspector.js";
import { revealSidebar, sidebarShown } from "./mobile.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { preserveScroll } from "./scroll_follow.js";
import { applySettings, persistSettings } from "./settings_store.js";
import {
  charactersView,
  conversationState,
  effectiveWorkflowEnabled,
  restingCooldowns,
  S,
  subscribe,
  turnActive,
} from "./state.js";
import { renderStatePanel } from "./state_panel.js";
import { $, avatarUrl, convUrl, esc, escAttr, sentenceTail } from "./utils.js";

let inspectionRequest = 0;

export async function inspectMessage(msgId) {
  if (!S.activeConvId) return;
  const conversationId = S.activeConvId;
  const viewToken = S.conversationViewToken;
  const request = ++inspectionRequest;
  S.inspectedMsgId = msgId;
  const cached = rememberedInspection(msgId);
  if (cached) showInspection(cached);
  const isCurrent = () =>
    request === inspectionRequest &&
    S.activeConvId === conversationId &&
    S.conversationViewToken === viewToken &&
    S.inspectedMsgId === msgId;
  try {
    const data = await api.get(convUrl(conversationId, "messages", msgId, "director-log"));
    if (!isCurrent()) return;
    rememberInspection(msgId, data);
    showInspection(data);
    refreshInlineInspector(msgId);
  } catch (_e) {
    if (!isCurrent()) return;
    S.inspectedDirectorData = null;
    renderInspector();
  }
}

function showInspection(data) {
  S.inspectedDirectorData = data;
  S.inspectedReasoning = {
    director: data.reasoning_director || "",
    writer: data.reasoning_writer || "",
    editor: data.reasoning_editor || "",
  };
  S.inspectedReasoningSelected = S.inspectedReasoning.editor ? 2 : S.inspectedReasoning.writer ? 1 : 0;
  renderInspector();
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

export function advanceReasoningPass(targetIdx) {
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
  const saved = S.inspectedMsgId != null && S.inspectedDirectorData;
  const streamIdx = saved ? S.inspectedReasoningSelected : S.reasoningPassActive;
  const selectedIdx = saved ? S.inspectedReasoningSelected : S.reasoningPassSelected;
  // A saved reply's reasoning is kept apart from the live turn's.
  const textOf = (key) =>
    (saved ? S.inspectedReasoning[key] : S[`reasoning${key.charAt(0).toUpperCase()}${key.slice(1)}`]) || "";
  const dotsHtml = REASONING_PASSES.map((p, i) => {
    const hasText = !!textOf(p.key);
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
        <button class="reasoning-dot" data-wf-action="inspector:pass" data-pass-index="${i}" style="${dotStyle}">${i + 1}</button>
        <label class="reasoning-enabled-label" for="${checkId}">
          <input type="checkbox" id="${checkId}" ${enabled ? "checked" : ""} data-wf-action="inspector:togglePass" data-wf-on="change" data-pass-key="${p.key}">
          <span>on</span>
        </label>
      </div>${i < 2 ? `<div class="reasoning-rail-line" style="background:${lineColor}"></div>` : ""}`;
  }).join("");

  const selectedPass = REASONING_PASSES[selectedIdx];
  const currentText = textOf(selectedPass.key);

  const key = selectedPass.key;
  const prefillHtml =
    _passTextMode(key) && S.reasoningEnabled[key] !== false
      ? `<textarea class="reasoning-prefill" id="reasoning-prefill" data-pass="${key}" rows="3"
         placeholder="Prefill this pass's reasoning… (macros resolved)"
       >${esc(S.reasoningPrefill[key] || "")}</textarea>`
      : "";

  // With the Inspector in the chat, the text lives on each reply.
  const boxHtml = S.inspectorInline ? "" : `<div class="inspect-raw" id="reasoning-box">${esc(currentText)}</div>`;
  return sectionHtml({
    key: "reasoning",
    open: S.reasoningOpen,
    id: "reasoning-section",
    className: "reasoning-section",
    title: "Reasoning",
    body: `<div class="reasoning-stepper">
        ${dotsHtml}
        <span class="reasoning-pass-label">${esc(selectedPass.label)}</span>
      </div>
      ${boxHtml}
      ${prefillHtml}`,
  });
}

function _passTextMode(key) {
  const separate = !S.agentSameAsWriter && !!S.agentEndpointId;
  const id = key === "writer" || !separate ? S.activeEndpointId : S.agentEndpointId;
  return S.endpoints.find((e) => e.id === id)?.completion_mode === "text";
}

document.addEventListener("input", (e) => {
  if (e.target.id !== "reasoning-prefill") return;
  applySettings({ reasoning_prefill_passes: { ...S.reasoningPrefill, [e.target.dataset.pass]: e.target.value } });
  S.reasoningUserOverride = true;
});
document.addEventListener("change", (e) => {
  if (e.target.id === "reasoning-prefill") void persistSettings({ reasoning_prefill_passes: S.reasoningPrefill });
});
/** Rebuild the reasoning views. Returns whether any reasoning box now holds the full text. */
function _refreshReasoningSection() {
  const existing = document.getElementById("reasoning-section");
  if (existing) withReasoningScroll(() => (existing.outerHTML = _buildReasoningHtml()));
  return renderLiveInspector() || Boolean(existing);
}

// The streaming reply's pass tabs pick the same pass as the panel's dots.
document.addEventListener("click", (e) => {
  const tab = e.target.closest?.(".msg-inspect-live button[data-inspect-pass]");
  if (tab) selectReasoningPass(Number(tab.dataset.inspectPass));
});

function selectReasoningPass(idx) {
  if (S.inspectedMsgId != null) S.inspectedReasoningSelected = idx;
  else {
    S.reasoningPassSelected = idx;
    S.reasoningUserOverride = true;
  }
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
              <button class="reasoning-dot" data-wf-action="inspector:pipelinePass" data-pipeline-id="${escAttr(pipeline.id)}" data-pass-id="${escAttr(p.id)}" style="${dotStyle}">${i + 1}</button>
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
        <div class="inspect-raw" id="reasoning-box-${escAttr(pipeline.id)}" data-pass-id="${escAttr(selectedId)}">${esc(text)}</div>
      </div>`;
    })
    .join("");
}

export function relightWorkflowPipelinePass(pipeline, passId) {
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
function renderInspectorWorkflows() {
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

function setToolsTab(name) {
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
  el.title = el.textContent;
}

export function syncGenerationStatus() {
  const el = $("generation-status");
  if (!el) return;
  const label = turnLabel(S.turn);
  const text = el.querySelector(".gen-text");
  // Keep the last label while the strip collapses.
  if (text && label !== null) text.textContent = label;
  const stepShown = label !== null;
  const pillActive = Object.keys(S.workflowPhases).length > 0;
  el.classList.toggle("hidden", !(stepShown || pillActive));
  el.classList.toggle("pill-only", !stepShown && pillActive);
  syncGenerationStatusMarquee(el);
}

export function setWorkflowPhase(channel, label, state = S) {
  if (typeof state === "string") state = conversationState(state);
  if (typeof channel === "string" && channel.startsWith("workflow:")) {
    const wid = channel.split(":")[1];
    if (wid && !effectiveWorkflowEnabled(wid)) return;
  }
  if (label?.trim()) state.workflowPhases[channel] = label;
  else delete state.workflowPhases[channel];
  if (state.activeConvId !== S.activeConvId) return;
  _renderWorkflowPhasesPill();
  syncGenerationStatus();
}

export function clearWorkflowPhase(channel, state = S) {
  if (typeof state === "string") state = conversationState(state);
  if (channel === undefined) state.workflowPhases = {};
  else delete state.workflowPhases[channel];
  if (state.activeConvId !== S.activeConvId) return;
  _renderWorkflowPhasesPill();
  syncGenerationStatus();
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

async function toggleReasoningPass(passKey) {
  await persistSettings({
    reasoning_enabled_passes: { ...S.reasoningEnabled, [passKey]: !S.reasoningEnabled[passKey] },
  });
}

function clearRefineDiff() {
  S.pendingRefineDiff = null;
  renderMessages();
}

export function toggleInspector() {
  if (isUtilityPanelOpen("inspector")) {
    closeUtilityPanel("inspector");
  } else {
    openUtilityPanel("inspector", () => {
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

function currentMoodsHtml() {
  const inspecting = S.inspectedMsgId != null;
  if (!inspecting && !turnActive() && !S.messages.some((message) => message.role === "assistant")) return "";
  const data = inspecting ? S.inspectedDirectorData : S.lastDirectorData;
  const history = turnActive() ? S.messages.slice(0, S.turn.cutoffIndex ?? S.messages.length) : S.messages;
  const lastAssistant = history.findLast((message) => message.role === "assistant");
  // A saved reply stores cooldowns for the following turn. Read the baseline
  // before this reply, including before the whole exchange in group chats.
  const resting = inspecting
    ? restingCooldowns(S.inspectedMsgId)
    : turnActive()
      ? lastAssistant?.fragment_cooldowns || {}
      : restingCooldowns(lastAssistant?.id);
  return moodsHtml(moodsOf(data, resting), { showNone: true });
}

function _renderDirectorPanel({ latency, latencyBreakdown, toolCalls, injection, feedback, stateChanges }) {
  withReasoningScroll(() => {
    $("inspector-content").innerHTML = `
      ${currentMoodsHtml()}
      ${_buildReasoningHtml()}
      ${currentDecisionsHtml()}
      ${buildFeedbackHtml(feedback)}
      ${buildStateHtml(stateChanges)}
      ${toolCallsHtml(toolCalls)}
      ${injectionHtml(injection)}
      ${latencyHtml(latency, latencyBreakdown)}`;
  });
}

function _renderInspectorMain() {
  // Turn details live on each reply; the panel keeps the next-turn controls.
  if (S.inspectorInline) {
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       ${_buildReasoningHtml()}`;
    });
    return;
  }

  if (S.inspectedMsgId == null && turnActive() && S.lastDirectorData === null) {
    withReasoningScroll(() => {
      $("inspector-content").innerHTML = `
       ${currentMoodsHtml()}
       ${_buildReasoningHtml()}
       ${currentDecisionsHtml()}
       <div style="color:var(--text-muted);font-size:12px;display:flex;align-items:center;gap:8px">
         <span class="typing-indicator"><span></span><span></span><span></span></span> Director thinking…
       </div>`;
    });
    return;
  }

  const insp = S.inspectedMsgId && S.inspectedDirectorData ? S.inspectedDirectorData : null;

  if (S.inspectedMsgId != null) {
    const data = insp || {};
    _renderDirectorPanel({
      latency: data.agent_latency_ms || 0,
      latencyBreakdown: data.latency_breakdown,
      toolCalls: data.tool_calls || [],
      injection: shownInjection(data),
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
       ${currentMoodsHtml()}
       ${_buildReasoningHtml()}
       ${decHtml}
       ${fbHtml}
       ${stateHtml}
       ${fbHtml || stateHtml || decHtml ? "" : `<div style="color:var(--text-muted);font-size:12px;">Send a message to see director output</div>`}`;
    });
    return;
  }

  const ld = S.lastDirectorData || {};
  _renderDirectorPanel({
    latency: ld.agent_latency_ms || 0,
    latencyBreakdown: ld.latency_breakdown,
    toolCalls: ld.tool_calls || [],
    injection: shownInjection(ld),
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

function expressionCharId() {
  const cue = expressionPlaybackCue();
  if (cue?.charId) return cue.charId;
  if (!S.groupCast) return S.activeCharId;
  if (S.turn.speaker?.card_id) return S.turn.speaker.card_id;
  const lastSpoken = [...S.messages].reverse().find((m) => m.role === "assistant" && m.speaker_member_id);
  const member = lastSpoken && S.groupCast.members.find((item) => item.id === lastSpoken.speaker_member_id);
  return (
    member?.character_card_id || S.groupCast.members.find((item) => item.character_card_id)?.character_card_id || null
  );
}

function _portraitSection(title, rows, extra = "") {
  const list = rows.map(([term, value]) => `<dt>${term}</dt><dd>${value}</dd>`).join("");
  return `<section class="portrait-info-section"><div class="portrait-info-title">${title}</div>${
    list ? `<dl class="portrait-info-rows">${list}</dl>` : ""
  }${extra}</section>`;
}

function _contextBarsHtml(size) {
  const total = size.total_tokens_est;
  return Object.entries(size.breakdown)
    .filter(([, part]) => part.tokens_est > 0)
    .sort((a, b) => b[1].tokens_est - a[1].tokens_est)
    .map(([key, part]) => {
      const pct = ((part.tokens_est / total) * 100).toFixed(0);
      const label = key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
      return `<div class="ctx-row">
        <span class="ctx-label">${esc(label)}</span>
        <span class="ctx-bar"><span class="ctx-bar-fill" style="width:${pct}%"></span></span>
        <span class="ctx-tokens">${part.tokens_est.toLocaleString()}</span>
      </div>`;
    })
    .join("");
}

function _portraitInfoHtml(card) {
  let html = "";
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  const started = conv?.created_at && new Date(conv.created_at);
  const chatRows = [];
  if (started && !Number.isNaN(started.getTime())) {
    chatRows.push(["Started", esc(started.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }))]);
  }
  const size = S.contextSize;
  if (size) {
    const msgs = `${size.message_count.toLocaleString()} msg${size.message_count === 1 ? "" : "s"}`;
    chatRows.push(["Context", `≈ ${size.total_tokens_est.toLocaleString()} tokens · ${msgs}`]);
  }
  const bars = size?.total_tokens_est ? _contextBarsHtml(size) : "";
  if (chatRows.length)
    html += _portraitSection("This chat", chatRows, bars && `<div class="portrait-info-context">${bars}</div>`);
  if (!card) return html;
  const rows = [];
  if (card.creator) rows.push(["Creator", esc(card.creator)]);
  if (card.character_version) rows.push(["Version", esc(card.character_version)]);
  const tokens = cardDefTokens(card);
  if (tokens) rows.push(["Definition", `≈ ${tokens.toLocaleString()} tokens`]);
  const world = card.world_id && worldName(card.world_id);
  if (world) {
    rows.push([
      "World",
      `<button class="portrait-info-world" data-wf-action="lorebook:open" data-world-id="${escAttr(card.world_id)}">${esc(world)}</button>`,
    ]);
  }
  const tags = (card.tags || []).map((tag) => `<span class="char-tag">${esc(tag)}</span>`).join("");
  if (rows.length || tags) html += _portraitSection("Card", rows, tags && `<div class="char-tags">${tags}</div>`);
  return html;
}

/** Name the portrait and redraw its info when either changed. */
function _renderPortraitInfo(img, charId) {
  const card = charactersView().find((c) => c.id === charId);
  const name = card?.name || "";
  const label = $("sidebar-portrait-name");
  if (label && label.textContent !== name) label.textContent = name;
  img.alt = name || "Character portrait";
  const box = $("sidebar-portrait-info");
  const html = _portraitInfoHtml(card);
  if (box && box._html !== html) {
    box._html = html;
    box.innerHTML = html;
  }
}

/** Show *label*'s expression image, or the plain avatar for none, and caption it when the card has expressions. */
function _showExpression(img, charId, label) {
  const next = label ? `/api/characters/${charId}/expressions/${label}` : avatarUrl(charId, { full: true });
  if (img._exprSrc !== next) {
    img._exprSrc = next; // swap only on change (ETag handles caching; no ?t= flicker)
    img.src = next;
  }
  const caption = $("sidebar-portrait-expression");
  if (!caption) return;
  const shown = Boolean(label && img._exprLabels?.length);
  caption.textContent = shown ? label.replaceAll("_", " ") : "";
  caption.classList.toggle("hidden", !shown);
}

async function _bindExpressionChar(img, charId) {
  img._exprCharId = charId;
  img._exprSrc = null;
  img._exprText = null;
  img._exprFullLen = 0;
  _exprLastCallAt = 0;
  img._exprLabels = null;
  $("sidebar-portrait-expression")?.classList.add("hidden");
  _renderPortraitInfo(img, charId);
  const labels = await expressionLabels(charId);
  if (img._exprCharId !== charId || _portraitHidden()) return;
  img._exprLabels = labels;
  _showExpression(img, charId, labels.includes("neutral") ? "neutral" : null);
}

async function _expressionTick() {
  const img = $("sidebar-portrait-image");
  if (!img || S.turn.expressionBuffering || _portraitHidden()) return;
  const charId = expressionCharId();
  if (!charId) return;
  if (charId !== img._exprCharId) {
    await _bindExpressionChar(img, charId);
    if (charId !== img._exprCharId) return;
  }
  _renderPortraitInfo(img, charId);
  const cue = expressionPlaybackCue();
  if (cue) {
    if (cue.loading) return; // hold the current image rather than flash the plain avatar
    _showExpression(img, charId, cue.label || null);
    return;
  }
  if (!img._exprLabels?.length) return;
  const streaming = turnActive();
  const full = streaming
    ? S.turn.content
    : [...S.messages].reverse().find((m) => m.role === "assistant" && m.id)?.content;
  if (!full) return;
  const now = Date.now();
  if (now - _exprLastCallAt < _EXPR_MIN_INTERVAL_MS) return; // fast models: rate floor
  let text = sentenceTail(full, _EXPR_TAIL_SENTENCES, streaming);
  if (
    (!text || img._exprText === text) &&
    streaming &&
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
  const convId = S.activeConvId;
  try {
    ({ label } = await api.post("/local-ml/classify-emotion", { text }));
  } catch (_e) {
    clearInterval(_exprTimer);
    _exprTimer = null;
    return;
  }
  if (convId !== S.activeConvId || img._exprCharId !== charId || expressionPlaybackCue() || S.turn.expressionBuffering)
    return;
  const labels = img._exprLabels || [];
  _showExpression(img, charId, labels.includes(label) ? label : labels.includes("neutral") ? "neutral" : null);
}

subscribe("expression-playback", () => void _expressionTick());
subscribe("context-size", () => {
  const img = $("sidebar-portrait-image");
  if (img?._exprCharId && !_portraitHidden()) _renderPortraitInfo(img, img._exprCharId);
});
subscribe("settings", (patch) => {
  if ("inspector_inline" in patch || "agent_same_as_writer" in patch || "show_mood_injection" in patch)
    renderInspector();
  else if ("workflows_globally_enabled" in patch || "workflow_enabled" in patch) renderInspectorWorkflows();
  else if ("reasoning_enabled_passes" in patch) _refreshReasoningSection();
});
subscribe("endpoints", () => renderInspector()); // the lane in use gates the prefill box

// Put the sidebar back as it was when the portrait opened over it; null when the reader already had it out.
let _restoreSidebar = null;
let _sidebarScrollTop = 0;

function _portraitHidden() {
  return $("sidebar-portrait")?.classList.contains("hidden") ?? true;
}

/** Toggle the character portrait, which takes over the sidebar column and follows the reply's expression. */
export async function togglePortrait() {
  const charId = expressionCharId();
  if (!charId) return;
  const portrait = $("sidebar-portrait");
  const img = $("sidebar-portrait-image");
  if (!portrait || !img) return;
  if (!portrait.classList.contains("hidden")) {
    closePortrait();
    return;
  }
  const hasExpr = charactersView().find((c) => c.id === charId)?.has_expressions;
  if (!hasExpr) img.src = avatarUrl(charId, { full: true });
  _sidebarScrollTop = document.querySelector("#sidebar .sidebar-scroll")?.scrollTop ?? 0;
  portrait.classList.remove("hidden");
  _restoreSidebar = revealSidebar();
  img._exprCharId = null;
  await _bindExpressionChar(img, charId);
  if (_portraitHidden()) return;
  _expressionTick();
  _exprTimer = setInterval(_expressionTick, 1000);
}

function closePortrait() {
  const portrait = $("sidebar-portrait");
  if (!portrait || portrait.classList.contains("hidden")) return;
  portrait.classList.add("hidden");
  const scroller = document.querySelector("#sidebar .sidebar-scroll");
  if (scroller) scroller.scrollTop = _sidebarScrollTop;
  const img = $("sidebar-portrait-image");
  if (img) img._exprCharId = null;
  clearInterval(_exprTimer);
  _exprTimer = null;
  const restore = _restoreSidebar;
  _restoreSidebar = null;
  restore?.();
}

// Collapsing the sidebar or closing its drawer takes the portrait with it.
const _appRoot = $("app");
if (_appRoot) {
  new MutationObserver(() => {
    if (!_portraitHidden() && !sidebarShown()) {
      _restoreSidebar = null;
      closePortrait();
    }
  }).observe(_appRoot, { attributes: true, attributeFilter: ["class"] });
}

registerActions("inspector", {
  pass: (el) => selectReasoningPass(Number(el.dataset.passIndex)),
  togglePass: (el) => toggleReasoningPass(el.dataset.passKey),
  pipelinePass: (el) => selectWorkflowPipelinePass(el.dataset.pipelineId, el.dataset.passId),
  clearDiff: () => clearRefineDiff(),
  toolsTab: (el) => setToolsTab(el.dataset.tab),
  togglePortrait: () => togglePortrait(),
  closePortrait: () => closePortrait(),
});
