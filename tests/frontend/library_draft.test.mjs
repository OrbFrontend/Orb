import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const key of ["document", "Node", "NodeFilter", "Element", "HTMLElement", "DOMParser", "MutationObserver"]) {
  globalThis[key] = dom.window[key];
}
const { api } = await import("../../frontend/api.js");
const { showCharEditModal, saveImportedChar } = await import("../../frontend/library.js");
const { closeModal } = await import("../../frontend/modal.js");
let saved;
beforeEach(() => {
  document.body.innerHTML = '<div id="modal-root"></div><div id="char-list"></div><div id="world-list"></div><div id="toast"></div>';
  saved = null;
  api.get = async (path) => path.endsWith("/expressions") ? { labels: [] } : [];
  api.post = async (path, data) => {
    assert.equal(path, "/characters");
    saved = data;
    return { ...data, id: "new-card" };
  };
});

test("an avatarless draft preserves provenance through the real editor save gate", async () => {
  await showCharEditModal({ name: "Mara", first_mes: "Hello, {{user}}.\nCome inside.", source_format: "generated" });
  await saveImportedChar();
  assert.equal(saved.source_format, "generated");
  assert.equal(saved.first_mes, "Hello, {{user}}.\nCome inside.");
  assert.ok(!("id" in saved));
});

test("an abandoned PNG import cannot lend its identity or avatar to the next draft", async () => {
  await showCharEditModal({ id: "abandoned", name: "Old", avatar_b64: "YWJj", source_format: "png" });
  closeModal();
  await showCharEditModal({ name: "New", first_mes: "Welcome.", source_format: "generated" });
  await saveImportedChar();
  assert.ok(!("id" in saved));
  assert.ok(!("avatar_b64" in saved));
  assert.equal(saved.source_format, "generated");
});
