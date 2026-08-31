import { createScrollFollow } from "./scroll_follow.js";
import { charactersView, S } from "./state.js";
import { endsWithSentenceTerminator, sentenceStream } from "./text_segmentation.js";

export function $(id) {
  return document.getElementById(id);
}

export function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : s;
  return div.innerHTML;
}

export function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function escHandlerArg(s) {
  const js = String(s == null ? "" : s)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n");
  return js.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function boolFlag(value) {
  return value === true || value === 1;
}

export { notifyError, toast } from "./notify.js";

let _chatFollow = null;

export function initChatScrollFollow(el, { onScroll } = {}) {
  _chatFollow = createScrollFollow(el, { threshold: 20, onScroll });
}

export function setChatFollowing(enabled) {
  _chatFollow?.setFollowing(enabled);
}

export function markChatProgrammaticScroll(ms) {
  _chatFollow?.markProgrammatic(ms);
}

function scrollChatTarget(el, align) {
  const ct = $("chat-messages");
  if (!ct || !el) return;
  const topWithinScroller = el.getBoundingClientRect().top - ct.getBoundingClientRect().top + ct.scrollTop;
  let targetTop = topWithinScroller;
  if (align === "center") {
    targetTop -= Math.max(0, (ct.clientHeight - el.offsetHeight) / 2);
  }
  targetTop = Math.min(Math.max(0, targetTop), Math.max(0, ct.scrollHeight - ct.clientHeight));
  markChatProgrammaticScroll();
  ct.scrollTo({ top: targetTop, behavior: "instant" });
}

export function scrollToBottom(smooth = false) {
  const ct = $("chat-messages");
  const pinnedTarget = ct?.querySelector(".stream-scroll-target");
  if (ct && pinnedTarget && _chatFollow?.isFollowing()) {
    markChatProgrammaticScroll();
    requestAnimationFrame(() => {
      const topWithinScroller =
        pinnedTarget.getBoundingClientRect().top - ct.getBoundingClientRect().top + ct.scrollTop;
      const desiredTop = Math.max(topWithinScroller, topWithinScroller + pinnedTarget.offsetHeight - ct.clientHeight);
      const targetTop = Math.min(Math.max(0, desiredTop), Math.max(0, ct.scrollHeight - ct.clientHeight));
      ct.scrollTo({ top: targetTop, behavior: smooth ? "smooth" : "instant" });
    });
    return;
  }
  _chatFollow?.toBottom({ smooth });
}

export function scrollToMessage(msgId) {
  const ct = $("chat-messages");
  const el = ct?.querySelector(`.message[data-msg-id="${msgId}"]`);
  if (el) scrollChatTarget(el, "center");
}

export function pinStreamingMessage(el) {
  el.classList.add("stream-scroll-target");
  if (el.isConnected) scrollChatTarget(el, "start");
}

export function avatarUrl(charId) {
  return `/api/characters/${charId}/avatar`;
}

export function convActivity(c) {
  return [c.last_accessed_at, c.updated_at, c.created_at].reduce((a, b) => (b && b > a ? b : a), "");
}

export const NO_AVATAR_ICON = "👤"; // character lists
export const CHAT_AVATAR_ICON = "📜"; // active conversation header

export function avatarCell(src, { icon = NO_AVATAR_ICON, attrs = "" } = {}) {
  if (!src) return icon;
  return `<img src="${src}"${attrs ? ` ${attrs}` : ""} onerror="this.parentElement.textContent='${icon}'">`;
}

export function convUrl(...parts) {
  return `/conversations/${parts.join("/")}`;
}

export function formatRelativeDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const now = new Date();
  const diffMs = now - date;
  const diffMins = Math.round(diffMs / 60000);
  const diffHours = Math.round(diffMs / 3600000);
  const diffDays = Math.round(diffMs / 86400000);
  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

function _sentenceDiffTokens(text) {
  const units = sentenceStream(text).map((unit) => unit.text);
  return units.length ? units : [text];
}

function _lcs(a, b) {
  const m = a.length,
    n = b.length;
  const band = Math.max(2, Math.ceil(Math.max(m, n) * 0.4));
  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] =
        a[i - 1] === b[j - 1] && Math.abs(i - j) <= band ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  const ops = [];
  let i = m,
    j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1] && Math.abs(i - j) <= band) {
      ops.push({ type: "equal", text: a[i - 1] });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push({ type: "insert", text: b[j - 1] });
      j--;
    } else {
      ops.push({ type: "delete", text: a[i - 1] });
      i--;
    }
  }
  return ops.reverse();
}

