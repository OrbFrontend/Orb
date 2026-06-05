// Streaming + generation lifecycle: the generation-phase indicator, the SSE
// reader/dispatcher, the post-stream reconcile (afterStream), and the user-
// facing send / continue / regenerate / super-regenerate / magic-rewrite
// entry points. Split out of chat.js; the public surface is re-exported from
// chat.js.
import { api } from "./api.js";
import { onTurnStart } from "./audio_player.js";
import { updateAttachmentPreview } from "./chat_composer.js";
import {
  ICON_DEL,
  ICON_EDIT,
  ICON_REGEN,
  _applyWorkflowTextSegments,
  buildMsgToolbar,
  canStartGeneration,
  getCharName,
  renderMessages,
  setMessages,
  updateContextCounter,
} from "./chat_core.js";
import {
  REASONING_PASSES,
  _advanceReasoningPass,
  _relightWorkflowPipelinePass,
  _syncGenerationStatusVisibility,
  clearWorkflowPhase,
  renderInspector,
  setWorkflowPhase,
} from "./chat_inspector.js";
import { clearInspectedMessage } from "./chat_messages.js";
import { _mergeWorkflowRejections } from "./chat_workflow.js";
import { refreshCharacters } from "./library.js";
import { S } from "./state.js";
import {
  $,
  convUrl,
  esc,
  formatProse,
  formatProseWithDiff,
  resolvePlaceholders,
  scrollToBottom,
  sentenceDiff,
  toast,
} from "./utils.js";

