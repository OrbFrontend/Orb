import { S } from './state.js';
import { $, esc, formatProse, toast, scrollToBottom, scrollToMessage, avatarUrl, convUrl, formatRelativeDate, resolvePlaceholders } from './utils.js';
import { api } from './api.js';
import { showModal, closeModal } from './modal.js';
import { renderCharacters, loadCharacters } from './library.js';

// ── Generation Phase ─────────────────────────
const PHASE_ORDER  = { pending: 0, directing: 0, generating: 1, refining: 2 };
const PHASE_LABELS = {
  pending:    'Waiting for response…',
  directing:  'Director analyzing scene…',
  generating: 'Generating response…',
  refining:   'Refining response…',
};
let _refineTimer = null;

function setGenerationPhase(phase) {
  if (!phase) {
    S.generationPhase = null;
  } else if (S.generationPhase && PHASE_ORDER[phase] < PHASE_ORDER[S.generationPhase]) {
    return; // never go backwards
  } else {
    S.generationPhase = phase;
  }
  const el = $('generation-status');
  if (!S.generationPhase) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.querySelector('.gen-text').textContent = PHASE_LABELS[S.generationPhase] || 'Processing…';
  el.querySelector('.gen-dot').className = 'gen-dot' + (S.generationPhase === 'refining' ? ' spin' : '');
}

function scheduleRefineTimer() {
  clearTimeout(_refineTimer);
  _refineTimer = setTimeout(() => {
    if (S.isStreaming && S.generationPhase === 'generating') setGenerationPhase('refining');
  }, 1500);
}

function clearRefineTimer() { clearTimeout(_refineTimer); _refineTimer = null; }

// ── Conversations ────────────────────────────
export async function loadConversations() {
  S.conversations = await api.get('/conversations');
}

export function resetChatUI() {
  S.activeCharId = null;
  S.activeConvId = null;
  S.messages = [];
  $('chat-title-text').textContent = 'Select a character';
  $('chat-avatar').textContent = '📜';
  $('chat-input').disabled = true;
  $('send-btn').disabled = true;
  renderMessages();
}

export async function selectChar(id) {
  if (S.activeCharId === id || S._selectCharLock) return;
  S._selectCharLock = true;
  try {
    S.activeCharId = id;
    renderCharacters();
    const existing = S.conversations.find(c => c.character_card_id === id);
    if (existing) {
      await selectConversation(existing.id);
    } else {
      try {
        const conv = await api.post('/conversations', { character_card_id: id });
        await loadConversations();
        await selectConversation(conv.id);
      } catch (e) { toast(e.message, true); }
    }
  } finally { S._selectCharLock = false; }
}

export async function newConvForChar(id) {
  try {
    const conv = await api.post('/conversations', { character_card_id: id });
    await loadConversations();
    S.activeCharId = id;
    renderCharacters();
    await selectConversation(conv.id);
  } catch (e) { toast(e.message, true); }
}

export async function selectConversation(id) {
  S.activeConvId = id;
  const conv = S.conversations.find(c => c.id === id);
  if (conv?.character_card_id && S.activeCharId !== conv.character_card_id) {
    S.activeCharId = conv.character_card_id;
    renderCharacters();
  }
  $('chat-title-text').textContent = conv ? (conv.title || conv.character_name) : '';
  const av = $('chat-avatar');
  if (conv?.character_card_id) {
    av.innerHTML = `<img src="${avatarUrl(conv.character_card_id)}" onerror="this.parentElement.textContent='📜'">`;
  } else { av.textContent = '📜'; }
  $('chat-input').disabled = false;
  $('send-btn').disabled = false;
  S.messages      = await api.get(convUrl(id, 'messages'));
  S.directorState = await api.get(convUrl(id, 'director'));
  S.editingMsgId  = null;
  renderMessages();
  renderInspector();
  scrollToBottom();
}

async function deleteConversation(id) {
  if (!confirm('Delete?')) return;
  try {
    await api.del('/conversations/' + id);
    if (S.activeConvId === id) {
      S.activeConvId = null;
      S.messages = [];
      $('chat-input').disabled = true;
      $('send-btn').disabled = true;
      renderMessages();
    }
    await loadConversations();
  } catch (e) { toast(e.message, true); }
}

export async function deleteConversationFromModal(id) {
  if (!confirm('Delete?')) return;
  try {
    await api.del('/conversations/' + id);
    if (S.activeConvId === id) {
      S.activeConvId = null;
      S.messages = [];
      $('chat-input').disabled = true;
      $('send-btn').disabled = true;
      renderMessages();
    }
    await showConvHistoryModal();
  } catch (e) { toast(e.message, true); }
}

