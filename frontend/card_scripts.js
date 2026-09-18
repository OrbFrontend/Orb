import { charactersView, S } from "./state.js";
import { resolvePlaceholders } from "./utils.js";

const MAX_TEXT_LENGTH = 100_000;
const JS_FLAGS = /^(?!.*?(.).*?\1)[gmixXsuUAJ]+$/; // Flag-shaped to the original engine's parser.
const ENGINE_FLAGS = /^[gimsu]*$/; // Flag-shaped and honoured here.
const TOKEN = /\$(?:[$&`']|<[^>]*>|[0-9]{1,2})/g;

/** Compile a JavaScript pattern literal, or the whole string when it is not one. */
function compilePattern(source) {
  let pattern = source;
  let flags = "";
  const end = source.startsWith("/") ? source.lastIndexOf("/") : 0;
  const declared = end > 0 ? source.slice(end + 1) : "";
  if (end > 0 && (!declared || JS_FLAGS.test(declared))) {
    // A pattern that is not a well-formed literal is its own pattern, matching
    // only its first occurrence, as `new RegExp(string)` does.
    pattern = source.slice(1, end);
    flags = declared;
  }
  if (!ENGINE_FLAGS.test(flags)) return null;
  return new RegExp(pattern, flags);
}

/** Expand replacement tokens identically to `core/card_scripts.py:_replacement`. */
function expandReplacement(template, captures, groups, offset, source) {
  const expanded = template.replace(/\{\{match\}\}/gi, "$0").replace(TOKEN, (token) => {
    const key = token.slice(1);
    if (key === "$") return "$";
    if (key === "&") return captures[0];
    if (key === "`") return source.slice(0, offset);
    if (key === "'") return source.slice(offset + captures[0].length);
    if (key.startsWith("<")) return groups?.[key.slice(1, -1)] ?? "";
    const index = Number(key);
    if (index === 0) return captures[0];
    if (index > 0 && index < captures.length) return captures[index] ?? "";
    if (key.length === 2 && Number(key[0]) > 0 && Number(key[0]) < captures.length)
      return (captures[Number(key[0])] ?? "") + key[1];
    return token;
  });
  return expanded.includes("{{") ? resolvePlaceholders(expanded) : expanded;
}

/** Apply the list endpoint's display-only projection before HTML memoization. */
export function applyCardScripts(text, scripts, role) {
  const placement = { user: 1, assistant: 2 }[role];
  if (!placement || !Array.isArray(scripts) || text.length > MAX_TEXT_LENGTH) return text;
  const original = text;
  for (const script of scripts.slice(0, 50)) {
    if (!script || script.disabled || !Array.isArray(script.placement) || !script.placement.includes(placement))
      continue;
    if (script.promptOnly && !script.markdownOnly) continue;
    const source = script.findRegex;
    if (typeof source !== "string" || !source || source.length > 4096) continue;
    const replacement = script.replaceString ?? "";
    if (typeof replacement !== "string") continue;
    try {
      const pattern = compilePattern(source);
      if (!pattern) throw new SyntaxError("unsupported regex flags");
      text = text.replace(pattern, (...args) => {
        const named = typeof args.at(-1) === "object" ? args.at(-1) : undefined;
        const tail = named ? 3 : 2;
        return expandReplacement(replacement, args.slice(0, -tail), named, args.at(-tail), args.at(-tail + 1));
      });
    } catch {
      console.warn("Ignoring invalid or unsupported card regex");
    }
    if (text.length > MAX_TEXT_LENGTH) return original;
  }
  return text;
}

/** CSS stays inside the existing message sanitizer and per-message CSS scope. */
export function projectCardDisplay(text, card, role) {
  text = applyCardScripts(text, card?.display_scripts, role);
  const css = card?.display_css;
  if (role === "assistant" && typeof css === "string" && css.trim()) {
    // Prevent a stylesheet field from terminating its element and adding prose.
    text = `<style>${css.replace(/<\/style/gi, "<\\/style")}</style>\n${text}`;
  }
  return text;
}

export function messageDisplaySource(message) {
  const conv = S.conversations?.find((c) => c.id === S.activeConvId);
  const cardId = S.groupCast
    ? (S.groupCast.speakerCardIds?.get(message.speaker_member_id) ??
      S.groupCast.members?.find((m) => m.id === message.speaker_member_id)?.character_card_id)
    : conv?.character_card_id;
  const card = cardId ? charactersView().find((c) => c.id === cardId) : null;
  return projectCardDisplay(resolvePlaceholders(message.content || ""), card, message.role);
}
