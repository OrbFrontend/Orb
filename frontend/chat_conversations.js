import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { onConvSwitch, stopAll as stopAllAudio } from "./audio_player.js";
import { renderMessages, resetRenderWindow, setMessages } from "./chat_core.js";
import { clearInspectedMessage, renderInspector } from "./chat_inspector.js";
import { inspectMessage } from "./chat_messages.js";
import { cancelStreamingPaint, restoreStreamingView, syncSendButton, syncStreamControls } from "./chat_stream.js";
import { resetWorkflowViewportState } from "./chat_workflow.js";
import { sseError } from "./errors.js";
import { groupFamily, groupRootId } from "./group_cast.js";
import { loadGroupCast, renderGroupCast, renderGroupList } from "./group_setup.js";
import { CHEVRON_LEFT_ICON, CHEVRON_RIGHT_ICON } from "./icons.js";
import { renderInteractiveFragments, renderMoodFragments } from "./library_fragments.js";
import { chatHeaderAvatarHtml, refreshCharacters, renderCharacters } from "./library_sidebar.js";
import { reflectConversationWorldActivation, renderWorldsSidebar } from "./lorebooks.js";
import { closeModal, setModalDismiss, showConfirmModal, showModal } from "./modal.js";
import { begin, beginStream, finish, ownsView, runningFor, settle, streamEvents } from "./operations.js";
import { updateUserBtn } from "./settings_personas.js";
import { unescapeSSE } from "./sse.js";
import {
  charactersView,
  conversationState,
  notify,
  releaseConversationState,
  S,
  turnActive,
  upgradeLegacyFragment,
} from "./state.js";
import { refreshState } from "./state_panel.js";
import {
  $,
  CHAT_AVATAR_ICON,
  convUrl,
  esc,
  formatRelativeDate,
  plural,
  scrollToBottom,
  setChatFollowing,
  toast,
} from "./utils.js";
import { validate } from "./validate.js";
import { clearTextEffect } from "./workflow_text_effects.js";

export async function loadConversations() {
  S.conversations = await api.get("/conversations");
  notify("conversations");
  renderGroupList();
}

document.addEventListener("group-created", async (event) => {
  await loadConversations();
  await selectConversation(event.detail);
});
document.addEventListener("group-selected", (event) => selectConversation(event.detail));
document.addEventListener("group-delete-request", (event) => _deleteGroupFamily(event.detail));
document.addEventListener("group-cast-updated", () => refreshSceneCardFragments());
document.addEventListener("chat-view-resumed", () => {
  restoreStreamingView();
  syncStreamControls();
  renderMessages();
  renderInspector();
});

export function stashSceneCards(cards) {
  const list = (Array.isArray(cards) ? cards : [cards]).filter(Boolean);
  const merge = (pick, globals) => {
    const claimed = new Set(globals.map((g) => g.id));
    const out = [];
    for (const card of list) {
      const frags = pick(card?.extensions?.orb?.fragments);
      for (const f of Array.isArray(frags) ? frags : []) {
        if (!f?.id || f.enabled === false || claimed.has(f.id)) continue;
        claimed.add(f.id);
        out.push(upgradeLegacyFragment(f));
      }
    }
    return out;
  };
  S.cardMoodFragments = merge((frags) => frags?.mood, S.moodFragments);
  S.cardInteractiveFragments = merge((frags) => frags?.interactive, S.interactiveFragments);
  S.sceneIntro = sceneIntroFrom(list);
  renderMoodFragments();
  renderInteractiveFragments();
  // The scene's cards carry state fragments too: re-read the State tab on a
  // conversation switch, a cast edit, and a card save alike.
  refreshState();
}

// Refresh solo-card framing with the card. Groups use their scene settings instead.
function sceneIntroFrom(cards) {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  if (!conv || conv.kind === "group") return null;
  const card = cards.find((c) => c.id === conv.character_card_id);
  if (!card) return null;
  return { convId: conv.id, scenario: card.scenario || "", creatorNotes: card.creator_notes || "" };
}

