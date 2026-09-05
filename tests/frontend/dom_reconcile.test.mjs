import assert from "node:assert/strict";
import { test } from "node:test";

// Minimal element stub: enough of the child/sibling API for the reconciler.
// innerHTML "parsing" wraps the string in one child, which is all the caller
// ever feeds it (one root element per row).
class FakeEl {
  constructor(html = "") {
    this.html = html;
    this.dataset = {};
    this.classes = new Set();
    this.parent = null;
    this._children = [];
    this.classList = {
      add: (c) => this.classes.add(c),
      remove: (c) => this.classes.delete(c),
      contains: (c) => this.classes.has(c),
    };
  }
  get children() {
    return this._children.slice();
  }
  get firstChild() {
    return this._children[0] ?? null;
  }
  get firstElementChild() {
    return this._children[0] ?? null;
  }
  get nextSibling() {
    if (!this.parent) return null;
    return this.parent._children[this.parent._children.indexOf(this) + 1] ?? null;
  }
  set innerHTML(html) {
    for (const c of this._children) c.parent = null;
    this._children = [];
    if (html) this.appendChild(new FakeEl(html));
  }
  remove() {
    if (!this.parent) return;
    const i = this.parent._children.indexOf(this);
    if (i >= 0) this.parent._children.splice(i, 1);
    this.parent = null;
  }
  insertBefore(node, anchor) {
    node.remove();
    const i = anchor ? this._children.indexOf(anchor) : -1;
    this._children.splice(i < 0 ? this._children.length : i, 0, node);
    node.parent = this;
    return node;
  }
  appendChild(node) {
    return this.insertBefore(node, null);
  }
}

globalThis.document = { createElement: () => new FakeEl() };

const { reconcileChildren } = await import("../../frontend/dom_reconcile.js");

const rows = (container) => container.children.map((c) => [c.dataset.rkey, c.html]);
const entries = (...pairs) => pairs.map(([key, html]) => ({ key, html }));

test("an unchanged row keeps its DOM node", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"]));
  const [a, b] = ct.children;

  const fresh = reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"]));

  assert.deepEqual(fresh, []);
  assert.equal(ct.children[0], a);
  assert.equal(ct.children[1], b);
});

test("only the row whose markup changed is rebuilt", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"], ["c", "<div>C</div>"]));
  const [a, b, c] = ct.children;

  const fresh = reconcileChildren(
    ct,
    entries(["a", "<div>A</div>"], ["b", "<div>B2</div>"], ["c", "<div>C</div>"]),
    "msg-swap",
  );

  assert.equal(fresh.length, 1);
  assert.equal(fresh[0].html, "<div>B2</div>");
  assert.equal(ct.children[0], a);
  assert.notEqual(ct.children[1], b);
  assert.equal(ct.children[2], c);
  assert.deepEqual(rows(ct), [
    ["a", "<div>A</div>"],
    ["b", "<div>B2</div>"],
    ["c", "<div>C</div>"],
  ]);
});

test("a replaced row is marked as a swap; a genuinely new row is not", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"]), "msg-swap");
  assert.equal(ct.children[0].classes.has("msg-swap"), false);

  reconcileChildren(ct, entries(["a", "<div>A2</div>"], ["b", "<div>B</div>"]), "msg-swap");

  assert.equal(ct.children[0].classes.has("msg-swap"), true, "in-place update swaps silently");
  assert.equal(ct.children[1].classes.has("msg-swap"), false, "an arriving row still animates");
});

test("dropped keys and foreign children are removed", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"]));
  const badge = new FakeEl("<div>badge</div>");
  ct.appendChild(badge);

  reconcileChildren(ct, entries(["a", "<div>A</div>"]));

  assert.deepEqual(rows(ct), [["a", "<div>A</div>"]]);
  assert.equal(badge.parent, null);
});

test("rows are reordered to match the entry order", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"], ["c", "<div>C</div>"]));
  const [a, b, c] = ct.children;

  const fresh = reconcileChildren(ct, entries(["c", "<div>C</div>"], ["a", "<div>A</div>"], ["b", "<div>B</div>"]));

  assert.deepEqual(fresh, [], "reordering reuses every node");
  assert.deepEqual(ct.children, [c, a, b]);
});

test("a prefix that is still identical survives an appended row", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"]));
  const [a, b] = ct.children;

  const fresh = reconcileChildren(ct, entries(["a", "<div>A</div>"], ["b", "<div>B</div>"], ["c", "<div>C</div>"]));

  assert.equal(fresh.length, 1);
  assert.deepEqual(ct.children.slice(0, 2), [a, b]);
  assert.equal(ct.children[2].html, "<div>C</div>");
});

test("repeated keys still render one node each", () => {
  // The caller must key id-less rows apart, but a collision must degrade to
  // "rebuild both" rather than dropping one of them off the screen.
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["p", "<div>USER</div>"], ["p", "<div>ASST</div>"]));
  assert.deepEqual(
    ct.children.map((c) => c.html),
    ["<div>USER</div>", "<div>ASST</div>"],
  );

  reconcileChildren(ct, entries(["p", "<div>USER</div>"], ["p", "<div>ASST</div>"]));
  assert.deepEqual(
    ct.children.map((c) => c.html),
    ["<div>USER</div>", "<div>ASST</div>"],
  );
});

test("a container wiped behind the reconciler's back rebuilds cleanly", () => {
  const ct = new FakeEl();
  reconcileChildren(ct, entries(["a", "<div>A</div>"]));
  ct.innerHTML = "<div class='empty-state'></div>";

  const fresh = reconcileChildren(ct, entries(["a", "<div>A</div>"]));

  assert.equal(fresh.length, 1, "the cached signature must not resurrect a discarded node");
  assert.deepEqual(rows(ct), [["a", "<div>A</div>"]]);
});