function _mergeOps(ops) {
  const result = [];
  for (const op of ops) {
    const last = result[result.length - 1];
    if (last && last.type === op.type) last.text += op.text;
    else result.push({ ...op });
  }
  return result;
}

export function sentenceTail(text, n = 3, dropFragment = false) {
  if (!Number.isFinite(n) || n <= 0) return "";
  const units = sentenceStream(text);
  const sentenceIndices = units.flatMap((unit, index) => (unit.kind === "sentence" ? [index] : []));
  let end = units.length;
  const lastSentence = sentenceIndices.at(-1);
  if (dropFragment && lastSentence != null) {
    const closedByLineBreak = units.slice(lastSentence + 1).some((unit) => unit.kind === "linebreak");
    if (!closedByLineBreak && !endsWithSentenceTerminator(units[lastSentence].text)) {
      sentenceIndices.pop();
      end = lastSentence;
    }
  }
  const start = sentenceIndices.slice(-Math.floor(n))[0];
  if (start == null) return "";
  return units
    .slice(start, end)
    .map((unit) => unit.text)
    .join("")
    .trim();
}

export function sentenceDiff(oldText, newText) {
  if (!oldText || !newText) return [{ type: "equal", text: newText || "" }];
  return _mergeOps(_lcs(_sentenceDiffTokens(oldText), _sentenceDiffTokens(newText)));
}

const INLINE_QUOTE_RE = /"[^"]+"|“[^”]+”|‘[^’]+’|«[^»]+»|‹[^›]+›|「[^」]+」|『[^』]+』|„[^“]+“|‚[^‘]+‘/g;

function _applyInlineFormatting(escaped) {
  escaped = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  escaped = escaped.replace(/\*([^*]+?)\*/g, "<em>$1</em>");
  return escaped.replace(INLINE_QUOTE_RE, '<span class="quoted">$&</span>');
}

export function formatProseWithDiff(ops) {
  let html = "";
  for (let i = 0; i < ops.length; i++) {
    const op = ops[i];
    if (op.type === "equal") {
      html += _applyInlineFormatting(esc(op.text));
    } else if (op.type === "delete") {
      const next = ops[i + 1];
      if (next?.type === "insert") {
        html += `<span class="diff-deleted">${_applyInlineFormatting(esc(op.text))}</span>`;
        html += `<span class="diff-change">${_applyInlineFormatting(esc(next.text))}</span>`;
        i++; // consume the paired insert
      } else {
        html += `<span class="diff-deleted">${_applyInlineFormatting(esc(op.text))}</span>`;
      }
    } else if (op.type === "insert") {
      html += `<span class="diff-change">${_applyInlineFormatting(esc(op.text))}</span>`;
    }
  }
  return html.replace(/\n{2,}/g, '<br><span class="pbreak"></span>').replace(/\n/g, "<br>");
}

const ICON_WRAP = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><line x1="3" y1="6" x2="21" y2="6"/><path d="M3 12h15a3 3 0 1 1 0 6h-4"/><polyline points="16 16 14 18 16 20"/><line x1="3" y1="18" x2="10" y2="18"/></svg>`;
const ICON_COPY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;

const IMG_LINK_RE = /!\[([^\]]*)\]\((https?:\/\/[^\s)]+\.(?:jpe?g|png|gif|webp))\)/i;

function renderImageEmbed(url, alt) {
  const safeUrl = escAttr(url);
  const safeAlt = escAttr(alt || "");
  return (
    `<details class="msg-image-embed">` +
    `<summary><span class="reasoning-summary-arrow">▶</span>` +
    `<span class="msg-image-label">🖼️ Image</span></summary>` +
    `<a class="msg-image-link" href="${safeUrl}" target="_blank" rel="noopener">` +
    `<img class="msg-image" src="${safeUrl}" alt="${safeAlt}" loading="lazy" ` +
    `onerror="this.replaceWith(Object.assign(document.createElement('span'),` +
    `{className:'msg-image-broken',textContent:this.src}))">` +
    `</a>` +
    `</details>`
  );
}

const _proseCache = new Map();
const _PROSE_CACHE_MAX = 2000;

