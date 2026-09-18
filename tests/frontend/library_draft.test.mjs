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

test("the card save includes edited regex drafts and preserves unrelated extensions", async () => {
  const extensions = {
    regex_scripts: [{ id: "imported", findRegex: "/old/g", replaceString: "old", placement: [2, 5], minDepth: 3 }],
    foreign: { keep: true },
    orb: { display_css: ".dialogue { color: red; }" },
  };
  await showCharEditModal({ name: "Scripted", extensions });
  const replacement = document.querySelector('[data-script-field="replaceString"]');
  replacement.value = " \n$1\n ";
  replacement.dispatchEvent(new window.Event("input", { bubbles: true }));
  document.querySelector('[data-script-field="enabled"]').click();
  document.getElementById("ce-scripts-enabled").checked = false;
  await saveImportedChar();
  assert.deepEqual(saved.extensions, {
    ...extensions,
    regex_scripts: [{ ...extensions.regex_scripts[0], replaceString: " \n$1\n ", disabled: true }],
    orb: { ...extensions.orb, card_scripts_enabled: false },
  });
  assert.equal(extensions.regex_scripts[0].replaceString, "old");

  await showCharEditModal({ name: "Scripted", extensions });
  document.querySelector('[data-script-action="remove"]').click();
  closeModal();
  await showCharEditModal({ name: "Scripted", extensions });
  await saveImportedChar();
  assert.deepEqual(saved.extensions, extensions, "Cancel discards script deletion");

  await showCharEditModal({ name: "Scripted", extensions });
  document.querySelector('[data-script-action="remove"]').click();
  await saveImportedChar();
  assert.deepEqual(saved.extensions.regex_scripts, [], "Save persists deleting the last script");
});

test("generated fields at every clamp limit pass the actual form save", async () => {
  const draft = {
    name: "N".repeat(100), description: "D".repeat(6000), personality: "P".repeat(1200),
    scenario: "S".repeat(1600), first_mes: "F".repeat(2000), mes_example: "E".repeat(1600),
    creator_notes: "C".repeat(400), source_format: "generated",
  };
  await showCharEditModal(draft);
  await saveImportedChar();
  for (const [key, value] of Object.entries(draft)) assert.equal(saved[key], value);
});

test("the generated editor restores Manager after save or cancel", async () => {
  const { showCharacterBrowserModal } = await import("../../frontend/library_browser.js");
  const { S } = await import("../../frontend/state.js");
  const tick = () => new Promise((resolve) => setImmediate(resolve));
  S.characterBrowserView = "list";
  api.get = async (path) => path === "/library/tags"
    ? { vocabulary: [], total: 0, pending: 0, tagged: 0, revision: "test" }
    : [];
  globalThis.fetch = async () => new Response(`event: start\ndata: {}\n\nevent: done\ndata: ${JSON.stringify({ card: {
    name: "Generated", first_mes: "Welcome.", source_format: "generated",
  } })}\n\n`);
  for (const save of [false, true]) {
    await showCharacterBrowserModal();
    document.querySelector('[data-view="manager"]').click();
    const idea = document.querySelector("[data-cardgen-idea]");
    idea.value = "A fence";
    idea.dispatchEvent(new window.Event("input", { bubbles: true }));
    document.querySelector('[data-cardgen-action="generate"]').click();
    await tick();
    assert.equal(document.getElementById("ce-name").value, "Generated");
    if (save) await saveImportedChar();
    else closeModal();
    await tick();
    assert.ok(document.querySelector('[data-tool="card-generator"]'));
    assert.equal(document.querySelector('[data-view="manager"]').classList.contains("active"), true);
    assert.equal(S.characterBrowserView, "list", "restoring Manager does not change the saved card view");
    closeModal();
  }
});
