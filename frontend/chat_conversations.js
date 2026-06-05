// Conversation lifecycle: load / select / create / delete, the conversation
// history modal, history compression, and inline title editing. Split out of
// chat.js; the public surface is re-exported from chat.js.
import { api } from "./api.js";
import { onConvSwitch, stopAll as stopAllAudio } from "./audio_player.js";
import { stopConversation } from "./chat_stream.js";
import { renderMessages, setMessages } from "./chat_core.js";
import { renderInspector } from "./chat_inspector.js";
import { clearInspectedMessage, inspectMessage } from "./chat_messages.js";
import { resetWorkflowViewportState } from "./chat_workflow.js";
import { loadCharacters, refreshCharacters, renderCharacters } from "./library.js";
import { activateAndPrioritizeWorld, deactivateWorld } from "./lorebooks.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, avatarUrl, convUrl, esc, formatRelativeDate, scrollToBottom, toast } from "./utils.js";
import { validate } from "./validate.js";
import { clearTextEffect } from "./workflow_text_effects.js";

// ── Conversations
export async function loadConversations() {
  S.conversations = await api.get("/conversations");
}

export function resetChatUI() {
  stopAllAudio();
  S.activeCharId = null;
  S.activeConvId = null;
  S.messages = [];
  S.lastDirectorData = null;
  S.directorState = null;
  S.inspectedMsgId = null;
  S.inspectedDirectorData = null;
  $("chat-title-text").textContent = "Select a character";
  $("chat-avatar").textContent = "📜";
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
  renderMessages();
  renderInspector();
}

