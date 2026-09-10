// Pure HTML builders for the Character Library duplicate finder.

// This stays DOM-free so result grouping and side-by-side rendering are testable
// under node --test. The L5 controller owns fetching, event wiring, and mutation.
import { esc, escAttr, formatProseWithDiff, sentenceDiff } from "./utils.js";

const COMPARE_FIELDS = [
  ["description", "Description"],
  ["personality", "Personality"],
  ["scenario", "Scenario"],
  ["first_mes", "Opening greeting"],
  ["alternate_greetings", "Alternate greetings"],
  ["tags", "Tags"],
  ["world", "World"],
];

function cardName(id, names) {
  return names.get(id) || "Unnamed character";
}

function pairControls(pair) {
  return `
    <div class="lib-dupe-pair-actions">
      <button class="btn btn-sm" data-dupe-action="compare" data-dupe-a="${escAttr(pair.a)}" data-dupe-b="${escAttr(pair.b)}">Compare</button>
      <button class="btn btn-sm" data-dupe-action="dismiss-pair" data-dupe-a="${escAttr(pair.a)}" data-dupe-b="${escAttr(pair.b)}">Keep both</button>
    </div>`;
}

function reasonHtml(reasons) {
  return (reasons || []).map((reason) => `<span class="lib-dupe-reason">${esc(reason)}</span>`).join("");
}

function pairHtml(pair, names, tier) {
  return `
    <div class="lib-dupe-pair lib-dupe-${escAttr(tier)}">
      <div class="lib-dupe-pair-main">
        <div class="lib-dupe-pair-names">${esc(cardName(pair.a, names))} <span aria-hidden="true">↔</span> ${esc(cardName(pair.b, names))}</div>
        <div class="lib-dupe-reasons">${reasonHtml(pair.reasons)}</div>
      </div>
      ${pairControls(pair)}
    </div>`;
}

function combinations(ids) {
  const pairs = [];
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) pairs.push([ids[i], ids[j]]);
  }
  return pairs;
}

/** Render a strong connected component with the matching evidence that formed it. */
export function strongGroupsHtml(groups = [], cards = []) {
  const names = new Map((cards || []).map((card) => [card.id, card.name]));
  if (!groups.length) return "";
  return groups
    .map((group) => {
      const ids = group.cards || [];
      const n = ids.length;
      return `
        <article class="lib-dupe-group">
          <header class="lib-dupe-group-head">
            <div>
              <div class="lib-dupe-badge">Strong match</div>
              <div class="lib-dupe-group-title">${n} likely copies</div>
            </div>
            <button class="btn btn-sm" data-dupe-action="dismiss-group" data-dupe-cards="${escAttr(ids.join(","))}">Keep all ${n}</button>
          </header>
          <div class="lib-dupe-members">${ids.map((id) => `<span>${esc(cardName(id, names))}</span>`).join("")}</div>
          <div class="lib-dupe-pairs">${(group.pairs || []).map((pair) => pairHtml(pair, names, "strong")).join("")}</div>
        </article>`;
    })
    .join("");
}

/** Render ungrouped weak evidence. Possible pairs never imply a three-way group. */
export function possiblePairsHtml(pairs = [], cards = []) {
  const names = new Map((cards || []).map((card) => [card.id, card.name]));
  if (!pairs.length) return "";
  return `
    <section class="lib-dupe-possible">
      <div class="lib-dupe-section-label">Possible matches</div>
      <p class="lib-manager-note">These share some signals but need a quick review before deleting anything.</p>
      <div class="lib-dupe-pairs">${pairs.map((pair) => pairHtml(pair, names, "possible")).join("")}</div>
    </section>`;
}

/** Render the report's review surface, separating strong groups from weak pairs. */
export function duplicateResultsHtml(report) {
  const groups = report?.groups || [];
  const pairs = report?.pairs || [];
  if (!groups.length && !pairs.length) {
    return `<div class="lib-dupe-empty">No duplicates found. This is a completed scan, not a loading state.</div>`;
  }
  return `${strongGroupsHtml(groups, report?.cards)}${possiblePairsHtml(pairs, report?.cards)}`;
}

