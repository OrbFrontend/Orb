import { api } from "./api.js";
import { onTurnStart } from "./audio_player.js";
import { updateAttachmentPreview } from "./chat_composer.js";
import {
  _applyWorkflowTextSegments,
  buildMsgToolbar,
  canStartGeneration,
  getCharName,
  ICON_DEL,
  ICON_EDIT,
  ICON_REGEN,
  renderMessages,
  setMessages,
  updateContextCounter,
} from "./chat_core.js";
import { renderTurnError } from "./chat_error.js";
import {
  _advanceReasoningPass,
  _relightWorkflowPipelinePass,
  _syncGenerationStatusVisibility,
  appendReasoningDelta,
  clearWorkflowPhase,
  REASONING_PASSES,
  renderInspector,
  setWorkflowPhase,
} from "./chat_inspector.js";
import { clearInspectedMessage } from "./chat_messages.js";
import { _mergeWorkflowRejections } from "./chat_workflow.js";
import {
  clearDirectionNotesRegenCut,
  optimisticDropDirectionNotesFrom,
  renderDirectionNotesPanel,
} from "./direction_notes_panel.js";
import { restNotice, unansweredHint } from "./group_cast.js";
import { consumeSpeakerOverride, refreshSheetProposals, renderGroupCast } from "./group_setup.js";
import { refreshCharacters } from "./library.js";
import { isUtilityPanelOpen } from "./panels.js";
import { ensurePersonaPinned } from "./settings_personas.js";
import { sseEvents, streamPost, unescapeSSE } from "./sse.js";
import { effectiveWorkflowEnabled, S } from "./state.js";
import {
  $,
  convUrl,
  esc,
  formatProse,
  formatProseWithDiff,
  notifyError,
  pinStreamingMessage,
  resolvePlaceholders,
  scrollToBottom,
  sentenceDiff,
  setChatFollowing,
  toast,
} from "./utils.js";

// Generation entry points share the SSE handling in this module.

export function stopConversation(convId) {
  fetch(`/api/conversations/${convId}/stop`, { method: "POST" }).catch(() => {});
}

const PHASE_ORDER = { pending: 0, directing: 0, generating: 1, refining: 2 };
const PHASE_LABELS = {
  pending: "Waiting for response…",
  directing: "Director analyzing scene…",
  generating: "Generating response…",
  refining: "Refining response…",
};

const PHASE_STAGES = { pending: "", directing: "director pass", generating: "writer pass", refining: "editor pass" };

function phaseStage() {
  return PHASE_STAGES[S.generationPhase] || "";
}

export function setGenerationPhase(phase) {
  if (!phase) {
    S.generationPhase = null;
  } else if (S.generationPhase && PHASE_ORDER[phase] < PHASE_ORDER[S.generationPhase]) {
    return; // never go backwards
  } else {
    S.generationPhase = phase;
  }
  _syncGenerationStatusVisibility();
  const el = $("generation-status");
  if (!S.generationPhase || !el) return;
  el.querySelector(".gen-text").textContent = PHASE_LABELS[S.generationPhase] || "Processing…";
  el.querySelector(".gen-dot").className = `gen-dot${S.generationPhase === "refining" ? " spin" : ""}`;
}

function smoothUpdateBody(el, newHtml, onComplete) {
  if (!el || el.innerHTML === newHtml) return;
  const prev = el.offsetHeight;
  el.innerHTML = newHtml;
  const next = el.scrollHeight;
  if (Math.abs(next - prev) > 4) {
    el.style.height = `${prev}px`;
    el.style.overflow = "hidden";
    el.offsetHeight; // force reflow
    el.style.transition = "height 0.3s ease";
    el.style.height = `${next}px`;
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      el.style.height = "";
      el.style.overflow = "";
      el.style.transition = "";
      onComplete?.();
    };
    el.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 350); // fallback
  } else {
    onComplete?.();
  }
}

