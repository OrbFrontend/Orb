import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { BLOCK_TAGS, ORB_CLASS_PREFIXES, ORB_CLASSES } from "../../frontend/message_html.js";

// message_html.js's class hook rewrites every class token a message body carries
// to `custom-<token>` unless ORB_CLASSES vouches for it. That is what stops card
// markup borrowing an app class — and what would silently mangle Orb's own
// markup if formatProse grew a class the set does not know about.
const SOURCES = ["../../frontend/utils.js", "../../frontend/icons.js"];

function classLiterals(spec) {
  const src = readFileSync(fileURLToPath(new URL(spec, import.meta.url)), "utf8");
  return [...src.matchAll(/class="([^"]*)"/g)].map((m) => m[1]);
}

test("every class formatProse can emit is in ORB_CLASSES", () => {
  const seen = [];
  for (const spec of SOURCES) {
    for (const literal of classLiterals(spec)) {
      for (const token of literal.split(/\s+/).filter(Boolean)) {
        seen.push(token);
        if (token.includes("${")) {
          // Built at runtime, e.g. class="md-h${n}" or class="language-${lang}".
          const prefix = token.slice(0, token.indexOf("${"));
          const covered =
            ORB_CLASS_PREFIXES.some((p) => prefix.startsWith(p)) ||
            [...ORB_CLASSES].some((c) => c.startsWith(prefix));
          assert.ok(covered, `no ORB_CLASSES entry or prefix covers "${prefix}" (from ${token})`);
          continue;
        }
        assert.ok(
          ORB_CLASSES.has(token) || ORB_CLASS_PREFIXES.some((p) => token.startsWith(p)),
          `class "${token}" reaches the sanitiser but is not in ORB_CLASSES`,
        );
      }
    }
  }
  assert.ok(seen.length > 10, "class literals were not found — did the scan break?");
});

test("block tags are upper case, so tagName comparisons match", () => {
  for (const tag of BLOCK_TAGS) assert.equal(tag, tag.toUpperCase());
  for (const tag of ["DIV", "P", "UL", "LI", "TABLE", "TR", "TD", "PRE", "BLOCKQUOTE", "H1"]) {
    assert.ok(BLOCK_TAGS.has(tag), `${tag} should count as a block boundary`);
  }
  for (const tag of ["SPAN", "EM", "STRONG", "A", "CODE", "B"]) {
    assert.ok(!BLOCK_TAGS.has(tag), `${tag} is inline and must not force a break`);
  }
});