function sceneCardIds(conv) {
  if (conv?.kind === "group") {
    return [...new Set((S.groupCast?.members || []).map((member) => member.character_card_id).filter(Boolean))];
  }
  return conv?.character_card_id ? [conv.character_card_id] : [];
}

export async function refreshSceneCardFragments() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  const token = S.conversationViewToken;
  const cards = await Promise.all(
    sceneCardIds(conv).map((cardId) => api.get(`/characters/${cardId}`).catch(() => null)),
  );
  if (ownsView(token, conv?.id)) stashSceneCards(cards);
}

export function resetChatUI() {
  stopAllAudio();
  S.activeCharId = null;
  S.activeConvId = null;
  notify("conversations");
  S.groupCast = null;
  S.pinnedSpeakerId = null;
  S.consumedSpeakerId = null;
  stashSceneCards(null);
  S.messages = [];
  S.lastDirectorData = null;
  S.lastFeedback = null;
  S.lastState = null;
  S.lastDecisions = null;
  S.directorState = null;
  S.inspectedMsgId = null;
  S.inspectedDirectorData = null;
  $("chat-title-text").textContent = "Select a character";
  $("chat-avatar").textContent = CHAT_AVATAR_ICON;
  $("chat-avatar").style.cursor = "";
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
  renderGroupCast();
  renderMessages();
  renderInspector();
  renderWorldsSidebar();
  updateUserBtn(); // no active character -> drop any locked-to-character icon
}

export async function selectChar(id, source = "recent") {
  if (S.activeCharId === id || S._selectCharLock) return;
  S._selectCharLock = true;
  try {
    S.activeCharId = id;
    refreshCharacters();
    const existing = S.conversations.find((c) => c.character_card_id === id);
    if (existing) {
      if (source === "library") {
        try {
          await api.post(`/conversations/${existing.id}/touch`);
          existing.last_accessed_at = new Date().toISOString();
        } catch (e) {
          console.warn("Failed to touch conversation:", e);
        }
      }
      await selectConversation(existing.id);
    } else {
      try {
        const conv = await api.post("/conversations", { character_card_id: id });
        await loadConversations();
        await selectConversation(conv.id);
      } catch (e) {
        toast(e.message, true);
      }
    }
    refreshCharacters();
  } finally {
    S._selectCharLock = false;
  }
}

async function newConvForChar(id) {
  try {
    const conv = await api.post("/conversations", { character_card_id: id });
    await loadConversations();
    S.activeCharId = id;
    renderCharacters();
    await selectConversation(conv.id);
  } catch (e) {
    toast(e.message, true);
  }
}

async function newConversationHere() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  if (conv?.kind !== "group") {
    if (!S.activeCharId) {
      toast("Select a character first", true);
      return;
    }
    await newConvForChar(S.activeCharId);
    return;
  }
  try {
    const fresh = await api.post(convUrl(conv.id, "group-conversation"));
    await loadConversations();
    await selectConversation(fresh.id);
  } catch (e) {
    toast(e.message, true);
  }
}