function finalizeStreamingDiv(lastMsg) {
  const body = S.streamingBodyEl;
  if (!body) return false;
  const div = body.closest(".message");
  if (!div?.isConnected || !lastMsg || lastMsg.role !== "assistant" || !lastMsg.id) return false;

  div.classList.remove("stream-scroll-target");
  div.setAttribute("data-msg-id", lastMsg.id);
  body.removeAttribute("id");

  const bodyHtml =
    S.pendingRefineDiff && S.showEditorDiff
      ? formatProseWithDiff(S.pendingRefineDiff.ops)
      : formatProse(resolvePlaceholders(lastMsg.content));
  smoothUpdateBody(body, bodyHtml, () => scrollToBottom(true));
  if ((S.workflowTextEffects.length || S.workflowClickHandlers.length) && !(S.pendingRefineDiff && S.showEditorDiff)) {
    _applyWorkflowTextSegments(body, lastMsg);
  }

  const tb = div.querySelector(".msg-toolbar");
  if (tb) {
    tb.innerHTML = buildMsgToolbar(lastMsg);
  }

  const bc = lastMsg.branch_count || 1;
  if (bc > 1) {
    const bi = lastMsg.branch_index || 0;
    const roleEl = div.querySelector(".msg-role");
    if (roleEl && !roleEl.querySelector(".swipe-nav")) {
      roleEl.insertAdjacentHTML(
        "beforeend",
        `<span class="swipe-nav">
        <button onclick="event.stopPropagation();switchBranch(${lastMsg.prev_branch_id})" ${!lastMsg.prev_branch_id ? "disabled" : ""}>◀</button>
        <span class="swipe-counter">${bi + 1}/${bc}</span>
        <button onclick="event.stopPropagation();switchBranch(${lastMsg.next_branch_id})" ${!lastMsg.next_branch_id ? "disabled" : ""}>▶</button>
      </span>`,
      );
    }
  }

  return true;
}

export function setStreaming(active) {
  S.isStreaming = active;
  $("send-btn").style.display = active ? "none" : "flex";
  $("stop-btn").style.display = active ? "flex" : "none";
  const cm = $("chat-messages");
  if (cm) cm.classList.toggle("streaming", active);
  if (active && !S.groupCast) onTurnStart();
  renderGroupCast();
}

export function stopGeneration() {
  if (S.abortController) S.abortController.abort();
  if (S.activeConvId) {
    stopConversation(S.activeConvId);
  }
}

export function createStreamingDiv(name = null) {
  const div = document.createElement("div");
  div.className = "message assistant";
  div.innerHTML = `<div class="msg-role">${esc(name || getCharName())}</div>
    <div class="msg-body" id="streaming-body">
      <span class="typing-indicator"><span></span><span></span><span></span></span>
    </div>
    <div class="msg-toolbar">
      <button disabled>${ICON_EDIT}</button>
      <button disabled>${ICON_REGEN}</button>
      <button disabled class="msg-btn-del">${ICON_DEL}</button>
    </div>`;
  S.streamingBodyEl = div.querySelector(".msg-body");
  return div;
}

function patchParentUserMessage(assistantMsg) {
  if (!assistantMsg?.parent_id || S.hasMultipleTabs) return;
  const userDiv = document.querySelector(`.message.user[data-msg-id="${assistantMsg.parent_id}"]`);
  if (!userDiv) return;
  const regenBtn = userDiv.querySelector('.msg-toolbar [title="Regenerate"]');
  if (regenBtn) regenBtn.setAttribute("onclick", `regenerate(${assistantMsg.id})`);
}

function patchPendingUserMessage(pendingMsg) {
  const freshMsg = S.messages.find((m) => m.role === "user" && m.id && m.content === pendingMsg.content);
  if (!freshMsg) return;
  const div = document.querySelector('.message.user[data-msg-id="null"]');
  if (!div) return;
  div.setAttribute("data-msg-id", freshMsg.id);
  const tb = div.querySelector(".msg-toolbar");
  if (tb) tb.innerHTML = buildMsgToolbar(freshMsg);
}

