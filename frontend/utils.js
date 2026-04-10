export const $ = id => document.getElementById(id);

export function esc(s) {
  return s
    ? s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    : '';
}

export function formatProse(t) {
  return '<p>' +
    t.replace(/&/g, '&amp;')
     .replace(/</g, '&lt;')
     .replace(/>/g, '&gt;')
     .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
     .replace(/\*(.+?)\*/g, '<em>$1</em>')
     .replace(/\n\n+/g, '</p><p>')
     .replace(/\n/g, '<br>') +
    '</p>';
}

export function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

export function scrollToBottom() {
  const el = $('chat-messages');
  setTimeout(() => el.scrollTop = el.scrollHeight, 50);
}

export function scrollToMessage(msgId) {
  const el = $('chat-messages');
  const msgEl = el.querySelector(`[data-msg-id="${msgId}"]`);
  if (msgEl) {
    // Scroll the message into view with smooth behavior
    msgEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

export function avatarUrl(id) {
  return '/api/characters/' + id + '/avatar';
}

export function convUrl(convId, ...parts) {
  return '/conversations/' + convId + (parts.length ? '/' + parts.join('/') : '');
}

export function formatRelativeDate(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso);
  if (diff < 60_000)      return 'just now';
  if (diff < 3_600_000)   return Math.floor(diff / 60_000) + 'm ago';
  if (diff < 86_400_000)  return Math.floor(diff / 3_600_000) + 'h ago';
  if (diff < 604_800_000) return Math.floor(diff / 86_400_000) + 'd ago';
  return new Date(iso).toLocaleDateString();
}