export async function selectConversation(id) {
  cancelStreamingPaint();
  const previousId = S.activeConvId;
  if (previousId) {
    const draft = $("chat-input").value;
    conversationState(previousId).draft = draft;
    localStorage.setItem(`orb-chat-draft:${previousId}`, draft);
  }
  const token = ++S.conversationViewToken;
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
  S.activeConvId = id;
  notify("conversations");
  if (previousId !== id) releaseConversationState(previousId);
  S.conversationLoading = true;
  try {
    if (!turnActive()) {
      S.lastDirectorData = null;
      S.lastFeedback = null;
      S.lastState = null;
      S.lastDecisions = null;
      S.reasoningDirector = "";
      S.reasoningWriter = "";
      S.reasoningEditor = "";
      S.reasoningByPass = {};
      S.reasoningPassActive = 0;
      S.reasoningPassSelected = 0;
    }
    $("chat-input").value = conversationState(id).draft || localStorage.getItem(`orb-chat-draft:${id}`) || "";
    syncStreamControls();
    const conv = S.conversations.find((c) => c.id === id);
    await loadGroupCast(conv);
    if (!ownsView(token, id)) return;
    const prevCharId = S.activeCharId;
    if (conv?.kind === "group") S.activeCharId = null;
    else if (conv?.character_card_id) S.activeCharId = conv.character_card_id;
    if (S.activeCharId !== prevCharId) refreshCharacters();
    renderGroupList();
    updateUserBtn();
    $("chat-title-text").textContent = conv ? conv.title || conv.character_name : "";
    const av = $("chat-avatar");
    av.style.cursor = conv?.kind === "group" ? "pointer" : "";
    if (conv?.kind === "group") {
      av.textContent = "👥";
    } else if (conv?.character_card_id) {
      // The library's bust token, not a fresh timestamp: an avatar edit bumps it, and otherwise the header reuses the
      // cached image instead of downloading it again on every switch.
      av.innerHTML = chatHeaderAvatarHtml(conv.character_card_id);
    } else {
      av.textContent = CHAT_AVATAR_ICON;
    }
    const expressive = (cardId) => Boolean(charactersView().find((c) => c.id === cardId)?.has_expressions);
    const hasExpr = conv?.kind === "group" ? sceneCardIds(conv).some(expressive) : expressive(conv?.character_card_id);
    av.classList.toggle("avatar-halo", hasExpr);

    // Navigation reads scene choices without changing any World defaults.
    const cardIds = sceneCardIds(conv);
    const [activation, msgs, directorState, ...cards] = await Promise.all([
      conv ? api.get(convUrl(id, "worlds")) : null,
      api.get(convUrl(id, "messages")),
      api.get(convUrl(id, "director")),
      ...cardIds.map((cardId) => api.get(`/characters/${cardId}`).catch(() => null)),
    ]);
    if (!ownsView(token, id)) return;
    S.conversationLoading = false;
    $("chat-input").disabled = false;
    syncSendButton();
    if (activation) reflectConversationWorldActivation(activation.world_ids);
    setMessages(msgs);
    S.directorState = directorState;
    stashSceneCards(cards);
    resetRenderWindow();
    S.editingMsgId = null;
    S.magicInputMsgId = null;
    resetWorkflowViewportState();
    clearTextEffect();
    onConvSwitch();
    setChatFollowing(true);
    restoreStreamingView();
    renderMessages(true);
    scrollToBottom();
    if (conv?.kind === "group" && !S.messages.length) $("chat-input").focus();
    const lastAsst = [...S.messages].reverse().find((m) => m.role === "assistant" && m.id);
    if (turnActive()) {
      renderInspector();
    } else if (lastAsst) {
      inspectMessage(lastAsst.id);
    } else {
      clearInspectedMessage();
    }
  } catch (error) {
    if (ownsView(token, id)) {
      S.conversationLoading = true;
      $("chat-input").disabled = true;
      $("send-btn").disabled = true;
      toast(`Conversation failed to load: ${error.message}`, true);
    }
  }
}

/** Let go of chat *id*, deleted on the server: if it is the one open, the chat view empties. */
function closeDeletedConversation(id) {
  if (S.activeConvId !== id) return;
  const wasGroup = S.conversations.find((item) => item.id === id)?.kind === "group";
  S.activeConvId = null;
  notify("conversations");
  S.messages = [];
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
  if (wasGroup) {
    S.groupCast = null;
    S.pinnedSpeakerId = null;
    S.consumedSpeakerId = null;
    $("chat-title-text").textContent = "Select a character";
    $("chat-avatar").textContent = CHAT_AVATAR_ICON;
    renderGroupCast();
  }
  renderMessages();
}