function valueFor(cardView, key) {
  const card = cardView?.card || {};
  if (key === "alternate_greetings" || key === "tags") return Array.isArray(card[key]) ? card[key].join("\n") : "";
  if (key === "world") return cardView?.world_name || "";
  return String(card[key] || "");
}

function diffHtml(left, right) {
  // formatProseWithDiff is a prose formatter rather than an escaping boundary.
  // Escape each input before it enters the diff tokens, then retain its familiar
  // wc-before/wc-after visual language for one-sided values.
  const before = esc(left || "");
  const after = esc(right || "");
  if (!before && !after) return `<div class="wc-before wc-empty">(nothing)</div>`;
  if (before === after) return `<div class="lib-dupe-same">${before}</div>`;
  const beforeHtml = before ? formatProseWithDiff(sentenceDiff(before, after)) : "";
  const afterHtml = after ? formatProseWithDiff(sentenceDiff(after, before)) : "";
  return `<div class="wc-before">${beforeHtml || "(nothing)"}</div><div class="wc-after">${afterHtml || "(nothing)"}</div>`;
}

function avatarHtml(cardView) {
  const card = cardView?.card || {};
  if (!card.has_avatar) return `<div class="lib-dupe-avatar lib-dupe-no-avatar">No avatar</div>`;
  return `<img class="lib-dupe-avatar" src="/api/characters/${escAttr(card.id)}/avatar" alt="${escAttr(card.name || "Character")} avatar">`;
}

function activityHtml(cardView) {
  const activity = cardView?.activity || {};
  const total = Number(activity.total) || 0;
  const last = activity.last_used_at ? String(activity.last_used_at).slice(0, 10) : "Never";
  return `${total} conversation${total === 1 ? "" : "s"} · last used ${esc(last)}`;
}

/** Build the inline, two-card comparison. It deliberately is not a sub-modal. */
export function compareHtml(compare) {
  const left = compare?.a;
  const right = compare?.b;
  if (!left || !right) return "";
  const leftCard = left.card || {};
  const rightCard = right.card || {};
  const rows = COMPARE_FIELDS.map(([key, label]) => {
    const leftValue = valueFor(left, key);
    const rightValue = valueFor(right, key);
    return `<tr><th>${esc(label)}</th><td>${diffHtml(leftValue, rightValue)}</td><td>${diffHtml(rightValue, leftValue)}</td></tr>`;
  }).join("");
  const collision = Number(compare.shared_group_collisions) || 0;
  const collisionNote = collision
    ? `<div class="lib-dupe-collision-note">${collision} shared group conversation${collision === 1 ? " already has" : "s already have"} both cards. Relinking drops the redundant slot.</div>`
    : "";
  return `
    <section class="lib-dupe-compare">
      <div class="lib-dupe-compare-head">
        <button class="btn btn-sm" data-dupe-action="back-results">← Results</button>
        <span class="lib-dupe-section-label">Compare duplicates</span>
      </div>
      <div class="lib-dupe-compare-cards">
        <div>${avatarHtml(left)}<strong>${esc(leftCard.name || "Unnamed character")}</strong><span>${activityHtml(left)}</span></div>
        <div>${avatarHtml(right)}<strong>${esc(rightCard.name || "Unnamed character")}</strong><span>${activityHtml(right)}</span></div>
      </div>
      ${collisionNote}
      <div class="lib-dupe-table-wrap"><table class="lib-dupe-table"><thead><tr><th>Field</th><th>${esc(leftCard.name || "Left")}</th><th>${esc(rightCard.name || "Right")}</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="lib-dupe-resolve-actions">
        <button class="btn btn-accent" data-dupe-action="resolve" data-dupe-keep="${escAttr(leftCard.id)}" data-dupe-remove="${escAttr(rightCard.id)}">Keep ${esc(leftCard.name || "left")} · remove copy</button>
        <button class="btn" data-dupe-action="resolve" data-dupe-keep="${escAttr(rightCard.id)}" data-dupe-remove="${escAttr(leftCard.id)}">Keep ${esc(rightCard.name || "right")} · remove copy</button>
      </div>
    </section>`;
}

export { combinations };
