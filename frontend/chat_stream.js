import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { onTurnStart } from "./audio_player.js";
import { messageDisplaySource } from "./card_scripts.js";
import {
  applyWorkflowTextSegments,
  buildMsgToolbar,
  canStartGeneration,
  getCharName,
  ICON_DEL,
  ICON_EDIT,
  ICON_REGEN,
  msgNumHtml,
  renderMessages,
  setMessages,
  swipeNavHtml,
  updateContextCounter,
} from "./chat_core.js";
import { renderTurnError } from "./chat_error.js";
import {
  advanceReasoningPass,
  appendReasoningDelta,
  clearInspectedMessage,
  inspectMessage,
  relightWorkflowPipelinePass,
  renderInspector,
  syncGenerationStatus,
} from "./chat_inspector.js";
import { mergeWorkflowRejections } from "./chat_workflow.js";
import { skipNoticeText } from "./decisions.js";
import { patchHtml } from "./dom_reconcile.js";
import {
  beginExpressionPrewarm,
  bufferExpressionReply,
  cancelExpressionPlayback,
  endExpressionPrewarm,
  prewarmExpressionLabels,
  startExpressionPlayback,
} from "./expression_playback.js";
import { generationStepLabel, WAITING_LABEL } from "./generation_status.js";
import { restNotice, speakerAvatarCell, unansweredHint } from "./group_cast.js";
import {
  consumeSpeakerOverride,
  refreshCastRailIntent,
  refreshSheetProposals,
  renderGroupCast,
} from "./group_setup.js";
import { refreshCharacters } from "./library_sidebar.js";
import { fitMessageCards } from "./message_fit.js";
import { renderMessageDiffHtml, renderMessageHtml } from "./message_html.js";
import { REASONING_PASSES, rememberBoxScrolls } from "./message_inspector.js";
import { beginStream, finish } from "./operations.js";
import { ensurePersonaPinned } from "./settings_personas.js";
import { sseEvents, streamPost, unescapeSSE } from "./sse.js";
import {
  conversationState,
  effectiveWorkflowEnabled,
  isViewing,
  releaseConversationState,
  S,
  streamingHidden,
} from "./state.js";
import { refreshState } from "./state_panel.js";
import { settledReply, streamAnchor } from "./stream_settle.js";
import {
  $,
  convUrl,
  esc,
  notifyError,
  pinStreamingMessage,
  resolvePlaceholders,
  scrollToBottom,
  sentenceDiff,
  setChatFollowing,
  toast,
} from "./utils.js";

/** Start the conversation's one stoppable stream; the stop button drives it. */
export function beginStreamOperation(convId) {
  const record = beginStream("chat", { conversationId: convId }, `/conversations/${convId}/stop`);
  const op = Object.assign(record.stream, { convId, record, state: conversationState(convId) });
  S.streamOp = op;
  return op;
}

/** Give up the stop button, unless a newer operation already owns it. */
export function endStreamOperation(op) {
  if (S.streamOp === op) S.streamOp = null;
  if (op.state?.streamOp === op) op.state.streamOp = null;
  finish(op.record);
}

// Once Stop is pressed the bubble holds still until the saved reply replaces it.
function previewFrozen() {
  return !!S.streamOp?.stopping;
}

// Use the visible step when an error has no explicit stage.
function phaseStage() {
  return (S.generationStep || "").replace(/(…|\.\.\.)$/, "");
}

// Empty means waiting; null means no active turn.
function setGenerationStep(label) {
  S.generationStep = label;
  syncGenerationStatus();
}

// Coalesce expensive full-body renders to one paint per animation frame.

let _paintFrame = 0;
let _paintPending = null;
let _paintedHtml = "";
let _streamScopeId = 0;
const _streamScopes = new WeakMap();

function streamingScope(body) {
  if (!_streamScopes.has(body)) _streamScopes.set(body, `msg-stream-${++_streamScopeId}`);
  return _streamScopes.get(body);
}

function streamingDisplaySource(content) {
  return messageDisplaySource({ role: "assistant", content, speaker_member_id: S.currentSpeaker?.member_id });
}

function paintStreamingBody(text) {
  if (previewFrozen() || S.expressionBuffering) return;
  const cid = S.activeConvId;
  const token = S.conversationViewToken;
  _paintPending = text;
  if (_paintFrame) return;
  _paintFrame = requestAnimationFrame(() => {
    _paintFrame = 0;
    if (S.activeConvId !== cid || S.conversationViewToken !== token) {
      _paintPending = null;
      return;
    }
    const pending = _paintPending;
    _paintPending = null;
    const body = S.streamingBodyEl;
    if (!body || pending === null) return;
    const html = renderMessageHtml(streamingDisplaySource(pending), { streaming: true, scope: streamingScope(body) });
    if (html !== _paintedHtml) {
      _paintedHtml = html;
      patchHtml(body, html);
    }
    // Already inside a frame, so scroll now rather than a frame late.
    scrollToBottom(false, { now: true });
  });
}

