import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { sentenceTail } from "../../frontend/utils.js";
import { sentenceStream, splitSentences } from "../../frontend/text_segmentation.js";
import { tokenizeRun } from "../../frontend/workflow_segmentation.js";

const fixtureUrl = new URL("../fixtures/text_segmentation_cases.json", import.meta.url);
const cases = JSON.parse(readFileSync(fileURLToPath(fixtureUrl), "utf8"));
const hardBreak = /[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u;

for (const fixture of cases) {
  test(`sentence contract: ${fixture.name}`, () => {
    const stream = sentenceStream(fixture.text);
    assert.equal(stream.map((unit) => unit.text).join(""), fixture.text);
    assert.ok(stream.filter((unit) => unit.kind === "sentence").every((unit) => !hardBreak.test(unit.text)));
    assert.deepEqual(splitSentences(fixture.text), fixture.sentences);
    assert.equal(sentenceTail(fixture.text, 1), fixture.sentences.at(-1));

    const initial = {
      wordIndex: -1,
      sentIndex: 0,
      midWord: false,
      pendingWord: "",
      separated: false,
      breakPending: false,
    };
    const segmented = tokenizeRun(fixture.text, initial, {}).words;
    assert.equal(Math.max(...segmented.map((word) => word.sentIndex)) + 1, fixture.sentences.length);
  });
}

test("sentence diff preserves hard breaks losslessly", async () => {
  const { sentenceDiff } = await import("../../frontend/utils.js");
  const ops = sentenceDiff("A.\nB.", "A.\nC.");
  assert.equal(ops.filter((op) => op.type !== "insert").map((op) => op.text).join(""), "A.\nB.");
  assert.equal(ops.filter((op) => op.type !== "delete").map((op) => op.text).join(""), "A.\nC.");
});

test("streaming tails drop only the incomplete line and reject nonpositive counts", () => {
  assert.equal(sentenceTail("Complete line\nunfinished", 3, true), "Complete line");
  assert.equal(sentenceTail("Complete line\nunfinished", 0, false), "");
});
