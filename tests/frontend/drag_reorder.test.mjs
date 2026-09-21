import assert from "node:assert/strict";
import { test } from "node:test";

import { JSDOM } from "jsdom";

const dom = new JSDOM(`
  <div id="root">
    <div class="lane" id="director">
      <div class="item" id="director-a"><button class="handle">A</button></div>
      <div class="item" id="director-b"><button class="handle">B</button></div>
    </div>
    <div class="lane" id="editor">
      <div class="item" id="editor-a"><button class="handle">E</button></div>
    </div>
  </div>
`);
globalThis.document = dom.window.document;
globalThis.getComputedStyle = dom.window.getComputedStyle;

const { initDragReorder } = await import("../../frontend/drag_reorder.js");

function pointer(target, type, { pointerId, clientY }) {
  const event = new dom.window.Event(type, { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    button: { value: 0 },
    clientX: { value: 0 },
    clientY: { value: clientY },
    pointerId: { value: pointerId },
    pointerType: { value: "mouse" },
  });
  target.dispatchEvent(event);
}

test("a nested reorder lane cannot drop an item into another lane", () => {
  const root = document.getElementById("root");
  const director = document.getElementById("director");
  const editor = document.getElementById("editor");
  const directorA = document.getElementById("director-a");
  const directorB = document.getElementById("director-b");
  const editorA = document.getElementById("editor-a");
  directorB.getBoundingClientRect = () => ({ top: 20, height: 10 });
  editorA.getBoundingClientRect = () => ({ top: 40, height: 10 });

  const reordered = [];
  const teardown = initDragReorder(root, {
    itemSelector: ".item",
    handleSelector: ".handle",
    itemContainer: (item) => item.closest(".lane"),
    onReorder: (container) => reordered.push(container),
  });

  pointer(directorA.querySelector(".handle"), "pointerdown", { pointerId: 1, clientY: 5 });
  pointer(document, "pointermove", { pointerId: 1, clientY: 100 });
  pointer(document, "pointerup", { pointerId: 1, clientY: 100 });

  assert.deepEqual([...director.children].map((item) => item.id), ["director-b", "director-a"]);
  assert.deepEqual([...editor.children].map((item) => item.id), ["editor-a"]);
  assert.deepEqual(reordered, [director]);
  teardown();
});
