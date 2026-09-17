import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeSegment } from "../../frontend/audio_schedule.js";

test("a whole row keys by its id", () => {
  const seg = normalizeSegment({ row: 12 });
  assert.equal(seg.sourceKey, "row:12");
  assert.deepEqual(seg.source, { row: 12 });
});

test("a byte span of a row is its own source", () => {
  // Two spans of one attachment are two clips: each decodes (and caches)
  // separately, so they must not share the whole row's key or each other's.
  const a = normalizeSegment({ row: 12, byte_start: 0, byte_end: 100 });
  const b = normalizeSegment({ row: 12, byte_start: 100, byte_end: 250 });
  assert.deepEqual(a.source, { row: 12, byteStart: 0, byteEnd: 100 });
  assert.notEqual(a.sourceKey, b.sourceKey);
  assert.notEqual(a.sourceKey, "row:12");
});

test("a span that is half-given, empty, or reversed is malformed", () => {
  for (const span of [
    { byte_start: 0 },
    { byte_end: 10 },
    { byte_start: 10, byte_end: 10 },
    { byte_start: 10, byte_end: 5 },
    { byte_start: -1, byte_end: 5 },
    { byte_start: 0.5, byte_end: 5 },
    { byte_start: "0", byte_end: "5" },
  ]) {
    assert.equal(normalizeSegment({ row: 12, ...span }), null, JSON.stringify(span));
  }
});
