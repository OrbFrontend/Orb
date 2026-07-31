import assert from "node:assert/strict";
import { test } from "node:test";

// chat_inspector.js is browser-facing and its import graph registers delegated
// handlers. These tiny stubs are enough to exercise the DOM-local scroll helper
// without pulling a browser emulator into the zero-dependency frontend tests.
globalThis.window = {};
globalThis.document = {
  addEventListener() {},
  createTextNode(text) {
    return { text };
  },
};

const { appendReasoningDelta } = await import("../../frontend/chat_inspector.js");

function reasoningBox({ scrollTop }) {
  return {
    scrollHeight: 300,
    scrollTop,
    clientHeight: 140,
    text: "",
    appendChild(node) {
      this.text += node.text;
      this.scrollHeight += 30;
    },
  };
}

test("reasoning stream follows when the box is at the bottom", () => {
  const box = reasoningBox({ scrollTop: 160 });

  appendReasoningDelta(box, "next");

  assert.equal(box.text, "next");
  assert.equal(box.scrollTop, box.scrollHeight);
});

test("reasoning stream keeps the user's position after they scroll up", () => {
  const box = reasoningBox({ scrollTop: 100 });

  appendReasoningDelta(box, "next");

  assert.equal(box.text, "next");
  assert.equal(box.scrollTop, 100);
});

test("reasoning stream re-enables following once the user returns near the bottom", () => {
  const box = reasoningBox({ scrollTop: 140 }); // 20px from the old bottom

  appendReasoningDelta(box, "next");

  assert.equal(box.scrollTop, box.scrollHeight);
});