export async function showConvHistoryModal() {
  if (!S.activeCharId) { toast('Select a character first', true); return; }
  await loadConversations();
  const convs = S.conversations.filter(c => c.character_card_id === S.activeCharId);
  if (!convs.length) { toast('No conversations yet', true); return; }
  const char     = S.characters.find(c => c.id === S.activeCharId);
  const charName = char ? char.name : 'Character';
  const items = convs.map(c => {
    const isActive = c.id === S.activeConvId;
    const preview  = esc((c.last_message_preview || '').substring(0, 80));
    const title    = esc(c.title || c.character_name || 'Untitled');
    const ts       = c.updated_at || c.created_at;
    return `<div class="conv-history-item${isActive ? ' active-conv' : ''}" onclick="closeModal();selectConversation('${c.id}')">
      <div class="conv-history-meta">
        <span class="conv-history-title">${title}</span>
        <span class="conv-history-date">${formatRelativeDate(ts)}</span>
        <button class="conv-history-delete" title="Delete conversation" onclick="event.stopPropagation();deleteConversationFromModal('${c.id}')">&#x2715;</button>
      </div>
      ${preview
        ? `<div class="conv-history-preview">${preview}</div>`
        : `<div class="conv-history-preview" style="color:var(--text-muted);font-style:italic">No messages yet</div>`}
    </div>`;
  }).join('');
  showModal(`
    <h2>Conversations — ${esc(charName)}</h2>
    <div style="margin:-8px -24px 0;max-height:60vh;overflow-y:auto;">${items}</div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">Close</button></div>`);
}

// ── Messages ─────────────────────────────────
function getCharName() {
  const c = S.conversations.find(c => c.id === S.activeConvId);
  return c?.character_name || 'Assistant';
}

export function renderMessages() {
  const ct = $('chat-messages');
  let streamingEl = null;
  let badgeEl     = null;
  if (S.isStreaming) {
    streamingEl = S.streamingBodyEl?.closest('.message') ?? null;
    badgeEl     = document.getElementById('active-director-badge');
  }
  if (!S.activeConvId) {
    ct.innerHTML = '<div class="empty-state"><div class="icon">📜</div><div>Select a character to begin</div></div>';
  } else if (!S.messages.length) {
    ct.innerHTML = '<div class="empty-state"><div class="icon">📜</div><div>Start writing to begin the scene</div></div>';
  } else {
    let msgs = S.messages;
    if (S.isStreaming && S.streamCutoffIndex != null) {
      msgs = S.messages.slice(0, S.streamCutoffIndex);
    }
    ct.innerHTML = msgs.map(m => {
      const isEditing = S.editingMsgId !== null && S.editingMsgId === m.id;
      const bc = m.branch_count || 1;
      const bi = m.branch_index || 0;
      const branchHtml = bc > 1 ? `
        <span class="swipe-nav">
          <button onclick="event.stopPropagation();switchBranch(${m.prev_branch_id})" ${!m.prev_branch_id ? 'disabled' : ''}>◀</button>
          <span class="swipe-counter">${bi + 1}/${bc}</span>
          <button onclick="event.stopPropagation();switchBranch(${m.next_branch_id})" ${!m.next_branch_id ? 'disabled' : ''}>▶</button>
        </span>` : '';
      const toolbar = isEditing ? '' : `
        <div class="msg-toolbar">
          ${m.id ? `<button onclick="startEdit(${m.id})" title="Edit">✏️ Edit</button>` : ''}
          ${m.role === 'assistant' && m.id ? `<button onclick="regenerate(${m.id})" title="Regenerate">🔄 Regen</button>` : ''}
          ${m.id ? `<button onclick="deleteMessage(${m.id})" title="Delete message and all children" style="color:var(--red)">✕ Del</button>` : ''}
        </div>`;
      const body = isEditing ? `
        <div class="msg-edit-area">
          <textarea id="edit-textarea-${m.id}" rows="5">${esc(m.content)}</textarea>
          <div class="msg-edit-actions">
            <button class="btn btn-sm" onclick="cancelEdit()">Cancel</button>
            <button class="btn btn-sm btn-accent" onclick="saveEdit(${m.id},'${m.role}')">
              Save${m.role === 'user' ? ' & Regen' : ''}
            </button>
          </div>
        </div>` : `<div class="msg-body">${formatProse(resolvePlaceholders(m.content))}</div>`;
      return `<div class="message ${m.role}" data-msg-id="${m.id}">
        <div class="msg-role">${m.role === 'user' ? 'You' : esc(getCharName())} ${branchHtml}</div>
        ${body}${toolbar}
      </div>`;
    }).join('');
  }
  if (badgeEl)     ct.appendChild(badgeEl);
  if (streamingEl) ct.appendChild(streamingEl);
}