export function formatProse(text) {
  if (!text) return "";
  const cached = _proseCache.get(text);
  if (cached !== undefined) return cached;
  const html = _formatProse(text);
  if (_proseCache.size >= _PROSE_CACHE_MAX) {
    _proseCache.delete(_proseCache.keys().next().value);
  }
  _proseCache.set(text, html);
  return html;
}

function _formatProse(text) {
  const parts = text.split(/(```[\w]*\n?[\s\S]*?```|!\[[^\]]*\]\((?:https?:\/\/[^\s)]+\.(?:jpe?g|png|gif|webp))\))/gi);
  return parts
    .map((part, i) => {
      const imgMatch = part.match(IMG_LINK_RE);
      if (imgMatch) {
        return renderImageEmbed(imgMatch[2], imgMatch[1]);
      }
      const codeMatch = part.match(/^```(\w*)(\n)?([\s\S]*?)```$/);
      if (codeMatch) {
        const hasNewline = !!codeMatch[2];
        const lang = hasNewline ? codeMatch[1] : "";
        const code = esc(hasNewline ? codeMatch[3] : codeMatch[1] + codeMatch[3]);
        const langAttr = lang ? ` class="language-${escAttr(lang)}"` : "";
        return (
          `<div class="code-block">` +
          `<div class="code-block-bar">` +
          `<button type="button" class="code-block-btn" title="Toggle word wrap" aria-label="Toggle word wrap" aria-pressed="false" ` +
          `onclick="this.setAttribute('aria-pressed', this.closest('.code-block').classList.toggle('wrap'))">${ICON_WRAP}</button>` +
          `<button type="button" class="code-block-btn" title="Copy" aria-label="Copy code" ` +
          `onclick="navigator.clipboard.writeText(this.closest('.code-block').querySelector('code').textContent).then(() => { this.classList.add('copied'); setTimeout(() => this.classList.remove('copied'), 1200); })">${ICON_COPY}</button>` +
          `</div>` +
          `<pre><code${langAttr}>${code}</code></pre>` +
          `</div>`
        );
      }
      let prose = part;
      if (i > 0) prose = prose.replace(/^\n/, ""); // after a code block
      if (i < parts.length - 1) prose = prose.replace(/\n$/, ""); // before a code block
      let escaped = _applyInlineFormatting(esc(prose));
      escaped = escaped.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');
      escaped = escaped.replace(
        /^(#{1,6}) (.+)$/gm,
        (_, hashes, content) => `<strong class="md-h${hashes.length}">${content}</strong>`,
      );
      return escaped.replace(/\n{2,}/g, '<br><span class="pbreak"></span>').replace(/\n/g, "<br>");
    })
    .join("");
}

export function replacePlaceholders(text, userName, charName) {
  if (!text || typeof text !== "string") return text || "";
  let result = text;
  if (userName) {
    result = result.replace(/\{\{user\}\}/gi, userName);
  }
  if (charName) {
    result = result.replace(/\{\{char\}\}/gi, charName);
  }
  return result;
}

export function resolvePlaceholders(text) {
  let userName = S.settings?.user_name || "User";
  const personaId = effectivePersonaId();
  if (personaId) {
    const persona = S.personas.find((p) => p.id === personaId);
    if (persona?.name) {
      userName = persona.name;
    }
  }
  const conv = S.conversations?.find((c) => c.id === S.activeConvId);
  const charName = conv?.kind === "group" ? conv.title || "" : conv?.character_name || "";
  const resolved = replacePlaceholders(text, userName, charName);
  const cast = S.groupCast?.members?.map((member) => member.display_name).join(", ") || "";
  return cast ? resolved.replace(/\{\{cast\}\}/gi, cast) : resolved;
}

export function effectivePersonaId() {
  const conv = S.conversations?.find((c) => c.id === S.activeConvId);
  if (conv?.persona_lock_id) return conv.persona_lock_id;
  if (conv?.kind === "group") return S.activePersonaId || null;
  const card = conv?.character_card_id ? charactersView().find((c) => c.id === conv.character_card_id) : null;
  return card?.persona_lock_id || S.activePersonaId || null;
}

export function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / k ** i).toFixed(1))} ${sizes[i]}`;
}

export function downloadBlob(filename, source) {
  const isBlob = source instanceof Blob;
  const href = isBlob ? URL.createObjectURL(source) : source;
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  if (isBlob) setTimeout(() => URL.revokeObjectURL(href), 0);
}
