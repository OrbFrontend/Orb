// The Character Library's filter predicate. library_filter.js is pure and
// imports nothing, so it loads under node --test with no DOM stub at all.
//
// What matters here is what the 31-bit tag mask could not do and what it must
// not start doing wrong: more than 31 tags, an imported tag matching a curated
// one across casing, and no substring false positives.
import assert from "node:assert/strict";
import { test } from "node:test";

import { matchesFilter, tagsAttrFor } from "../../frontend/library_filter.js";

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