export async function afterStream() {
  const wasGroupExchange = S.currentExchangeId != null;
  const groupExchangeId = S.currentExchangeId;
  const inFlightSpeaker = S.currentSpeaker;
  const preservedContent = S.streamingContent;
  const pendingUserMsg = S.pendingUserMsg || null;
  const wasAborted = S.wasAborted;
  S.abortController = null;
  S.streamCutoffIndex = null;
  S.streamingContent = null;
  S.pendingUserMsg = null;
  S.wasAborted = false;
  S.hideStreamingBox = false; // Ensure streaming box is visible after streaming ends
  setGenerationPhase(null);
  clearWorkflowPhase();

  if (!S.activeConvId) {
    S.streamingBodyEl = null;
    setStreaming(false);
    $("send-btn").disabled = false;
    renderMessages();
    clearInspectedMessage();
    return;
  }

  if (wasAborted) {
    await new Promise((r) => setTimeout(r, 500));
  }

  try {
    setMessages(await api.get(convUrl(S.activeConvId, "messages")));
    S.directorState = await api.get(convUrl(S.activeConvId, "director"));
    if (S.activeConvId) {
      const conv = S.conversations?.find((c) => c.id === S.activeConvId);
      if (conv) conv.updated_at = new Date().toISOString();
    }
  } catch (e) {
    toast(`Failed to sync messages: ${e.message}`, true);
  }

  if (pendingUserMsg) {
    const present = pendingUserMsg.id
      ? S.messages.some((m) => m.id === pendingUserMsg.id)
      : S.messages.some((m) => m.role === "user" && m.content === pendingUserMsg.content);
    if (!present) {
      if (S.pendingUserMsgEdit != null) pendingUserMsg.content = S.pendingUserMsgEdit;
      S.messages.push(pendingUserMsg);
    }
  }

  if (S.pendingUserMsgEdit != null) {
    const target = pendingUserMsg?.id
      ? S.messages.find((m) => m.id === pendingUserMsg.id)
      : S.messages.findLast((m) => m.role === "user" && m.id);
    if (target?.id && !(target.id in S.queuedEdits)) S.queuedEdits[target.id] = S.pendingUserMsgEdit;
  }
  S.pendingUserMsgEdit = null;

  for (const [id, content] of Object.entries(S.queuedEdits)) {
    const target = S.messages.find((m) => m.id === Number(id));
    if (target) target.content = content;
    api
      .post(convUrl(S.activeConvId, "messages", Number(id), "edit"), { content, regenerate: false })
      .catch((e) => toast(`Failed to save edit: ${e.message}`, true));
  }
  S.queuedEdits = {};

  if (wasGroupExchange && inFlightSpeaker && preservedContent?.trim()) {
    const persisted = S.messages.some(
      (message) =>
        message.role === "assistant" &&
        message.exchange_id === groupExchangeId &&
        message.speaker_member_id === inFlightSpeaker.member_id,
    );
    if (!persisted) {
      const parent = S.messages[S.messages.length - 1];
      S.messages.push({
        role: "assistant",
        content: preservedContent,
        id: null,
        parent_id: parent?.id || null,
        speaker_member_id: inFlightSpeaker.member_id,
        exchange_id: groupExchangeId,
        branch_count: 1,
        branch_index: 0,
        prev_branch_id: null,
        next_branch_id: null,
      });
    }
  } else if (!wasGroupExchange && preservedContent?.trim()) {
    const lastMsg = S.messages[S.messages.length - 1];
    if (lastMsg?.role !== "assistant") {
      S.messages.push({
        role: "assistant",
        content: preservedContent,
        id: null,
        branch_count: 1,
        branch_index: 0,
        prev_branch_id: null,
        next_branch_id: null,
      });
    }
  }

  setStreaming(false);
  $("send-btn").disabled = false;

  if (S.pendingRefineDiff) {
    const lastAssistant = [...S.messages].reverse().find((m) => m.role === "assistant" && m.id);
    S.pendingRefineDiff.msgId = lastAssistant?.id ?? null;
  }

  const lastMsg = S.messages[S.messages.length - 1];
  const finalized = !wasGroupExchange && !S.worldProposalArrived && finalizeStreamingDiv(lastMsg);
  S.worldProposalArrived = false;
  S.streamingBodyEl = null;

  if (finalized) {
    if (pendingUserMsg) patchPendingUserMessage(pendingUserMsg);
    patchParentUserMessage(lastMsg);
    updateContextCounter();
    const ct = $("chat-messages");
    if (ct.querySelectorAll(".message[data-msg-id]").length < S.messages.length) {
      renderMessages();
    } else {
      renderTurnError(ct);
    }
  } else {
    renderMessages();
  }
  S.currentExchangeId = null;
  S.currentSpeaker = null;
  S.speakingPlan = null;
  if (wasGroupExchange && S.completedExchangeMessageIds.length) consumeSpeakerOverride();
  S.completedExchangeMessageIds = [];
  renderGroupCast();
  if (wasGroupExchange && S.groupCast?.sheet_updates) refreshSheetProposals().then(renderGroupCast);
  clearInspectedMessage();
  clearDirectionNotesRegenCut();
  if (isUtilityPanelOpen("direction-notes-panel")) renderDirectionNotesPanel();
  scrollToBottom(true);
  refreshCharacters();
}

