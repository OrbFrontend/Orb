// The Character Library's search + tag predicate, and the chip row it filters
// on. Pure, DOM-free, imports nothing — the browser's filter runs over hundreds
// of rendered cards on every keystroke, and this is the part of it worth being
// able to test directly.
//
// The row lives here rather than in the browser because it has to fold casing
// exactly the way the predicate below does: a chip the filter cannot tell apart
// from another chip is one tag drawn twice.
//
// It replaces a 31-bit tag mask. The mask was fine while the chip row was the
// 15 most frequent imported strings, but the curated vocabulary holds up to 64
// tags and `1 << 40` is not a thing, so membership moved into a delimited
// string attribute instead.

const DELIM = "|";

/** `["Fantasy","Romance"]` -> `"|fantasy|romance|"`, the `data-tags` attribute.
 *
 * Lowercased so an imported `fantasy` and a curated `Fantasy` are one tag —
 * which is a bonus, not an accident: the vocabulary dedupes case-insensitively
 * for the same reason.
 *
 * The delimiter is stripped from the values, not escaped. An imported card can
 * carry anything in its tags (the vocabulary strips `|` on save, imports do
 * not), and a tag containing the delimiter would otherwise match as two.
 */
export function tagsAttrFor(tags) {
  const seen = new Set();
  for (const raw of tags || []) {
    const tag = String(raw == null ? "" : raw)
      .split(DELIM)
      .join("")
      .trim()
      .toLowerCase();
    if (tag) seen.add(tag);
  }
  return seen.size ? DELIM + [...seen].join(DELIM) + DELIM : "";
}

/** Whether one card passes the current filter.
 *
 * AND across everything, which is what the mask did too: a name substring the
 * caller has already lowercased, plus every selected tag. Selecting more chips
 * narrows. An empty query and no selection match everything, so the caller can
 * run this unconditionally.
 *
 * Tags are matched as `|tag|` against the delimited attribute, so `elf` does not
 * match a card tagged `self` — the substring false positive is the whole reason
 * the delimiters are there.
 */
export function matchesFilter(name, tagsAttr, query, selectedTags) {
  if (query && !String(name || "").includes(query)) return false;
  const attr = String(tagsAttr || "");
  for (const raw of selectedTags || []) {
    const tag = String(raw == null ? "" : raw)
      .split(DELIM)
      .join("")
      .trim()
      .toLowerCase();
    if (tag && !attr.includes(DELIM + tag + DELIM)) return false;
  }
  return true;
}

/** The chip row: the most-used tags across *tagLists*, at most *limit* of them.
 *
 * One derivation, because a card has one tag list — the auto-tagger writes the
 * curated vocabulary onto the cards themselves, so a curated tag earns its chip
 * by being used, and a vocabulary tag no card carries would only ever be a chip
 * that filters to nothing.
 *
 * Counted case-insensitively, matching `tagsAttrFor` above: an imported
 * `fantasy` and a curated `Fantasy` select exactly the same cards. The label
 * shown is whichever spelling is most common, ties going to the first seen, so
 * a run's canonical casing wins over a stray import as soon as it is in the
 * majority. Ties on count sort by name, so the row is stable between renders.
 */
export function topTags(tagLists, limit) {
  const merged = new Map(); // lowercased tag -> every spelling seen, with its count
  for (const tags of tagLists || []) {
    for (const raw of tags || []) {
      const label = String(raw == null ? "" : raw).trim();
      if (!label) continue;
      const key = label.toLowerCase();
      const spellings = merged.get(key) || new Map();
      spellings.set(label, (spellings.get(label) || 0) + 1);
      merged.set(key, spellings);
    }
  }
  return [...merged.values()]
    .map((spellings) => {
      let total = 0;
      let label = "";
      let best = 0;
      for (const [spelling, n] of spellings) {
        total += n;
        if (n > best) [label, best] = [spelling, n];
      }
      return { label, total };
    })
    .sort((a, b) => b.total - a.total || a.label.localeCompare(b.label))
    .slice(0, limit)
    .map((entry) => entry.label);
}
