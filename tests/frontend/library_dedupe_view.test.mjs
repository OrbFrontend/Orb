// The duplicate-finder render helpers are DOM-free; this tiny escaping shim is
// only what utils.esc() needs while node loads the pure module.
import assert from "node:assert/strict";
import { test } from "node:test";

globalThis.document = {
  createElement() {
    return {
      innerHTML: "",
      set textContent(value) {
        this.innerHTML = String(value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      },
    };
  },
};

import { combinations, compareHtml, duplicateResultsHtml, possiblePairsHtml, strongGroupsHtml } from "../../frontend/library_dedupe_view.js";

const cards = [
  { id: "a", name: "Mara", conversations: 0, last_used_at: null, created_at: "2026-01-02T00:00:00+00:00", has_avatar: 1 },
  { id: "b", name: "Mara (download)", conversations: 4, last_used_at: "2026-09-01T00:00:00+00:00", created_at: "2026-02-02T00:00:00+00:00", has_avatar: 1 },
  { id: "c", name: "Mara draft", conversations: 0, last_used_at: null, created_at: "2026-03-02T00:00:00+00:00", has_avatar: 0 },
];

// Three cards that share one name: the case a name alone cannot disambiguate.
const sameName = [
  { id: "x", name: "Reimu", conversations: 0, last_used_at: null, created_at: "2026-05-01T00:00:00+00:00", has_avatar: 1 },
  { id: "y", name: "Reimu", conversations: 3, last_used_at: "2026-08-20T00:00:00+00:00", created_at: "2026-04-01T00:00:00+00:00", has_avatar: 1 },
  { id: "z", name: "Reimu", conversations: 0, last_used_at: null, created_at: "2026-06-01T00:00:00+00:00", has_avatar: 0 },
];

const pair = (over = {}) => ({
  a: "a",
  b: "b",
  tier: "strong",
  reasons: ["Identical content", "Same avatar"],
  ...over,
});

test("strong groups render names, evidence, and one action for the whole group", () => {
  const html = strongGroupsHtml([{ cards: ["a", "b", "c"], pairs: [pair()] }], cards);

  assert.match(html, /Strong match/);
  assert.match(html, /Mara \(download\)/);
  assert.match(html, /Identical content/);
  assert.match(html, /Same avatar/);
  assert.match(html, /data-dupe-action="dismiss-group"/);
  assert.match(html, /data-dupe-cards="b,a,c"/);
  // The cost of keeping a copy is the same for every match, so it is stated
  // once above the whole list rather than under each header.
  assert.doesNotMatch(html, /lib-manager-note/);
});

test("a two-card match states its names and its dismissal exactly once", () => {
  const html = strongGroupsHtml([{ cards: ["a", "b"], pairs: [pair()] }], cards);

  // The evidence is why they matched; the names and the dismissal are already
  // on screen in the choice list and the header directly above it.
  assert.match(html, /Identical content/);
  assert.doesNotMatch(html, /lib-dupe-pair-names/);
  assert.doesNotMatch(html, /data-dupe-action="dismiss-pair"/);
  assert.equal(html.match(/Not duplicates/g).length, 1);
  assert.doesNotMatch(html, /1 comparison/);
});

test("a cluster larger than two still names each edge and can dismiss one pair", () => {
  const pairs = [pair({ a: "x", b: "y" }), pair({ a: "x", b: "z" })];
  const html = strongGroupsHtml([{ cards: ["x", "y", "z"], pairs }], sameName);

  assert.equal(html.match(/lib-dupe-pair-names/g).length, 2);
  assert.equal(html.match(/data-dupe-action="dismiss-pair"/g).length, 2);
  assert.match(html, /2 comparisons/);
});

test("possible pairs stay separate from strong groups", () => {
  const report = {
    groups: [{ cards: ["a", "b"], pairs: [pair()] }],
    pairs: [pair({ a: "b", b: "c", tier: "possible", reasons: ["Nearly identical text (66% overlap)"] })],
    cards,
  };
  const html = duplicateResultsHtml(report);

  assert.match(html, /Strong match/);
  assert.match(html, /Possible match/);
  assert.match(html, /Nearly identical text/);
  assert.match(possiblePairsHtml(report.pairs, cards), /Possible match/);
  // A possible pair is a cluster of two, never chained into the strong group.
  assert.equal(html.match(/lib-dupe-group-head/g).length, 2);
});

test("an empty completed report renders nothing, leaving the status line to say it", () => {
  assert.equal(duplicateResultsHtml({ groups: [], pairs: [] }), "");
});

test("what keeping a copy costs is stated once for the list, not once per match", () => {
  const report = {
    groups: [
      { cards: ["a", "b"], pairs: [pair()] },
      { cards: ["x", "y"], pairs: [pair({ a: "x", b: "y" })] },
    ],
    pairs: [],
    cards: [...cards, ...sameName],
  };
  const html = duplicateResultsHtml(report);

  assert.equal(html.match(/lib-dupe-group-head/g).length, 2);
  assert.equal(html.match(/moves their conversations/g).length, 1);
  // The weaker tier's caveat rides the same line, and only when it applies.
  assert.doesNotMatch(html, /compare those first/);
  assert.match(duplicateResultsHtml({ groups: [], pairs: [pair()], cards }), /compare those first/);
});

// ── Telling identical names apart ───────────────────────────────────────────

test("a three-card group offers one keeper choice per member, not three pair reviews", () => {
  const pairs = [
    pair({ a: "x", b: "y" }),
    pair({ a: "x", b: "z" }),
    pair({ a: "y", b: "z" }),
  ];
  const html = strongGroupsHtml([{ cards: ["x", "y", "z"], pairs }], sameName);

  assert.match(html, /3 copies of “Reimu”/);
  assert.equal(html.match(/data-dupe-action="keep-one"/g).length, 3);
  // Keeping any member names the other two as the removals, in one confirmation.
  assert.match(html, /data-dupe-keep="y" data-dupe-remove="x,z"/);
  assert.match(html, /data-dupe-keep="x" data-dupe-remove="y,z"/);
  assert.match(html, /3 comparisons/);
});

test("cluster members are marked and ordered so the same-named copies are distinguishable", () => {
  const html = strongGroupsHtml([{ cards: ["x", "y", "z"], pairs: [pair({ a: "x", b: "y" })] }], sameName);
  const marks = [...html.matchAll(/data-dupe-keep="(\w+)"/g)].map((match) => match[1]);

  // Most conversations first, then oldest: y (3 chats), x (added May), z (added June).
  assert.deepEqual(marks, ["y", "x", "z"]);
  assert.match(html, /class="lib-dupe-mark">A</);
  assert.match(html, /class="lib-dupe-mark">C</);
  assert.match(html, /3 conversations · last used/);
  assert.match(html, /No conversations/);
  assert.match(html, /Most used/);
});

test("a tie on conversations claims no most-used copy", () => {
  const tied = sameName.map((card) => ({ ...card, conversations: 2 }));
  assert.doesNotMatch(strongGroupsHtml([{ cards: ["x", "y", "z"], pairs: [] }], tied), /Most used/);
});

test("compare buttons carry the cluster marks so the drill-down keeps its letters", () => {
  const html = strongGroupsHtml([{ cards: ["x", "y", "z"], pairs: [pair({ a: "x", b: "z" })] }], sameName);

  // x sorted to B and z to C, and the Compare button hands both letters onward.
  assert.match(html, /data-dupe-a="x" data-dupe-b="z" data-dupe-mark-a="B" data-dupe-mark-b="C"/);
});

test("evidence rows read in cluster order, not in card-id order", () => {
  // The report pairs x (mark B) with y (mark A); the row still runs A then B.
  const html = strongGroupsHtml([{ cards: ["x", "y", "z"], pairs: [pair({ a: "x", b: "y" })] }], sameName);

  assert.match(html, /data-dupe-a="y" data-dupe-b="x" data-dupe-mark-a="A" data-dupe-mark-b="B"/);
  assert.match(html, /lib-dupe-mark">A<\/span>Reimu <span aria-hidden="true">↔<\/span> <span class="lib-dupe-mark">B</);
});

test("comparison shows field diffs, activity, worlds and relink collisions", () => {
  const compare = {
    shared_group_collisions: 1,
    a: {
      card: {
        id: "a",
        name: "Mara",
        description: "She maps the river.",
        first_mes: "Welcome home.",
        tags: ["favorite"],
        alternate_greetings: [],
        has_avatar: false,
      },
      activity: { total: 2, last_used_at: "2026-09-10T12:00:00+00:00" },
      world_name: "The Estuary",
    },
    b: {
      card: {
        id: "b",
        name: "Mara <copy>",
        description: "She maps the old river.",
        first_mes: "Welcome home.",
        tags: ["archive"],
        alternate_greetings: ["Hello again."],
        has_avatar: true,
      },
      activity: { total: 0, last_used_at: null },
      world_name: "Old Estuary",
    },
  };

  const html = compareHtml(compare);

  assert.match(html, /diff-deleted/);
  assert.match(html, /diff-change/);
  assert.match(html, /2 conversations/);
  assert.match(html, /The Estuary/);
  assert.match(html, /shared group conversation already has both cards/);
  assert.match(html, /&lt;copy&gt;/);
  assert.ok(!html.includes("Mara <copy>"));
  assert.match(html, /data-dupe-action="resolve"/);
});

// ── One unified diff per field ──────────────────────────────────────────────

const field = (over) => ({
  card: {
    id: "x",
    name: "Reimu",
    description: "A shrine maiden.",
    personality: "Laid-back.",
    scenario: "",
    first_mes: "Welcome.",
    tags: [],
    alternate_greetings: [],
    has_avatar: true,
    created_at: "2026-07-12T00:00:00+00:00",
    ...over,
  },
  activity: { total: 0, last_used_at: null },
});

test("a field renders once, not once per card and direction", () => {
  const html = compareHtml({ a: field({}), b: field({ id: "y" }) });

  // The old side-by-side table printed each value up to four times.
  assert.equal(html.match(/A shrine maiden\./g).length, 1);
  assert.match(html, /Every field is identical/);
});

test("shared text stays plain and each side's own text is marked", () => {
  const html = compareHtml({ a: field({}), b: field({ id: "y", personality: "Laid-back but fierce." }) });

  assert.match(html, /<span class="diff-deleted">Laid-back\.<\/span>/);
  assert.match(html, /<span class="diff-change">Laid-back but fierce\.<\/span>/);
  assert.match(html, /1 of 7 fields differ/);
});

test("a value present on only one side is marked rather than shown as unchanged", () => {
  // sentenceDiff calls a one-sided value equal, which would hide the difference.
  const gone = compareHtml({ a: field({}), b: field({ id: "y", first_mes: "" }) });
  const added = compareHtml({ a: field({ first_mes: "" }), b: field({ id: "y" }) });

  assert.match(gone, /<span class="diff-deleted">Welcome\.<\/span>/);
  assert.match(added, /<span class="diff-change">Welcome\.<\/span>/);
});

test("only the differing fields open, and fields empty on both sides do not expand", () => {
  const html = compareHtml({ a: field({}), b: field({ id: "y", personality: "Laid-back but fierce." }) });

  const named = (tag, label) =>
    new RegExp(`${tag}<span class="lib-dupe-field-mark">(?:<svg[^]*?</svg>)?</span><span class="lib-dupe-field-name">${label}</span>`);

  assert.match(html, named('<details class="lib-dupe-field" open><summary class="lib-dupe-field-head">', "Personality"));
  assert.match(html, named('<details class="lib-dupe-field"><summary class="lib-dupe-field-head">', "Description"));
  // Scenario is blank on both cards: a plain row, never a <details> with no body.
  assert.match(html, named('<div class="lib-dupe-field is-empty"><div class="lib-dupe-field-head">', "Scenario"));
  assert.doesNotMatch(html, named("<summary[^>]*>", "Scenario"));
});

test("the legend attributes each diff colour to a lettered card", () => {
  const html = compareHtml({ a: field({}), b: field({ id: "y" }) }, { a: "B", b: "C" });

  assert.match(html, /class="diff-deleted">only in <span class="lib-dupe-mark">B</);
  assert.match(html, /class="diff-change">only in <span class="lib-dupe-mark">C</);
});

test("both keeper buttons say which copy they keep when the two names match", () => {
  const side = (id, total) => ({
    card: { id, name: "Reimu", tags: [], alternate_greetings: [], has_avatar: true, created_at: "2026-04-01T00:00:00+00:00" },
    activity: { total, last_used_at: null },
  });
  const html = compareHtml({ a: side("x", 0), b: side("y", 3) }, { a: "B", b: "C" });

  // Never two identically labelled buttons: the mark and the card's own facts
  // separate them even though both cards are called Reimu.
  assert.match(html, /data-dupe-keep="x" data-dupe-remove="y"/);
  assert.match(html, /data-dupe-keep="y" data-dupe-remove="x"/);
  assert.match(html, /class="lib-dupe-mark">B<\/span>Keep this one/);
  assert.match(html, /class="lib-dupe-mark">C<\/span>Keep this one/);
  assert.match(html, /3 conversations/);
  assert.match(html, /No conversations/);
  assert.match(html, /Keeping one card deletes the other/);
});

test("the comparison states each copy's use and age once, on the keeper buttons", () => {
  // The header carries the portrait and the letter the legend and diff refer to.
  // Repeating the counts there too said the same thing at both ends of the diff.
  const side = (id, avatar, total) => ({
    card: { id, name: "Reimu", tags: [], alternate_greetings: [], has_avatar: avatar, created_at: "2026-07-12T00:00:00+00:00" },
    activity: { total, last_used_at: null },
  });
  const html = compareHtml({ a: side("x", true, 4), b: side("y", false, 1) });

  const header = html.slice(html.indexOf("lib-dupe-compare-cards"), html.indexOf("lib-dupe-diff-head"));

  assert.doesNotMatch(header, /conversations?|added/);
  assert.match(html, /lib-dupe-keep-sub">4 conversations/);
  assert.match(html, /lib-dupe-keep-sub">1 conversation ·/);
  // The header still binds each letter to a portrait, placeholder included.
  assert.match(html, /<span class="lib-dupe-avatar lib-dupe-no-avatar">/);
  assert.equal(html.match(/class="lib-dupe-choice-label"/g).length, 2);
});

test("group dismissal expands each pair exactly once", () => {
  assert.deepEqual(combinations(["a", "b", "c"]), [
    ["a", "b"],
    ["a", "c"],
    ["b", "c"],
  ]);
});
