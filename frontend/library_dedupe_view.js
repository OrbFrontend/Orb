// DOM-free duplicate-finder HTML builders. The controller owns fetching, events and mutations.
import { ARROW_LEFT_ICON, CHEVRON_RIGHT_ICON } from "./icons.js";
import { avatarUrl, esc, escAttr, formatProseWithDiff, formatRelativeDate, sentenceDiff } from "./utils.js";

const COMPARE_FIELDS = [
  ["description", "Description"],
  ["personality", "Personality"],
  ["scenario", "Scenario"],
  ["first_mes", "Opening greeting"],
  ["alternate_greetings", "Alternate greetings"],
  ["tags", "Tags"],
  ["world", "World"],
];

// Assign stable letters per cluster so identically named cards remain distinguishable
// in both the list and comparison controls.
const MARKS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

function markAt(index) {
  return index < MARKS.length ? MARKS[index] : String(index + 1);
}

function cardName(card) {
  return card?.name || "Unnamed character";
}

function cardsById(cards) {
  return new Map((cards || []).map((card) => [card.id, card]));
}

function cardInfo(id, byId) {
  return byId.get(id) || { id, name: "", conversations: 0, last_used_at: null, created_at: "", has_avatar: 0 };
}

function conversationCount(card) {
  const total = Number(card?.conversations) || 0;
  return total ? `${total} conversation${total === 1 ? "" : "s"}` : "No conversations";
}

/** The one line that separates two identical names: how used, and how old. */
function memberMeta(card) {
  const parts = [conversationCount(card)];
  if (card?.last_used_at) parts.push(`last used ${formatRelativeDate(card.last_used_at)}`);
  if (card?.created_at) parts.push(`added ${formatRelativeDate(card.created_at)}`);
  return parts.join(" · ");
}

function markHtml(mark) {
  return `<span class="lib-dupe-mark">${esc(mark)}</span>`;
}

function avatarHtml(card, { alt = "" } = {}) {
  if (!card?.has_avatar) return `<span class="lib-dupe-avatar lib-dupe-no-avatar">No art</span>`;
  return `<img class="lib-dupe-avatar" src="${escAttr(avatarUrl(card.id))}" alt="${escAttr(alt)}">`;
}

/** Order a cluster so the likeliest keeper leads, then label every member. */
function clusterMembers(ids, byId) {
  return (ids || [])
    .map((id) => cardInfo(id, byId))
    .sort(
      (left, right) =>
        (Number(right.conversations) || 0) - (Number(left.conversations) || 0) ||
        String(left.created_at || "").localeCompare(String(right.created_at || "")) ||
        String(left.id).localeCompare(String(right.id)),
    )
    .map((card, index) => ({ ...card, index, mark: markAt(index) }));
}

/** The member with strictly the most conversations, or "" when it is a tie. */
function mostUsedId(members) {
  const [top, next] = members;
  if (!top || !(Number(top.conversations) || 0)) return "";
  return next && Number(next.conversations) === Number(top.conversations) ? "" : top.id;
}

function reasonHtml(reasons) {
  return (reasons || []).map((reason) => `<span class="lib-dupe-reason">${esc(reason)}</span>`).join("");
}

/** One "keep this, drop the rest" choice. The same gesture resolves any cluster size. */
function choiceHtml(card, members, mostUsed) {
  const others = members.filter((member) => member.id !== card.id).map((member) => member.id);
  const chip = card.id === mostUsed ? `<span class="lib-dupe-chip">Most used</span>` : "";
  return `
    <li class="lib-dupe-choice">
      ${avatarHtml(card)}
      <div class="lib-dupe-choice-main">
        <div class="lib-dupe-choice-name">${markHtml(card.mark)}<span class="lib-dupe-choice-label">${esc(cardName(card))}</span>${chip}</div>
        <div class="lib-dupe-choice-meta">${esc(memberMeta(card))}</div>
      </div>
      <button class="btn btn-sm" data-dupe-action="keep-one" data-dupe-keep="${escAttr(card.id)}" data-dupe-remove="${escAttr(others.join(","))}">Keep this one</button>
    </li>`;
}

/**
 * Render pair evidence. `named` identifies edges in clusters larger than two;
 * a two-card cluster already supplies names and dismissal controls.
 */
