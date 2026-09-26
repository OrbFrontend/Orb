import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const key of ["document", "Node", "NodeFilter", "Element", "HTMLElement", "DOMParser", "MutationObserver"]) {
  globalThis[key] = dom.window[key];
}
// jsdom has no scroller; the chat repaint the save triggers calls into one, and
// the save status scrolls itself into view.
dom.window.Element.prototype.scrollTo = function scrollTo() {};
dom.window.Element.prototype.scrollIntoView = function scrollIntoView() {};
const { api } = await import("../../frontend/api.js");
const { deleteCharacter, saveCharEdit, showCharEditModal } = await import("../../frontend/library.js");
const { closeModal } = await import("../../frontend/modal.js");

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

test("closing asks before dropping unsaved edits, and not after they are saved", async () => {
  const asked = [];
  window.confirm = (message) => {
    asked.push(message);
    return false;
  };
  await showCharEditModal(CARD.id);
  closeModal();
  assert.ok(!editorOpen(), "an untouched editor closes without asking");
  assert.equal(asked.length, 0);

  await showCharEditModal(CARD.id);
  document.getElementById("ce-desc").value = "A fence-mender.";
  closeModal();
  assert.ok(editorOpen(), "declining the prompt keeps the edits");
  assert.equal(asked.length, 1);

  await saveCharEdit(CARD.id);
  closeModal();
  assert.ok(!editorOpen(), "a saved editor closes without asking");
  assert.equal(asked.length, 1);
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

test("deleting a card offers to take its conversations only when it has some, and counts them", async () => {
  let usage = { solo: 0, active_groups: 0, historical_groups: 0 };
  api.get = async (path) => (path.endsWith("/usage") ? usage : []);
  await deleteCharacter(CARD.id);
  assert.equal(document.getElementById("delete-conversations-checkbox"), null);

  usage = { solo: 1, active_groups: 1, historical_groups: 1 };
  await deleteCharacter(CARD.id);
  const label = document.getElementById("delete-conversations-checkbox").closest("label");
  assert.equal(label.textContent.trim().replace(/\s+/g, " "), "Also delete 1 conversation and 2 group chats they appear in");
});
