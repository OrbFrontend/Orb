import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { alignableKeys, extractBlocks } from "../../frontend/workflows/tts/extract.js";

const fixtureUrl = new URL("../fixtures/tts_extraction_cases.json", import.meta.url);
const cases = JSON.parse(readFileSync(fileURLToPath(fixtureUrl), "utf8"));
const alignmentCases = JSON.parse(
  readFileSync(fileURLToPath(new URL("../fixtures/tts_alignment_cases.json", import.meta.url)), "utf8"),
);

for (const fixture of cases) {
  test(`TTS extraction contract: ${fixture.name}`, () => {
    assert.deepEqual(extractBlocks(fixture.text), fixture.blocks);
    assert.ok(extractBlocks(fixture.text).every((block) => !/[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u.test(block)));
  });
}

for (const fixture of alignmentCases) {
  test(`TTS alignment contract: ${fixture.text}`, () => {
    assert.deepEqual(alignableKeys(fixture.text), fixture.keys);
  });
}