function pairHtml(pair, marks, named) {
  // The report orders a pair by card id; evidence rows read in cluster order so
  // that every row runs A -> B -> C and the drill-down inherits that same order.
  const [left, right] = [marks.get(pair.a), marks.get(pair.b)].sort((a, b) => (a?.index ?? 0) - (b?.index ?? 0));
  const label = (card) => `${markHtml(card?.mark || "?")}${esc(cardName(card))}`;
  const names = named
    ? `<div class="lib-dupe-pair-names">${label(left)} <span aria-hidden="true">↔</span> ${label(right)}</div>`
    : "";
  const dismiss = named
    ? `<button class="btn btn-sm" data-dupe-action="dismiss-pair" data-dupe-a="${escAttr(left?.id)}" data-dupe-b="${escAttr(right?.id)}">Not duplicates</button>`
    : "";
  return `
    <div class="lib-dupe-pair">
      <div class="lib-dupe-pair-main">
        ${names}
        <div class="lib-dupe-reasons">${reasonHtml(pair.reasons)}</div>
      </div>
      <div class="lib-dupe-pair-actions">
        <button class="btn btn-sm" data-dupe-action="compare" data-dupe-a="${escAttr(left?.id)}" data-dupe-b="${escAttr(right?.id)}" data-dupe-mark-a="${escAttr(left?.mark || "A")}" data-dupe-mark-b="${escAttr(right?.mark || "B")}">Compare</button>
        ${dismiss}
      </div>
    </div>`;
}

/** Title a cluster by its shared name when it has one; that is the reader's cue. */
function clusterTitle(members) {
  const names = new Set(members.map((card) => cardName(card)));
  const [only] = [...names];
  return names.size === 1 ? `${members.length} copies of “${only}”` : `${members.length} likely copies`;
}

/** Render strong groups and possible pairs with the same review controls. */
function clusterHtml(ids, pairs, tier, byId) {
  const members = clusterMembers(ids, byId);
  const marks = new Map(members.map((card) => [card.id, card]));
  const mostUsed = mostUsedId(members);
  const strong = tier !== "possible";
  const evidence = pairs || [];
  const count = evidence.length > 1 ? ` · ${evidence.length} comparisons` : "";
  const why = evidence.length
    ? `<details class="lib-dupe-evidence"${evidence.length === 1 ? " open" : ""}>
        <summary>${CHEVRON_RIGHT_ICON}<span>Why these matched${count}</span></summary>
        <div class="lib-dupe-pairs">${evidence.map((pair) => pairHtml(pair, marks, members.length > 2)).join("")}</div>
      </details>`
    : "";
  return `
    <article class="lib-dupe-group">
      <header class="lib-dupe-group-head">
        <div class="lib-dupe-badge${strong ? "" : " is-possible"}">${strong ? "Strong match" : "Possible match"}</div>
        <div class="lib-dupe-group-titlerow">
          <div class="lib-dupe-group-title">${esc(clusterTitle(members))}</div>
          <button class="btn btn-sm" data-dupe-action="dismiss-group" data-dupe-cards="${escAttr(members.map((card) => card.id).join(","))}">Not duplicates</button>
        </div>
      </header>
      <ul class="lib-dupe-choices">${members.map((card) => choiceHtml(card, members, mostUsed)).join("")}</ul>
      ${why}
    </article>`;
}

/** Render every strong connected component with the evidence that formed it. */
export function strongGroupsHtml(groups = [], cards = []) {
  const byId = cardsById(cards);
  return groups.map((group) => clusterHtml(group.cards || [], group.pairs || [], "strong", byId)).join("");
}

/** Render ungrouped weak evidence. Possible pairs never imply a three-way group. */
export function possiblePairsHtml(pairs = [], cards = []) {
  const byId = cardsById(cards);
  return pairs.map((pair) => clusterHtml([pair.a, pair.b], [pair], "possible", byId)).join("");
}

/** Separate strong groups from weak pairs. Empty results leave reporting to the status line. */
export function duplicateResultsHtml(report) {
  const groups = report?.groups || [];
  const pairs = report?.pairs || [];
  if (!groups.length && !pairs.length) return "";
  const caveat = pairs.length ? " Possible matches share only some signals — compare those first." : "";
  const lede = `Keeping a copy deletes the others in that match and moves their conversations to the one you keep.${caveat}`;
  return `<p class="lib-manager-note">${lede}</p>${strongGroupsHtml(groups, report?.cards)}${possiblePairsHtml(pairs, report?.cards)}`;
}

function valueFor(cardView, key) {
  const card = cardView?.card || {};
  if (key === "alternate_greetings" || key === "tags") return Array.isArray(card[key]) ? card[key].join("\n") : "";
  if (key === "world") return cardView?.world_name || "";
  return String(card[key] || "");
}

/**
 * Render a unified field diff: shared text is plain, A-only struck, B-only highlighted.
 * Escape values first; formatProseWithDiff does not escape input.
 */
function fieldDiff(a, b) {
  const before = esc(a || "");
  const after = esc(b || "");
  if (!before && !after) return { state: "empty", html: "" };
  if (before === after) return { state: "same", html: formatProseWithDiff([{ type: "equal", text: before }]) };
  // sentenceDiff reports a one-sided value as unchanged text, which would hide
  // the only difference there is, so each of those is marked here instead.
  if (!before) return { state: "changed", html: formatProseWithDiff([{ type: "insert", text: after }]) };
  if (!after) return { state: "changed", html: formatProseWithDiff([{ type: "delete", text: before }]) };
  return { state: "changed", html: formatProseWithDiff(sentenceDiff(before, after)) };
}

