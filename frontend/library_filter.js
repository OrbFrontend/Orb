// DOM-free Character Library search, tag matching and chip derivation.
// Chips and predicates share case folding. Delimited attributes support more
// tags than a 31-bit mask.

const DELIM = "|";

/**
 * Encode `["Fantasy","Romance"]` as `"|fantasy|romance|"` for `data-tags`.
 * Lowercase and strip embedded delimiters so one imported tag cannot match as two.
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

/**
 * Match a lowercased name query AND every selected tag. Empty filters match all.
 * Delimiters prevent substring matches such as `elf` against `self`.
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

/**
 * Return the most-used tags, at most *limit*, counted case-insensitively.
 * Use the most common spelling (first seen on ties); sort count ties by name.
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
