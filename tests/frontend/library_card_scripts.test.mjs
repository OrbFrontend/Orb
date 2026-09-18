import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";
import { mountCardScriptsEditor } from "../../frontend/library_card_scripts.js";
import { applyCardScripts } from "../../frontend/card_scripts.js";

let root;
let window;
beforeEach(() => {
  ({ window } = new JSDOM('<div id="editor"></div>'));
  globalThis.document = window.document;
  root = window.document.getElementById("editor");
});
const row = (index = 0) => root.querySelector(`[data-script-index="${index}"]`);
const field = (name, index = 0) => row(index).querySelector(`[data-script-field="${name}"]`);
function input(name, value, index = 0) {
  const el = field(name, index);
  el.value = value;
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}
const click = (action, index = 0) => (action === "add" ? root : row(index)).querySelector(`[data-script-action="${action}"]`).click();

test("editing search and replacement preserves whitespace, unknown options, invalid entries and the source card", () => {
  const original = [{
    id: "imported", findRegex: "/old/g", replaceString: "\nold", placement: [2, 5],
    minDepth: 3, trimStrings: ["\n"], vendor: { extra: true },
  }, null, "bad", { placement: { includes: 1 } }];
  const snapshot = structuredClone(original);
  const read = mountCardScriptsEditor(root, original);
  assert.deepEqual(read(), original);
  assert.equal(field("replaceString").value, "\nold", "HTML parsing must not strip a leading newline");
  assert.equal(field("scope").value, "both");
  input("findRegex", " /new/g ");
  input("replaceString", " \n$1\n ");
  assert.deepEqual(read(), [{ ...original[0], findRegex: " /new/g ", replaceString: " \n$1\n " }, ...original.slice(1)]);
  assert.deepEqual(original, snapshot);
  read()[0].vendor.extra = false;
  assert.equal(read()[0].vendor.extra, true);
});

test("add, edit, reorder and remove change real projection order without dropping drafts", () => {
  const read = mountCardScriptsEditor(root);
  assert.equal(read(), undefined);
  click("add");
  input("findRegex", "/a/g");
  input("replaceString", "b");
  click("add");
  input("findRegex", "/b/g", 1);
  input("replaceString", "c", 1);
  assert.ok(read()[0].id);
  assert.notEqual(read()[0].id, read()[1].id);
  assert.equal(applyCardScripts("a", read(), "assistant"), "c");
  assert.equal(applyCardScripts("a", read(), "user"), "a");
  click("up", 1);
  assert.equal(applyCardScripts("a", read(), "assistant"), "b");
  assert.equal(field("findRegex").value, "/b/g");
  click("down", 0);
  assert.equal(applyCardScripts("a", read(), "assistant"), "c");
  input("replaceString", "", 1);
  assert.equal(applyCardScripts("a", read(), "assistant"), "");
  click("remove", 0);
  click("remove", 0);
  assert.deepEqual(read(), []);
  assert.match(root.textContent, /carries no scripts/);
});

test("scope, individual enablement and targets preserve unsupported placements", () => {
  const original = [{ findRegex: "a", replaceString: "b", placement: [2, 5, 6] }];
  const read = mountCardScriptsEditor(root, original);
  input("scope", "prompt");
  assert.equal(read()[0].promptOnly, true);
  assert.equal(read()[0].markdownOnly, false);
  assert.equal(applyCardScripts("a", read(), "assistant"), "a");
  input("scope", "display");
  assert.equal(applyCardScripts("a", read(), "assistant"), "b");
  input("scope", "both");
  assert.equal(read()[0].promptOnly, true);
  assert.equal(read()[0].markdownOnly, true);
  row().querySelector('[data-script-field="placement"][value="1"]').click();
  row().querySelector('[data-script-field="placement"][value="2"]').click();
  assert.deepEqual(read()[0].placement, [5, 6, 1]);
  assert.equal(applyCardScripts("a", read(), "user"), "b");
  field("enabled").click();
  assert.equal(applyCardScripts("a", read(), "user"), "a");
});

test("pattern warnings share rendering's literal parsing and imported HTML stays inert", () => {
  const html = '</textarea><img src=x onerror="alert(1)">';
  mountCardScriptsEditor(root, [{ scriptName: html, findRegex: "/a/x", replaceString: html, placement: [2], extra: html }]);
  assert.equal(root.querySelector("img"), null);
  assert.equal(field("replaceString").value, html);
  assert.match(row().textContent, /Unsupported flags/);
  input("findRegex", "/[/g");
  assert.match(row().textContent, /Invalid display regex/);
  input("findRegex", "/a/gg"); // malformed delimiters fall back to a bare pattern
  assert.equal(row().querySelector(".ce-script-warning").textContent, "");
  input("findRegex", "a".repeat(4097));
  assert.match(row().textContent, /4,096/);
});

