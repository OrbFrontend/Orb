import { S } from './state.js';

export function $(id) { return document.getElementById(id); }

export function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

export function toast(msg, isError = false) {
  const el = $('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast' + (isError ? ' toast-error' : '');
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 3000);
}

export function scrollToBottom() {
  const ct = $('chat-messages');
  if (ct) ct.scrollTop = ct.scrollHeight;
}

export function scrollToMessage(msgId) {
  const el = document.querySelector(`[data-msg-id="${msgId}"]`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

export function avatarUrl(charId) {
  return `/api/characters/${charId}/avatar`;
}

export function convUrl(...parts) {
  return '/conversations/' + parts.join('/');
}

export function formatRelativeDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  const now = new Date();
  const diffMs = now - date;
  const diffMins = Math.round(diffMs / 60000);
  const diffHours = Math.round(diffMs / 3600000);
  const diffDays = Math.round(diffMs / 86400000);
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

export function formatProse(text) {
  if (!text) return '';
  let escaped = esc(text);
  // Convert markdown-like formatting
  // Handle **bold** -> <strong>
  escaped = escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Handle *italic* -> <em>
  escaped = escaped.replace(/\*([^*]+?)\*/g, '<em>$1</em>');
  // Replace newlines with <br>
  return escaped.replace(/\n/g, '<br>');
}

/**
 * Replace {{user}} and {{char}} placeholders with actual names.
 * @param {string} text - Input text containing placeholders
 * @param {string} userName - User's name (from settings)
 * @param {string} charName - Character's name (from conversation)
 * @returns {string} Text with placeholders replaced
 */
export function replacePlaceholders(text, userName, charName) {
  if (!text || typeof text !== 'string') return text || '';
  let result = text;
  // Replace {{user}} with userName (default "User")
  if (userName) {
    result = result.replace(/\{\{user\}\}/gi, userName);
  }
  // Replace {{char}} with charName (default empty? but should be character name)
  if (charName) {
    result = result.replace(/\{\{char\}\}/gi, charName);
  }
  return result;
}

/**
 * Get resolved text for display or sending, using current state.
 * For use in chat messages and character card display.
 * @param {string} text - Raw text possibly containing placeholders
 * @returns {string} Resolved text
 */
export function resolvePlaceholders(text) {
  const userName = S.settings?.user_name || 'User';
  const conv = S.conversations?.find(c => c.id === S.activeConvId);
  const charName = conv?.character_name || '';
  return replacePlaceholders(text, userName, charName);
}