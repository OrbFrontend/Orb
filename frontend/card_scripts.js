import { charactersView, S } from "./state.js";
import { resolvePlaceholders } from "./utils.js";

const MAX_TEXT_LENGTH = 100_000;

/** Apply the list endpoint's display-only projection before HTML memoization. */
export function applyCardScripts(text, scripts, role) {
  const placement = { user: 1, assistant: 2 }[role];
  if (!placement || !Array.isArray(scripts) || text.length > MAX_TEXT_LENGTH) return text;
  const original = text;
  for (const script of scripts.slice(0, 50)) {
    if (!script || script.disabled || !Array.isArray(script.placement) || !script.placement.includes(placement))
      continue;
    if (script.promptOnly && !script.markdownOnly) continue;
    let source = script.findRegex;
    if (typeof source !== "string" || !source || source.length > 4096) continue;
    const replacement = script.replaceString ?? "";
    if (typeof replacement !== "string") continue;
    let flags = "g";
    if (source.startsWith("/")) {
      const end = source.lastIndexOf("/");
      if (end === 0) continue;
      flags = source.slice(end + 1);
      source = source.slice(1, end);
    }
    if (/[^gimsu]/.test(flags)) continue;
    try {
      text = text.replace(new RegExp(source, flags), replacement);
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
