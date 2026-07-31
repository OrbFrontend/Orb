// SSE parser fixtures for frontend/sse.js — the app-wide single SSE path.
// Zero deps (node --test, node v22+); no jsdom. Exercises the wire contract from
// backend/api/deps.py `_sse_stream`: frame terminators, keepalive comments,
// chunk-boundary splits, and the parser's must-NOT-unescape guarantee (token
// escaping vs raw probs-JSON are opposite rules — un-escaping is consumer-side).
import assert from "node:assert/strict";
import { test } from "node:test";
import { sseEvents, unescapeSSE } from "../../frontend/sse.js";

// A fake Response.body whose getReader() emits the given string chunks. `split`
// lets a test deliver a payload one arbitrary byte-slice at a time to prove the
// parser reassembles across read() boundaries.
function bodyFromChunks(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    getReader() {
      return {
        read() {
          return i < chunks.length
            ? Promise.resolve({ done: false, value: enc.encode(chunks[i++]) })
            : Promise.resolve({ done: true, value: undefined });
        },
        cancel() {
          i = chunks.length;
          return Promise.resolve();
        },
        releaseLock() {},
      };
    },
  };
}

async function collect(chunks) {
  const out = [];
  for await (const ev of sseEvents(bodyFromChunks(chunks))) out.push(ev);
  return out;
}

test("parses a single frame", async () => {
  assert.deepEqual(await collect(["event: token\ndata: hi\n\n"]), [{ event: "token", data: "hi" }]);
});

test("skips keepalive comment frames", async () => {
  const evs = await collect([": keepalive\n\n", "event: done\ndata: \n\n"]);
  assert.deepEqual(evs, [{ event: "done", data: "" }]);
});

test("reassembles a frame split across chunk boundaries", async () => {
  assert.deepEqual(await collect(["event: tok", "en\ndata: h", "i\n\n"]), [{ event: "token", data: "hi" }]);
});

test("reassembles when the \\n\\n terminator itself is split", async () => {
  const evs = await collect(["event: token\ndata: hi\n", "\nevent: done\ndata: \n\n"]);
  assert.deepEqual(evs, [
    { event: "token", data: "hi" },
    { event: "done", data: "" },
  ]);
});

test("token data is NOT unescaped by the parser (raw \\n preserved)", async () => {
  const [ev] = await collect(["event: token\ndata: a\\nb\n\n"]);
  assert.equal(ev.data, "a\\nb"); // literal backslash-n, untouched
  assert.equal(unescapeSSE(ev.data), "a\nb"); // consumer un-escapes
});

test("probs JSON data is left raw so JSON.parse round-trips (must not unescape)", async () => {
  const json = '{"t":"x\\ny","p":0.5}';
  const [ev] = await collect([`event: probs\ndata: ${json}\n\n`]);
  assert.equal(ev.data, json); // untouched — unescaping would corrupt the JSON
  assert.deepEqual(JSON.parse(ev.data), { t: "x\ny", p: 0.5 });
});

test("a leading space in a token delta is preserved", async () => {
  const [ev] = await collect(["event: token\ndata:  world\n\n"]);
  assert.equal(ev.data, " world");
});

test("an event with empty data (director_start) dispatches with data \"\"", async () => {
  assert.deepEqual(await collect(["event: director_start\ndata: \n\n"]), [{ event: "director_start", data: "" }]);
});

test("multiple frames in one chunk all yield", async () => {
  const evs = await collect(["event: a\ndata: 1\n\nevent: b\ndata: 2\n\n"]);
  assert.deepEqual(evs, [
    { event: "a", data: "1" },
    { event: "b", data: "2" },
  ]);
});

test("a trailing partial frame (no terminator) is dropped", async () => {
  assert.deepEqual(await collect(["event: token\ndata: hi\n\nevent: partial\ndata: x"]), [{ event: "token", data: "hi" }]);
});

test("unescapeSSE only touches literal \\n sequences", () => {
  assert.equal(unescapeSSE("a\\nb\\nc"), "a\nb\nc");
  assert.equal(unescapeSSE("no escapes"), "no escapes");
});