export async function selectChar(id, source = "recent") {
  if (S.isStreaming) {
    toast("Stop generation before switching characters", true);
    return;
  }
  if (S.activeCharId === id || S._selectCharLock) return;
  S._selectCharLock = true;
  try {
    const oldWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
    const newWorldId = (S.allCharacters || []).find((c) => c.id === id)?.world_id || null;
    S.activeCharId = id;
    renderCharacters();
    if (oldWorldId && oldWorldId !== newWorldId) {
      await deactivateWorld(oldWorldId);
    }
    const existing = S.conversations.find((c) => c.character_card_id === id);
    if (existing) {
      // If selecting from library modal, bump conversation's updated_at
      if (source === "library") {
        try {
          await api.post(`/conversations/${existing.id}/touch`);
          // Update local conversation's updated_at to now
          existing.updated_at = new Date().toISOString();
        } catch (e) {
          // silently fail, not critical
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
    // Refresh the recent characters panel to reflect updated timestamps
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
    const oldWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
    const newWorldId = (S.allCharacters || []).find((c) => c.id === id)?.world_id || null;
    const conv = await api.post("/conversations", { character_card_id: id });
    await loadConversations();
    S.activeCharId = id;
    renderCharacters();
    if (oldWorldId && oldWorldId !== newWorldId) {
      await deactivateWorld(oldWorldId);
    }
    await selectConversation(conv.id);
  } catch (e) {
    toast(e.message, true);
  }
}

export async function selectConversation(id) {
  if (S.isStreaming) {
    toast("Stop generation before switching conversations", true);
    return;
  }
  const oldWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
  S.activeConvId = id;
  S.lastDirectorData = null;
  S.reasoningDirector = "";
  S.reasoningWriter = "";
  S.reasoningEditor = "";
  S.reasoningByPass = {}; // inspectMessage rehydrates the fields above but not this buffer, so it must be reset here
  S.reasoningPassActive = 0;
  S.reasoningPassSelected = 0;
  const conv = S.conversations.find((c) => c.id === id);
  if (conv?.character_card_id && S.activeCharId !== conv.character_card_id) {
    S.activeCharId = conv.character_card_id;
    renderCharacters();
  }
  $("chat-title-text").textContent = conv ? conv.title || conv.character_name : "";
  const av = $("chat-avatar");
  if (conv?.character_card_id) {
    av.innerHTML = `<img src="${avatarUrl(conv.character_card_id)}?t=${Date.now()}" onerror="this.parentElement.textContent='📜'" onclick="showAvatarPopup()" style="cursor:pointer">`;
  } else {
    av.textContent = "📜";
  }
  $("chat-input").disabled = false;
  $("send-btn").disabled = false;

  // If the character has a linked lorebook, activate it and move it to the top
  if (conv?.character_card_id) {
    const char = (S.allCharacters || []).find((c) => c.id === conv.character_card_id);
    if (char?.world_id) {
      await activateAndPrioritizeWorld(char.world_id);
    }
  }

  const newWorldId = (S.allCharacters || []).find((c) => c.id === S.activeCharId)?.world_id || null;
  if (oldWorldId && oldWorldId !== newWorldId) {
    await deactivateWorld(oldWorldId);
  }

  setMessages(await api.get(convUrl(id, "messages")));
  S.directorState = await api.get(convUrl(id, "director"));
  S.editingMsgId = null;
  S.magicInputMsgId = null;
  // Reset viewport-tracking state before re-rendering so each conv-open
  // starts a fresh "what has been reported" session.
  resetWorkflowViewportState();
  clearTextEffect();
  onConvSwitch();
  renderMessages();
  const lastAsst = [...S.messages].reverse().find((m) => m.role === "assistant" && m.id);
  if (lastAsst) {
    await inspectMessage(lastAsst.id);
  } else {
    clearInspectedMessage();
  }
  scrollToBottom();
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
        await api.del("/conversations/" + id);
        if (S.activeConvId === id) {
          S.activeConvId = null;
          S.messages = [];
          $("chat-input").disabled = true;
          $("send-btn").disabled = true;
          renderMessages();
        }
        await afterDelete();
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

async function deleteConversation(id) {
  const conv = S.conversations.find((c) => c.id === id);
  confirmDeleteConversation(
    id,
    conv?.message_count ?? (S.activeConvId === id ? S.messages.length : null),
    loadConversations,
  );
}

export async function deleteConversationFromModal(id) {
  const conv = S.conversations.find((c) => c.id === id);
  confirmDeleteConversation(id, conv?.message_count ?? null, showConvHistoryModal);
}

export async function showConvHistoryModal() {
  if (!S.activeCharId) {
    toast("Select a character first", true);
    return;
  }
  await loadConversations();
  const convs = S.conversations.filter((c) => c.character_card_id === S.activeCharId);
  if (!convs.length) {
    toast("No conversations yet", true);
    return;
  }
  const char = S.characters.find((c) => c.id === S.activeCharId);
  const charName = char ? char.name : "Character";
  const items = convs
    .map((c) => {
      const isActive = c.id === S.activeConvId;
      const preview = esc((c.last_message_preview || "").substring(0, 80));
      const title = esc(c.title || c.character_name || "Untitled");
      const ts = c.updated_at || c.created_at;
      return `<div class="conv-history-item${isActive ? " active-conv" : ""}" onclick="closeModal();selectConversation('${c.id}')">
      <div class="conv-history-meta">
        <span class="conv-history-title">${title}</span>
        <span class="conv-history-date">${formatRelativeDate(ts)}</span>
        <button class="conv-history-delete" title="Delete conversation" onclick="event.stopPropagation();deleteConversationFromModal('${c.id}')">&#x2715;</button>
      </div>
      ${
        preview
          ? `<div class="conv-history-preview">${preview}</div>`
          : `<div class="conv-history-preview" style="color:var(--text-muted);font-style:italic">No messages yet</div>`
      }
    </div>`;
    })
    .join("");
  showModal(`
    <h2>Conversations — ${esc(charName)}</h2>
    <div class="modal-list">${items}</div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`);
}

// History Compression

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
        <select id="compress-keep-select" style="padding:4px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg-input,var(--bg-secondary));color:var(--text)">
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

// Streams an SSE summary, so it returns the raw Response for resp.body.getReader()
// and takes an abort signal — neither of which the `api` helper supports.
function summarizeConversation(convId, { keepCount, customInstructions }, signal) {
  return fetch(`/api/conversations/${convId}/summarize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_count: keepCount, custom_instructions: customInstructions }),
    signal,
  });
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
    const resp = await summarizeConversation(
      S.activeConvId,
      { keepCount: _compressKeepCount, customInstructions },
      _compressAbort.signal,
    );

    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(detail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ") && currentEvent) {
          const data = line.slice(6);
          if (currentEvent === "token") {
            summaryText += data.replace(/\\n/g, "\n");
            if (textarea) textarea.value = summaryText;
          } else if (currentEvent === "error") {
            throw new Error(data);
          }
          currentEvent = null;
        }
      }
    }

    if (statusEl) statusEl.textContent = "Review and edit the summary, then create the new conversation.";
    if (regenBtn) regenBtn.disabled = false;
    if (applyBtn) applyBtn.disabled = false;
  } catch (e) {
    if (e.name === "AbortError") return;
    if (statusEl) statusEl.textContent = `Error: ${e.message}`;
    toast("Summary generation failed: " + e.message, true);
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
    toast("Failed to apply compression: " + e.message, true);
    if (applyBtn) applyBtn.disabled = false;
    if (regenBtn) regenBtn.disabled = false;
  }
}

// ── Title Edit
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
    const updated = await api.put("/conversations/" + S.activeConvId, { title: newTitle });
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