// ── Streaming transport
// These bypass the `api` helper deliberately: SSE responses must be read off
// the raw `Response` (api._req would consume the body with .json()), and stop
// is fire-and-forget. Domain-specific, so they live with the stream machinery.
export function streamPost(path, body, signal) {
  return fetch("/api" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export function stopConversation(convId) {
  fetch(`/api/conversations/${convId}/stop`, { method: "POST" }).catch(() => {});
}

// ── Generation Phase
const PHASE_ORDER = { pending: 0, directing: 0, generating: 1, refining: 2 };
const PHASE_LABELS = {
  pending: "Waiting for response…",
  directing: "Director analyzing scene…",
  generating: "Generating response…",
  refining: "Refining response…",
};
let _refineTimer = null;

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
  el.querySelector(".gen-dot").className = "gen-dot" + (S.generationPhase === "refining" ? " spin" : "");
}

function smoothUpdateBody(el, newHtml, onComplete) {
  if (!el || el.innerHTML === newHtml) return;
  const prev = el.offsetHeight;
  el.innerHTML = newHtml;
  const next = el.scrollHeight;
  if (Math.abs(next - prev) > 4) {
    el.style.height = prev + "px";
    el.style.overflow = "hidden";
    el.offsetHeight; // force reflow
    el.style.transition = "height 0.3s ease";
    el.style.height = next + "px";
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
  if (!div || !div.isConnected || !lastMsg || lastMsg.role !== "assistant" || !lastMsg.id) return false;

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

function scheduleRefineTimer() {
  clearTimeout(_refineTimer);
  _refineTimer = setTimeout(() => {
    if (S.isStreaming && S.generationPhase === "generating") setGenerationPhase("refining");
  }, 1500);
}

function clearRefineTimer() {
  clearTimeout(_refineTimer);
  _refineTimer = null;
}

// ── Streaming Helpers
export function setStreaming(active) {
  S.isStreaming = active;
  $("send-btn").style.display = active ? "none" : "flex";
  $("stop-btn").style.display = active ? "flex" : "none";
  const cm = $("chat-messages");
  if (cm) cm.classList.toggle("streaming", active);
  if (active) onTurnStart();
}

export function stopGeneration() {
  if (S.abortController) S.abortController.abort();
  if (S.activeConvId) {
    stopConversation(S.activeConvId);
  }
}

export function createStreamingDiv() {
  const div = document.createElement("div");
  div.className = "message assistant";
  div.innerHTML = `<div class="msg-role">${esc(getCharName())}</div>
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
  const preservedContent = S.streamingContent;
  const pendingUserMsg = S.pendingUserMsg || null;
  const wasAborted = S.wasAborted;
  S.abortController = null;
  S.streamCutoffIndex = null;
  S.streamingContent = null;
  S.pendingUserMsg = null;
  S.wasAborted = false;
  S.hideStreamingBox = false; // Ensure streaming box is visible after streaming ends
  clearRefineTimer();
  setGenerationPhase(null);
  // The phase_status handler clears a channel's label only on a terminal "done"
  // state; a workflow that stops without one (error or dropped stream) would
  // leave stale pill text. Clear on stream close as a backstop.
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
    // Update the conversation's updated_at timestamp so refreshCharacters() can
    // correctly place the active character at the top of the recent list.
    if (S.activeConvId) {
      const conv = S.conversations?.find((c) => c.id === S.activeConvId);
      if (conv) conv.updated_at = new Date().toISOString();
    }
  } catch (e) {
    toast("Failed to sync messages: " + e.message, true);
  }

  if (pendingUserMsg) {
    // Identify by id once the message is persisted; fall back to content for the
    // rare case where the turn errored before the user message was saved.
    // Matching by content alone would mis-fire when an edit changed it mid-turn.
    const present = pendingUserMsg.id
      ? S.messages.some((m) => m.id === pendingUserMsg.id)
      : S.messages.some((m) => m.role === "user" && m.content === pendingUserMsg.content);
    if (!present) {
      if (S.pendingUserMsgEdit != null) pendingUserMsg.content = S.pendingUserMsgEdit;
      S.messages.push(pendingUserMsg);
    }
  }

  // Edits saved mid-stream were queued because the /edit route blocks on the
  // stream lock for the whole turn. The lock is free now, so persist them and
  // keep the local copies in sync (the refetch above reverted them to the
  // server's pre-edit content). The id-less pending user message is queued
  // separately (pendingUserMsgEdit) since it has no id to key on yet; it carries
  // a real id by this point (user_message_created lands before the stream ends).
  if (S.pendingUserMsgEdit != null) {
    const target = pendingUserMsg?.id
      ? S.messages.find((m) => m.id === pendingUserMsg.id)
      : S.messages.findLast((m) => m.role === "user" && m.id);
    // A later id-keyed edit of the same message (queued via saveEdit once the id
    // arrived) supersedes this earlier id-less one, so don't clobber it.
    if (target?.id && !(target.id in S.queuedEdits)) S.queuedEdits[target.id] = S.pendingUserMsgEdit;
  }
  S.pendingUserMsgEdit = null;

  for (const [id, content] of Object.entries(S.queuedEdits)) {
    const target = S.messages.find((m) => m.id === Number(id));
    if (target) target.content = content;
    api
      .post(convUrl(S.activeConvId, "messages", Number(id), "edit"), { content, regenerate: false })
      .catch((e) => toast("Failed to save edit: " + e.message, true));
  }
  S.queuedEdits = {};

  if (preservedContent?.trim()) {
    const lastMsg = S.messages[S.messages.length - 1];
    if (!lastMsg || lastMsg.role !== "assistant") {
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

  // Anchor the pending diff to the specific message ID it was generated for,
  // so branch navigation doesn't show stale diffs on the wrong message.
  if (S.pendingRefineDiff) {
    const lastAssistant = [...S.messages].reverse().find((m) => m.role === "assistant" && m.id);
    S.pendingRefineDiff.msgId = lastAssistant?.id ?? null;
  }

  // Finalize the streaming div in-place — no DOM destruction, no flash
  const lastMsg = S.messages[S.messages.length - 1];
  const finalized = finalizeStreamingDiv(lastMsg);
  S.streamingBodyEl = null;

  if (finalized) {
    // Streaming div already updated in-place — no full re-render needed.
    // Only patch the pending user message if one exists (sendMessage path).
    if (pendingUserMsg) patchPendingUserMessage(pendingUserMsg);
    patchParentUserMessage(lastMsg);
    updateContextCounter();
    // Magic rewrite updates a message in-place, so subsequent messages survive on the
    // backend but were hidden by streamCutoffIndex during streaming. Re-render if any
    // are missing from the DOM.
    const ct = $("chat-messages");
    if (ct.querySelectorAll(".message[data-msg-id]").length < S.messages.length) {
      renderMessages();
    }
  } else {
    renderMessages();
  }
  clearInspectedMessage();
  scrollToBottom(true);
  refreshCharacters();
}

export async function processSSEStream(resp, container, msgDiv, signal) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "",
    fullResponse = "",
    rewrittenResponse = null,
    firstToken = true,
    currentEvent = null;

  // Clear any diff from the previous turn
  S.pendingRefineDiff = null;

  // Reset reasoning state for this generation turn
  S.reasoningDirector = "";
  S.reasoningWriter = "";
  S.reasoningEditor = "";
  S.reasoningByPass = {};
  S.reasoningPassActive = 0; // tracks streaming progress (for dot lighting)
  S.reasoningPassSelected = 0; // tracks what the user is viewing
  S.reasoningUserOverride = false; // true when user has manually clicked a dot

  if (signal) signal.addEventListener("abort", () => reader.cancel());

  while (true) {
    const { done, value } = await reader.read();
    if (done || signal?.aborted) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();

    for (const line of lines) {
      if (line.startsWith("event: ")) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith("data: ") && currentEvent) {
        const data = line.slice(6);
        handleSSEEvent(
          currentEvent,
          data,
          container,
          msgDiv,
          () => {
            if (firstToken) {
              firstToken = false;
              if (!msgDiv.isConnected && !S.hideUntilBaked) container.appendChild(msgDiv);
              if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = "";
            }
            fullResponse += data.replace(/\\n/g, "\n");
            S.streamingContent = rewrittenResponse || fullResponse;
            if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = formatProse(rewrittenResponse || fullResponse);
            scrollToBottom();
          },
          (text) => {
            rewrittenResponse = text;
            S.streamingContent = text;
            if (S.streamingBodyEl) {
              const html =
                S.pendingRefineDiff && S.showEditorDiff
                  ? formatProseWithDiff(S.pendingRefineDiff.ops)
                  : formatProse(text);
              smoothUpdateBody(S.streamingBodyEl, html, scrollToBottom);
            } else {
              scrollToBottom();
            }
          },
        );
        currentEvent = null;
      }
    }
  }
  // reader.cancel() resolves read() with done:true rather than throwing, so
  // re-throw here so callers can set S.wasAborted and wait for the backend.
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
}

function handleSSEEvent(event, data, container, msgDiv, onToken, onRewrite) {
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
    case "prompt_rewritten":
      try {
        const d = JSON.parse(data);
        const lastUser =
          [...S.messages].reverse().find((m) => m.role === "user" && !m.id) ||
          [...S.messages].reverse().find((m) => m.role === "user");
        if (lastUser) lastUser.content = d.refined_message;
        if (S.pendingUserMsg) S.pendingUserMsg.content = d.refined_message;
        if (S.isStreaming) {
          const userBodies = document.querySelectorAll("#chat-messages .message.user .msg-body");
          const last = userBodies[userBodies.length - 1];
          if (last) last.innerHTML = formatProse(d.refined_message);
        } else {
          renderMessages();
        }
      } catch (_) {}
      break;
    case "token":
      setGenerationPhase("generating");
      onToken();
      scheduleRefineTimer();
      break;
    case "writer_rewrite":
      clearRefineTimer();
      setGenerationPhase("refining");
      _advanceReasoningPass(2); // writer done, editor starting → move to Editor dot
      try {
        const refined = JSON.parse(data).refined_text;
        // S.streamingContent still holds the writer's unrefined text at this point
        const original = resolvePlaceholders(S.streamingContent || "");
        const refinedResolved = resolvePlaceholders(refined);
        S.pendingRefineDiff = { original, ops: sentenceDiff(original, refinedResolved) };
        onRewrite(refined);
      } catch (_) {}
      break;
    case "reasoning": {
      try {
        const d = JSON.parse(data);
        const passKey = d.pass;
        const delta = d.delta;
        const builtinIdx = REASONING_PASSES.findIndex((p) => p.key === passKey);
        if (builtinIdx >= 0) {
          // Built-in pass: append delta to the named state and update the Main reasoning box.
          const stateKey = "reasoning" + passKey.charAt(0).toUpperCase() + passKey.slice(1);
          S[stateKey] = (S[stateKey] || "") + delta;
          const rebuilt = _advanceReasoningPass(builtinIdx);
          const viewingThisPass = S.reasoningPassSelected === builtinIdx;
          const box = document.getElementById("reasoning-box");
          if (box && viewingThisPass) {
            // Skip the append when _advanceReasoningPass already rebuilt the section
            // from the (now-current) state; appending again would duplicate this delta.
            // Text node append (not `textContent += ...`) avoids the DOM re-serialisation
            // that produced the visible scrollbar wobble on long streams.
            if (!rebuilt) box.appendChild(document.createTextNode(delta));
            box.scrollTop = box.scrollHeight;
          }
          // When the box is absent (Inspector closed, or user is on the Secondary tab)
          // state accumulates silently; renderInspector will paint the full text the
          // next time it runs.
          break;
        }
        const pipeline = S.workflowPipelines.find((p) => p.passes.some((pp) => pp.id === passKey));
        if (pipeline) {
          // Pre-write read: the dot/line lit-state changes only on this empty ->
          // non-empty transition, so relight once rather than on every delta.
          const firstDelta = !S.reasoningByPass[passKey];
          S.reasoningByPass[passKey] = (S.reasoningByPass[passKey] || "") + delta;
          if (S.inspectorTab === "secondary") {
            if (firstDelta) _relightWorkflowPipelinePass(pipeline, passKey);
            const wbox = document.getElementById("reasoning-box-" + pipeline.id);
            if (wbox && wbox.dataset.passId === passKey) {
              wbox.appendChild(document.createTextNode(delta));
              wbox.scrollTop = wbox.scrollHeight;
            }
          }
          break;
        }
        console.warn("Unrouted reasoning event for pass id:", passKey, d);
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
        // Find the pending user message (most recent user message without an id)
        const pendingIdx = S.messages.findLastIndex((m) => m.role === "user" && !m.id);
        if (pendingIdx >= 0) {
          S.messages[pendingIdx].id = realId;
        }
        if (S.pendingUserMsg) {
          S.pendingUserMsg.id = realId;
        }
        // If the user is currently editing the pending message, transition to normal edit mode
        if (S.editingPendingUserMsg) {
          S.editingPendingUserMsg = false;
          S.editingMsgId = realId;
          renderMessages();
          const ta = $("edit-textarea-" + realId);
          if (ta) {
            ta.focus();
            ta.selectionStart = ta.selectionEnd = ta.value.length;
          }
        } else {
          // Patch the DOM element's data-msg-id and toolbar
          const div = document.querySelector('.message.user[data-msg-id="null"]');
          if (div) {
            div.setAttribute("data-msg-id", realId);
            const tb = div.querySelector(".msg-toolbar");
            if (tb) tb.innerHTML = buildMsgToolbar({ id: realId, role: "user" });
          }
        }
        // A queued edit (S.pendingUserMsgEdit) is intentionally NOT POSTed here:
        // mid-stream the /edit route blocks on the stream lock, so afterStream()
        // persists it once the lock frees. The local copy already shows the edit.
      } catch (_) {}
      break;
    }
    case "error":
      toast("Error: " + data, true);
      break;
    case "workflow_attachments_rejected": {
      // Stash for the post-stream renderMessages paint. Do NOT call
      // renderMessages here -- S.messages doesn't yet contain the new
      // asst_id (it lands via afterStream's setMessages refetch),
      // and renderMessages mid-stream would clobber the streaming bubble.
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
      const handler = S.workflowEventHandlers[event];
      if (typeof handler === "function") {
        let parsed = data;
        try {
          parsed = JSON.parse(data);
        } catch (_) {}
        try {
          handler(parsed, msgDiv || null);
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

async function runStreamRequest(path, body, cutoffMsgId = null) {
  setStreaming(true);
  setGenerationPhase("pending");
  $("send-btn").disabled = true;

  if (cutoffMsgId != null) {
    const idx = S.messages.findIndex((m) => m.id === cutoffMsgId);
    S.streamCutoffIndex = idx >= 0 ? idx : S.messages.length;
    S.autoscrollEnabled = true;
  }

  renderMessages();
  const ct = $("chat-messages");
  const msgDiv = createStreamingDiv();
  if (!S.hideUntilBaked) ct.appendChild(msgDiv);
  scrollToBottom();
  S.abortController = new AbortController();
  try {
    const resp = await streamPost(path, body, S.abortController.signal);
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === "AbortError") S.wasAborted = true;
    else toast("Error: " + e.message, true);
  }
  await afterStream();
}

export async function continueFromUser() {
  if (!S.activeConvId || !canStartGeneration()) return;
  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role !== "user") {
    toast("Last message is not a user message", true);
    return;
  }
  await runStreamRequest(convUrl(S.activeConvId, "continue"), agentPayload());
}

// ── Send Message
export async function sendMessage() {
  if (!S.activeConvId || !canStartGeneration()) return;

  const inp = $("chat-input");
  let content = inp.value.trim();

  // Guard against double user turns: if the last message is already from the user,
  // ask the backend to generate a response for it without creating a new message.
  const lastMsg = S.messages[S.messages.length - 1];
  if (lastMsg?.role === "user" && lastMsg.id) {
    inp.value = "";
    inp.style.height = "auto";
    await continueFromUser();
    return;
  }

  if (!content) return;

  // Resolve {{user}} and {{char}} placeholders before sending
  content = resolvePlaceholders(content);

  setStreaming(true);
  setGenerationPhase("pending");
  inp.value = "";
  inp.style.height = "auto";
  $("send-btn").disabled = true;

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
    // Key matches the renderer's read (`m.user_attachments` in renderMessages)
    // so the optimistic bubble shows the image during the SSE stream window,
    // not just after afterStream() re-fetches the server-shaped message.
    user_attachments: attachments,
  };
  S.messages.push(userMsg);
  S.pendingUserMsg = userMsg;
  renderMessages();

  const ct = $("chat-messages");
  const msgDiv = createStreamingDiv();
  if (!S.hideUntilBaked) ct.appendChild(msgDiv);
  scrollToBottom();

  S.abortController = new AbortController();
  try {
    const resp = await streamPost(
      convUrl(S.activeConvId, "send"),
      { content, attachments, ...agentPayload() },
      S.abortController.signal,
    );
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === "AbortError") {
      S.wasAborted = true;
    } else {
      toast("Connection error: " + e.message, true);
    }
  }
  await afterStream();
}

// ── Regenerate
export async function regenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "regenerate"), agentPayload(), msgId);
}

// ── Super Regenerate
export async function superRegenerate(msgId) {
  if (!S.activeConvId || !canStartGeneration()) return;
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "super_regenerate"), agentPayload(), msgId);
}

// ── Magic Rewrite
export function toggleMagicInput(msgId) {
  S.magicInputMsgId = S.magicInputMsgId === msgId ? null : msgId;
  renderMessages();
  if (S.magicInputMsgId !== msgId) return;

  requestAnimationFrame(() => {
    const el = document.getElementById(`magic-input-${msgId}`);
    if (el) el.focus();
  });

  const onMouseDown = (e) => {
    const el = document.getElementById(`magic-input-${msgId}`);
    if (el?.contains(e.target)) return;
    // If the magic button itself was clicked, its onclick will handle the toggle.
    if (e.target.closest(`[onclick="toggleMagicInput(${msgId})"]`)) {
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
  await runStreamRequest(convUrl(S.activeConvId, "messages", msgId, "magic_rewrite"), { direction }, msgId);
}
