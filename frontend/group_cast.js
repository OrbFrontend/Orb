import { memberById, S } from "./state.js";
import { avatarCell, avatarUrl, esc, escAttr } from "./utils.js";

// The three durable reply strategies in user language. The stored values stay
// `director` / `round_robin` / `manual` — only the labels are product-facing, so
// this table is the single place the wording lives (creation and group settings
// both read it).
export const TURN_MODES = {
  director: { label: "Auto", hint: "Director chooses" },
  round_robin: { label: "Rotate", hint: "Cast replies in order" },
  manual: { label: "Choose", hint: "Select every reply" },
};

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
      "Each speaker gets its full card; every other member shows only its public profile — description, personality, examples and instructions never reach another character's turn.",
    billing:
      "Efficient when speakers change: one long shared history stays cached, and only the speaking card is re-sent each turn.",
  },
  shared: {
    label: "Shared dossier",
    detail:
      "Every speaker receives a labelled dossier for the whole cast — description, personality and examples for every active member. Members can read one another's card details; nothing is held back.",
    billing:
      "Every call carries the whole cast, so the first is the most expensive — after that, often the cheapest mode where the provider discounts cached input.",
  },
  swap: {
    label: "Classic card swap",
    detail:
      "Only the active speaker's card is sent, in the conventional single-character layout — other members appear as names alone, no profiles or details.",
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

// ── Group families ──────────────────────────────────────────────────────────
// A group is one *family* of conversations, not one conversation: Checkpoint and
// Compress History both fork the scene, and every fork carries the root's id in
// `group_root_id`. A root stores null and keys on itself, so these three are the
// only place that fallback is written — everything downstream keys on the value
// `groupRootId` returns.

export function groupRootId(conv) {
  return conv?.group_root_id || conv?.id || null;
}

// Every conversation in *rootId*'s family, keeping the caller's order. The
// conversation list arrives sorted by last activity, so the first entry is the
// one to reopen and the last is the family's quietest.
export function groupFamily(conversations, rootId) {
  return (conversations || []).filter((conv) => conv.kind === "group" && groupRootId(conv) === rootId);
}

// One entry per group, newest-active first, each carrying its family. The root
// names the group — a checkpoint renaming itself "… (checkpoint)" must not
// rename the group — while the most recently active member supplies the cast
// line and is what a click opens, since rosters may have diverged since the fork.
export function groupFamilies(conversations) {
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
    // A family whose root was deleted mid-session still renders: the newest
    // member stands in until the next list refresh reports the promotion.
    return { rootId, newest: members[0], root: members.find((conv) => conv.id === rootId) || members[0], members };
  });
}

function memberFor(msg) {
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

// A click on a cast chip means one of two things, and the scene decides which:
// on a resting scene it hands that member the floor immediately, and while a
// beat is streaming — or while an unsent draft is waiting for someone to answer
// it — it only *queues* them as the next speaker. This is the single definition
// of that rule; the rail's tooltips and group_setup's click handler both read it
// rather than re-deriving it.
//
// The draft is the caller's to report: this module stays DOM-free.
export function castClickSpeaksNow(hasDraft = false) {
  return !S.isStreaming && !hasDraft;
}

// Cast chips + the manage affordance, sitting directly above the composer. A
// chip's title states what clicking it does, because the pressed state means
// "this member replies next", not "this member is selected" in any general
// sense — and because the same click speaks or queues depending on the scene.
// Only a queueing click can be taken back by clicking again; a resting scene has
// no toggle, because there the click resolves the queue by using it.
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
  return `${chips}<button type="button" class="cast-manage" data-cast-manage title="Add, reorder, or mute cast members">+ Manage cast</button>`;
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
