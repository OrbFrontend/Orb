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
  { id: "a", name: "Mara" },
  { id: "b", name: "Mara (download)" },
  { id: "c", name: "Mara draft" },
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
  assert.match(html, /data-dupe-cards="a,b,c"/);
});

test("possible pairs stay separate from strong groups", () => {
  const report = {
    groups: [{ cards: ["a", "b"], pairs: [pair()] }],
    pairs: [pair({ a: "b", b: "c", tier: "possible", reasons: ["Nearly identical text (66% overlap)"] })],
    cards,
  };
  const html = duplicateResultsHtml(report);

  assert.match(html, /Strong match/);
  assert.match(html, /Possible matches/);
  assert.match(html, /Nearly identical text/);
  assert.match(possiblePairsHtml(report.pairs, cards), /lib-dupe-possible/);
});

test("an empty completed report explains that nothing was found", () => {
  assert.match(duplicateResultsHtml({ groups: [], pairs: [] }), /No duplicates found/);
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

  assert.match(html, /lib-dupe-table/);
  assert.match(html, /diff-deleted|diff-change/);
  assert.match(html, /2 conversations/);
  assert.match(html, /The Estuary/);
  assert.match(html, /shared group conversation already has both cards/);
  assert.match(html, /&lt;copy&gt;/);
  assert.ok(!html.includes("Mara <copy>"));
  assert.match(html, /data-dupe-action="resolve"/);
});

test("group dismissal expands each pair exactly once", () => {
  assert.deepEqual(combinations(["a", "b", "c"]), [
    ["a", "b"],
    ["a", "c"],
    ["b", "c"],
  ]);
});
