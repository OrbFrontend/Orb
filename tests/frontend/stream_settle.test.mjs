import assert from "node:assert/strict";
import { test } from "node:test";

import { createStreamOperation, settledReply, streamAnchor } from "../../frontend/stream_settle.js";

// A /stop the test answers by hand, recording every call.
function stopServer() {
  const calls = [];
  const pending = [];
  return {
    calls,
    requestStop(convId) {
      calls.push(convId);
      return new Promise((resolve, reject) => pending.push({ resolve, reject }));
    },
    answer(reply) {
      pending.shift().resolve(reply);
    },
    fail() {
      pending.shift().reject(new Error("offline"));
    },
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("a settled stop keeps the stream open until the server closes it, and repeat clicks are one stop", async () => {
  const server = stopServer();
  const op = createStreamOperation({ convId: "c1", requestStop: server.requestStop });

  op.stop();
  op.stop();
  await tick();
  assert.deepEqual(server.calls, ["c1"]);

  server.answer({ ok: true, active: true, settled: true });
  // The server finished; its close arrives and the loop ends normally.
  const settled = op.settle();
  assert.equal(await settled, true);
  assert.equal(op.signal.aborted, false, "a settled stream is never dropped");
  assert.deepEqual(server.calls, ["c1"]);
});

test("stop before the request registered drops the connection, then waits for the server's cleanup", async () => {
  const server = stopServer();
  const op = createStreamOperation({ convId: "c1", requestStop: server.requestStop });

  op.stop();
  await tick();
  // Nothing registered yet: the request is still on its way.
  server.answer({ ok: true, active: false, settled: true });
  await tick();
  assert.equal(op.signal.aborted, true);

  const settled = op.settle();
  await tick();
  assert.deepEqual(server.calls, ["c1", "c1"], "settling asks again, now that the request may have registered");
  server.answer({ ok: true, active: true, settled: true });
  assert.equal(await settled, true);
});

test("a stop that times out or cannot be delivered drops the connection and is not claimed settled", async () => {
  for (const deliver of [(s) => s.answer({ ok: true, active: true, settled: false }), (s) => s.fail()]) {
    const server = stopServer();
    const op = createStreamOperation({ convId: "c1", requestStop: server.requestStop });
    op.stop();
    await tick();
    deliver(server);
    await tick();
    assert.equal(op.signal.aborted, true);

    const settled = op.settle();
    await tick();
    server.fail();
    assert.equal(await settled, false, "an unconfirmed save must not read as saved");
  }
});

test("a settled stream that never closes is dropped after the grace period", async () => {
  const server = stopServer();
  const op = createStreamOperation({ convId: "c1", requestStop: server.requestStop, closeGraceMs: 5 });
  op.stop();
  await tick();
  server.answer({ ok: true, active: true, settled: true });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(op.signal.aborted, true);
});

test("a stop pressed after the stream ended sends nothing", async () => {
  const server = stopServer();
  const op = createStreamOperation({ convId: "c1", requestStop: server.requestStop });
  assert.equal(await op.settle(), true);
  op.stop();
  await tick();
  assert.deepEqual(server.calls, []);
});

test("the settled reply is this operation's own, never the branch it regenerates", () => {
  const before = [
    { id: 1, role: "user", content: "hi" },
    { id: 2, role: "assistant", content: "original" },
  ];
  const anchor = streamAnchor(before);

  // Nothing new was saved: the original stays, and is not adopted.
  assert.equal(settledReply(before, anchor), null);

  const after = [
    { id: 1, role: "user", content: "hi" },
    { id: 9, role: "assistant", content: "the new sibling" },
  ];
  assert.equal(settledReply(after, anchor)?.id, 9);
});

test("a group turn matches its own exchange and speaker", () => {
  const anchor = streamAnchor([{ id: 1, role: "user", content: "hi" }]);
  const messages = [
    { id: 1, role: "user", content: "hi" },
    { id: 5, role: "assistant", exchange_id: "x1", speaker_member_id: "aria", content: "earlier speaker" },
    { id: 6, role: "assistant", exchange_id: "x2", speaker_member_id: "kael", content: "another request" },
  ];

  assert.equal(settledReply(messages, anchor, { exchangeId: "x1", memberId: "aria" })?.id, 5);
  assert.equal(settledReply(messages, anchor, { exchangeId: "x1", memberId: "kael" }), null);
});