export async function processSSEStream(resp, container, holder, signal) {
  let fullResponse = "",
    rewrittenResponse = null,
    firstToken = true,
    dispatchErrorToasted = false;

  S.pendingRefineDiff = null;
  S.editorDraftBaseline = null;

  S.reasoningDirector = "";
  S.reasoningWriter = "";
  S.reasoningEditor = "";
  S.lastFeedback = null;
  S.lastDirectionNotes = null;
  S.reasoningByPass = {};
  S.reasoningPassActive = 0; // tracks streaming progress (for dot lighting)
  S.reasoningPassSelected = 0; // tracks what the user is viewing
  S.reasoningUserOverride = false; // true when user has manually clicked a dot

  const resetSpeakerTurnState = () => {
    fullResponse = "";
    rewrittenResponse = null;
    firstToken = true;
    S.streamingContent = null;
    S.pendingRefineDiff = null;
    S.editorDraftBaseline = null;
    S.reasoningDirector = "";
    S.reasoningWriter = "";
    S.reasoningEditor = "";
    S.lastFeedback = null;
    S.lastDirectionNotes = null;
    S.reasoningByPass = {};
    S.reasoningPassActive = 0;
    S.reasoningPassSelected = 0;
    S.reasoningUserOverride = false;
  };

  for await (const { event, data } of sseEvents(resp.body, { signal })) {
    if (event === "speaking_plan") {
      try {
        const parsed = JSON.parse(data);
        S.currentExchangeId = parsed.exchange_id;
        S.speakingPlan = Array.isArray(parsed.plan) ? parsed.plan : [];
        if (!S.speakingPlan.length) toast(restNotice());
        renderGroupCast();
      } catch (_) {}
      continue;
    }
    if (event === "speaker_start") {
      try {
        const parsed = JSON.parse(data);
        S.currentExchangeId = parsed.exchange_id;
        S.currentSpeaker = parsed;
        resetSpeakerTurnState();
        holder.el = createStreamingDiv(parsed.name);
        if (!S.hideUntilBaked) container.appendChild(holder.el);
        onTurnStart();
        renderGroupCast();
        scrollToBottom();
      } catch (_) {}
      continue;
    }
    if (event === "speaker_done") {
      try {
        const parsed = JSON.parse(data);
        if (parsed.message_id) S.completedExchangeMessageIds.push(parsed.message_id);
        finalizeStreamingDiv({ ...parsed, id: parsed.message_id, role: "assistant" });
        S.streamingBodyEl = null;
        holder.el = null;
        S.currentSpeaker = null;
        renderGroupCast();
      } catch (_) {}
      continue;
    }
    const onToken = () => {
      if (firstToken) {
        firstToken = false;
        if (holder.el && !holder.el.isConnected && !S.hideUntilBaked) container.appendChild(holder.el);
        if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = "";
      }
      fullResponse += unescapeSSE(data);
      S.streamingContent = rewrittenResponse || fullResponse;
      if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = formatProse(rewrittenResponse || fullResponse);
      scrollToBottom();
    };
    const onRewrite = (text) => {
      rewrittenResponse = text;
      S.streamingContent = text;
      if (S.streamingBodyEl) {
        const html =
          S.pendingRefineDiff && S.showEditorDiff ? formatProseWithDiff(S.pendingRefineDiff.ops) : formatProse(text);
        smoothUpdateBody(S.streamingBodyEl, html, scrollToBottom);
      } else {
        scrollToBottom();
      }
    };
    try {
      handleSSEEvent(event, data, container, holder.el, onToken, onRewrite);
    } catch (e) {
      console.error(`SSE handler for "${event}" threw:`, e);
      if (!dispatchErrorToasted) {
        dispatchErrorToasted = true;
        toast(`Stream handler error on "${event}": ${e.message}`, true);
      }
    }
  }
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
}

