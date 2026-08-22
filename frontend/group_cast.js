import { S } from "./state.js";
import { avatarCell, avatarUrl, esc, escAttr } from "./utils.js";

// The three durable reply strategies in user language. The stored values stay
// `director` / `round_robin` / `manual` — only the labels are product-facing, so
// this table is the single place the wording lives (creation and group settings
// both read it).
export const TURN_MODES = {
  director: { label: "Auto", hint: "Director chooses" },
  round_robin: { label: "Rotate", hint: "Cast replies in order" },
  manual: { label: "Manual", hint: "Select every reply" },
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
      "Only the active speaker's card is sent, in the conventional single-character layout — other members appear as names alone, no profiles or details.",
    billing: "Every speaker has its own cache lane that will be fully billed.",
  },
};

export function contextMode(mode) {
  return CONTEXT_MODES[mode] || CONTEXT_MODES.private;
}

// ── Context-mode recommendation ─────────────────────────────────────────────
// New Group Chat can answer "which context mode for *this* cast?" before a
// scene exists, because the answer turns on two things it already knows: how
// many characters are in it and how heavy their cards are.
//
// The two modes fail in opposite directions, and that is the whole rule:
//
//   Private perspective keeps the shared body tiny (one public profile per
//   member) but puts the speaking card in the trailing message, *after* the
//   history — the one place a prefix cache can never reach. So the speaker's
//   card is re-read on every writer and editor call, forever. Its cost tracks
//   CARD SIZE and barely moves with cast size.
//
//   Classic card swap parks the speaking card in the cached body *before* the
//   history, so a character's card is read once and then reused across turns —
//   but each character is then its own cache lineage, and the server holds only
//   so many. Its cost tracks CAST SIZE and barely moves with card size.
//
// Simulated over 30-exchange, three-pass sessions (director → writer → editor)
// against these same renderers: swap wins iff the cast is narrow enough to keep
// a branch per character warm and the cards are heavy enough to be worth
// caching. The boundary lands on `mean >= 500 * (cast - 1)`, and the cast cap
// is where a server with several cache lanes starts thrashing — swap needs
// roughly 2.5 lanes per member (measured: 5 lanes at 2 members, 8 at 3, 10 at
// 4), so a fourth member is where the branches stop fitting and swap's cost
// jumps 4-6x rather than drifting.
//
// The rule is therefore deliberately asymmetric. Every case it gets wrong, it
// gets wrong toward Private: recommending swap on a cast too wide for the cache
// costs multiples, while recommending private where swap was marginally better
// costs at most ~1.3x. Private is also the safer default on meaning — it is the
// only mode with a privacy boundary, and the only one where characters know
// anything about each other beyond names.

// Mirrors CHARS_PER_TOKEN in backend/core/utils.py. Both are the same rough
// estimate; this one only ever has to be right enough to pick a side of a
// boundary that is itself a heuristic.
const CHARS_PER_TOKEN = 4;

// Beyond this many members, swap needs more warm cache branches than a server
// keeps and its cost stops being competitive at any card size.
const SWAP_MAX_CAST = 3;

// Mean card weight, per member past the first, at which parking one card in the
// cache beats re-sending it after the history every call.
const SWAP_TOKENS_PER_MEMBER = 500;

// A recommendation is about a *cast*, and one character is not one. Two reasons
// to stay quiet below this, and the second is the load-bearing one:
//
//   The threshold is `500 * (cast - 1)`, which at one member is zero — so every
//   card, down to an eight-token stub, cleared it and got told it was "heavy
//   enough to cache". The rule has nothing to say about a scene with no second
//   character to weigh against.
//
//   And the panel recomputes on every pick. Advising at one member means
//   answering for a cast the user is still assembling, flipping as they click.
//   The first card is never the whole answer, so it is not worth an answer.
//
// A genuine one-member group does leave measured savings on the table (swap
// caches that single card instead of re-sending it). It is also a solo chat
// with extra steps, and Orb has those.
const MIN_CAST_FOR_ADVICE = 2;

