// The Character Library's filter predicate. library_filter.js is pure and
// imports nothing, so it loads under node --test with no DOM stub at all.
//
// What matters here is what the 31-bit tag mask could not do and what it must
// not start doing wrong: more than 31 tags, an imported tag matching a curated
// one across casing, and no substring false positives.
import assert from "node:assert/strict";
import { test } from "node:test";

import { matchesFilter, tagsAttrFor, topTags } from "../../frontend/library_filter.js";

test("tagsAttrFor delimits and lowercases", () => {
  assert.equal(tagsAttrFor(["Fantasy", "Romance"]), "|fantasy|romance|");
});

test("tagsAttrFor is empty for a card with no tags", () => {
  assert.equal(tagsAttrFor([]), "");
  assert.equal(tagsAttrFor(null), "");
  assert.equal(tagsAttrFor(["", "   "]), "");
});

test("tagsAttrFor dedupes across casing and whitespace", () => {
  assert.equal(tagsAttrFor(["Fantasy", "fantasy", " FANTASY "]), "|fantasy|");
});

test("tagsAttrFor strips the delimiter out of imported tag values", () => {
  // The vocabulary strips `|` on save; an imported card's tags never went
  // through that, and one containing the delimiter would match as two.
  assert.equal(tagsAttrFor(["Sci|Fi"]), "|scifi|");
  assert.ok(!matchesFilter("lira", tagsAttrFor(["Sci|Fi"]), "", ["Fi"]));
});

test("an empty filter matches everything", () => {
  assert.ok(matchesFilter("lira", "|fantasy|", "", []));
  assert.ok(matchesFilter("lira", "", "", []));
});

test("the name query is a substring match", () => {
  assert.ok(matchesFilter("lira the bard", "", "bard", []));
  assert.ok(!matchesFilter("lira the bard", "", "rook", []));
});

test("selected tags are ANDed", () => {
  const attr = tagsAttrFor(["Fantasy", "Romance", "Elf"]);
  assert.ok(matchesFilter("lira", attr, "", ["Fantasy", "Romance"]));
  assert.ok(!matchesFilter("lira", attr, "", ["Fantasy", "Sci-Fi"]));
});

test("name and tags are ANDed together", () => {
  const attr = tagsAttrFor(["Fantasy"]);
  assert.ok(matchesFilter("lira", attr, "lir", ["Fantasy"]));
  assert.ok(!matchesFilter("lira", attr, "rook", ["Fantasy"]));
});

test("an imported tag matches a curated tag across casing", () => {
  // The card was imported with `fantasy`; the vocabulary spells it `Fantasy`.
  assert.ok(matchesFilter("lira", tagsAttrFor(["fantasy"]), "", ["Fantasy"]));
});

test("a tag does not match a card by substring", () => {
  // |elf| must not be found inside |self|. This is what the delimiters buy.
  assert.ok(!matchesFilter("lira", tagsAttrFor(["self"]), "", ["elf"]));
  assert.ok(!matchesFilter("lira", tagsAttrFor(["elfin"]), "", ["elf"]));
  assert.ok(matchesFilter("lira", tagsAttrFor(["elf"]), "", ["elf"]));
});

test("a card with no tags fails any tag filter", () => {
  assert.ok(!matchesFilter("lira", "", "", ["Fantasy"]));
});

test("more than 31 tags still filter correctly", () => {
  // The case the 1 << i bitmask could not express: the 40th tag's bit was lost,
  // so selecting it silently matched every card.
  const vocabulary = Array.from({ length: 64 }, (_, i) => `Tag${i}`);
  const attr = tagsAttrFor(vocabulary);
  assert.ok(matchesFilter("lira", attr, "", ["Tag40"]));
  assert.ok(matchesFilter("lira", attr, "", ["Tag0", "Tag63"]));
  assert.ok(!matchesFilter("lira", tagsAttrFor(["Tag1"]), "", ["Tag40"]));
});

test("tag matching is not confused by numeric prefixes", () => {
  // |tag4| must not match |tag40|, the same class as elf/self.
  assert.ok(!matchesFilter("lira", tagsAttrFor(["Tag40"]), "", ["Tag4"]));
});

// ── the chip row ─────────────────────────────────────────────────────────────
//
// One derivation over the cards' own tags, since the auto-tagger writes the
// curated vocabulary onto them. What matters is that it folds casing the same
// way the predicate above does, and that a chip it emits always selects
// something.

test("topTags ranks by use and caps the row", () => {
  const cards = [["Fantasy", "Romance"], ["Fantasy"], ["Fantasy", "Sci-Fi"], ["Romance"]];
  assert.deepEqual(topTags(cards, 15), ["Fantasy", "Romance", "Sci-Fi"]);
  assert.deepEqual(topTags(cards, 2), ["Fantasy", "Romance"]);
});

test("topTags merges spellings the filter cannot tell apart", () => {
  // Two chips here would be one tag drawn twice: selecting either matches both
  // cards, because the predicate lowercases.
  const cards = [["Fantasy"], ["fantasy"], ["  FANTASY "]];
  assert.deepEqual(topTags(cards, 15), ["Fantasy"]);
});

test("topTags labels a merged tag with its most common spelling", () => {
  // A tagging run's canonical casing takes the label once it is in the majority.
  assert.deepEqual(topTags([["fantasy"], ["Fantasy"], ["Fantasy"]], 15), ["Fantasy"]);
  // Ties go to the spelling seen first, so the row does not flicker.
  assert.deepEqual(topTags([["fantasy"], ["Fantasy"]], 15), ["fantasy"]);
});

test("topTags breaks count ties by name, so the row is stable", () => {
  assert.deepEqual(topTags([["Romance"], ["Fantasy"]], 15), ["Fantasy", "Romance"]);
});

test("topTags tolerates a library with nothing to count", () => {
  assert.deepEqual(topTags([], 15), []);
  assert.deepEqual(topTags(null, 15), []);
  assert.deepEqual(topTags([null, [], ["", "  "]], 15), []);
});

test("every chip topTags emits selects at least one card", () => {
  // The invariant the old vocabulary-sourced row could not hold: a curated tag
  // no card carried was a chip that filtered the library to nothing.
  const cards = [["Fantasy", "romance"], ["FANTASY"], ["Sci|Fi"]];
  for (const tag of topTags(cards, 15)) {
    assert.ok(
      cards.some((tags) => matchesFilter("lira", tagsAttrFor(tags), "", [tag])),
      `chip ${tag} matches no card`,
    );
  }
});