export function startEdit(msgId) {
  S.editingMsgId = msgId;
  renderMessages();
  scrollToMessage(msgId);
  const ta = $('edit-textarea-' + msgId);
  if (ta) {
    ta.focus();
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    ta.style.height = 'auto';
    const lineH = parseFloat(getComputedStyle(ta).lineHeight) || 20;
    ta.style.height = Math.max(lineH * 3, ta.scrollHeight) + 'px';
  }
}

export function cancelEdit() {
  S.editingMsgId = null;
  renderMessages();
}

export async function deleteMessage(msgId) {
  if (S.isStreaming) return;
  if (!confirm('Delete this message and all its children?')) return;
  try {
    S.messages = await api.del(convUrl(S.activeConvId, 'messages', msgId));
    S.lastDirectorData = null;
    renderMessages(); renderInspector(); scrollToBottom();
    toast('Message deleted');
  } catch (e) { toast(e.message, true); }
}

export async function switchBranch(msgId) {
  if (!msgId || S.isStreaming) return;
  try {
    S.messages = await api.post(convUrl(S.activeConvId, 'messages', msgId, 'switch-branch'), {});
    S.lastDirectorData = null;
    renderMessages(); renderInspector(); scrollToBottom();
  } catch (e) { toast(e.message, true); }
}

// ── Edit Message ─────────────────────────────
export async function saveEdit(msgId, role) {
  const ta = $('edit-textarea-' + msgId);
  if (!ta) return;
  const content = ta.value.trim();
  if (!content) { toast('Message cannot be empty', true); return; }
  if (S.isStreaming) { toast('Wait for generation to finish', true); return; }
  S.editingMsgId = null;

  // If "Save & Regen" was clicked but the user message wasn't changed,
  // treat it as a plain regen instead of creating a duplicate branch.
  if (role === 'user') {
    const msg = S.messages.find(m => m.id === msgId);
    if (msg && msg.content === content) {
      const idx = S.messages.findIndex(m => m.id === msgId);
      const nextAssistant = S.messages.slice(idx + 1).find(m => m.role === 'assistant' && m.id);
      if (nextAssistant) {
        renderMessages();
        return regenerate(nextAssistant.id);
      }
      // No assistant message after this user message; fall through to normal edit+regen
    }
  }

  if (role === 'assistant') {
    try {
      await api.post(convUrl(S.activeConvId, 'messages', msgId, 'edit'), { content, regenerate: false });
      S.messages = await api.get(convUrl(S.activeConvId, 'messages'));
      renderMessages();
      toast('Message edited');
    } catch (e) { toast(e.message, true); }
    return;
  }

  const msg = S.messages.find(m => m.id === msgId);
  if (msg) msg.content = content;
  const idx = S.messages.findIndex(m => m.id === msgId);
  S.streamCutoffIndex = idx >= 0 ? idx + 1 : S.messages.length;

  setStreaming(true);
  setGenerationPhase('directing');
  $('send-btn').disabled = true;
  renderMessages();

  const ct = $('chat-messages');
  const editedEl = ct.querySelector(`[data-msg-id="${msgId}"]`);
  if (editedEl) {
    let next = editedEl.nextElementSibling;
    while (next) { const n = next.nextElementSibling; next.remove(); next = n; }
  }

  const msgDiv = createStreamingDiv();
  S.abortController = new AbortController();
  try {
    const resp = await fetch('/api' + convUrl(S.activeConvId, 'messages', msgId, 'edit'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, regenerate: true, ...agentPayload() }),
      signal: S.abortController.signal,
    });
    if (resp.headers.get('content-type')?.includes('text/event-stream')) {
      await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
    } else {
      S.messages = await api.get(convUrl(S.activeConvId, 'messages'));
      renderMessages();
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      S.wasAborted = true;
    } else {
      toast('Error: ' + e.message, true);
    }
  }
  await afterStream();
}

