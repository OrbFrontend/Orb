import { S } from "./state.js";
import {
  avatarCell,
  avatarUrl,
  effectivePersonaId,
  esc,
  escAttr,
  escHandlerArg,
  personaAvatarSrc,
  readableInk,
  safePersonaColour,
} from "./utils.js";

export const TURN_MODES = {
  director: { label: "Auto", hint: "Director chooses" },
  round_robin: { label: "Rotate", hint: "Cast replies in order" },
  manual: { label: "Manual", hint: "Select every reply" },
};

export const CONTEXT_MODES = {
  private: {
    label: "Private perspective",
    detail: "Each speaker gets its full card appended at tail of prompt; other members only know its public profile.",
    billing:
      "Efficient when speakers change: one long shared history stays cached, and only the speaking card is re-sent each turn.",
  },
  shared: {
    label: "Shared dossier",
    detail:
      "Every speaker receives a labelled dossier for the whole cast — description, personality and examples for every active member. Members can read one another's card details. Risk leaking secrets.",
    billing:
      "Every call carries the whole cast, so the first is the most expensive — after that, often the cheapest mode where the provider discounts cached input.",
  },
  swap: {
    label: "Classic card swap",
    detail:
      "Only the active speaker's card is sent, in the conventional single-character layout — other members are known by their public profile, never their card details.",
    billing: "Every speaker has its own cache lane that will be fully billed.",
  },
};

export function contextMode(mode) {
  return CONTEXT_MODES[mode] || CONTEXT_MODES.private;
}

const CHARS_PER_TOKEN = 4;

const SWAP_MAX_CAST = 3;

const SWAP_TOKENS_PER_MEMBER = 500;

const MIN_CAST_FOR_ADVICE = 2;

export function cardDefTokens(card) {
  const chars = Number(card?.def_chars);
  return Number.isFinite(chars) && chars > 0 ? Math.round(chars / CHARS_PER_TOKEN) : 0;
}

export function recommendContextMode(cards) {
  const chosen = (cards || []).filter(Boolean);
  const cast = chosen.length;
  if (cast < MIN_CAST_FOR_ADVICE) return null;
  const weights = chosen.map(cardDefTokens);
  const meanTokens = Math.round(weights.reduce((sum, value) => sum + value, 0) / cast);
  const threshold = SWAP_TOKENS_PER_MEMBER * (cast - 1);
  const swapFits = cast <= SWAP_MAX_CAST;
  const weight = `${cast} characters averaging about ${meanTokens.toLocaleString()} tokens of card text.`;

  if (swapFits && meanTokens >= threshold) {
    return {
      mode: "swap",
      cast,
      meanTokens,
      threshold,
      why: `${weight} Cards this heavy are worth caching: Classic card swap puts the speaking card ahead of the history, where it is read once per character instead of re-sent on every reply.`,
      cost: "The trade: every character holds a cached branch of its own, so a fourth cast member would flip this back.",
    };
  }
  return {
    mode: "private",
    cast,
    meanTokens,
    threshold,
    why: swapFits
      ? `${weight} Cards this light cost less re-sent each reply than they would holding a separate cached branch per character.`
      : `${cast} characters is a wide cast. Classic card swap would need a warm cache branch for each of them; Private perspective keeps every speaker on one shared branch, and its cost does not grow with the cast.`,
    cost: "The trade: the speaking card is re-read after the history on every reply, so cost tracks card size rather than cast size.",
  };
}

export function restNotice() {
  return S.groupCast?.turn_mode === "manual"
    ? "Sent. Click a cast member to choose who answers."
    : "The scene rests — nobody replies to that.";
}

export function unansweredHint() {
  return S.groupCast?.turn_mode === "manual"
    ? "Nobody has answered that yet — click a cast member to give them the floor."
    : "Nobody has answered that yet — press Send with an empty box to continue from it.";
}

export function overrideIsOneShot() {
  return S.groupCast?.turn_mode !== "manual";
}

