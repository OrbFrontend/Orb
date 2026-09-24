import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM('<!doctype html><html><body><div id="chat-messages"></div></body></html>', {
  url: "https://orb.invalid/",
});
globalThis.window = dom.window;
for (const name of ["document", "Node", "NodeFilter", "Element", "DocumentFragment", "HTMLElement", "DOMParser"]) {
  globalThis[name] = dom.window[name];
}
dom.window.Element.prototype.scrollTo = function scrollTo() {};

// The app imports the chat facade during boot. That must connect workflow
// presentation before the first message repaint.
const { renderMessages } = await import("../../frontend/chat.js");
const { S } = await import("../../frontend/state.js");

test("workflow artifacts appear in a chat message after boot", () => {
  S.activeConvId = "c1";
  S.isStreaming = true; // keep the context-size timer out of this render test
  S.messages = [
    {
      id: 1,
      role: "assistant",
      content: "Hello",
      workflow_attachments: [{ id: 2, workflow_id: "example", filename: "proof.txt", mime_type: "text/plain" }],
    },
  ];

  renderMessages();

  const artifact = document.querySelector(".workflow-artifacts");
  assert.ok(artifact);
  assert.match(artifact.textContent, /proof\.txt/);
});