// Follow every growth of the streaming bubble (in-chat Inspector blocks too), not
// only body paints. ResizeObserver fires before paint, so the scroll shares the frame.
let _streamResize = null;

function followStreamingMessage(div) {
  _streamResize?.disconnect();
  _streamResize = null;
  if (!div || typeof ResizeObserver === "undefined") return;
  _streamResize = new ResizeObserver(() => scrollToBottom(false, { now: true }));
  _streamResize.observe(div);
}

/** Cancel a queued streaming paint. */
export function cancelStreamingPaint() {
  if (_paintFrame) cancelAnimationFrame(_paintFrame);
  _paintFrame = 0;
  _paintPending = null;
  _paintedHtml = "";
}

function smoothUpdateBody(el, newHtml, onComplete) {
  if (!el || el.innerHTML === newHtml) return;
  const prev = el.offsetHeight;
  el.innerHTML = newHtml;
  // Before the height is read: a rescued card bubble is thousands of pixels
  // shorter than the collapsed one, and this animates to whatever it sees.
  fitMessageCards(el);
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
  cancelStreamingPaint();
  followStreamingMessage(null);
  if (S.expressionBuffering) return false;
  const body = S.streamingBodyEl;
  if (!body) return false;
  const div = body.closest(".message");
  if (!div?.isConnected || !lastMsg || lastMsg.role !== "assistant" || !lastMsg.id) return false;

  div.classList.remove("stream-scroll-target");
  div.setAttribute("data-msg-id", lastMsg.id);
  // Its Reasoning and Inspector boxes now go by that id, so the stored copy reopens them where they were.
  rememberBoxScrolls(div);
  body.removeAttribute("id");
  // The next bubble owns the live slot; this one waits for the stored copy (refreshInlineInspector).
  div.querySelector("#reasoning-box")?.removeAttribute("id");
  div.querySelector(".msg-inspect-live")?.classList.replace("msg-inspect-live", "msg-inspect-baked");

  const bodyHtml =
    S.pendingRefineDiff && S.showEditorDiff
      ? renderMessageDiffHtml(S.pendingRefineDiff.ops)
      : renderMessageHtml(
          messageDisplaySource({
            ...lastMsg,
            speaker_member_id: lastMsg.speaker_member_id ?? S.currentSpeaker?.member_id,
          }),
        );
  smoothUpdateBody(body, bodyHtml, () => scrollToBottom(true));
  if ((S.workflowTextEffects.length || S.workflowClickHandlers.length) && !(S.pendingRefineDiff && S.showEditorDiff)) {
    applyWorkflowTextSegments(body, lastMsg);
  }

  const tb = div.querySelector(".msg-toolbar");
  if (tb) {
    tb.innerHTML = buildMsgToolbar(lastMsg);
  }

  const nav = swipeNavHtml(lastMsg);
  const roleEl = nav ? div.querySelector(".msg-role") : null;
  if (roleEl && !roleEl.querySelector(".swipe-nav")) roleEl.insertAdjacentHTML("beforeend", nav);

  return true;
}

/** Send waits for this conversation's running work, its load and its unsaved edits. */
export function syncSendButton(state = S) {
  $("send-btn").disabled =
    state.isStreaming ||
    !!state.proseRewriteMsgId ||
    state.conversationLoading ||
    Object.keys(state.queuedEdits).length > 0;
}

export function setStreaming(active) {
  S.isStreaming = active;
  const stoppable = active || !!S.proseRewriteMsgId;
  syncSendButton();
  $("send-btn").style.display = stoppable ? "none" : "flex";
  $("stop-btn").style.display = stoppable ? "flex" : "none";
  const cm = $("chat-messages");
  if (cm) cm.classList.toggle("streaming", active);
  if (active && !S.groupCast) onTurnStart();
  renderGroupCast();
}

function stopGeneration() {
  S.streamOp?.stop();
}

export function createStreamingDiv(name = null, memberId = null) {
  cancelStreamingPaint();
  const div = document.createElement("div");
  div.className = "message assistant";
  const avatar = S.showChatAvatars ? speakerAvatarCell({ role: "assistant", speaker_member_id: memberId }) : "";
  // It lands after every turn above the cut, plus the group speakers who already finished this exchange.
  const num = (S.streamCutoffIndex ?? S.messages.length) + S.completedExchangeMessageIds.length + 1;
  div.innerHTML = `${avatar}<div class="msg-role">${esc(name || getCharName())} ${msgNumHtml(num)}</div>
    <div class="msg-inspect-live"></div>
    <div class="msg-body" id="streaming-body">
      <span class="typing-indicator"><span></span><span></span><span></span></span>
    </div>
    <div class="msg-toolbar">
      <button disabled>${ICON_EDIT}</button>
      <button disabled>${ICON_REGEN}</button>
      <button disabled class="msg-btn-del">${ICON_DEL}</button>
    </div>`;
  S.streamingBodyEl = div.querySelector(".msg-body");
  followStreamingMessage(div);
  return div;
}