const FIELD_TAGS = { changed: "changed", same: "identical", empty: "empty on both" };

/** One collapsible field. Only the fields that actually differ are open on arrival. */
function fieldHtml(label, diff) {
  const head = `<span class="lib-dupe-field-name">${esc(label)}</span><span class="lib-dupe-field-tag is-${diff.state}">${FIELD_TAGS[diff.state]}</span>`;
  // Keep empty fields as plain rows, retaining the chevron column for alignment.
  if (diff.state === "empty") {
    return `<div class="lib-dupe-field is-empty"><div class="lib-dupe-field-head"><span class="lib-dupe-field-mark"></span>${head}</div></div>`;
  }
  return `<details class="lib-dupe-field"${diff.state === "changed" ? " open" : ""}><summary class="lib-dupe-field-head"><span class="lib-dupe-field-mark">${CHEVRON_RIGHT_ICON}</span>${head}</summary><div class="lib-dupe-field-body">${diff.html}</div></details>`;
}

/** Fold a compare payload into the same shape the cluster list labels members with. */
function compareMember(cardView, mark) {
  const card = cardView?.card || {};
  return {
    id: card.id,
    name: card.name,
    mark,
    has_avatar: card.has_avatar ? 1 : 0,
    created_at: card.created_at || "",
    conversations: Number(cardView?.activity?.total) || 0,
    last_used_at: cardView?.activity?.last_used_at || null,
  };
}

/** The keeper button, captioned with the facts that tell two same-named cards apart. */
function keepButton(member, other, accent) {
  return `
    <button class="btn${accent ? " btn-accent" : ""} lib-dupe-keep" data-dupe-action="resolve" data-dupe-keep="${escAttr(member.id)}" data-dupe-remove="${escAttr(other.id)}">
      <span class="lib-dupe-keep-title">${markHtml(member.mark)}Keep this one</span>
      <span class="lib-dupe-keep-sub">${esc(memberMeta(member))}</span>
    </button>`;
}

/** Build the inline, two-card comparison. It deliberately is not a sub-modal. */
export function compareHtml(compare, marks = {}) {
  const left = compare?.a;
  const right = compare?.b;
  if (!left || !right) return "";
  const leftMember = compareMember(left, marks.a || "A");
  const rightMember = compareMember(right, marks.b || "B");
  const mostUsed = mostUsedId([leftMember, rightMember].sort((a, b) => b.conversations - a.conversations));
  const diffs = COMPARE_FIELDS.map(([key, label]) => [label, fieldDiff(valueFor(left, key), valueFor(right, key))]);
  const changed = diffs.filter(([, diff]) => diff.state === "changed").length;
  const collision = Number(compare.shared_group_collisions) || 0;
  const collisionNote = collision
    ? `<div class="lib-dupe-collision-note">${collision} shared group conversation${collision === 1 ? " already has" : "s already have"} both cards. Relinking drops the redundant slot.</div>`
    : "";
  // Only the portrait and the letter->name binding the legend and diff rely on. How used and how old each copy is
  // belongs on the keeper buttons, at the point of decision, rather than being restated at both ends of a long diff.
  const head = (member, cardView) => `
    <div class="lib-dupe-compare-card">
      ${avatarHtml(member, { alt: `${cardName(cardView?.card)} avatar` })}
      <strong>${markHtml(member.mark)}<span class="lib-dupe-choice-label">${esc(cardName(cardView?.card))}</span></strong>
    </div>`;
  return `
    <section class="lib-dupe-compare">
      <div class="lib-dupe-compare-head">
        <button class="btn btn-sm" data-dupe-action="back-results">${ARROW_LEFT_ICON}Results</button>
        <span class="lib-dupe-section-label">Compare duplicates</span>
      </div>
      <div class="lib-dupe-compare-cards">${head(leftMember, left)}${head(rightMember, right)}</div>
      ${collisionNote}
      <div class="lib-dupe-diff-head">
        <span>${changed ? `${changed} of ${diffs.length} fields differ` : "Every field is identical"}</span>
        <span class="lib-dupe-legend"><span class="diff-deleted">only in ${markHtml(leftMember.mark)}</span><span class="diff-change">only in ${markHtml(rightMember.mark)}</span></span>
      </div>
      <div class="lib-dupe-fields">${diffs.map(([label, diff]) => fieldHtml(label, diff)).join("")}</div>
      <p class="lib-manager-note">Keeping one card deletes the other. Any conversations move to the card you keep.</p>
      <div class="lib-dupe-resolve-actions">
        ${keepButton(leftMember, rightMember, leftMember.id === mostUsed)}
        ${keepButton(rightMember, leftMember, rightMember.id === mostUsed)}
      </div>
    </section>`;
}

function combinations(ids) {
  const pairs = [];
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) pairs.push([ids[i], ids[j]]);
  }
  return pairs;
}

export { combinations, memberMeta };