function swapStreamingDraft(text, onRewrite) {
  if (S.editorDraftBaseline === null) S.editorDraftBaseline = S.streamingContent || "";
  const original = resolvePlaceholders(S.editorDraftBaseline);
  S.pendingRefineDiff = { original, ops: sentenceDiff(original, resolvePlaceholders(text)) };
  onRewrite(text);
}

function foreignSentence(o) {
  const first = (v) => {
    if (typeof v === "string") return v;
    if (Array.isArray(v)) return v.map(first).filter(Boolean).join("; ");
    if (v && typeof v === "object") return first(v.msg ?? v.message ?? v.detail);
    return "";
  };
  return first(o.detail) || first(o.error?.message) || first(o.error) || first(o.message) || "";
}

function parseFailure(data) {
  const raw = String(data ?? "");
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.headline === "string" && parsed.headline) {
        return { sentence: "", kind: "internal", ...parsed };
      }
      return { headline: "", sentence: foreignSentence(parsed), kind: "internal", body: raw };
    }
  } catch (_) {}
  return { headline: unescapeSSE(raw), sentence: "", kind: "internal" };
}

function handleSSEEvent(event, data, _container, msgDiv, onToken, onRewrite) {
  switch (event) {
    case "director_start":
      setGenerationPhase("directing");
      S.lastDirectorData = null;
      S.inspectedMsgId = null;
      S.inspectedDirectorData = null;
      renderInspector();
      break;
    case "director_done": {
      try {
        S.lastDirectorData = JSON.parse(data);
      } catch (_) {}
      _advanceReasoningPass(1); // director done → move to Writer dot
      renderInspector();
      break;
    }
    case "token":
      setGenerationPhase("generating");
      onToken();
      break;
    case "writer_done":
      try {
        if (JSON.parse(data).editor_will_run) setGenerationPhase("refining");
      } catch (_) {}
      break;
    case "draft_update":
      try {
        const draft = JSON.parse(data).draft;
        if (draft !== S.streamingContent) swapStreamingDraft(draft, onRewrite);
      } catch (_) {}
      break;
    case "writer_rewrite":
      setGenerationPhase("refining");
      _advanceReasoningPass(2); // writer done, editor starting → move to Editor dot
      try {
        swapStreamingDraft(JSON.parse(data).refined_text, onRewrite);
      } catch (_) {}
      break;
    case "reasoning": {
      try {
        const d = JSON.parse(data);
        const passKey = d.pass;
        const delta = d.delta;
        const builtinIdx = REASONING_PASSES.findIndex((p) => p.key === passKey);
        if (builtinIdx >= 0) {
          const stateKey = `reasoning${passKey.charAt(0).toUpperCase()}${passKey.slice(1)}`;
          S[stateKey] = (S[stateKey] || "") + delta;
          const rebuilt = _advanceReasoningPass(builtinIdx);
          const viewingThisPass = S.reasoningPassSelected === builtinIdx;
          const box = document.getElementById("reasoning-box");
          if (box && viewingThisPass) {
            if (!rebuilt) appendReasoningDelta(box, delta);
          }
          break;
        }
        const pipeline = S.workflowPipelines.find((p) => p.passes.some((pp) => pp.id === passKey));
        if (pipeline) {
          const firstDelta = !S.reasoningByPass[passKey];
          S.reasoningByPass[passKey] = (S.reasoningByPass[passKey] || "") + delta;
          if (S.inspectorTab === "secondary") {
            if (firstDelta) _relightWorkflowPipelinePass(pipeline, passKey);
            const wbox = document.getElementById(`reasoning-box-${pipeline.id}`);
            if (wbox && wbox.dataset.passId === passKey) {
              appendReasoningDelta(wbox, delta);
            }
          }
          break;
        }
        console.warn("Unrouted reasoning event for pass id:", passKey, d);
      } catch (_) {}
      break;
    }
    case "feedback": {
      try {
        const d = JSON.parse(data);
        S.lastFeedback = { values: d.values || {} };
        renderInspector();
      } catch (_) {}
      break;
    }
    case "direction_notes": {
      try {
        const d = JSON.parse(data);
        S.lastDirectionNotes = { notes: d.notes || [] };
        renderInspector();
      } catch (_) {}
      break;
    }
    case "phase_status": {
      try {
        const d = JSON.parse(data);
        const channel = d.channel;
        if (typeof channel === "string" && channel.startsWith("workflow:")) {
          const label = typeof d.label === "string" ? d.label : "";
          if (d.state === "done" || !label.trim()) clearWorkflowPhase(channel);
          else setWorkflowPhase(channel, label);
        }
      } catch (_) {}
      break;
    }
    case "editor_done": {
      try {
        const d = JSON.parse(data);
        if (d.tool_calls?.length) {
          if (!S.lastDirectorData) S.lastDirectorData = {};
          S.lastDirectorData.tool_calls = [...(S.lastDirectorData.tool_calls || []), ...d.tool_calls];
          renderInspector();
        }
      } catch (_) {}
      break;
    }
    case "user_message_created": {
      try {
        const d = JSON.parse(data);
        const realId = d.id;
        if (!realId) break;
        const pendingIdx = S.messages.findLastIndex((m) => m.role === "user" && !m.id);
        const prevContent = pendingIdx >= 0 ? S.messages[pendingIdx].content : null;
        if (pendingIdx >= 0) {
          S.messages[pendingIdx].id = realId;
        }
        if (S.pendingUserMsg) {
          S.pendingUserMsg.id = realId;
        }
        const editing = S.editingPendingUserMsg || S.pendingUserMsgEdit != null;
        const resolved = typeof d.content === "string" && !editing ? d.content : null;
        if (resolved !== null) {
          if (pendingIdx >= 0) S.messages[pendingIdx].content = resolved;
          if (S.pendingUserMsg) S.pendingUserMsg.content = resolved;
        }
        if (S.editingPendingUserMsg) {
          S.editingPendingUserMsg = false;
          S.editingMsgId = realId;
          renderMessages();
          const ta = $(`edit-textarea-${realId}`);
          if (ta) {
            ta.focus();
            ta.selectionStart = ta.selectionEnd = ta.value.length;
          }
        } else {
          const div = document.querySelector('.message.user[data-msg-id="null"]');
          if (div) {
            div.setAttribute("data-msg-id", realId);
            const tb = div.querySelector(".msg-toolbar");
            if (tb) tb.innerHTML = buildMsgToolbar({ id: realId, role: "user" });
            if (resolved !== null && resolved !== prevContent) {
              const body = div.querySelector(".msg-body");
              if (body) body.innerHTML = formatProse(resolvePlaceholders(resolved));
            }
          }
        }
      } catch (_) {}
      break;
    }
    case "error":
      {
        const f = parseFailure(data);
        S.turnError = {
          ...f,
          headline: f.headline || "Generation failed.",
          convId: S.activeConvId,
          stage: f.stage || phaseStage(),
          at: Date.now(),
        };
      }
      break;
    case "warning":
      {
        const w = parseFailure(data);
        notifyError(w.headline || "A workflow step failed.", { sentence: w.sentence });
      }
      break;
    case "world_change_proposed": {
      S.worldProposalArrived = true;
      break;
    }
    case "workflow_attachments_rejected": {
      try {
        const parsed = JSON.parse(data);
        const msgIdNum = Number(parsed.message_id);
        const rejected = Array.isArray(parsed.rejected) ? parsed.rejected : [];
        if (Number.isFinite(msgIdNum) && rejected.length) {
          _mergeWorkflowRejections(msgIdNum, null, rejected);
        }
      } catch (e) {
        console.warn("workflow_attachments_rejected parse failed", e);
      }
      break;
    }
    default: {
      const entry = S.workflowEventHandlers[event];
      if (entry && typeof entry.handler === "function" && effectiveWorkflowEnabled(entry.workflowId)) {
        let parsed = data;
        try {
          parsed = JSON.parse(data);
        } catch (_) {}
        try {
          entry.handler(parsed, msgDiv || null);
        } catch (e) {
          console.error("workflow event handler for", event, "threw:", e);
        }
      }
      break;
    }
  }
}