export function groupRootId(conv) {
  return conv?.group_root_id || conv?.id || null;
}

export function groupFamily(conversations, rootId) {
  return (conversations || []).filter((conv) => conv.kind === "group" && groupRootId(conv) === rootId);
}

export function groupFamilies(conversations, openId = null) {
  const order = [];
  const byRoot = new Map();
  for (const conv of conversations || []) {
    if (conv.kind !== "group") continue;
    const rootId = groupRootId(conv);
    if (!byRoot.has(rootId)) {
      byRoot.set(rootId, []);
      order.push(rootId);
    }
    byRoot.get(rootId).push(conv);
  }
  return order.map((rootId) => {
    const members = byRoot.get(rootId);
    const shown = (openId && members.find((conv) => conv.id === openId)) || members[0];
    const root = members.find((conv) => conv.id === rootId) || members[0];
    return { rootId, root, shown, open: shown.id === openId, members };
  });
}

export const GROUP_LIMIT = 5;

export function visibleGroups(families, { query = "", expanded = false } = {}) {
  const q = query.trim().toLowerCase();
  if (q) {
    const match = ({ root, shown }) =>
      (root.title || "").toLowerCase().includes(q) ||
      (shown.group_member_names || []).some((name) => (name || "").toLowerCase().includes(q));
    return { shown: families.filter(match), hidden: 0 };
  }
  if (expanded || families.length <= GROUP_LIMIT) return { shown: families, hidden: 0 };
  const head = families.slice(0, GROUP_LIMIT);
  if (!head.some((family) => family.open)) {
    const openFamily = families.find((family) => family.open);
    if (openFamily) head[head.length - 1] = openFamily;
  }
  return { shown: head, hidden: families.length - head.length };
}

export function speakerLabel(msg) {
  if (msg?.role === "user") return "You";
  if (!S.groupCast) return S.conversations.find((c) => c.id === S.activeConvId)?.character_name || "Character";
  if (!msg?.speaker_member_id) return "Summary";
  return S.groupCast.speakerNames?.get(msg.speaker_member_id) || "Unknown speaker";
}

// The attributes renderUserAttachments already puts on images inside .message.
// The portrait is decorative -- the speaker's name is in .msg-role right beside
// it -- so alt stays empty rather than repeating that name to a screen reader.
const AVATAR_IMG_ATTRS = 'loading="lazy" decoding="async" alt=""';

const NARRATOR_AVATAR = "\u2712\ufe0f";
const GENERIC_AVATAR = "\ud83d\udc64";

/** The portrait for one rendered message, as the inner HTML of `.msg-avatar`.
 *
 * Resolution follows speakerLabel(): a user message shows the persona that is
 * actually in force (conversation pin -> card pin -> global default, the order
 * the backend's predicates use), a group reply shows its own speaker's card,
 * and a solo reply shows the conversation's card. Anything with no card behind
 * it -- a narrator line, a group summary -- falls back to a glyph. */
function messagePersona() {
  return S.personas?.find((p) => p.id === effectivePersonaId()) || null;
}

export function speakerAvatar(msg) {
  if (msg?.role === "user") {
    const persona = messagePersona();
    const src = personaAvatarSrc(persona);
    const initial = persona?.name?.charAt(0).toUpperCase() || "";
    if (src)
      return avatarCell(escAttr(src), { icon: escHandlerArg(initial) || GENERIC_AVATAR, attrs: AVATAR_IMG_ATTRS });
    return initial ? esc(initial) : GENERIC_AVATAR;
  }
  const cardId = S.groupCast
    ? S.groupCast.members?.find((m) => m.id === msg?.speaker_member_id)?.character_card_id
    : S.conversations?.find((c) => c.id === S.activeConvId)?.character_card_id;
  if (cardId) return avatarCell(escAttr(avatarUrl(cardId)), { icon: GENERIC_AVATAR, attrs: AVATAR_IMG_ATTRS });
  // No card: a narrator member speaking, or a group summary with no speaker.
  const member = S.groupCast?.members?.find((m) => m.id === msg?.speaker_member_id);
  return member?.member_kind === "narrator" ? NARRATOR_AVATAR : GENERIC_AVATAR;
}

