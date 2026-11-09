import { installEscapingDocument } from "./dom_fixture.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import { sentenceDiff } from "../../frontend/utils.js";

installEscapingDocument();

const blind = (before, after) => sentenceDiff(before, after, { ignoreMarkup: true });
const changed = (ops) => ops.filter((op) => op.type !== "equal");

// The rewrites Format Consistency makes, both ways where it makes both.
const MARKUP_ONLY = [
  [
    'The silence stretches. She waits.\n\nHello there, he says.',
    '*The silence stretches. She waits.*\n\n"Hello there," he says.',
  ],
  ["Stop. Now, he said. She waits.", '"Stop. Now," he said. She waits.'],
  ["_She waits._ He sighs.", "*She waits.* He sighs."],
  // Removing a clause's emphasis closes it with a period, which also moves the sentence boundary.
  ["*She waits* He sighs.", "She waits. He sighs."],
  ["He sighs.\n\n*She waits*", "He sighs.\n\nShe waits."],
  ["‘Go,’ she says.", "“Go,” she says."],
  ["He grins. *It **really** is.*", "He grins. It **really** is."],
];

test("a markup-only rewrite diffs as unchanged and shows the new markup", () => {
  for (const [before, after] of MARKUP_ONLY) {
    for (const [a, b] of [
      [before, after],
      [after, before],
    ]) {
      const ops = blind(a, b);
      assert.deepEqual(changed(ops), [], `${JSON.stringify(a)} -> ${JSON.stringify(b)}`);
      assert.equal(ops.map((op) => op.text).join(""), b);
    }
  }
});

test("word, bold, apostrophe and paragraph changes still show", () => {
  for (const [before, after] of [
    ["The silence stretches. She waits.", "*The silence lingers.* *She waits.*"],
    ["He grins. It really is.", "He grins. It **really** is."],
    ["He says it’s fine.", "He says its fine."],
    ["He sighs. She waits.", "He sighs.\n\nShe waits."],
  ]) {
    assert.notDeepEqual(changed(blind(before, after)), [], `${JSON.stringify(before)} -> ${JSON.stringify(after)}`);
  }
  assert.deepEqual(changed(blind("The silence stretches. She waits.", "*The silence lingers.* *She waits.*")), [
    { type: "delete", text: "The silence stretches. " },
    { type: "insert", text: "*The silence lingers.* " },
  ]);
});

test("the default diff stays exact for card comparisons", () => {
  assert.deepEqual(changed(sentenceDiff("She waits.", "*She waits.*")), [
    { type: "delete", text: "She waits." },
    { type: "insert", text: "*She waits.*" },
  ]);
});
