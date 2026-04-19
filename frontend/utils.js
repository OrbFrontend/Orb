import { S } from "./state.js";

export function $(id) {
  return document.getElementById(id);
}

export function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : s;
  return div.innerHTML;
}

export function toast(msg, isError = false) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = "toast" + (isError ? " toast-error" : "");
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3000);
}

export function scrollToBottom() {
  const ct = $("chat-messages");
  if (!ct || !S.autoscrollEnabled) return;
  S._programmaticScroll = true;
  requestAnimationFrame(() => {
    ct.scrollTop = ct.scrollHeight;
    S._programmaticScroll = false;
  });
}

export function scrollToMessage(msgId) {
  const el = document.querySelector(`[data-msg-id="${msgId}"]`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
}

export function avatarUrl(charId) {
  return `/api/characters/${charId}/avatar`;
}

export function convUrl(...parts) {
  return "/conversations/" + parts.join("/");
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

// ── Sentence-level diff

function _tokenizeSentences(text) {
  // Split on sentence boundaries: [.!?] + space before a capital letter, or at newlines.
  // Heuristic avoids splitting on mid-sentence abbreviations like "e.g." because those
  // are rarely followed directly by a capital letter.
  // Each token retains its terminating punctuation; the inter-sentence space stays with
  // the preceding token so round-tripping via join('') reproduces the original.
  const raw = text.split(/(?<=[.!?]) +(?=[A-Z])|(?=\n)|\n+/);
  const tokens = [];
  for (let i = 0; i < raw.length; i++) {
    const t = raw[i];
    if (!t.length) continue;
    // Re-attach one space if the original had an inter-sentence space here
    const needsTrailingSpace = i < raw.length - 1 && !t.endsWith("\n") && raw[i + 1] && !raw[i + 1].startsWith("\n");
    tokens.push(needsTrailingSpace ? t + " " : t);
  }
  return tokens.length > 0 ? tokens : [text];
}

function _lcs(a, b) {
  const m = a.length,
    n = b.length;
  // Restrict matches to a diagonal band to prevent greedy long-distance anchoring.
  // A sentence at position i in `a` can only match a sentence at position j in `b`
  // if they're within `band` slots of each other. Without this, a short sentence that
  // appears in both texts but far apart in relative position would anchor the LCS and
  // cause everything between the two occurrences to show as changed.
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

// Returns merged diff ops: [{type: 'equal'|'insert'|'delete', text}]
// Tokenises at sentence granularity so changed sentences appear as whole units
// rather than fragmented word-level edits.
export function sentenceDiff(oldText, newText) {
  if (!oldText || !newText) return [{ type: "equal", text: newText || "" }];
  return _mergeOps(_lcs(_tokenizeSentences(oldText), _tokenizeSentences(newText)));
}

function _applyInlineFormatting(escaped) {
  escaped = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  escaped = escaped.replace(/\*([^*]+?)\*/g, "<em>$1</em>");
  escaped = escaped.replace(/"([^"]+)"/g, '<span class="quoted">"$1"</span>');
  return escaped.replace(/\u201c([^\u201d]+)\u201d/g, '<span class="quoted">\u201c$1\u201d</span>');
}

// Renders diff ops as HTML with change highlights.
// For delete→insert pairs: shows the old sentence struck-through immediately before
// the new highlighted sentence so both are readable in context.
// Standalone deletes are shown as strikethrough; standalone inserts as highlighted.
export function formatProseWithDiff(ops) {
  let html = "";
  for (let i = 0; i < ops.length; i++) {
    const op = ops[i];
    if (op.type === "equal") {
      html += _applyInlineFormatting(esc(op.text));
    } else if (op.type === "delete") {
      const next = ops[i + 1];
      if (next?.type === "insert") {
        // Paired replacement: show old struck-through, new highlighted side-by-side
        html += `<span class="diff-deleted">${_applyInlineFormatting(esc(op.text))}</span>`;
        html += `<span class="diff-change">${_applyInlineFormatting(esc(next.text))}</span>`;
        i++; // consume the paired insert
      } else {
        // Standalone deletion
        html += `<span class="diff-deleted">${_applyInlineFormatting(esc(op.text))}</span>`;
      }
    } else if (op.type === "insert") {
      // Standalone insertion (not preceded by a delete)
      html += `<span class="diff-change">${_applyInlineFormatting(esc(op.text))}</span>`;
    }
  }
  return html.replace(/\n/g, "<br>");
}

export function formatProse(text) {
  if (!text) return "";
  // Split on fenced code blocks before escaping so we can handle them separately
  const parts = text.split(/(```[\w]*\n?[\s\S]*?```)/g);
  return parts
    .map((part, i) => {
      // Odd-indexed parts are fenced code block matches
      if (i % 2 === 1) {
        const match = part.match(/^```(\w*)\n?([\s\S]*?)```$/);
        if (match) {
          const lang = match[1];
          const code = esc(match[2]);
          const langAttr = lang ? ` class="language-${esc(lang)}"` : "";
          return `<pre><code${langAttr}>${code}</code></pre>`;
        }
      }
      // Strip boundary newlines that would double-up spacing next to <pre> blocks
      let prose = part;
      if (i > 0) prose = prose.replace(/^\n/, ""); // after a code block
      if (i < parts.length - 1) prose = prose.replace(/\n$/, ""); // before a code block
      // Normal prose: apply inline formatting
      // esc() does not affect # or `, so all patterns are applied post-escape
      let escaped = esc(prose);
      escaped = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      escaped = escaped.replace(/\*([^*]+?)\*/g, "<em>$1</em>");
      escaped = escaped.replace(/"([^"]+)"/g, '<span class="quoted">"$1"</span>');
      escaped = escaped.replace(/\u201c([^\u201d]+)\u201d/g, '<span class="quoted">\u201c$1\u201d</span>');
      escaped = escaped.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');
      // Headers applied last so prior patterns don't corrupt the injected HTML attributes
      escaped = escaped.replace(
        /^(#{1,6}) (.+)$/gm,
        (_, hashes, content) => `<strong class="md-h${hashes.length}">${content}</strong>`,
      );
      return escaped.replace(/\n/g, "<br>");
    })
    .join("");
}

/**
 * Replace {{user}} and {{char}} placeholders with actual names.
 * @param {string} text - Input text containing placeholders
 * @param {string} userName - User's name (from settings)
 * @param {string} charName - Character's name (from conversation)
 * @returns {string} Text with placeholders replaced
 */
export function replacePlaceholders(text, userName, charName) {
  if (!text || typeof text !== "string") return text || "";
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
  let userName = S.settings?.user_name || "User";
  if (S.activePersonaId) {
    const activePersona = S.personas.find((p) => p.id === S.activePersonaId);
    if (activePersona && activePersona.name) {
      userName = activePersona.name;
    }
  }
  const conv = S.conversations?.find((c) => c.id === S.activeConvId);
  const charName = conv?.character_name || "";
  return replacePlaceholders(text, userName, charName);
}