/** Rebuild the selected view from the retained turn, including a background speaker. */
export function restoreStreamingView() {
  syncGenerationStatus();
  if (!S.isStreaming || !S.streamOp) return;
  if (S.groupCast && !S.currentSpeaker) return;
  const div =
    S.streamingBodyEl?.closest(".message") || createStreamingDiv(S.currentSpeaker?.name, S.currentSpeaker?.member_id);
  if (S.streamOp.holder) S.streamOp.holder.el = div;
  if (S.streamingContent != null) {
    if (S.pendingRefineDiff && S.editorDraftBaseline != null) {
      const original = streamingDisplaySource(S.editorDraftBaseline);
      S.pendingRefineDiff = { original, ops: sentenceDiff(original, streamingDisplaySource(S.streamingContent)) };
    }
    S.streamingBodyEl.innerHTML =
      S.pendingRefineDiff && S.showEditorDiff
        ? renderMessageDiffHtml(S.pendingRefineDiff.ops)
        : renderMessageHtml(streamingDisplaySource(S.streamingContent));
  }
}

// SSE ack and post-stream sync promote the same optimistic user node through this helper.
function adoptPendingUserMessage(msg, content = null) {
  const div = document.querySelector('.message.user[data-msg-id="null"]');
  if (!div) return;
  div.setAttribute("data-msg-id", msg.id);
  const tb = div.querySelector(".msg-toolbar");
  if (tb) tb.innerHTML = buildMsgToolbar(msg);
  if (content === null) return;
  const body = div.querySelector(".msg-body");
  if (body) body.innerHTML = renderMessageHtml(messageDisplaySource({ ...msg, content }));
}

function patchPendingUserMessage(pendingMsg) {
  const freshMsg = S.messages.find((m) => m.role === "user" && m.id && m.content === pendingMsg.content);
  if (freshMsg) adoptPendingUserMessage(freshMsg);
}

// A row for reply text the server has not confirmed. It carries no id, so no
// message action can name it; the next sync replaces it.
function unsavedReply(content, extra = {}) {
  return {
    role: "assistant",
    content,
    id: null,
    branch_count: 1,
    branch_index: 0,
    prev_branch_id: null,
    next_branch_id: null,
    ...extra,
  };
}

// The live Editor diff was drawn against whatever the stream showed last,
// possibly a preview the turn never kept. Redraw it against the saved reply.
function settleRefineDiff(reply) {
  const baseline = S.editorDraftBaseline;
  if (!S.pendingRefineDiff || !reply || baseline == null || baseline === reply.content) {
    S.pendingRefineDiff = null;
    return;
  }
  const source = (content) => messageDisplaySource({ ...reply, content });
  const original = source(baseline);
  S.pendingRefineDiff = { original, ops: sentenceDiff(original, source(reply.content)), msgId: reply.id };
}

