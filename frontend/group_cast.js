import { memberById, S } from "./state.js";
import { avatarCell, avatarUrl, esc, escAttr } from "./utils.js";

// The three durable reply strategies in user language. The stored values stay
// `director` / `round_robin` / `manual` — only the labels are product-facing, so
// this table is the single place the wording lives (rail, reply bar, creation,
// group settings all read it).
export const TURN_MODES = {
  director: { label: "Auto", hint: "Director chooses" },
  round_robin: { label: "Rotate", hint: "Cast replies in order" },
  manual: { label: "Choose", hint: "Select every reply" },
};

export function turnMode(mode) {
  return TURN_MODES[mode] || TURN_MODES.director;
}

// What each speaker is *told* — distinct from TURN_MODES, which decides who
// replies. Stored values stay `private` / `shared` / `swap`; this table is the
// single place the wording lives, including the privacy and prompt-cache
// consequences each mode carries. `billing` is explanatory copy, never a token
// estimate: cached-input discounts, cache retention and tool rendering are all
// endpoint-specific.
//
// No `hint` twin to TURN_MODES': a turn mode's consequence fits in an option
// label, a context mode's does not. The dropdown shows `label` alone and the
// "How character context works" disclosure carries `detail` + `billing` for
// all three at once, so the user compares rather than reads one at a time.
export const CONTEXT_MODES = {
  private: {
    label: "Private perspective",
    detail:
      "Each speaker receives its full card. Every other member contributes only their public profile, so a card's description, personality, examples and instructions never reach another character's turn.",
    billing:
      "Efficient when speakers change: one long shared history stays cached, and only the speaking card is re-sent each turn.",
  },
  shared: {
    label: "Shared dossier",
    detail:
      "Every speaker receives a labelled identity dossier for the whole cast — description, personality and examples for every active member. Members can therefore read one another's card details; nothing is held back between them.",
    billing:
      "Every call carries the whole cast, so the first one is the most expensive. After that it is often the cheapest mode where the provider discounts cached input.",
  },
  swap: {
    label: "Classic card swap",
    detail:
      "Only the active speaker's card is sent, in the conventional single-character layout. Other members appear as names alone — no profiles, no card details.",
    billing:
      "Changing speakers can reduce prompt-cache reuse: the swap sits before the history, so a new speaker re-reads the whole conversation unless the provider keeps a warm branch per character.",
  },
};

export function contextMode(mode) {
  return CONTEXT_MODES[mode] || CONTEXT_MODES.private;
}

// A speaker override is one-shot everywhere except `manual`, where picking the
// speaker *is* the strategy and the choice therefore stays until it is used or
// cleared. Consumers must not re-derive this rule.
export function overrideIsOneShot() {
  return S.groupCast?.turn_mode !== "manual";
}

export function memberFor(msg) {
  return msg?.speaker_member_id ? memberById(msg.speaker_member_id) : null;
}

export function speakerLabel(msg) {
  if (msg?.role === "user") return "You";
  if (!S.groupCast) return S.conversations.find((c) => c.id === S.activeConvId)?.character_name || "Character";
  if (!msg?.speaker_member_id) return "Summary";
  return memberFor(msg)?.display_name || "Unknown speaker";
}

export function eligibleMembers() {
  return (S.groupCast?.members || []).filter((member) => !member.muted);
}

export function overrideMember() {
  return S.pinnedSpeakerId ? memberById(S.pinnedSpeakerId) : null;
}

