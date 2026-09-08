import { CHEVRON_RIGHT_ICON } from "./icons.js";
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

// An attachment's MIME type and payload arrive from the API, which accepts what
// the client sent — so both are attacker-controlled, and both land in an
// attribute *value* rather than in text. `esc` does not escape quotes, so
// interpolating either one raw is how a filename ending the attribute early
// turns into an event handler on the element.
// Building the URL here means no call site has to remember that.
const ATTACHMENT_MIME_RE = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/i;
const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}$/;

/** A `data:` URL for an attachment, or "" when the metadata is not usable. */
export function attachmentDataUrl(mime, b64) {
  const type = typeof mime === "string" ? mime.trim() : "";
  const data = typeof b64 === "string" ? b64.replace(/\s+/g, "") : "";
  if (!ATTACHMENT_MIME_RE.test(type) || !BASE64_RE.test(data)) return "";
  return `data:${type};base64,${data}`;
}

/** The attachment's declared MIME type, or "" when it is not a well-formed one. */
export function attachmentMime(mime) {
  const type = typeof mime === "string" ? mime.trim() : "";
  return ATTACHMENT_MIME_RE.test(type) ? type.toLowerCase() : "";
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

// Four modules paint into a message's body (prose rewrites, segmentation, text
// effects, slop marks). The render pass hands them back the same node whenever
// the markup is unchanged, so the way to reach one lives here rather than as a
// selector string copied into each of them.
export function messageBody(msgId) {
  return $("chat-messages")?.querySelector(`.message[data-msg-id="${msgId}"] .msg-body`) ?? null;
}

/**
 * True when `el` sits inside a message body — i.e. inside markup a model wrote.
 *
 * The app's global dispatchers select on attributes (`[data-chat-action]`,
 * `[data-wf-action]`) anywhere in the document, which is fine for chrome the app
 * built and wrong for a bubble. message_html.js already strips every `data-*`
 * from message markup, so nothing should ever reach those dispatchers from in
 * here; this is the second lock, and the one that does not depend on a
 * sanitiser config staying right.
 */
export function fromMessageBody(el) {
  return !!el?.closest?.(".msg-body");
}

export function pinStreamingMessage(el) {
  el.classList.add("stream-scroll-target");
  if (el.isConnected) scrollChatTarget(el, "start");
}

export function avatarUrl(charId) {
  return `/api/characters/${charId}/avatar`;
}

export function personaAvatarUrl(personaId) {
  return `/api/user-personas/${personaId}/avatar`;
}

/** Return a versioned persona portrait URL, or "" when absent. */
export function personaAvatarSrc(persona) {
  return persona?.has_avatar ? `${personaAvatarUrl(persona.id)}?v=${S.personaAvatarVersion}` : "";
}

const HEX_COLOUR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/** Return a safe literal hex colour, or "". */
export function safePersonaColour(colour) {
  return typeof colour === "string" && HEX_COLOUR.test(colour) ? colour : "";
}

/** Return readable ink for a validated hex colour. */
export function readableInk(hex) {
  const h =
    hex.length === 4
      ? hex
          .slice(1)
          .split("")
          .map((c) => c + c)
          .join("")
      : hex.slice(1);
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const lin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) > 0.36 ? "#14201c" : "#f2f5f4";
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

// Protect tags from the inline formatting passes; their attributes are not prose.
const TAG_SLOT_OPEN = "\uFFFC";
const TAG_SLOT_CLOSE = "\uFFFD";
const TAG_SLOT_RE = /\uFFFC(\d+)\uFFFD/g;
const CODE_SLOT_OPEN = "\uFFF9";
const CODE_SLOT_CLOSE = "\uFFFB";
const CODE_SLOT_RE = /\uFFF9(\d+)\uFFFB/g;
const SLOT_CHARS_RE = /[\uFFF9\uFFFB\uFFFC\uFFFD]/g;

/** Remove marker characters supplied by model text. */
function _stripSlots(text) {
  return text.replace(SLOT_CHARS_RE, "");
}

function _protectTags(text) {
  const tags = [];
  const body = text.replace(/<[^>]*>/g, (tag) => `${TAG_SLOT_OPEN}${tags.push(tag) - 1}${TAG_SLOT_CLOSE}`);
  return { body, tags };
}

function _restoreTags(html, tags) {
  return tags.length ? html.replace(TAG_SLOT_RE, (slot, i) => tags[i] ?? slot) : html;
}

/** Protect and escape inline code before applying prose formatting. */
function _protectInlineCode(text) {
  const codes = [];
  const body = text.replace(
    /`([^`]+)`/g,
    (_, code) =>
      `${CODE_SLOT_OPEN}${codes.push(`<code class="inline-code">${esc(code)}</code>`) - 1}${CODE_SLOT_CLOSE}`,
  );
  return { body, codes };
}

function _restoreInlineCode(html, codes) {
  return codes.length ? html.replace(CODE_SLOT_RE, (slot, i) => codes[i] ?? slot) : html;
}

function _applyInlineFormatting(protectedText) {
  let out = protectedText.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*]+?)\*/g, "<em>$1</em>");
  return out.replace(INLINE_QUOTE_RE, '<span class="quoted">$&</span>');
}

/** Emphasis and quotes only — the pass the diff renderer shares with prose. */
function _formatSpan(text) {
  const { body, tags } = _protectTags(_stripSlots(text));
  return _restoreTags(_applyInlineFormatting(body), tags);
}

/** Apply inline code, emphasis, quotes and ATX headings. */
function _formatInline(text) {
  const { body: prose, codes } = _protectInlineCode(_stripSlots(text));
  const { body, tags } = _protectTags(prose);
  let out = _applyInlineFormatting(body);
  out = out.replace(
    /^(#{1,6}) (.+)$/gm,
    (_, hashes, content) => `<strong class="md-h${hashes.length}">${content}</strong>`,
  );
  return _restoreInlineCode(_restoreTags(out, tags), codes);
}

/** Format an editor diff; the result still requires the message HTML pipeline. */
export function formatProseWithDiff(ops) {
  let html = "";
  for (let i = 0; i < ops.length; i++) {
    const op = ops[i];
    if (op.type === "equal") {
      html += _formatSpan(op.text);
    } else if (op.type === "delete") {
      const next = ops[i + 1];
      if (next?.type === "insert") {
        html += `<span class="diff-deleted">${_formatSpan(op.text)}</span>`;
        html += `<span class="diff-change">${_formatSpan(next.text)}</span>`;
        i++; // consume the paired insert
      } else {
        html += `<span class="diff-deleted">${_formatSpan(op.text)}</span>`;
      }
    } else if (op.type === "insert") {
      html += `<span class="diff-change">${_formatSpan(op.text)}</span>`;
    }
  }
  return html;
}

const IMG_LINK_RE = /!\[([^\]]*)\]\((https?:\/\/[^\s)]+\.(?:jpe?g|png|gif|webp))\)/i;

function renderImageEmbed(url, alt) {
  const safeUrl = escAttr(url);
  const safeAlt = escAttr(alt || "");
  return (
    `<details class="msg-image-embed">` +
    `<summary><span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>` +
    `<span class="msg-image-label">🖼️ Image</span></summary>` +
    `<a class="msg-image-link" href="${safeUrl}" target="_blank" rel="noopener">` +
    `<img class="msg-image" src="${safeUrl}" alt="${safeAlt}" loading="lazy">` +
    `</a>` +
    `</details>`
  );
}

// Split out fenced code, style blocks and image embeds before formatting prose.
// An open fence runs to the end so its contents remain escaped code.
const PROSE_PART_RE =
  /(```[\w]*\n?[\s\S]*?```|```[\w]*\n?[\s\S]*$|<style\b[^>]*>[\s\S]*?<\/style\s*>|!\[[^\]]*\]\((?:https?:\/\/[^\s)]+\.(?:jpe?g|png|gif|webp))\))/gi;