export async function afterStream(op, { settled = true } = {}) {
  const state = op.state || conversationState(op.convId);
  if (isViewing(state)) {
    cancelStreamingPaint();
    followStreamingMessage(null);
  }
  const wasGroupExchange = state.currentExchangeId != null;
  const groupExchangeId = state.currentExchangeId;
  const inFlightSpeaker = state.currentSpeaker;
  // The text the turn last made authoritative (Writer tokens or an announced rewrite), never a cosmetic preview.
  const preservedContent = state.streamingContent;
  const pendingUserMsg = state.pendingUserMsg || null;
  const lastCompletedId = state.completedExchangeMessageIds.at(-1) ?? null;
  const buffered = state.expressionBuffering;
  if (isViewing(state)) endExpressionPrewarm();
  state.streamCutoffIndex = null;
  state.streamingContent = null;
  state.pendingUserMsg = null;
  state.hideStreamingBox = false; // Ensure streaming box is visible after streaming ends
  state.generationStep = null;
  state.pendingGenerationStep = null;
  if (isViewing(state)) setGenerationStep(null);

  let synced = true;
  try {
    const [msgs, directorState] = await Promise.all([
      api.get(convUrl(op.convId, "messages")),
      api.get(convUrl(op.convId, "director")),
    ]);
    setMessages(msgs, state);
    state.directorState = directorState;
    const conv = S.conversations?.find((c) => c.id === op.convId);
    if (conv) conv.updated_at = new Date().toISOString();
  } catch (e) {
    synced = false;
    toast(`Failed to sync messages: ${e.message}`, true);
  }
  if (synced && !settled) toast("Failed to sync messages: the stopped reply may still be saving", true);

  if (pendingUserMsg) {
    const present = pendingUserMsg.id
      ? state.messages.some((m) => m.id === pendingUserMsg.id)
      : state.messages.some((m) => m.role === "user" && m.content === pendingUserMsg.content);
    if (!present) {
      if (state.pendingUserMsgEdit != null) pendingUserMsg.content = state.pendingUserMsgEdit;
      state.messages.push(pendingUserMsg);
    }
  }

  if (state.pendingUserMsgEdit != null) {
    const target = pendingUserMsg?.id
      ? state.messages.find((m) => m.id === pendingUserMsg.id)
      : state.messages.findLast((m) => m.role === "user" && m.id);
    if (target?.id && !(target.id in state.queuedEdits)) state.queuedEdits[target.id] = state.pendingUserMsgEdit;
  }
  state.pendingUserMsgEdit = null;

  await saveQueuedEdits(op.convId);

  // This operation's own saved reply: never one that was on screen before it
  // started, such as the branch a regeneration replaces.
  const speakerMatch =
    wasGroupExchange && inFlightSpeaker ? { exchangeId: groupExchangeId, memberId: inFlightSpeaker.member_id } : {};
  const saved = settledReply(state.messages, op.anchor, speakerMatch);
  // Text the server did not confirm stays on screen, unsaved, rather than lost.
  const unconfirmed = !synced || !settled || !!state.turnError;
  if (!saved && preservedContent?.trim() && unconfirmed && (!wasGroupExchange || inFlightSpeaker)) {
    const parent = state.messages[state.messages.length - 1];
    state.messages.push(
      unsavedReply(
        preservedContent,
        wasGroupExchange
          ? {
              parent_id: parent?.id || null,
              speaker_member_id: inFlightSpeaker.member_id,
              exchange_id: groupExchangeId,
            }
          : {},
      ),
    );
  }

  state.isStreaming = false;
  endStreamOperation(op);
  if (!isViewing(state)) {
    state.streamingBodyEl = null;
    state.currentExchangeId = null;
    state.currentSpeaker = null;
    state.speakingPlan = null;
    state.expressionBuffering = false;
    if (wasGroupExchange && state.completedExchangeMessageIds.length) consumeSpeakerOverride(state);
    state.completedExchangeMessageIds = [];
    releaseConversationState(op.convId);
    return;
  }
  setStreaming(false);

  settleRefineDiff(
    wasGroupExchange && !inFlightSpeaker ? state.messages.find((m) => m.id != null && m.id === lastCompletedId) : saved,
  );

  // Install the buffer before any saved-message repaint can reveal its text.
  if (buffered && synced && settled) {
    const streamedLive = new Set(state.completedExchangeMessageIds.slice(0, state.liveGroupReplies));
    const replies = state.messages.filter(
      (msg) =>
        msg.role === "assistant" &&
        msg.id &&
        !op.anchor.knownIds.has(msg.id) &&
        !streamedLive.has(msg.id) &&
        (!wasGroupExchange || msg.exchange_id === groupExchangeId),
    );
    void startExpressionPlayback(replies);
  }

  const finalized = !wasGroupExchange && !state.worldProposalArrived && !!saved && finalizeStreamingDiv(saved);
  state.expressionBuffering = false;
  state.worldProposalArrived = false;
  state.streamingBodyEl = null;

  if (finalized) {
    if (pendingUserMsg) patchPendingUserMessage(pendingUserMsg);
    updateContextCounter();
    const ct = $("chat-messages");
    if (ct.querySelectorAll(".message[data-msg-id]").length < state.messages.length) {
      renderMessages();
    } else {
      renderTurnError(ct);
    }
  } else {
    renderMessages();
  }
  state.currentExchangeId = null;
  state.currentSpeaker = null;
  state.speakingPlan = null;
  if (wasGroupExchange && state.completedExchangeMessageIds.length) consumeSpeakerOverride(state);
  state.completedExchangeMessageIds = [];
  renderGroupCast();
  if (wasGroupExchange && state.groupCast?.sheet_updates) refreshSheetProposals().then(renderGroupCast);
  state.lastDirectorData = null;
  const latestReply = state.messages.findLast((message) => message.role === "assistant" && message.id);
  if (latestReply) await inspectMessage(latestReply.id);
  else clearInspectedMessage();
  if (!isViewing(state)) return;
  refreshState();
  scrollToBottom(true);
  refreshCharacters();
}