// ── Streaming Helpers ────────────────────────
function setStreaming(active) {
  S.isStreaming = active;
  $('send-btn').style.display = active ? 'none' : 'flex';
  $('stop-btn').style.display = active ? 'flex' : 'none';
}

export function stopGeneration() {
  if (S.abortController) S.abortController.abort();
}

function createStreamingDiv() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `<div class="msg-role">${esc(getCharName())}</div>
    <div class="msg-body" id="streaming-body">
      <span class="typing-indicator"><span></span><span></span><span></span></span>
    </div>`;
  S.streamingBodyEl = div.querySelector('.msg-body');
  return div;
}

async function afterStream() {
  const preservedContent = S.streamingContent;
  const pendingUserMsg = S.pendingUserMsg || null;
  const wasAborted = S.wasAborted;
  S.abortController   = null;
  S.streamingBodyEl   = null;
  S.streamCutoffIndex = null;
  S.streamingContent  = null;
  S.pendingUserMsg    = null;
  S.wasAborted        = false;
  clearRefineTimer();
  setGenerationPhase(null);
  setStreaming(false);
  $('send-btn').disabled = false;

  if (!S.activeConvId) { renderMessages(); renderInspector(); return; }

  // When aborted, give the backend a moment to finish its fallback persistence
  // (the finally block in handle_turn/handle_regenerate saves the incomplete message)
  if (wasAborted) {
    await new Promise(r => setTimeout(r, 500));
  }

  try {
    S.messages      = await api.get(convUrl(S.activeConvId, 'messages'));
    S.directorState = await api.get(convUrl(S.activeConvId, 'director'));
  } catch (e) {
    // If server fetch fails, keep local messages as fallback
  }

  // Preserve user message if server didn't save it (e.g. abort before persist)
  if (pendingUserMsg) {
    const hasUserMsg = S.messages.some(m => m.role === 'user' && m.content === pendingUserMsg.content);
    if (!hasUserMsg) {
      S.messages.push(pendingUserMsg);
    }
  }

  // Add local fallback assistant message if the server doesn't have one yet.
  // For aborted streams, the backend should have persisted the incomplete message,
  // but there can be a timing gap. If the server already has an assistant message
  // (with a proper ID), we skip the fallback so edit/delete buttons work.
  if (preservedContent?.trim()) {
    const lastMsg = S.messages[S.messages.length - 1];
    if (!lastMsg || lastMsg.role !== 'assistant') {
      S.messages.push({
        role: 'assistant', content: preservedContent, id: null,
        branch_count: 1, branch_index: 0,
        prev_branch_id: null, next_branch_id: null,
      });
    }
  }
  renderMessages(); renderInspector(); scrollToBottom();
}

async function processSSEStream(resp, container, msgDiv, signal) {
  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', fullResponse = '', rewrittenResponse = null, firstToken = true, currentEvent = null;

  if (signal) signal.addEventListener('abort', () => reader.cancel());

  while (true) {
    const { done, value } = await reader.read();
    if (done || signal?.aborted) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ') && currentEvent) {
        const data = line.slice(6);
        handleSSEEvent(currentEvent, data, container, msgDiv,
          () => {
            if (firstToken) {
              firstToken = false;
              container.appendChild(msgDiv);
              if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = '';
            }
            fullResponse += data.replace(/\\n/g, '\n');
            S.streamingContent = rewrittenResponse || fullResponse;
            if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = formatProse(fullResponse);
            scrollToBottom();
          },
          (text) => {
            rewrittenResponse = text;
            S.streamingContent = text;
            if (S.streamingBodyEl) S.streamingBodyEl.innerHTML = formatProse(text);
            scrollToBottom();
          }
        );
        currentEvent = null;
      }
    }
  }
}

function handleSSEEvent(event, data, container, msgDiv, onToken, onRewrite) {
  switch (event) {
    case 'director_start':
      setGenerationPhase('directing');
      S.lastDirectorData = null;
      renderInspector();
      break;
    case 'director_done': {
      try { S.lastDirectorData = JSON.parse(data); renderInspector(); } catch (_) {}
      break;
    }
    case 'prompt_rewritten':
      try {
        const d = JSON.parse(data);
        const lastUser = [...S.messages].reverse().find(m => m.role === 'user' && !m.id)
                      || [...S.messages].reverse().find(m => m.role === 'user');
        if (lastUser) lastUser.content = d.refined_message;
        if (S.isStreaming) {
          const userBodies = document.querySelectorAll('#chat-messages .message.user .msg-body');
          const last = userBodies[userBodies.length - 1];
          if (last) last.innerHTML = formatProse(d.refined_message);
        } else {
          renderMessages();
        }
      } catch (_) {}
      break;
    case 'token':
      setGenerationPhase('generating');
      onToken();
      scheduleRefineTimer();
      break;
    case 'writer_rewrite':
      clearRefineTimer();
      setGenerationPhase('refining');
      try { onRewrite(JSON.parse(data).rewritten_text); } catch (_) {}
      break;
    case 'error':
      toast('Error: ' + data, true);
      break;
  }
}