/** A chat deleted elsewhere, by the assistant's Apply: closed if open, then gone from the chat list. */
export async function forgetConversation(id) {
  closeDeletedConversation(id);
  await loadConversations();
}

function confirmDeleteConversation(id, msgCount, afterDelete) {
  const what = msgCount ? `this conversation and its ${plural(msgCount, "message")}` : "this conversation";
  showConfirmModal(
    {
      title: "Delete conversation",
      message: `Delete ${what}? This cannot be undone. ${runningFor("conversationId", id)
        .map((op) => `${op.kind} will be stopped and settled first.`)
        .join(" ")}`,
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del(`/conversations/${id}`);
        closeDeletedConversation(id);
        await afterDelete();
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

async function _deleteGroupFamily(rootId) {
  const family = groupFamily(S.conversations, rootId);
  if (!family.length) return;
  const root = family.find((conv) => conv.id === rootId) || family[0];
  const messages = family.reduce((total, conv) => total + (conv.message_count ?? 0), 0);
  const scale =
    family.length > 1
      ? `${plural(family.length, "conversation")} (${plural(messages, "message")})`
      : plural(messages, "message");
  showConfirmModal(
    {
      title: "Delete group",
      message: `Delete "${esc(root.title)}" and its ${scale}? This cannot be undone. ${family
        .flatMap((conv) => runningFor("conversationId", conv.id))
        .map((op) => `${op.kind} will be stopped and settled first.`)
        .join(" ")}`,
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del(convUrl(root.id, "group"));
        if (family.some((conv) => conv.id === S.activeConvId)) resetChatUI();
        await loadConversations();
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

async function deleteConversationFromModal(id, rootId = "") {
  const conv = S.conversations.find((c) => c.id === id);
  confirmDeleteConversation(id, conv?.message_count ?? null, () =>
    showConvHistoryModal(rootId ? { groupRootId: rootId } : null),
  );
}

function convHistoryScope() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  if (conv?.kind === "group") return { groupRootId: groupRootId(conv) };
  return S.activeCharId ? { charId: S.activeCharId } : null;
}

async function showConvHistoryModal(scope = null) {
  const target = scope || convHistoryScope();
  if (!target) {
    toast("Select a character first", true);
    return;
  }
  await loadConversations();
  const convs = target.groupRootId
    ? groupFamily(S.conversations, target.groupRootId)
    : S.conversations.filter((c) => c.character_card_id === target.charId);
  if (!convs.length) {
    toast("No conversations yet", true);
    return;
  }
  const scopeName = target.groupRootId
    ? convs.find((c) => c.id === target.groupRootId)?.title || convs[0].title || "Group"
    : charactersView().find((c) => c.id === target.charId)?.name || "Character";
  const rootAttr = target.groupRootId || "";
  const items = convs
    .map((c) => {
      const isActive = c.id === S.activeConvId;
      const preview = esc((c.last_message_preview || "").substring(0, 80));
      const title = esc(c.title || c.character_name || "Untitled");
      const ts = c.updated_at || c.created_at;
      const count = c.message_count ?? 0;
      const pinnedPersona = c.persona_lock_id
        ? (S.personas || []).find((p) => p.id === c.persona_lock_id)?.name || null
        : null;
      const meta = [`${count} message${count !== 1 ? "s" : ""}`];
      if (pinnedPersona) meta.push(`💬 ${esc(pinnedPersona)}`);
      return `<div class="conv-history-item${isActive ? " active-conv" : ""}" data-wf-action="conversations:open" data-conv-id="${c.id}">
      <div class="conv-history-meta">
        <span class="conv-history-title">${title}</span>
        <span class="conv-history-date">${formatRelativeDate(ts)}</span>
        <button class="conv-history-delete" title="Delete conversation" data-wf-action="conversations:delete" data-conv-id="${c.id}" data-root-id="${rootAttr}">&#x2715;</button>
      </div>
      ${
        preview
          ? `<div class="conv-history-preview">${preview}</div>`
          : `<div class="conv-history-preview" style="color:var(--text-muted);font-style:italic">No messages yet</div>`
      }
      <div class="conv-history-info">${meta.join('<span class="conv-history-info-sep">·</span>')}</div>
    </div>`;
    })
    .join("");
  showModal(`
    <h2>Conversations — ${esc(scopeName)}</h2>
    <div class="modal-list">${items}</div>`);
}

async function createCheckpoint() {
  if (!S.activeConvId) {
    toast("No active conversation", true);
    return;
  }
  if (turnActive()) {
    toast("Stop generation before creating a checkpoint", true);
    return;
  }
  const cid = S.activeConvId;
  const token = S.conversationViewToken;
  try {
    const conv = await api.post(`/conversations/${cid}/checkpoint`, {});
    await loadConversations();
    toast(`Checkpoint created: ${conv.title}`);
    if (ownsView(token, cid)) await showConvHistoryModal();
  } catch (e) {
    toast(`Failed to create checkpoint: ${e.message}`, true);
  }
}

// The compress dialog's takes. Each Generate, Regenerate or Revise adds one, the way a reply gains swipes, and a hand edit
// stays on its take. They outlive the dialog: reopening it on the same history brings them back.
let _compressKeepCount = 4;
let _compress = null; // { cid, history, total, instructions, takes: [{ text, keep, instructions }], index }
let _compressOperation = null;
let _compressDialogToken = 0;

// Changes whenever the active path or any message on it does, so a take is only offered back for the history it summarized.
function compressionHistory() {
  return (S.messages || []).map((m) => `${m.id}:${(m.content || "").length}`).join(",");
}

function showCompressModal() {
  if (!S.activeConvId) {
    toast("No active conversation", true);
    return;
  }
  if ((S.messages || []).length < 4) {
    toast("Not enough messages to compress", true);
    return;
  }
  if (turnActive()) {
    toast("Wait for the reply before compressing", true);
    return;
  }
  if (runningFor("conversationId", S.activeConvId).some((op) => op.kind.startsWith("compression"))) {
    toast("Wait for the previous summary to settle", true);
    return;
  }
  const history = compressionHistory();
  if (_compress?.cid !== S.activeConvId || _compress.history !== history) {
    _compress = {
      cid: S.activeConvId,
      history,
      total: S.messages.length,
      instructions: "",
      takes: [],
      index: 0,
    };
  }
  _compressDialogToken++;
  _compressOperation = null;
  const c = _compress;
  const validOptions = [2, 4, 6, 8].filter((n) => n < c.total);
  const preferred = c.takes[c.index]?.keep ?? _compressKeepCount;
  const keep = validOptions.includes(preferred) ? preferred : validOptions[validOptions.length - 1];
  showModal(`
    <h2>Compress History</h2>
    <p class="modal-subtitle">Summarize the story so far into a new conversation, carrying over the most recent messages.</p>
    <div class="field">
      <label for="compress-instructions">Instructions (optional)</label>
      <textarea id="compress-instructions" class="modal-textarea" rows="2" spellcheck="false" placeholder="e.g. Past tense, omit small talk...">${esc(c.instructions)}</textarea>
    </div>
    <div class="field">
      <label for="compress-keep-select">Keep last</label>
      <select id="compress-keep-select" data-wf-action="chat-compression:keep" data-wf-on="change">
        ${validOptions.map((n) => `<option value="${n}"${keep === n ? " selected" : ""}>${n} messages</option>`).join("")}
      </select>
      <p class="modal-hint" id="compress-split"></p>
    </div>
    <div class="compress-draft" id="compress-draft" hidden>
      <div class="compress-draft-head">
        <span class="swipe-nav">
          <button id="compress-prev" data-wf-action="chat-compression:prev" title="Previous take" aria-label="Previous take">${CHEVRON_LEFT_ICON}</button>
          <span class="swipe-counter" id="compress-counter"></span>
          <button id="compress-next" data-wf-action="chat-compression:next" title="Next take" aria-label="Next take">${CHEVRON_RIGHT_ICON}</button>
        </span>
        <span class="compress-draft-meta" id="compress-meta"></span>
      </div>
      <textarea id="compress-textarea" class="modal-textarea-lg" spellcheck="false" data-wf-action="chat-compression:edit" data-wf-on="input"></textarea>
      <div class="compress-revise">
        <input id="compress-revise" type="text" spellcheck="false" placeholder="Revise, e.g. shorter, keep the ring scene..." data-wf-action="chat-compression:reviseKey" data-wf-on="keydown">
        <button class="btn" id="compress-revise-btn" data-wf-action="chat-compression:revise">Revise</button>
      </div>
    </div>
    <div class="modal-actions compress-actions">
      <span id="compress-status" class="modal-action-status" role="status" aria-live="polite"></span>
      <button class="btn" data-wf-action="chat-compression:cancel">Cancel</button>
      <button class="btn" id="compress-gen-btn" data-wf-action="chat-compression:generate">Generate</button>
      <button class="btn" id="compress-apply-btn" data-wf-action="chat-compression:apply">Create New Conversation</button>
    </div>`);
  setModalDismiss(cancelCompression);
  paintCompressionTake();
}

function selectedKeep() {
  const el = document.getElementById("compress-keep-select");
  return el ? parseInt(el.value, 10) : _compressKeepCount;
}

function currentTake() {
  return _compress?.takes[_compress.index] || null;
}

/** Show the current take in the editor, then the controls around it. */
function paintCompressionTake() {
  const take = currentTake();
  const draft = document.getElementById("compress-draft");
  if (draft) draft.hidden = !take;
  const textarea = document.getElementById("compress-textarea");
  if (textarea && take) textarea.value = take.text;
  syncCompressionControls();
}

function syncCompressionControls() {
  const c = _compress;
  if (!c || !document.getElementById("compress-gen-btn")) return;
  const op = _compressOperation;
  const streaming = op?.kind === "compression";
  const take = currentTake();
  const keep = selectedKeep();
  // A take summarizes exactly the history before its own kept tail, so it can only be applied with that tail.
  const fits = !!take && take.keep === keep;

  $("compress-split").textContent = `${c.total - keep} summarized · ${keep} kept`;

  const gen = $("compress-gen-btn");
  gen.textContent = streaming ? "Stop" : take ? "Regenerate" : "Generate";
  gen.disabled = !!op && !streaming;
  gen.classList.toggle("btn-accent", !streaming && !fits);

  const apply = $("compress-apply-btn");
  apply.hidden = !take;
  apply.disabled = !!op || !fits || !take.text.trim();
  apply.classList.toggle("btn-accent", fits);

  if (!take) return;
  $("compress-counter").textContent = `${c.index + 1}/${c.takes.length}`;
  $("compress-prev").disabled = !!op || c.index === 0;
  $("compress-next").disabled = !!op || c.index === c.takes.length - 1;
  const words = take.text.trim() ? take.text.trim().split(/\s+/).length : 0;
  const meta = [plural(words, "word"), `~${Math.round(take.text.length / 4)} tokens`];
  if (take.keep !== keep) meta.push(`keeps ${take.keep}`);
  $("compress-meta").textContent = meta.join(" · ");
  $("compress-textarea").readOnly = streaming;
  $("compress-revise").disabled = !!op;
  $("compress-revise-btn").disabled = !!op;
}

function setCompressionStatus(text, isError = false) {
  const el = document.getElementById("compress-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("is-error", isError);
}

function stepCompressionTake(delta) {
  const c = _compress;
  if (!c || _compressOperation) return;
  const next = c.index + delta;
  if (next < 0 || next >= c.takes.length) return;
  c.index = next;
  // Every take's keep is one of this history's options, so the select can always follow it.
  const select = document.getElementById("compress-keep-select");
  if (select) select.value = String(c.takes[next].keep);
  setCompressionStatus("");
  paintCompressionTake();
}

function editCompressionTake(el) {
  const take = currentTake();
  if (!take || _compressOperation) return;
  take.text = el.value;
  syncCompressionControls();
}

function saveCompressionInstructions() {
  const el = document.getElementById("compress-instructions");
  if (_compress && el) _compress.instructions = el.value;
}

function cancelCompression() {
  saveCompressionInstructions();
  _compressDialogToken++;
  _compressOperation?.stop?.();
  closeModal();
}

function generateOrStop() {
  if (_compressOperation?.kind === "compression") {
    _compressOperation.stop();
    return;
  }
  const instructions = (document.getElementById("compress-instructions")?.value || "").trim();
  saveCompressionInstructions();
  _compressKeepCount = selectedKeep();
  streamCompressionTake({ keep: _compressKeepCount, instructions });
}

function reviseCompressionTake() {
  const take = currentTake();
  const input = document.getElementById("compress-revise");
  if (!take || !input || _compressOperation) return;
  const request = input.value.trim();
  if (!request || !take.text.trim()) {
    input.focus();
    return;
  }
  // The revision replays the take's own request, so it reads the same history and shares its prompt prefix.
  streamCompressionTake({ keep: take.keep, instructions: take.instructions, draft: take.text, request });
}

async function streamCompressionTake({ keep, instructions, draft = null, request = null }) {
  const c = _compress;
  if (!c || _compressOperation) return;
  const cid = c.cid;
  const dialog = _compressDialogToken;
  const record = beginStream("compression", { conversationId: cid }, `/conversations/${cid}/stop`);
  _compressOperation = record;

  const take = { text: "", keep, instructions };
  const from = c.index;
  c.takes.push(take);
  c.index = c.takes.length - 1;
  setCompressionStatus(draft ? "Revising…" : "Summarizing…");
  paintCompressionTake();

  const textarea = document.getElementById("compress-textarea");
  const body = { keep_count: keep, custom_instructions: instructions || null };
  if (draft) Object.assign(body, { draft, revision: request });
  let failed = false;
  try {
    for await (const { event, data } of streamEvents(record, `/conversations/${cid}/summarize`, body, "Summary")) {
      if (event === "token") {
        take.text += unescapeSSE(data);
        if (textarea?.isConnected) {
          textarea.value = take.text;
          textarea.scrollTop = textarea.scrollHeight;
          syncCompressionControls();
        }
      } else if (event === "error") {
        throw sseError(data);
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      failed = true;
      if (dialog === _compressDialogToken) setCompressionStatus(e.message, true);
      else toast(`Summary failed: ${e.message}`, true);
    }
  } finally {
    // A Stop from a closed dialog still settles before this chat can summarize again.
    await settle(record);
    if (_compressOperation === record) _compressOperation = null;
    // A stopped take keeps what it wrote; one that wrote nothing is not a take, and the view returns to the take it came from.
    // Paging is locked while streaming, so this take is still the last one.
    if (!take.text.trim()) {
      c.takes.pop();
      c.index = from;
    }
    if (dialog === _compressDialogToken) {
      if (!failed) setCompressionStatus("");
      if (draft && !failed) {
        const input = document.getElementById("compress-revise");
        if (input) input.value = "";
      }
      paintCompressionTake();
    }
  }
}

async function applyCompression() {
  const c = _compress;
  const take = currentTake();
  if (_compressOperation || !c || !take || take.keep !== selectedKeep()) return;
  const summary = take.text.trim();
  if (!summary) {
    toast("Summary is empty", true);
    return;
  }
  const cid = c.cid;
  const dialogToken = _compressDialogToken;
  const viewToken = S.conversationViewToken;
  const record = begin("compression-apply", { conversationId: cid });
  _compressOperation = record;
  setCompressionStatus("");
  syncCompressionControls();

  try {
    const result = await api.post(`/conversations/${cid}/compress`, { summary, keep_count: take.keep });
    if (_compress === c) _compress = null;
    if (dialogToken === _compressDialogToken) closeModal();
    await loadConversations();
    if (dialogToken !== _compressDialogToken || !ownsView(viewToken, cid)) return;
    await selectConversation(result.new_conversation_id);
    const title = S.conversations.find((x) => x.id === result.new_conversation_id)?.title;
    toast(title ? `Continued in ${title}` : "New conversation created from compression");
  } catch (e) {
    if (dialogToken === _compressDialogToken) setCompressionStatus(e.message, true);
    else toast(`Failed to apply compression: ${e.message}`, true);
  } finally {
    finish(record);
    if (_compressOperation === record) _compressOperation = null;
    if (dialogToken === _compressDialogToken) syncCompressionControls();
  }
}

let _titleEditBackup = "";

function startEditTitle() {
  if (!S.activeConvId) return;
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  if (!conv) return;
  const area = $("chat-title-text");
  if (!area) return;
  _titleEditBackup = area.textContent;

  const input = document.createElement("input");
  input.type = "text";
  input.id = "chat-title-input";
  input.className = "chat-title-input";
  input.value = _titleEditBackup;
  input.addEventListener("keydown", handleTitleEditKey);
  input.addEventListener("blur", saveTitleEdit);

  area.replaceWith(input);
  input.focus();
  input.select();
}

function handleTitleEditKey(e) {
  if (e.key === "Enter") {
    e.preventDefault();
    saveTitleEdit();
  }
  if (e.key === "Escape") {
    e.preventDefault();
    cancelTitleEdit();
  }
}

async function saveTitleEdit() {
  const inp = $("chat-title-input");
  if (!inp) return;
  const newTitle = inp.value.trim();
  if (!newTitle) {
    cancelTitleEdit();
    return;
  }
  const validation = validate.validateConversationTitle(newTitle);
  if (!validation.valid) {
    toast(validation.error, true);
    cancelTitleEdit();
    return;
  }
  if (newTitle === _titleEditBackup) {
    cancelTitleEdit();
    return;
  }
  try {
    const updated = await api.put(`/conversations/${S.activeConvId}`, { title: newTitle });
    const conv = S.conversations.find((c) => c.id === S.activeConvId);
    if (conv) conv.title = updated.title;
    const div = document.createElement("div");
    div.className = "chat-title";
    div.id = "chat-title-text";
    div.textContent = updated.title || conv?.character_name || "";
    inp.replaceWith(div);
    _titleEditBackup = "";
    toast("Title updated");
  } catch (e) {
    toast(e.message, true);
    cancelTitleEdit();
  }
}

function cancelTitleEdit() {
  const inp = $("chat-title-input");
  if (!inp) return;
  const div = document.createElement("div");
  div.className = "chat-title";
  div.id = "chat-title-text";
  div.textContent = _titleEditBackup;
  inp.replaceWith(div);
  _titleEditBackup = "";
}

registerActions("conversations", {
  open: (el) => {
    closeModal();
    selectConversation(el.dataset.convId);
  },
  delete: (el) => deleteConversationFromModal(el.dataset.convId, el.dataset.rootId),
  selectChar: (el) => selectChar(el.dataset.charId, el.dataset.source),
  newHere: () => newConversationHere(),
  history: () => showConvHistoryModal(),
  compress: () => showCompressModal(),
  checkpoint: () => createCheckpoint(),
  editTitle: () => startEditTitle(),
});

registerActions("chat-compression", {
  generate: () => generateOrStop(),
  cancel: () => cancelCompression(),
  apply: () => applyCompression(),
  keep: () => syncCompressionControls(),
  prev: () => stepCompressionTake(-1),
  next: () => stepCompressionTake(1),
  edit: (el) => editCompressionTake(el),
  revise: () => reviseCompressionTake(),
  reviseKey: (_el, e) => {
    if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      reviseCompressionTake();
    }
  },
});