const STYLE_BLOCK_RE = /^<style\b[^>]*>([\s\S]*?)<\/style\s*>$/i;
const CLOSED_FENCE_RE = /^```(\w*)(\n)?([\s\S]*?)```$/;
const OPEN_FENCE_RE = /^```(\w*)(\n)?([\s\S]*)$/;

/** Format message text for the browser-side sanitise/layout pipeline. */
export function formatProse(text) {
  if (!text) return "";
  const parts = text.split(PROSE_PART_RE);
  return parts
    .map((part, i) => {
      const imgMatch = part.match(IMG_LINK_RE);
      if (imgMatch) {
        return renderImageEmbed(imgMatch[2], imgMatch[1]);
      }
      const styleMatch = part.match(STYLE_BLOCK_RE);
      if (styleMatch) {
        // Preserve CSS as encoded text until message_html.js can scope it.
        return `<custom-style>${encodeURIComponent(styleMatch[1])}</custom-style>`;
      }
      const codeMatch = part.match(CLOSED_FENCE_RE) || part.match(OPEN_FENCE_RE);
      if (codeMatch) {
        const hasNewline = !!codeMatch[2];
        const lang = hasNewline ? codeMatch[1] : "";
        const code = esc(hasNewline ? codeMatch[3] : codeMatch[1] + codeMatch[3]);
        const langAttr = lang ? ` class="language-${escAttr(lang)}"` : "";
        // The sanitiser strips data attributes; message_html.js rebuilds the bar.
        return `<div class="code-block"><pre><code${langAttr}>${code}</code></pre></div>`;
      }
      let prose = part;
      if (i > 0) prose = prose.replace(/^\n/, ""); // after a code block
      if (i < parts.length - 1) prose = prose.replace(/\n$/, ""); // before a code block
      return _formatInline(prose);
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
