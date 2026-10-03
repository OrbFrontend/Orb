import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>");
globalThis.document = dom.window.document;
const { registerAction } = await import("../../frontend/actions.js");

beforeEach(() => {
  document.body.innerHTML = "";
});

test("delegated actions receive their owning element and can cancel the event synchronously", () => {
  document.body.innerHTML = '<button data-wf-action="test:click"><span>Click</span></button>';
  const button = document.querySelector("button");
  const event = new dom.window.MouseEvent("click", { bubbles: true, cancelable: true });
  let called = false;
  registerAction("test", "click", async (element, received) => {
    assert.equal(element, button);
    assert.equal(received, event);
    received.preventDefault();
    called = true;
  });
  document.querySelector("span").dispatchEvent(event);
  assert.ok(called);
  assert.ok(event.defaultPrevented);
});

test("model message bodies cannot dispatch registered actions", () => {
  document.body.innerHTML = '<div class="msg-body"><button data-wf-action="test:blocked">Click</button></div>';
  let called = false;
  registerAction("test", "blocked", () => {
    called = true;
  });
  document.querySelector("button").click();
  assert.equal(called, false);
});

test("sync throws and async rejections use the same action error path", async (t) => {
  const failures = [];
  t.mock.method(console, "error", (...args) => failures.push(args));
  for (const name of ["sync", "async"]) {
    const error = new Error(name);
    registerAction(
      "test",
      name,
      name === "sync"
        ? () => {
            throw error;
          }
        : async () => {
            throw error;
          },
    );
    document.body.innerHTML = `<button data-wf-action="test:${name}">Click</button>`;
    document.querySelector("button").click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(failures.at(-1), [`data-wf-action "test:${name}" handler threw:`, error]);
  }
  assert.equal(failures.length, 2);
});