export function agentPayload() {
  return { enable_agent: S.agentEnabled };
}

export function turnPayload() {
  return { ...agentPayload(), speaker_member_id: S.pinnedSpeakerId || null };
}

export async function runStreamRequest(
  path,
  body,
  { cutoffMsgId = null, beforeRender = null, anchorStream = false, afterDone = null } = {},
) {
  S.consumedSpeakerId = body?.speaker_member_id || null;
  setStreaming(true);
  setGenerationPhase("pending");
  $("send-btn").disabled = true;
  S.turnError = null; // this attempt supersedes the last failure

  if (cutoffMsgId != null) {
    const idx = S.messages.findIndex((m) => m.id === cutoffMsgId);
    S.streamCutoffIndex = idx >= 0 ? idx : S.messages.length;
    setChatFollowing(true);
  }

  if (beforeRender) beforeRender();

  renderMessages();
  const ct = $("chat-messages");
  const holder = { el: null };
  if (S.groupCast) {
    S.currentExchangeId = "pending";
    S.completedExchangeMessageIds = [];
  } else {
    holder.el = createStreamingDiv();
    if (!S.hideUntilBaked) ct.appendChild(holder.el);
    if (cutoffMsgId != null || anchorStream) pinStreamingMessage(holder.el);
    else scrollToBottom();
  }
  S.abortController = new AbortController();
  try {
    const resp = await streamPost(path, body, S.abortController.signal);
    if (!resp.ok) {
      const raw = await resp.text().catch(() => "");
      const f = parseFailure(raw);
      S.turnError = {
        ...f,
        status: resp.status,
        convId: S.activeConvId,
        stage: f.stage || phaseStage(),
        at: Date.now(),
      };
      if (!S.turnError.headline) S.turnError.headline = `Orb returned HTTP ${resp.status}.`;
    } else {
      await processSSEStream(resp, ct, holder, S.abortController.signal);
    }
  } catch (e) {
    if (e.name === "AbortError") {
      S.wasAborted = true;
    } else {
      console.error("Stream failed client-side:", e);
      S.turnError = {
        headline: "Lost connection to Orb.",
        sentence: e.message,
        kind: "transport",
        convId: S.activeConvId,
        stage: phaseStage(),
        at: Date.now(),
      };
      stopGeneration();
    }
  }
  await afterStream();
  if (afterDone) await afterDone();
}