export async function processSSEStream(resp, container, holder, signal, state = S) {
  // Writer tokens, the last announced rewrite, and the last cosmetic preview are
  // kept apart: only the first two are ever what the turn saves.
  let fullResponse = "",
    rewrittenResponse = null,
    previewResponse = null,
    firstToken = true,
    dispatchErrorToasted = false;
  let terminalReceived = false;

  const resetSpeakerTurnState = () => {
    state.pendingGenerationStep = null;
    fullResponse = "";
    rewrittenResponse = null;
    previewResponse = null;
    firstToken = true;
    state.streamingContent = null;
    state.pendingRefineDiff = null;
    state.editorDraftBaseline = null;
    state.reasoningDirector = "";
    state.reasoningWriter = "";
    state.reasoningEditor = "";
    state.lastFeedback = null;
    state.lastState = null;
    state.reasoningByPass = {};
    state.reasoningPassActive = 0; // tracks streaming progress (for dot lighting)
    state.reasoningPassSelected = 0; // tracks what the user is viewing
    state.reasoningUserOverride = false; // true when user has manually clicked a dot
  };
  resetSpeakerTurnState();
  // Reset once per exchange; later speakers reuse its result.
  state.lastDecisions = null;

  for await (const { event, data } of sseEvents(resp.body, { signal })) {
    if (event === "done" || event === "error") terminalReceived = true;
    if (event === "speaking_plan") {
      try {
        const parsed = JSON.parse(data);
        state.currentExchangeId = parsed.exchange_id;
        state.speakingPlan = Array.isArray(parsed.plan) ? parsed.plan : [];
        if (!state.speakingPlan.length) toast(restNotice());
        if (isViewing(state)) renderGroupCast();
      } catch (_) {}
      continue;
    }
    if (event === "speaker_start") {
      try {
        const parsed = JSON.parse(data);
        state.currentExchangeId = parsed.exchange_id;
        state.currentSpeaker = parsed;
        const viewing = isViewing(state);
        if (viewing && bufferExpressionReply(parsed.member_id))
          state.liveGroupReplies = state.completedExchangeMessageIds.length;
        resetSpeakerTurnState();
        state.generationStep = WAITING_LABEL;
        if (viewing) {
          setGenerationStep(state.generationStep);
          holder.el = createStreamingDiv(parsed.name, parsed.member_id);
          if (!streamingHidden()) container.appendChild(holder.el);
          onTurnStart();
          renderGroupCast();
          scrollToBottom();
        } else {
          holder.el = null;
          state.streamingBodyEl = null;
        }
      } catch (_) {}
      continue;
    }
    if (event === "speaker_done") {
      try {
        const parsed = JSON.parse(data);
        if (parsed.message_id) state.completedExchangeMessageIds.push(parsed.message_id);
        const viewing = isViewing(state);
        if (viewing) finalizeStreamingDiv({ ...parsed, id: parsed.message_id, role: "assistant" });
        state.streamingBodyEl = null;
        holder.el = null;
        state.currentSpeaker = null;
        if (viewing) renderGroupCast();
      } catch (_) {}
      continue;
    }
    const onToken = () => {
      if (firstToken) {
        firstToken = false;
        if (isViewing(state) && holder.el && !holder.el.isConnected && !streamingHidden())
          container.appendChild(holder.el);
        if (state.streamingBodyEl) state.streamingBodyEl.innerHTML = "";
      }
      fullResponse += unescapeSSE(data);
      state.streamingContent = rewrittenResponse || fullResponse;
      if (isViewing(state)) {
        prewarmExpressionLabels(state.streamingContent, state.currentSpeaker?.member_id);
        if (state.streamingBodyEl) paintStreamingBody(previewResponse || rewrittenResponse || fullResponse);
        else scrollToBottom();
      }
    };
    const onRewrite = (text, { preview = false } = {}) => {
      if (preview) {
        previewResponse = text;
      } else {
        rewrittenResponse = text;
        previewResponse = null;
        state.streamingContent = text;
        if (isViewing(state)) prewarmExpressionLabels(text, state.currentSpeaker?.member_id);
      }
      if (!isViewing(state) || previewFrozen() || state.expressionBuffering) return;
      cancelStreamingPaint(); // the rewrite replaces the body outright
      if (state.streamingBodyEl) {
        const html =
          state.pendingRefineDiff && state.showEditorDiff
            ? renderMessageDiffHtml(state.pendingRefineDiff.ops)
            : renderMessageHtml(streamingDisplaySource(text));
        smoothUpdateBody(state.streamingBodyEl, html, scrollToBottom);
      } else {
        if (isViewing(state)) scrollToBottom();
      }
    };
    try {
      handleSSEEvent(event, data, holder.el, onToken, onRewrite, state);
    } catch (e) {
      console.error(`SSE handler for "${event}" threw:`, e);
      if (!dispatchErrorToasted) {
        dispatchErrorToasted = true;
        toast(`Stream handler error on "${event}": ${e.message}`, true);
      }
    }
  }
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  if (!terminalReceived) throw new Error("Generation stream ended before completion");
}

