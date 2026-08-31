import { api } from "./api.js";
import { onConvSwitch, stopAll as stopAllAudio } from "./audio_player.js";
import { renderMessages, resetRenderWindow, setMessages } from "./chat_core.js";
import { renderInspector } from "./chat_inspector.js";
import { clearInspectedMessage, inspectMessage } from "./chat_messages.js";
import { stopConversation } from "./chat_stream.js";
import { resetWorkflowViewportState } from "./chat_workflow.js";
import { renderDirectionNotesPanel } from "./direction_notes_panel.js";
import { groupFamily, groupRootId } from "./group_cast.js";
import { loadGroupCast, renderGroupCast, renderGroupList } from "./group_setup.js";
import { refreshCharacters, renderCharacters } from "./library.js";
import { renderInteractiveFragments, renderMoodFragments } from "./library_fragments.js";
import { reflectConversationWorldActivation } from "./lorebooks.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { isUtilityPanelOpen } from "./panels.js";
import { updateUserBtn } from "./settings_personas.js";
import { sseEvents, streamPost, unescapeSSE } from "./sse.js";
import { S } from "./state.js";
import {
  $,
  avatarCell,
  avatarUrl,
  CHAT_AVATAR_ICON,
  convUrl,
  esc,
  formatRelativeDate,
  scrollToBottom,
  setChatFollowing,
  toast,
} from "./utils.js";
import { validate } from "./validate.js";
import { clearTextEffect } from "./workflow_text_effects.js";

export async function loadConversations() {
  S.conversations = await api.get("/conversations");
  renderGroupList();
}

document.addEventListener("group-created", async (event) => {
  await loadConversations();
  await selectConversation(event.detail);
});
document.addEventListener("group-selected", (event) => selectConversation(event.detail));
document.addEventListener("group-delete-request", (event) => _deleteGroupFamily(event.detail));
document.addEventListener("group-cast-updated", () => refreshSceneCardFragments());

export function stashCardFragments(cards) {
  const list = (Array.isArray(cards) ? cards : [cards]).filter(Boolean);
  const merge = (pick, globals) => {
    const claimed = new Set(globals.map((g) => g.id));
    const out = [];
    for (const card of list) {
      const frags = pick(card?.extensions?.orb?.fragments);
      for (const f of Array.isArray(frags) ? frags : []) {
        if (!f?.id || f.enabled === false || claimed.has(f.id)) continue;
        claimed.add(f.id);
        out.push(f);
      }
    }
    return out;
  };
  S.cardMoodFragments = merge((frags) => frags?.mood, S.moodFragments);
  S.cardInteractiveFragments = merge((frags) => frags?.interactive, S.interactiveFragments);
  renderMoodFragments();
  renderInteractiveFragments();
}

function sceneCardIds(conv) {
  if (conv?.kind === "group") {
    return [...new Set((S.groupCast?.members || []).map((member) => member.character_card_id).filter(Boolean))];
  }
  return conv?.character_card_id ? [conv.character_card_id] : [];
}

export async function refreshSceneCardFragments() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  const cards = await Promise.all(
    sceneCardIds(conv).map((cardId) => api.get(`/characters/${cardId}`).catch(() => null)),
  );
  stashCardFragments(cards);
}

export function resetChatUI() {
  stopAllAudio();
  S.activeCharId = null;
  S.activeConvId = null;
  S.groupCast = null;
  S.pinnedSpeakerId = null;
  S.consumedSpeakerId = null;
  stashCardFragments(null);
  S.messages = [];
  S.lastDirectorData = null;
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
  updateUserBtn(); // no active character → drop any locked-to-character icon
}