// A card's weight in the only fields the two modes disagree about. `def_chars`
// is summed server-side (description + personality + mes_example) precisely so
// the library list can stay free of card bodies; a card list fetched before
// that field existed reads as 0, which lands on Private — the default, and the
// right answer for a cast with no card text to cache.
export function cardDefTokens(card) {
  const chars = Number(card?.def_chars);
  return Number.isFinite(chars) && chars > 0 ? Math.round(chars / CHARS_PER_TOKEN) : 0;
}

// The recommendation for a chosen cast, or null when nothing is chosen yet.
// Pure: takes the card rows the picker is holding, returns the mode plus the
// two figures and the sentences the UI renders. Callers never re-derive the
// rule — `mode` is the answer and `why`/`cost` are how it is explained.
export function recommendContextMode(cards) {
  const chosen = (cards || []).filter(Boolean);
  const cast = chosen.length;
  if (cast < MIN_CAST_FOR_ADVICE) return null;
  const weights = chosen.map(cardDefTokens);
  const meanTokens = Math.round(weights.reduce((sum, value) => sum + value, 0) / cast);
  // At two members and up this is never below SWAP_TOKENS_PER_MEMBER, so a cast
  // of empty or near-empty cards falls to Private on the comparison alone and
  // needs no separate floor.
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
      cost: "The trade: characters see only each other's names, never a profile.",
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
    cost: "Every member still sees the others' public profiles, and no card details leak between them.",
  };
}

// Why an exchange produced no reply, in the words of the mode that produced it. A
// rest under `Manual` is the user's own doing — they sent without naming anyone
// — so it reads as the next step rather than as the scene declining to answer.
export function restNotice() {
  return S.groupCast?.turn_mode === "manual"
    ? "Sent. Click a cast member to choose who answers."
    : "The scene rests — nobody replies to that.";
}

// How to get an unanswered user message answered — the state a rest leaves
// behind. `Manual` answers it with a cast chip, and an empty Send would only
// rest again; every other scene, solo or group, answers it with that empty Send.
// One sentence, one owner: the composer must not re-derive the rule.
export function unansweredHint() {
  return S.groupCast?.turn_mode === "manual"
    ? "Nobody has answered that yet — click a cast member to give them the floor."
    : "Nobody has answered that yet — press Send with an empty box to continue from it.";
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

// Resolved through `speakerNames`, never through `members`: `members` is the
// active roster, so a reply written by a member the user has since removed
// would fall through to "Unknown speaker" and let a roster edit silently
// rewrite the transcript. The backend refuses the same shortcut in
// `get_speaker_names` — this is that rule on the other side of the wire.
export function speakerLabel(msg) {
  if (msg?.role === "user") return "You";
  if (!S.groupCast) return S.conversations.find((c) => c.id === S.activeConvId)?.character_name || "Character";
  if (!msg?.speaker_member_id) return "Summary";
  return S.groupCast.speakerNames?.get(msg.speaker_member_id) || "Unknown speaker";
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
// exchange is streaming — or while an unsent draft is waiting for someone to answer
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
  // Staged sheet updates are reviewed on this modal's Cast tab, so without a
  // count on the button they are invisible — and a proposal nobody notices never
  // gets applied, which would make the whole pass pointless.
  const staged = (S.groupCast.sheet_proposals || []).length;
  const badge = staged ? `<span class="cast-manage-badge">${staged}</span>` : "";
  // The scene's only setup affordance now that the header ••• is gone, so the
  // title names both tabs — the button's own label can only name one of them.
  const manageTitle = staged
    ? `Cast and scene settings — ${staged} sheet update${staged === 1 ? "" : "s"} to review`
    : "Cast and scene settings";
  return `${chips}<button type="button" class="cast-manage" data-cast-manage title="${escAttr(manageTitle)}">+ Manage cast${badge}</button>`;
}

// Only a genuinely multi-speaker exchange earns the rail: a single planned speaker
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
