import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const key of ["document", "Node", "Element", "HTMLElement", "KeyboardEvent", "MouseEvent"]) {
  globalThis[key] = dom.window[key];
}
const { closeModal, closeTopModal, setModalCloseGuard, setModalDismiss, showConfirmModal, showModal, showSubModal } =
  await import("../../frontend/modal.js");

const base = () => document.getElementById("modal-root").innerHTML;
const sub = () => document.getElementById("modal-sub-root").innerHTML;
const escape = () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
const backdrop = (rootId) => {
  const overlay = document.getElementById(rootId).firstElementChild;
  overlay.dispatchEvent(new MouseEvent("click", { bubbles: true }));
};

beforeEach(() => {
  document.body.innerHTML = '<div id="modal-root"></div><div id="modal-sub-root"></div><div id="modal-crop-root"></div>';
});

test("Escape and the backdrop dismiss the way Cancel does", () => {
  for (const dismiss of [escape, () => backdrop("modal-root")]) {
    let returned = 0;
    showModal("<h2>Edit</h2>");
    setModalDismiss(() => returned++);
    dismiss();
    assert.equal(returned, 1);
    assert.notEqual(base(), "", "the dismissal decides what happens to the modal");
  }
  showModal("<h2>Plain</h2>");
  escape();
  assert.equal(base(), "", "without a dismissal, dismissing closes");
});

test("a dismissal lasts until the modal closes or is replaced", () => {
  let aborted = 0;
  showModal("<h2>Compress</h2>");
  setModalDismiss(() => {
    aborted++;
    closeModal();
  });
  escape();
  assert.equal(aborted, 1);
  assert.equal(base(), "");
  showModal("<h2>Next</h2>");
  escape();
  assert.equal(aborted, 1, "the next modal does not inherit it");
});

test("only the top layer is dismissed", () => {
  let dismissed = 0;
  showModal("<h2>Editor</h2>");
  setModalDismiss(() => dismissed++);
  showSubModal("<h2>Fragment</h2>");
  assert.equal(closeTopModal(), true);
  assert.equal(sub(), "");
  assert.notEqual(base(), "");
  assert.equal(dismissed, 0);
  closeModal();
  assert.equal(closeTopModal(), false);
});

test("a close guard can keep the modal open", () => {
  showModal("<h2>Dirty</h2>");
  setModalCloseGuard(() => false);
  escape();
  assert.notEqual(base(), "");
  setModalCloseGuard(null);
  escape();
  assert.equal(base(), "");
});

test("a confirm runs the chosen action, then closes", () => {
  const ran = [];
  showConfirmModal({
    title: "Delete attachment",
    message: "Which?",
    actions: [
      { label: "Delete this variant", run: () => ran.push("variant") },
      { label: "Delete all 2", run: () => ran.push("group") },
    ],
  });
  const labels = [...document.querySelectorAll("#modal-root .modal-actions .btn")].map((b) => b.textContent);
  assert.deepEqual(labels, ["Cancel", "Delete this variant", "Delete all 2"]);
  document.querySelector('[data-confirm-action="1"]').click();
  assert.deepEqual(ran, ["group"]);
  assert.equal(base(), "");

  showConfirmModal({ title: "Rename", message: "", confirmText: "Save", confirmClass: "" }, () => {});
  const save = document.querySelector('[data-confirm-action="0"]');
  assert.equal(save.classList.contains("btn-danger"), false, "an empty confirmClass means a plain button");
  closeModal();

  showConfirmModal({ title: "Delete", message: "Sure?", confirmText: "Delete" }, () => ran.push("single"));
  document.querySelector('[data-confirm-action="cancel"]').click();
  assert.deepEqual(ran, ["group"], "Cancel runs nothing");
  assert.equal(base(), "");
});

// JSDOM has no layout; supply visibility rectangles for the keyboard tests.
function visibleControls(t) {
  t.mock.method(HTMLElement.prototype, "getClientRects", function () {
    return this.closest("[hidden]") || this.matches('input[type="hidden"]') ? [] : [{}];
  });
}

function tab(shiftKey = false) {
  const event = new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true });
  document.activeElement.dispatchEvent(event);
  return event;
}

test("dialogs focus their content, contain Tab, and restore their opener", async (t) => {
  visibleControls(t);
  document.body.insertAdjacentHTML("afterbegin", '<div id="app"><button id="opener">Open</button></div>');
  const opener = document.getElementById("opener");
  opener.focus();
  showModal(
    '<h2>Edit character</h2><input id="name"><button hidden>Hidden</button><button disabled>Busy</button><button id="save">Save</button>',
  );
  await Promise.resolve();

  const dialog = document.querySelector('[role="dialog"]');
  const name = document.getElementById("name");
  const save = document.getElementById("save");
  assert.equal(dialog.getAttribute("aria-modal"), "true");
  assert.equal(dialog.getAttribute("aria-label"), "Edit character");
  assert.equal(document.activeElement, name);
  assert.ok(document.getElementById("app").hasAttribute("inert"));
  assert.ok(tab(true).defaultPrevented);
  assert.equal(document.activeElement, save);
  assert.ok(tab().defaultPrevented);
  assert.equal(document.activeElement, name);

  closeModal();
  assert.equal(document.activeElement, opener);
  assert.equal(document.getElementById("app").hasAttribute("inert"), false);
});

test("nested dialogs keep the lower layer inert and restore its focused control", async (t) => {
  visibleControls(t);
  showModal('<h2>Character</h2><button id="edit-fragment">Edit fragment</button>');
  await Promise.resolve();
  const opener = document.getElementById("edit-fragment");
  assert.equal(document.activeElement, opener);
  showSubModal('<h2>Fragment</h2><input id="fragment">');
  await Promise.resolve();

  assert.equal(document.activeElement.id, "fragment");
  assert.ok(document.getElementById("modal-root").hasAttribute("inert"));
  assert.equal(document.querySelector("#modal-root .modal").getAttribute("aria-modal"), "false");
  closeTopModal();
  assert.equal(document.activeElement, opener);
  assert.equal(document.getElementById("modal-root").hasAttribute("inert"), false);
  assert.equal(document.querySelector("#modal-root .modal").getAttribute("aria-modal"), "true");
  closeModal();
});

test("replacing a dialog preserves its opener and respects caller-selected focus", async (t) => {
  visibleControls(t);
  document.body.insertAdjacentHTML("afterbegin", '<button id="opener">Open</button>');
  const opener = document.getElementById("opener");
  opener.focus();
  showModal('<h2>First</h2><input id="first">');
  await Promise.resolve();
  showModal('<h2>Second</h2><input id="default"><input id="selected">');
  const selected = document.getElementById("selected");
  selected.focus();
  await Promise.resolve();
  assert.equal(document.activeElement, selected);
  closeModal();
  assert.equal(document.activeElement, opener);
});

test("a dialog without controls retains keyboard focus until dismissal is allowed", async () => {
  showModal("<h2>Working</h2>");
  await Promise.resolve();
  const dialog = document.querySelector('[role="dialog"]');
  assert.equal(document.activeElement, dialog);
  assert.ok(tab().defaultPrevented);
  assert.equal(document.activeElement, dialog);
  setModalCloseGuard(() => false);
  escape();
  assert.equal(document.activeElement, dialog);
  setModalCloseGuard(null);
  escape();
  assert.equal(base(), "");
});