export async function selectChar(id, source = "recent") {
  if (S.isStreaming) {
    toast("Stop generation before switching characters", true);
    return;
  }
  if (S.activeCharId === id || S._selectCharLock) return;
  S._selectCharLock = true;
  try {
    S.activeCharId = id;
    renderCharacters();
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

export async function newConvForChar(id) {
  if (S.isStreaming) {
    toast("Stop generation before switching characters", true);
    return;
  }
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

export async function newConversationHere() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  if (conv?.kind !== "group") {
    if (!S.activeCharId) {
      toast("Select a character first", true);
      return;
    }
    await newConvForChar(S.activeCharId);
    return;
  }
  if (S.isStreaming) {
    toast("Stop generation before starting a new conversation", true);
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
  if (S.isStreaming) {
    toast("Stop generation before switching conversations", true);
    return;
  }
  S.activeConvId = id;
  S.lastDirectorData = null;
  S.reasoningDirector = "";
  S.reasoningWriter = "";
  S.reasoningEditor = "";
  S.reasoningByPass = {};
  S.reasoningPassActive = 0;
  S.reasoningPassSelected = 0;
  const conv = S.conversations.find((c) => c.id === id);
  await loadGroupCast(conv);
  const prevCharId = S.activeCharId;
  if (conv?.kind === "group") S.activeCharId = null;
  else if (conv?.character_card_id) S.activeCharId = conv.character_card_id;
  if (S.activeCharId !== prevCharId) renderCharacters();
  renderGroupList();
  updateUserBtn();
  $("chat-title-text").textContent = conv ? conv.title || conv.character_name : "";
  const av = $("chat-avatar");
  av.style.cursor = conv?.kind === "group" ? "pointer" : "";
  if (conv?.kind === "group") {
    av.textContent = "👥";
  } else if (conv?.character_card_id) {
    av.innerHTML = avatarCell(`${avatarUrl(conv.character_card_id)}?t=${Date.now()}`, {
      icon: CHAT_AVATAR_ICON,
      attrs: 'onclick="showAvatarPopup()" style="cursor:pointer"',
    });
  } else {
    av.textContent = CHAT_AVATAR_ICON;
  }
  const expressive = (cardId) => Boolean((S.characters || []).find((c) => c.id === cardId)?.has_expressions);
  const hasExpr = conv?.kind === "group" ? sceneCardIds(conv).some(expressive) : expressive(conv?.character_card_id);
  av.classList.toggle("avatar-halo", hasExpr);
  $("chat-input").disabled = false;
  $("send-btn").disabled = false;

  if (conv) {
    const activation = await api.post(convUrl(id, "activate"));
    reflectConversationWorldActivation(activation.world_ids);
  }

  const cardIds = sceneCardIds(conv);
  const [msgs, directorState, ...cards] = await Promise.all([
    api.get(convUrl(id, "messages")),
    api.get(convUrl(id, "director")),
    ...cardIds.map((cardId) => api.get(`/characters/${cardId}`).catch(() => null)),
  ]);
  setMessages(msgs);
  S.directorState = directorState;
  stashCardFragments(cards);
  resetRenderWindow();
  S.editingMsgId = null;
  S.magicInputMsgId = null;
  resetWorkflowViewportState();
  clearTextEffect();
  onConvSwitch();
  setChatFollowing(true);
  renderMessages(true);
  scrollToBottom();
  if (conv?.kind === "group" && !S.messages.length) $("chat-input").focus();
  const lastAsst = [...S.messages].reverse().find((m) => m.role === "assistant" && m.id);
  if (lastAsst) {
    inspectMessage(lastAsst.id);
  } else {
    clearInspectedMessage();
  }
  if (isUtilityPanelOpen("direction-notes-panel")) renderDirectionNotesPanel();
}

function confirmDeleteConversation(id, msgCount, afterDelete) {
  const countNote =
    msgCount != null
      ? `<p style="color:var(--text-muted);font-size:0.88em;margin-top:8px">${msgCount} message${msgCount !== 1 ? "s" : ""} in this conversation</p>`
      : "";
  showConfirmModal(
    {
      title: "Delete Conversation",
      message: "Are you sure you want to delete this conversation?",
      confirmText: "Delete",
      extraHtml: countNote,
    },
    async () => {
      try {
        await api.del(`/conversations/${id}`);
        if (S.activeConvId === id) {
          const wasGroup = S.conversations.find((item) => item.id === id)?.kind === "group";
          S.activeConvId = null;
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
      ? `${family.length} conversations · ${messages} message${messages !== 1 ? "s" : ""}`
      : `${messages} message${messages !== 1 ? "s" : ""} in this conversation`;
  showConfirmModal(
    {
      title: "Delete Group",
      message: `Delete "${root.title}" and everything in it?`,
      confirmText: "Delete",
      extraHtml: `<p style="color:var(--text-muted);font-size:0.88em;margin-top:8px">${esc(scale)}</p>`,
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

export async function deleteConversationFromModal(id, rootId = "") {
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

export async function showConvHistoryModal(scope = null) {
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
    : S.characters.find((c) => c.id === target.charId)?.name || "Character";
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
      return `<div class="conv-history-item${isActive ? " active-conv" : ""}" onclick="closeModal();selectConversation('${c.id}')">
      <div class="conv-history-meta">
        <span class="conv-history-title">${title}</span>
        <span class="conv-history-date">${formatRelativeDate(ts)}</span>
        <button class="conv-history-delete" title="Delete conversation" onclick="event.stopPropagation();deleteConversationFromModal('${c.id}','${rootAttr}')">&#x2715;</button>
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
    <div class="modal-list">${items}</div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`);
}

export async function createCheckpoint() {
  if (!S.activeConvId) {
    toast("No active conversation", true);
    return;
  }
  if (S.isStreaming) {
    toast("Stop generation before creating a checkpoint", true);
    return;
  }
  try {
    const conv = await api.post(`/conversations/${S.activeConvId}/checkpoint`, {});
    await loadConversations();
    toast(`Checkpoint created: ${conv.title}`);
    await showConvHistoryModal();
  } catch (e) {
    toast(`Failed to create checkpoint: ${e.message}`, true);
  }
}

let _compressKeepCount = 4;
let _compressAbort = null;

export function showCompressModal() {
  if (!S.activeConvId) {
    toast("No active conversation", true);
    return;
  }
  if ((S.messages || []).length < 4) {
    toast("Not enough messages to compress", true);
    return;
  }
  const totalMsgs = (S.messages || []).length;
  const validOptions = [2, 4, 6, 8].filter((n) => n < totalMsgs);
  const defaultKeep = validOptions.includes(_compressKeepCount)
    ? _compressKeepCount
    : validOptions[validOptions.length - 1];
  showModal(`
    <h2>Compress History</h2>
    <p class="modal-subtitle">Summarize the story so far into a new conversation, carrying over the most recent messages.</p>
    <div style="margin-bottom:14px">
      <label style="display:block;font-size:0.9em;margin-bottom:6px;color:var(--text-muted)">Additional instructions (optional)</label>
      <textarea id="compress-instructions" class="modal-textarea" rows="3" spellcheck="false" placeholder="e.g. Past tense, omit small talk..." style="resize:vertical"></textarea>
    </div>
    <div style="margin-bottom:20px">
      <label style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:0.95em">
        Keep last
        <select id="compress-keep-select" style="padding:4px 8px;border-radius:4px;border:1px solid var(--border)">
          ${validOptions.map((n) => `<option value="${n}"${defaultKeep === n ? " selected" : ""}>${n} messages</option>`).join("")}
        </select>
      </label>
      <p style="color:var(--text-muted);font-size:0.88em;margin-top:8px">${totalMsgs} messages in this conversation</p>
    </div>
    <p id="compress-status" class="modal-subtitle" style="display:none"></p>
    <textarea id="compress-textarea" class="modal-textarea-lg" spellcheck="false" placeholder="Summary will appear here..." style="display:none"></textarea>
    <div class="modal-actions">
      <button class="btn" onclick="cancelCompression()">Cancel</button>
      <button class="btn" id="compress-regen-btn" onclick="generateCompressionSummary()" style="display:none" disabled>Regenerate</button>
      <button class="btn btn-accent" id="compress-apply-btn" onclick="applyCompression()" style="display:none" disabled>Create New Conversation</button>
      <button class="btn btn-accent" id="compress-gen-btn" onclick="generateCompressionSummary()">Generate</button>
    </div>`);
}

export function cancelCompression() {
  if (_compressAbort) {
    _compressAbort.abort();
    _compressAbort = null;
  }
  if (S.activeConvId) stopConversation(S.activeConvId);
  closeModal();
}

export async function generateCompressionSummary() {
  if (_compressAbort) {
    _compressAbort.abort();
    _compressAbort = null;
  }

  const selectEl = document.getElementById("compress-keep-select");
  if (selectEl) _compressKeepCount = parseInt(selectEl.value, 10);
  const customInstructions = (document.getElementById("compress-instructions")?.value || "").trim() || null;

  const genBtn = document.getElementById("compress-gen-btn");
  const regenBtn = document.getElementById("compress-regen-btn");
  const applyBtn = document.getElementById("compress-apply-btn");
  const statusEl = document.getElementById("compress-status");
  const textarea = document.getElementById("compress-textarea");

  if (genBtn) genBtn.style.display = "none";
  if (regenBtn) {
    regenBtn.style.display = "";
    regenBtn.disabled = true;
  }
  if (applyBtn) {
    applyBtn.style.display = "";
    applyBtn.disabled = true;
  }
  if (statusEl) {
    statusEl.style.display = "";
    statusEl.textContent = "Generating summary...";
  }
  if (textarea) {
    textarea.style.display = "";
    textarea.value = "";
  }

  const overlayEl = document.querySelector(".modal-overlay");
  if (overlayEl) overlayEl.setAttribute("onclick", "if(event.target===this)cancelCompression()");

  _compressAbort = new AbortController();
  let summaryText = "";

  try {
    const resp = await streamPost(
      `/conversations/${S.activeConvId}/summarize`,
      { keep_count: _compressKeepCount, custom_instructions: customInstructions },
      _compressAbort.signal,
    );
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(detail);
    }

    for await (const { event, data } of sseEvents(resp.body, { signal: _compressAbort.signal })) {
      if (event === "token") {
        summaryText += unescapeSSE(data);
        if (textarea) textarea.value = summaryText;
      } else if (event === "error") {
        throw new Error(data);
      }
    }

    if (statusEl) statusEl.textContent = "Review and edit the summary, then create the new conversation.";
    if (regenBtn) regenBtn.disabled = false;
    if (applyBtn) applyBtn.disabled = false;
  } catch (e) {
    if (e.name === "AbortError") return;
    if (statusEl) statusEl.textContent = `Error: ${e.message}`;
    toast(`Summary generation failed: ${e.message}`, true);
    if (regenBtn) regenBtn.disabled = false;
  } finally {
    _compressAbort = null;
  }
}

export async function applyCompression() {
  const textarea = document.getElementById("compress-textarea");
  if (!textarea) return;
  const summary = textarea.value.trim();
  if (!summary) {
    toast("Summary is empty", true);
    return;
  }

  const applyBtn = document.getElementById("compress-apply-btn");
  const regenBtn = document.getElementById("compress-regen-btn");
  if (applyBtn) applyBtn.disabled = true;
  if (regenBtn) regenBtn.disabled = true;

  try {
    const result = await api.post(`/conversations/${S.activeConvId}/compress`, {
      summary,
      keep_count: _compressKeepCount,
    });
    closeModal();
    await loadConversations();
    await selectConversation(result.new_conversation_id);
    toast("New conversation created from compression");
  } catch (e) {
    toast(`Failed to apply compression: ${e.message}`, true);
    if (applyBtn) applyBtn.disabled = false;
    if (regenBtn) regenBtn.disabled = false;
  }
}

let _titleEditBackup = "";

export function startEditTitle() {
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

export function handleTitleEditKey(e) {
  if (e.key === "Enter") {
    e.preventDefault();
    saveTitleEdit();
  }
  if (e.key === "Escape") {
    e.preventDefault();
    cancelTitleEdit();
  }
}

export async function saveTitleEdit() {
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

export function cancelTitleEdit() {
  const inp = $("chat-title-input");
  if (!inp) return;
  const div = document.createElement("div");
  div.className = "chat-title";
  div.id = "chat-title-text";
  div.textContent = _titleEditBackup;
  inp.replaceWith(div);
  _titleEditBackup = "";
}
