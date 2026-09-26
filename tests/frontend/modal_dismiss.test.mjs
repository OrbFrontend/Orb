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