export async function continueFromUser() {
  if (!S.activeConvId || !canStartGeneration()) return;
  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role !== "user") {
    toast("Last message is not a user message", true);
    return;
  }
  await runStreamRequest(convUrl(S.activeConvId, "continue"), turnPayload());
}

export async function speakAsMember(memberId) {
  if (!S.activeConvId || !memberId || !canStartGeneration()) return;
  await runStreamRequest(convUrl(S.activeConvId, "speak"), { speaker_member_id: memberId });
}

document.addEventListener("group-speak-request", (event) => speakAsMember(event.detail || S.pinnedSpeakerId));

export async function sendMessage() {
  if (!S.activeConvId || !canStartGeneration()) return;

  const inp = $("chat-input");
  let content = inp.value.trim();

  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role === "user" && lastMsg.id) {
    if (content) {
      toast(unansweredHint());
      return;
    }
    inp.value = "";
    inp.style.height = "auto";
    await continueFromUser();
    return;
  }

  if (!content) return;

  content = resolvePlaceholders(content);
  inp.value = "";
  inp.style.height = "auto";

  const attachments = [...S.attachments];
  S.attachments.length = 0;
  updateAttachmentPreview();
  const userMsg = {
    role: "user",
    content,
    id: null,
    branch_count: 1,
    branch_index: 0,
    prev_branch_id: null,
    next_branch_id: null,
    user_attachments: attachments,
  };

  await runStreamRequest(
    convUrl(S.activeConvId, "send"),
    { content, attachments, ...turnPayload() },
    {
      beforeRender() {
        S.messages.push(userMsg);
        S.pendingUserMsg = userMsg;
      },
      afterDone: ensurePersonaPinned,
    },
  );
}

