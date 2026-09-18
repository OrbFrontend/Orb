import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const key of ["document", "Node", "NodeFilter", "Element", "HTMLElement", "DOMParser", "MutationObserver"]) {
  globalThis[key] = dom.window[key];
}
// jsdom has no scroller; the chat repaint the save triggers calls into one.
dom.window.Element.prototype.scrollTo = function scrollTo() {};
const { api } = await import("../../frontend/api.js");
const { saveCharEdit, showCharEditModal } = await import("../../frontend/library.js");

const CARD = { id: "mara", name: "Mara", first_mes: "Hello." };
const status = () => document.getElementById("ce-save-status");
const editorOpen = () => !!document.getElementById("ce-name");

let put;
beforeEach(() => {
  document.body.innerHTML =
    '<div id="modal-root"></div><div id="char-list"></div><div id="world-list"></div><div id="conv-list"></div><div id="chat-messages"></div><div id="toast"></div>';
  put = null;
  api.get = async (path) => {
    if (path.endsWith("/expressions")) return { labels: [] };
    if (path === `/characters/${CARD.id}`) return { ...CARD };
    return [];
  };
  api.put = async (path, data) => {
    put = { path, data };
    return { ...CARD, ...data };
  };
});

test("saving an existing card keeps the editor open and confirms in the action row", async () => {
  await showCharEditModal(CARD.id);
  document.getElementById("ce-desc").value = "A fence-mender.";
  await saveCharEdit(CARD.id);

  assert.equal(put.path, `/characters/${CARD.id}`);
  assert.equal(put.data.description, "A fence-mender.");
  assert.ok(editorOpen(), "the editor stays open after a save");
  assert.equal(status().textContent, "Saved");
  assert.equal(status().classList.contains("is-error"), false);
  assert.equal(document.getElementById("ce-cancel-btn").textContent, "Close");
});

test("editing again retracts the confirmation", async () => {
  await showCharEditModal(CARD.id);
  await saveCharEdit(CARD.id);
  assert.equal(status().textContent, "Saved");

  const desc = document.getElementById("ce-desc");
  desc.value = "Second thoughts.";
  desc.dispatchEvent(new window.Event("input", { bubbles: true }));

  assert.equal(status().textContent, "");
  assert.equal(document.getElementById("ce-cancel-btn").textContent, "Cancel");
});

test("a failed save reports in the same line and leaves the card editable", async () => {
  api.put = async () => {
    throw new Error("Name already taken");
  };
  await showCharEditModal(CARD.id);
  await saveCharEdit(CARD.id);

  assert.ok(editorOpen(), "the editor stays open after a failed save");
  assert.equal(status().textContent, "Name already taken");
  assert.equal(status().classList.contains("is-error"), true);
  assert.equal(document.getElementById("ce-cancel-btn").textContent, "Cancel", "unsaved edits still read as a discard");
});

test("validation failures report in the action row, before any request", async () => {
  await showCharEditModal(CARD.id);
  document.getElementById("ce-name").value = "   ";
  await saveCharEdit(CARD.id);

  assert.equal(put, null, "no request is sent");
  assert.ok(status().textContent.length > 0);
  assert.equal(status().classList.contains("is-error"), true);
});