function swapStreamingDraft(text, onRewrite, options, state = S) {
  if (state.editorDraftBaseline === null) state.editorDraftBaseline = state.streamingContent || "";
  const original = streamingDisplaySource(state.editorDraftBaseline);
  state.pendingRefineDiff = { original, ops: sentenceDiff(original, streamingDisplaySource(text)) };
  onRewrite(text, options);
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

function handleSSEEvent(event, data, msgDiv, onToken, onRewrite, state = S) {
  if ((event === "token" || event === "reasoning") && state.pendingGenerationStep) {
    state.generationStep = state.pendingGenerationStep;
    state.pendingGenerationStep = null;
    if (isViewing(state)) setGenerationStep(state.generationStep);
  }
  switch (event) {
    case "director_start":
      state.pendingGenerationStep = generationStepLabel("director");
      state.generationStep = WAITING_LABEL;
      if (isViewing(state)) setGenerationStep(state.generationStep);
      state.lastDirectorData = null;
      state.inspectedMsgId = null;
      state.inspectedDirectorData = null;
      if (isViewing(state)) renderInspector();
      break;
    case "director_done": {
      try {
        state.lastDirectorData = JSON.parse(data);
      } catch (_) {}
      if (isViewing(state)) {
        advanceReasoningPass(1); // director done → move to Writer dot
        renderInspector();
      }
      break;
    }
    case "step_start": {
      try {
        const step = JSON.parse(data).step;
        const label = generationStepLabel(step);
        if (label) {
          state.pendingGenerationStep = step === "writer" ? label : null;
          state.generationStep = step === "writer" ? WAITING_LABEL : label;
          if (isViewing(state)) setGenerationStep(state.generationStep);
        }
      } catch (_) {}
      break;
    }
    case "token":
      onToken();
      break;
    case "draft_update":
      try {
        const draft = JSON.parse(data).draft;
        if (draft !== state.streamingContent) swapStreamingDraft(draft, onRewrite, { preview: true }, state);
      } catch (_) {}
      break;
    case "writer_rewrite":
      if (isViewing(state)) advanceReasoningPass(2); // writer done, editor starting → move to Editor dot
      try {
        swapStreamingDraft(JSON.parse(data).refined_text, onRewrite, undefined, state);
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
          state[stateKey] = (state[stateKey] || "") + delta;
          state.reasoningPassActive = Math.max(state.reasoningPassActive, builtinIdx);
          const rebuilt = isViewing(state) && state.inspectedMsgId == null && advanceReasoningPass(builtinIdx);
          const viewingThisPass = state.reasoningPassSelected === builtinIdx;
          const box = document.getElementById("reasoning-box");
          if (isViewing(state) && state.inspectedMsgId == null && box && viewingThisPass) {
            if (!rebuilt) appendReasoningDelta(box, delta);
          }
          break;
        }
        const pipeline = state.workflowPipelines.find((p) => p.passes.some((pp) => pp.id === passKey));
        if (pipeline) {
          const firstDelta = !state.reasoningByPass[passKey];
          state.reasoningByPass[passKey] = (state.reasoningByPass[passKey] || "") + delta;
          if (isViewing(state) && firstDelta) relightWorkflowPipelinePass(pipeline, passKey);
          const wbox = document.getElementById(`reasoning-box-${pipeline.id}`);
          if (isViewing(state) && wbox && wbox.dataset.passId === passKey) {
            appendReasoningDelta(wbox, delta);
          }
          break;
        }
        console.warn("Unrouted reasoning event for pass id:", passKey, d);
      } catch (_) {}
      break;
    }
    case "decisions": {
      try {
        state.lastDecisions = JSON.parse(data);
        if (isViewing(state)) renderInspector();
        // Show new skips once; inherited results did not ask again.
        const notice = state.lastDecisions.inherited ? "" : skipNoticeText(state.lastDecisions.skipped);
        if (notice) toast(notice);
      } catch (_) {}
      break;
    }
    case "feedback": {
      try {
        const d = JSON.parse(data);
        state.lastFeedback = { values: d.values || {} };
        if (isViewing(state)) renderInspector();
      } catch (_) {}
      break;
    }
    case "state": {
      try {
        state.lastState = JSON.parse(data);
        if (isViewing(state)) renderInspector();
      } catch (_) {}
      break;
    }
    case "phase_status": {
      try {
        const d = JSON.parse(data);
        // Turn workflow steps use the primary status line.
        const label = typeof d.label === "string" ? d.label.trim() : "";
        if (label && d.state !== "done") {
          state.generationStep = label;
          if (isViewing(state)) setGenerationStep(label);
        }
      } catch (_) {}
      break;
    }
    case "editor_done": {
      try {
        const d = JSON.parse(data);
        if (d.tool_calls?.length) {
          if (!state.lastDirectorData) state.lastDirectorData = {};
          state.lastDirectorData.tool_calls = [...(state.lastDirectorData.tool_calls || []), ...d.tool_calls];
          if (isViewing(state)) renderInspector();
        }
      } catch (_) {}
      break;
    }
    case "user_message_created": {
      try {
        const d = JSON.parse(data);
        const realId = d.id;
        if (!realId) break;
        const pendingIdx = state.messages.findLastIndex((m) => m.role === "user" && !m.id);
        const prevContent = pendingIdx >= 0 ? state.messages[pendingIdx].content : null;
        if (pendingIdx >= 0) {
          state.messages[pendingIdx].id = realId;
        }
        if (state.pendingUserMsg) {
          state.pendingUserMsg.id = realId;
        }
        const editing = state.editingPendingUserMsg || state.pendingUserMsgEdit != null;
        const resolved = typeof d.content === "string" && !editing ? d.content : null;
        if (resolved !== null) {
          if (pendingIdx >= 0) state.messages[pendingIdx].content = resolved;
          if (state.pendingUserMsg) state.pendingUserMsg.content = resolved;
        }
        if (state.editingPendingUserMsg) {
          state.editingPendingUserMsg = false;
          state.editingMsgId = realId;
          if (isViewing(state)) {
            renderMessages();
            const ta = $(`edit-textarea-${realId}`);
            if (ta) {
              ta.focus();
              ta.selectionStart = ta.selectionEnd = ta.value.length;
            }
          }
        } else {
          const rewritten = resolved !== null && resolved !== prevContent ? resolved : null;
          if (isViewing(state)) adoptPendingUserMessage({ id: realId, role: "user" }, rewritten);
        }
      } catch (_) {}
      break;
    }
    case "error":
      {
        const f = parseFailure(data);
        state.turnError = {
          ...f,
          headline: f.headline || "Generation failed.",
          convId: state.activeConvId,
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
      state.worldProposalArrived = true;
      break;
    }
    case "workflow_attachments_rejected": {
      try {
        const parsed = JSON.parse(data);
        const msgIdNum = Number(parsed.message_id);
        const rejected = Array.isArray(parsed.rejected) ? parsed.rejected : [];
        if (Number.isFinite(msgIdNum) && rejected.length) {
          if (isViewing(state)) mergeWorkflowRejections(msgIdNum, null, rejected);
        }
      } catch (e) {
        console.warn("workflow_attachments_rejected parse failed", e);
      }
      break;
    }
    default: {
      const entry = state.workflowEventHandlers[event];
      if (
        isViewing(state) &&
        entry &&
        typeof entry.handler === "function" &&
        effectiveWorkflowEnabled(entry.workflowId)
      ) {
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

function agentPayload() {
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
  const state = conversationState(S.activeConvId);
  cancelExpressionPlayback();
  beginExpressionPrewarm();
  state.liveGroupReplies = 0;
  if (!state.groupCast) bufferExpressionReply();
  state.consumedSpeakerId = body?.speaker_member_id || null;
  setStreaming(true);
  setGenerationStep(WAITING_LABEL);
  state.turnError = null; // this attempt supersedes the last failure
  // Before the optimistic rows go in: what this request adds is what is new.
  const anchor = streamAnchor(state.messages);

  if (cutoffMsgId != null) {
    const idx = state.messages.findIndex((m) => m.id === cutoffMsgId);
    state.streamCutoffIndex = idx >= 0 ? idx : state.messages.length;
    setChatFollowing(true);
  }

  if (beforeRender) beforeRender();

  state.lastDirectorData = null;
  clearInspectedMessage();
  renderMessages();
  const op = beginStreamOperation(state.activeConvId);
  op.anchor = anchor;
  const ct = $("chat-messages");
  const holder = { el: null };
  op.holder = holder;
  if (state.groupCast) {
    state.currentExchangeId = "pending";
    state.completedExchangeMessageIds = [];
  } else {
    holder.el = createStreamingDiv();
    if (!streamingHidden()) ct.appendChild(holder.el);
    if (cutoffMsgId != null || anchorStream) pinStreamingMessage(holder.el);
    else scrollToBottom();
  }
  try {
    const resp = await streamPost(
      `${path}${path.includes("?") ? "&" : "?"}operation_id=${op.record.id}`,
      body,
      op.signal,
    );
    if (!resp.ok) {
      const raw = await resp.text().catch(() => "");
      const f = parseFailure(raw);
      state.turnError = {
        ...f,
        status: resp.status,
        convId: state.activeConvId,
        stage: f.stage || phaseStage(),
        at: Date.now(),
      };
      if (!state.turnError.headline) state.turnError.headline = `Orb returned HTTP ${resp.status}.`;
    } else {
      await processSSEStream(resp, ct, holder, op.signal, state);
    }
  } catch (e) {
    // An AbortError is this operation dropping its own connection (a Stop the
    // server could not confirm); settling below waits out the server's cleanup.
    if (e.name !== "AbortError") {
      console.error("Stream failed client-side:", e);
      state.turnError = {
        headline: "Lost connection to Orb.",
        sentence: e.message,
        kind: "transport",
        convId: op.convId,
        stage: phaseStage(),
        at: Date.now(),
      };
    }
    op.disconnect();
  }
  const settled = await op.settle();
  await afterStream(op, { settled });
  if (afterDone && isViewing(state)) await afterDone();
}

async function continueFromUser() {
  if (!S.activeConvId || !canStartGeneration()) return;
  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role !== "user") {
    toast("Last message is not a user message", true);
    return;
  }
  await runStreamRequest(convUrl(S.activeConvId, "continue"), turnPayload());
}

async function speakAsMember(memberId) {
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
  conversationState(S.activeConvId).draft = "";
  localStorage.removeItem(`orb-chat-draft:${S.activeConvId}`);

  const attachments = [...S.attachments];
  S.attachments.length = 0;
  $("attachment-preview").innerHTML = "";
  refreshCastRailIntent();
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

// Resolve the reply target now because it may have been deleted or swiped since paint.
// With no reply left, continue from the user message.
async function regenerateFromUser(userMsgId) {
  const reply = S.messages.find((m) => m.role === "assistant" && m.id && m.parent_id === userMsgId);
  if (reply) {
    await regenerate(reply.id);
    return;
  }
  await continueFromUser();
}

async function regenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "regenerate"), agentPayload(), {
    cutoffMsgId: msgId,
  });
}

async function superRegenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "super_regenerate"), agentPayload(), {
    cutoffMsgId: msgId,
  });
}