export async function regenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  optimisticDropDirectionNotesFrom(msgId);
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "regenerate"), agentPayload(), {
    cutoffMsgId: msgId,
  });
}

export async function superRegenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  optimisticDropDirectionNotesFrom(msgId);
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "super_regenerate"), agentPayload(), {
    cutoffMsgId: msgId,
  });
}

export function toggleMagicInput(msgId) {
  S.magicInputMsgId = S.magicInputMsgId === msgId ? null : msgId;
  renderMessages();
  if (S.magicInputMsgId !== msgId) return;

  requestAnimationFrame(() => {
    const el = document.getElementById(`magic-input-${msgId}`);
    if (el) el.focus();
  });

  const onMouseDown = (e) => {
    const wrap = document.getElementById(`magic-wrap-${msgId}`);
    if (wrap?.contains(e.target)) return;
    if (e.target.closest(".msg-btn-magic")) {
      document.removeEventListener("mousedown", onMouseDown);
      return;
    }
    document.removeEventListener("mousedown", onMouseDown);
    if (S.magicInputMsgId === msgId) {
      S.magicInputMsgId = null;
      renderMessages();
    }
  };
  document.addEventListener("mousedown", onMouseDown);
}

export function handleMagicKey(event, msgId) {
  if (event.key === "Enter") {
    event.preventDefault();
    submitMagicRewrite(msgId);
  } else if (event.key === "Escape") {
    S.magicInputMsgId = null;
    renderMessages();
  }
}

export async function submitMagicRewrite(msgId) {
  const input = document.getElementById(`magic-input-${msgId}`);
  if (!input) return;
  const direction = input.value.trim();
  if (!direction) return;
  if (!S.activeConvId || !canStartGeneration()) return;
  S.magicInputMsgId = null;
  optimisticDropDirectionNotesFrom(msgId);
  await runStreamRequest(
    convUrl(S.activeConvId, "messages", msgId, "magic_rewrite"),
    { direction },
    {
      cutoffMsgId: msgId,
    },
  );
}