test("a row explains the projection its scope selects rather than narrating the form", () => {
  mountCardScriptsEditor(root, [{ findRegex: "/a/g", replaceString: "b", placement: [2], markdownOnly: true }]);
  const note = () => row().querySelector(".ce-script-note").textContent;
  const warning = () => row().querySelector(".ce-script-warning").textContent;
  const label = () => row().querySelector("[data-script-scope-label]").textContent;
  assert.equal(label(), "Replace matches in");
  assert.match(note(), /Replaces matching text in the chat display/);
  assert.match(note(), /This script leaves the model prompt unchanged/);
  input("scope", "prompt");
  assert.match(note(), /Replaces matching text in the model prompt/);
  assert.match(note(), /This script leaves the chat display unchanged/);
  input("scope", "both");
  assert.match(note(), /Replaces matching text in both the chat display and the model prompt/);
  input("replaceString", "");
  assert.equal(label(), "Remove matches from");
  assert.match(note(), /Removes matching text from both the chat display and the model prompt/);
  input("scope", "display");
  assert.match(note(), /Removes matching text from the chat display/);
  assert.match(note(), /This script leaves the model prompt unchanged/);
  input("scope", "prompt");
  assert.match(note(), /Removes matching text from the model prompt/);
  assert.match(note(), /This script leaves the chat display unchanged/);
  input("replaceString", "$1");
  assert.equal(label(), "Replace matches in");
  assert.match(note(), /Replaces matching text in the model prompt/);
  field("enabled").click();
  assert.match(note(), /disabled/);
  field("enabled").click();
  row().querySelector('[data-script-field="placement"][value="2"]').click();
  assert.match(note(), /never runs/);
  assert.equal(warning(), "", "an untargeted script is already visible in its own checkboxes");
  input("findRegex", "");
  assert.equal(warning(), "", "an empty pattern is already visible in its own field");
});

test("opening a different draft does not retain abandoned scripts and preserves non-array imports", () => {
  const raw = { unsupported: true };
  let read = mountCardScriptsEditor(root, raw);
  assert.deepEqual(read(), raw);
  click("add");
  input("scriptName", "Abandoned");
  root = window.document.createElement("div");
  read = mountCardScriptsEditor(root, []);
  assert.deepEqual(read(), []);
  assert.equal(root.querySelector(".ce-script-row"), null);
});

test("rows stand collapsed to a preview of their own effect until they are opened", () => {
  mountCardScriptsEditor(root, [
    { scriptName: "dialogue", findRegex: "/<dialogue>/ig", replaceString: '<div\n  class="dialogue">', placement: [2], markdownOnly: true },
    { findRegex: "/a/g", replaceString: "", placement: [1, 2] },
    { findRegex: "", replaceString: "b", placement: [] },
  ]);
  const part = (name, index = 0) => row(index).querySelector(`[data-script-${name}]`).textContent;
  const collapsed = (index = 0) => row(index).classList.contains("is-collapsed");
  const toggle = (index = 0) => row(index).querySelector('[data-script-action="toggle"]');
  assert.ok([0, 1, 2].every((i) => collapsed(i)));
  assert.equal(part("label"), "dialogue");
  assert.equal(part("summary"), '/<dialogue>/ig → <div class="dialogue">', "a multi-line replacement previews on one line");
  assert.equal(part("meta"), "Assistant · Chat display");
  assert.equal(part("summary", 1), "/a/g → (removed)");
  assert.equal(part("meta", 1), "User and Assistant · Display and prompt");
  assert.equal(part("summary", 2), "No search pattern");
  assert.equal(part("meta", 2), "No authors · Display and prompt");
  toggle(0).click();
  assert.equal(collapsed(0), false);
  assert.equal(toggle(0).getAttribute("aria-expanded"), "true");
  assert.equal(toggle(0).getAttribute("aria-controls"), row(0).querySelector(".ce-script-body").id);
  toggle(0).click();
  assert.equal(collapsed(0), true);
});

test("a preview tracks its own edits and an open row rides along with reordering", () => {
  const read = mountCardScriptsEditor(root, [
    { scriptName: "  padded  ", findRegex: "/a/g", replaceString: "b", placement: [2] },
    { scriptName: "second", findRegex: "/c/g", replaceString: "d", placement: [2] },
  ]);
  const part = (name, index = 0) => row(index).querySelector(`[data-script-${name}]`).textContent;
  row(0).querySelector('[data-script-action="toggle"]').click();
  assert.equal(part("label"), "padded", "the header trims a name the card is free to keep padded");
  assert.equal(field("scriptName").value, "  padded  ");
  assert.equal(read()[0].scriptName, "  padded  ");
  input("scriptName", "renamed");
  input("findRegex", "/x/g");
  input("replaceString", "");
  input("scope", "prompt");
  assert.equal(part("label"), "renamed");
  assert.equal(part("summary"), "/x/g → (removed)");
  assert.equal(part("meta"), "Assistant · Model prompt");
  click("down", 0);
  assert.equal(row(0).classList.contains("is-collapsed"), true, "the untouched row keeps its collapsed state");
  assert.equal(row(1).classList.contains("is-collapsed"), false, "the open row stays open where it moved");
  assert.equal(part("label", 1), "renamed");
  assert.equal(read()[1].scriptName, "renamed");
});

test("a new script opens for editing and an unreadable entry has nothing to collapse", () => {
  mountCardScriptsEditor(root, ["bad"]);
  assert.equal(row().querySelector('[data-script-action="toggle"]'), null);
  assert.equal(row().classList.contains("is-collapsed"), false);
  assert.match(row().textContent, /Preserved imported data/);
  click("add");
  assert.equal(row(1).classList.contains("is-collapsed"), false);
  assert.equal(window.document.activeElement, field("scriptName", 1));
});