function toggleMagicInput(msgId) {
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

function handleMagicKey(event, msgId) {
  if (event.key === "Enter") {
    event.preventDefault();
    submitMagicRewrite(msgId);
  } else if (event.key === "Escape") {
    S.magicInputMsgId = null;
    renderMessages();
  }
}

async function submitMagicRewrite(msgId) {
  const input = document.getElementById(`magic-input-${msgId}`);
  if (!input) return;
  const direction = input.value.trim();
  if (!direction) return;
  if (!S.activeConvId || !canStartGeneration()) return;
  S.magicInputMsgId = null;
  await runStreamRequest(
    convUrl(S.activeConvId, "messages", msgId, "magic_rewrite"),
    { direction },
    {
      cutoffMsgId: msgId,
    },
  );
}

async function saveQueuedEdits(convId = S.activeConvId) {
  const state = conversationState(convId);
  for (const [id, content] of Object.entries(state.queuedEdits)) {
    const target = state.messages.find((m) => m.id === Number(id));
    if (target) target.content = content;
    try {
      await api.post(convUrl(convId, "messages", Number(id), "edit"), { content, regenerate: false });
      if (state.queuedEdits[id] === content) delete state.queuedEdits[id];
    } catch (error) {
      toast(`Edit not saved: ${error.message}`, true);
      break;
    }
  }
  if (S.activeConvId === convId) syncSendButton(state);
}

// "Edit not saved" controls: Retry saves every pending edit in order; Discard drops one and shows the saved text again.
async function resolveQueuedEdit(change) {
  const cid = S.activeConvId;
  const token = S.conversationViewToken;
  const state = conversationState(cid);
  try {
    await change(cid, state);
  } catch (error) {
    toast(`Could not update the edit: ${error.message}`, true);
  }
  if (S.activeConvId !== cid || S.conversationViewToken !== token) return;
  syncSendButton(state);
  renderMessages();
}

export function retryQueuedEdits() {
  return resolveQueuedEdit((cid) => saveQueuedEdits(cid));
}

function discardQueuedEdit(button) {
  return resolveQueuedEdit(async (cid, state) => {
    const msgs = await api.get(convUrl(cid, "messages"));
    delete state.queuedEdits[button.dataset.msgId];
    setMessages(msgs, state);
  });
}

registerActions("chat", {
  send: () => sendMessage(),
  stop: () => stopGeneration(),
  continue: () => continueFromUser(),
  regenerate: (el) => regenerate(Number(el.dataset.msgId)),
  regenerateFromUser: (el) => regenerateFromUser(Number(el.dataset.msgId)),
  superRegenerate: (el) => superRegenerate(Number(el.dataset.msgId)),
  toggleMagic: (el) => toggleMagicInput(Number(el.dataset.msgId)),
  magicKey: (el, e) => handleMagicKey(e, Number(el.dataset.msgId)),
  submitMagic: (el) => submitMagicRewrite(Number(el.dataset.msgId)),
});

registerActions("queued-edit", {
  retry: () => retryQueuedEdits(),
  discard: (el) => discardQueuedEdit(el),
});