function agentPayload() {
  return { enable_agent: S.agentEnabled };
}

// ── Send Message ─────────────────────────────
export async function sendMessage() {
  const inp     = $('chat-input');
  let content = inp.value.trim();
  if (!content || !S.activeConvId || S.isStreaming) return;

  // Resolve {{user}} and {{char}} placeholders before sending
  content = resolvePlaceholders(content);

  setStreaming(true);
  setGenerationPhase('pending');
  inp.value = ''; inp.style.height = 'auto';
  $('send-btn').disabled = true;

  const userMsg = { role: 'user', content, id: null, branch_count: 1, branch_index: 0, prev_branch_id: null, next_branch_id: null };
  S.messages.push(userMsg);
  S.pendingUserMsg = userMsg;
  renderMessages(); scrollToBottom();

  const ct     = $('chat-messages');
  const msgDiv = createStreamingDiv();

  S.abortController = new AbortController();
  try {
    const resp = await fetch('/api' + convUrl(S.activeConvId, 'send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, ...agentPayload() }),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === 'AbortError') {
      S.wasAborted = true;
    } else {
      toast('Connection error: ' + e.message, true);
    }
  }
  await afterStream();
}

// ── Regenerate ───────────────────────────────
export async function regenerate(msgId) {
  if (S.isStreaming || !S.activeConvId) return;
  setStreaming(true);
  setGenerationPhase('pending');
  $('send-btn').disabled = true;

  const idx = S.messages.findIndex(m => m.id === msgId);
  S.streamCutoffIndex = idx >= 0 ? idx : S.messages.length;

  const ct = $('chat-messages');
  const el = ct.querySelector(`[data-msg-id="${msgId}"]`);
  if (el) el.remove();

  const msgDiv = createStreamingDiv();
  S.abortController = new AbortController();
  try {
    const resp = await fetch('/api' + convUrl(S.activeConvId, 'messages', msgId, 'regenerate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(agentPayload()),
      signal: S.abortController.signal,
    });
    await processSSEStream(resp, ct, msgDiv, S.abortController.signal);
  } catch (e) {
    if (e.name === 'AbortError') {
      S.wasAborted = true;
    } else {
      toast('Error: ' + e.message, true);
    }
  }
  await afterStream();
}

// ── Inspector ────────────────────────────────
export function toggleInspector() { $('inspector').classList.toggle('open'); }

export function renderInspector() {
  if (S.isStreaming && S.lastDirectorData === null) {
    $('inspector-content').innerHTML =
      `<div style="color:var(--text-muted);font-size:12px;display:flex;align-items:center;gap:8px">
         <span class="typing-indicator"><span></span><span></span><span></span></span> Director thinking…
       </div>`;
    return;
  }
  const ds        = S.directorState || {};
  const ld        = S.lastDirectorData || {};
  const activeIds = ld.active_moods || ds.active_moods || [];
  const stylesHtml = S.fragments
    .map(f => `<span class="style-tag ${activeIds.includes(f.id) ? 'active' : ''}">${f.id}</span>`)
    .join('');
  const lat = ld.agent_latency_ms || 0;
  const tc  = ld.tool_calls || [];
  const inj = ld.injection_block || '';
  $('inspector-content').innerHTML = `
    <div class="inspector-block"><h4>Active Styles</h4>
      <div>${stylesHtml || '<span style="color:var(--text-muted);font-size:12px">None</span>'}</div>
    </div>
    ${lat ? `<div class="inspector-block"><h4>Agent Latency</h4>
               <div style="font-size:12px;color:var(--text-secondary)">${lat}ms</div></div>` : ''}
    ${tc.length ? `<div class="inspector-block"><h4>Tool Calls</h4>
                    <div class="injection-box">${esc(JSON.stringify(tc, null, 2))}</div></div>` : ''}
    ${inj ? `<div class="inspector-block"><h4>Injection Block</h4>
               <div class="injection-box">${esc(inj)}</div></div>` : ''}`;
}