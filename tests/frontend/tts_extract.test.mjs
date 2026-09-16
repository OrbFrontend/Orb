import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  alignableKeys,
  alignBlocks,
  alignmentKey,
  attachmentBlocks,
  extractBlocks,
} from "../../frontend/workflows/tts/extract.js";

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


test("TTS uses persisted speech blocks instead of guessing the model's convention", () => {
  const text = "*nods* Come here.";
  assert.deepEqual(extractBlocks(text), []);
  assert.deepEqual(attachmentBlocks(text, [{ spoken_text: "Come here." }]), ["Come here."]);
  assert.deepEqual(attachmentBlocks(text, []), []);
});

test("TTS preserves the legacy scanner for old audio", () => {
  const text = 'She — tired — sat down. "Hello."';
  assert.deepEqual(attachmentBlocks(text, [{ words: [] }]), ["tired", "Hello."]);
});

// Stands in for the rendered word stream the widget aligns against: every word
// the reader sees, narration included, with its delimiters still attached. The
// separator set is the one the DOM tokenizer uses, hard line breaks included.
function shownWords(text) {
  return text
    .split(/[\s\u001c-\u001e\u0085\u2028\u2029]+/u)
    .filter(Boolean)
    .map((raw) => ({ t: alignmentKey(raw), raw }));
}

function placeBlocks(text, blocks) {
  const words = shownWords(text);
  const blockTokens = blocks.map(alignableKeys);
  return alignBlocks(words, blockTokens).map((at, bi) =>
    at < 0
      ? null
      : words
          .slice(at, at + blockTokens[bi].length)
          .map((word) => word.raw)
          .join(" "),
  );
}

test("TTS alignment skips a narration echo of a quoted line", () => {
  const text = '*There is no one to talk to right now.*\n\n"Right," *she mutters.*\n"Christ, pathetic."';
  assert.deepEqual(placeBlocks(text, ["Right,", "Christ, pathetic."]), ['"Right,"', '"Christ, pathetic."']);
});

test("TTS alignment reads every quoting convention the extractor does", () => {
  assert.deepEqual(placeBlocks("*She was right.*\n\n“Right,” she said.", ["Right,"]), ["“Right,”"]);
  assert.deepEqual(placeBlocks("She is right, mostly.\n\n—Right, then.—", ["Right, then."]), [
    "—Right, then.—",
  ]);
});

test("TTS alignment takes each repeated line in turn", () => {
  assert.deepEqual(placeBlocks('*yes, she thought*\n\n"Yes." *a beat* "Yes."', ["Yes.", "Yes."]), ['"Yes."', '"Yes."']);
});

test("TTS alignment keeps the leftmost run when nothing is delimited", () => {
  assert.deepEqual(placeBlocks("Hello. Let's get to know each other.", ["Hello.", "Let's get to know each other."]), [
    "Hello.",
    "Let's get to know each other.",
  ]);
  assert.deepEqual(placeBlocks("go on. go on.", ["go on."]), ["go on."]);
});

test("TTS alignment reports a block the message no longer contains", () => {
  assert.deepEqual(placeBlocks('"Hello."', ["Goodbye."]), [null]);
  // A block that fell out must not drag the ones around it out of place.
  assert.deepEqual(placeBlocks('"Hi." "Bye."', ["Hi.", "Gone.", "Bye."]), ['"Hi."', null, '"Bye."']);
});

test("TTS alignment never takes a run a later block needs", () => {
  // A bare convention speaks unquoted prose, so the better-delimited run further
  // on belongs to the second block and the first block must stay put.
  assert.deepEqual(placeBlocks('Yes. She waited. "Yes."', ["Yes.", "Yes."]), ["Yes.", '"Yes."']);
  assert.deepEqual(placeBlocks('Right. "Right." "Right."', ["Right.", "Right.", "Right."]), [
    "Right.",
    '"Right."',
    '"Right."',
  ]);
});

test("TTS alignment tokenizes blocks the way the backend times them", () => {
  // The backend emits one timing span per `alignableKeys` token and the karaoke
  // driver drops a block whose index count disagrees, so the two must not drift
  // on the separators that only one of the splitters treats as whitespace.
  const text = "one\u001ctwo\u001dthree\u001efour";
  const words = shownWords(text);
  const tokens = alignableKeys(text);
  assert.deepEqual(tokens, ["one", "two", "three", "four"]);
  const [at] = alignBlocks(words, [tokens]);
  assert.equal(at, 0);
  assert.deepEqual(
    words.slice(at, at + tokens.length).map((word) => word.raw),
    ["one", "two", "three", "four"],
  );
});
