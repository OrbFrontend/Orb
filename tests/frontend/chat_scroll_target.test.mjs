import assert from "node:assert/strict";
import { test } from "node:test";

let chat = null;
globalThis.document = {
  getElementById(id) {
    return id === "chat-messages" ? chat : null;
  },
};

const { pinStreamingMessage, scrollToMessage } = await import("../../frontend/utils.js");

function makeChat({ baseScrollHeight, scrollTop, targetTop, targetHeight }) {
  const target = {
    classList: { add() {} },
    getBoundingClientRect: () => ({ top: targetTop }),
    isConnected: true,
    offsetHeight: targetHeight,
  };
  return {
    target,
    ct: {
      clientHeight: 600,
      scrollHeight: baseScrollHeight,
      scrollTop,
      getBoundingClientRect: () => ({ top: 50 }),
      querySelector(selector) {
        if (selector === '.message[data-msg-id="7"]') return target;
        return null;
      },
      scrollTo(options) {
        this.lastScroll = options;
        this.scrollTop = options.top;
      },
    },
  };
}

test("message targeting centers a normal bubble using scroller-relative geometry", () => {
  const fixture = makeChat({
    baseScrollHeight: 1800,
    scrollTop: 900,
    targetTop: 250,
    targetHeight: 100,
  });
  chat = fixture.ct;

  scrollToMessage(7);

  assert.deepEqual(chat.lastScroll, { top: 850, behavior: "instant" });
});

test("message targeting clamps to the real bottom instead of adding trailing space", () => {
  const fixture = makeChat({
    baseScrollHeight: 1400,
    scrollTop: 700,
    targetTop: 550,
    targetHeight: 80,
  });
  chat = fixture.ct;

  scrollToMessage(7);

  assert.deepEqual(chat.lastScroll, { top: 800, behavior: "instant" });
});

test("a replacement stream is pinned without extending the real scroll range", () => {
  const fixture = makeChat({
    baseScrollHeight: 1400,
    scrollTop: 700,
    targetTop: 550,
    targetHeight: 80,
  });
  let pinned = false;
  fixture.target.classList.add = (name) => {
    pinned = name === "stream-scroll-target";
  };
  chat = fixture.ct;

  pinStreamingMessage(fixture.target);

  assert.equal(pinned, true);
  assert.deepEqual(chat.lastScroll, { top: 800, behavior: "instant" });
});