// "Artus", "Artus and Assistant", "Artus, Assistant, and Vela".
export function joinNames(names) {
  if (!names.length) return "";
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

// Wrapped in its own span so avatarCell's onerror fallback replaces the portrait
// only — its parentElement is this cell, never the chip (which also holds the name).
export function memberAvatar(member) {
  const inner = member.character_card_id
    ? avatarCell(escAttr(avatarUrl(member.character_card_id)), { icon: "👤" })
    : member.member_kind === "narrator"
      ? "✒️"
      : "👤";
  return `<span class="cast-avatar">${inner}</span>`;
}

// Cast chips + the manage affordance. A chip's title states what clicking it
// does, because the selected state means "this member replies next", not
// "this member is selected" in any general sense.
export function castRailHtml() {
  if (!S.groupCast) return "";
  const chips = S.groupCast.members
    .map((member) => {
      const isNext = member.id === S.pinnedSpeakerId;
      const speaking = member.id === S.currentSpeaker?.member_id ? " speaking" : "";
      const title = member.muted
        ? `${member.display_name} — not replying in this scene`
        : isNext
          ? `${member.display_name} replies next — click to clear`
          : `Have ${member.display_name} reply next`;
      return `<button type="button" class="cast-member${isNext ? " next" : ""}${speaking}${member.muted ? " muted" : ""}" data-cast-member-id="${escAttr(member.id)}" aria-pressed="${isNext}" ${member.muted ? "disabled" : ""} title="${escAttr(title)}">${memberAvatar(member)}<span>${esc(member.display_name)}</span></button>`;
    })
    .join("");
  return `${chips}<button type="button" class="cast-manage" data-cast-manage title="Add, reorder, or mute cast members">+ Manage cast</button>`;
}

// The composer's one line of group state: how replies are normally chosen, and —
// only when one is set — who replies next instead.
export function replyBarHtml() {
  if (!S.groupCast) return "";
  const member = overrideMember();
  if (member) {
    const name = esc(member.display_name);
    return `<span class="reply-label">Next reply:</span>
      <span class="reply-override">${name}<button type="button" class="reply-clear" data-clear-override title="Clear the next-reply override" aria-label="Clear the next-reply override">×</button></span>
      <button type="button" class="btn btn-sm" data-speak-now ${S.isStreaming ? "disabled" : ""}>Let ${name} speak now</button>
      ${overrideIsOneShot() ? '<span class="reply-hint">Applies to the next reply only.</span>' : ""}`;
  }
  const mode = turnMode(S.groupCast.turn_mode);
  const needsPick = S.groupCast.turn_mode === "manual";
  return `<span class="reply-label">Next reply:</span>
    <button type="button" class="reply-strategy" data-group-settings title="Change how replies are chosen">${esc(mode.label)} · ${esc(mode.hint)}</button>
    ${needsPick ? '<span class="reply-hint">Pick who replies from the cast above.</span>' : ""}`;
}

// Only a genuinely multi-speaker beat earns the rail: a single planned speaker
// is already announced by its cast chip, and an empty plan (the scene rests)
// is reported as a toast rather than a permanent strip.
export function speakingPlanHtml() {
  if (!S.groupCast || !S.speakingPlan || S.speakingPlan.length < 2) return "";
  return S.speakingPlan
    .map(
      (item, index) =>
        `<span class="plan-pill${index < (S.currentSpeaker?.index ?? -1) ? " done" : ""}${item.member_id === S.currentSpeaker?.member_id ? " active" : ""}">${esc(item.name)}${item.beat ? ` · ${esc(item.beat)}` : ""}</span>`,
    )
    .join("");
}

// A group's empty canvas reads as a ready scene: who is in it, and two quiet
// ways to start. No structural or conversion controls here.
export function sceneEmptyStateHtml() {
  const cast = joinNames(eligibleMembers().map((member) => member.display_name));
  const line = cast ? `Set the scene for ${esc(cast)}.` : "Add a cast member to begin the scene.";
  return `<div class="empty-state">
    <div class="icon">👥</div>
    <div>${line}</div>
    <div class="scene-starters">
      <button type="button" class="scene-starter" data-scene-starter="describe">Describe the opening</button>
      ${cast ? '<button type="button" class="scene-starter" data-scene-starter="character">Let a character begin</button>' : ""}
    </div>
  </div>`;
}