/** The gutter cell itself.
 *
 * Emitted for every theme, since a theme is one swapped stylesheet and cannot
 * add DOM of its own -- what it can do is restyle `.msg-avatar`. The callers
 * gate it on the "Show avatars in chat" setting rather than always emitting it,
 * so an install with the gutter off issues no portrait requests at all;
 * `#chat-messages[data-avatars="on"]` carries the matching layout rules.
 *
 * A user message falling back to its initial wears the persona's own colour, so
 * the chip reads as the same identity the persona picker shows. An image fills
 * the chip and a glyph has no persona behind it, so neither is tinted. */
export function speakerAvatarCell(msg) {
  const inner = speakerAvatar(msg);
  let style = "";
  if (msg?.role === "user" && !inner.startsWith("<img")) {
    const colour = safePersonaColour(messagePersona()?.avatar_color);
    if (colour) style = ` style="background:${colour};color:${readableInk(colour)}"`;
  }
  return `<div class="msg-avatar"${style}>${inner}</div>`;
}

export function eligibleMembers() {
  return (S.groupCast?.members || []).filter((member) => !member.muted);
}

export function joinNames(names) {
  if (!names.length) return "";
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

export function memberAvatar(member) {
  const inner = member.character_card_id
    ? avatarCell(escAttr(avatarUrl(member.character_card_id)), { icon: "👤" })
    : member.member_kind === "narrator"
      ? "✒️"
      : "👤";
  return `<span class="cast-avatar">${inner}</span>`;
}

export function castClickSpeaksNow(hasDraft = false) {
  return !S.isStreaming && !hasDraft;
}

export function castRailHtml({ hasDraft = false } = {}) {
  if (!S.groupCast) return "";
  const speaksNow = castClickSpeaksNow(hasDraft);
  const chips = S.groupCast.members
    .map((member) => {
      const isNext = member.id === S.pinnedSpeakerId;
      const speaking = member.id === S.currentSpeaker?.member_id ? " speaking" : "";
      const title = member.muted
        ? `${member.display_name} — not replying in this scene`
        : speaksNow
          ? `Give ${member.display_name} the floor now`
          : isNext
            ? `${member.display_name} is up next — click to clear`
            : `Queue ${member.display_name} to reply next`;
      return `<button type="button" class="cast-member${isNext ? " next" : ""}${speaking}${member.muted ? " muted" : ""}" data-cast-member-id="${escAttr(member.id)}" aria-pressed="${isNext}" ${member.muted ? "disabled" : ""} title="${escAttr(title)}">${memberAvatar(member)}<span>${esc(member.display_name)}</span></button>`;
    })
    .join("");
  const staged = (S.groupCast.sheet_proposals || []).length;
  const badge = staged ? `<span class="cast-manage-badge">${staged}</span>` : "";
  const manageTitle = staged
    ? `Cast and scene settings — ${staged} sheet update${staged === 1 ? "" : "s"} to review`
    : "Cast and scene settings";
  return `${chips}<button type="button" class="cast-manage" data-cast-manage title="${escAttr(manageTitle)}">+ Manage cast${badge}</button>`;
}

export function speakingPlanHtml() {
  if (!S.groupCast || !S.speakingPlan || S.speakingPlan.length < 2) return "";
  return S.speakingPlan
    .map(
      (item, index) =>
        `<span class="plan-pill${index < (S.currentSpeaker?.index ?? -1) ? " done" : ""}${item.member_id === S.currentSpeaker?.member_id ? " active" : ""}">${esc(item.name)}${item.cue ? ` · ${esc(item.cue)}` : ""}</span>`,
    )
    .join("");
}

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
