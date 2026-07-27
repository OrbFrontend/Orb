// The community component renderer, tested on the property it exists for:
// package-supplied values become *text*, and nothing else.
//
// A minimal DOM shim rather than jsdom (this repo has no test deps). The shim
// records which sink each value reached, which is stronger than checking the
// output for entities: it fails if a future edit switches a field to innerHTML
// even when the value it happened to be handed was harmless.
import assert from "node:assert/strict";
import { test } from "node:test";

const innerHTMLWrites = [];

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.classList = {
      _set: new Set(),
      add: (c) => this.classList._set.add(c),
      remove: (c) => this.classList._set.delete(c),
      toggle: (c, on) => (on ? this.classList._set.add(c) : this.classList._set.delete(c)),
      contains: (c) => this.classList._set.has(c),
    };
    this.style = {
      _props: {},
      setProperty: (name, value) => {
        this.style._props[name] = value;
      },
    };
    this._text = "";
    this.className = "";
  }
  set textContent(value) {
    this._text = String(value);
  }
  get textContent() {
    return this._text;
  }
  set innerHTML(value) {
    innerHTMLWrites.push(value);
    this._text = String(value);
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  querySelectorAll() {
    return [];
  }
  addEventListener(name, fn) {
    (this.listeners[name] ||= []).push(fn);
  }
  allText() {
    return [this._text, ...this.children.flatMap((c) => c.allText())].filter(Boolean);
  }
  allAttributes() {
    return [...Object.entries(this.attributes), ...this.children.flatMap((c) => c.allAttributes())];
  }
  allClasses() {
    return [this.className, ...this.children.flatMap((c) => c.allClasses())].filter(Boolean);
  }
  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) {
      const hit = child.find(predicate);
      if (hit) return hit;
    }
    return null;
  }
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (value) => {
    const node = new FakeElement("#text");
    node.textContent = value;
    return node;
  },
  getElementById: () => null,
  addEventListener() {},
  body: new FakeElement("body"),
};

const renderer = await import("../../frontend/extension_renderer.js");

const XSS = '<img src=x onerror="alert(1)">';

function render(view, extra = {}) {
  innerHTMLWrites.length = 0;
  renderer.clearDrafts();
  const host = new FakeElement("div");
  renderer.renderView(
    host,
    { view, data: extra.data || {}, config: extra.config || {}, state: extra.state || {}, errors: extra.errors || {} },
    {
      extensionId: extra.extensionId || "pkg",
      viewId: "v",
      instanceId: "0",
      digest: extra.digest || "d1",
      conversationId: extra.conversationId || null,
      onAction: extra.onAction || (async () => {}),
      onSaveState: extra.onSaveState || (async () => {}),
    },
  );
  return host;
}

function tree(root, data = {}) {
  return { view_version: 1, root, data: {} , ...(data.viewExtras || {}) };
}

// ── the core safety property ────────────────────────────────────────────────

test("a hostile text value renders as text and never as markup", () => {
  const host = render(tree({ component: "text", value: XSS }));
  assert.ok(host.allText().includes(XSS));
  assert.deepEqual(innerHTMLWrites, []);
});

test("no package string becomes an event-handler attribute", () => {
  const host = render(
    tree({
      component: "stack",
      children: [
        { component: "badge", value: XSS },
        { component: "image", source: { kind: "asset", path: "a.png" }, alt: XSS },
        { component: "empty-state", title: XSS, description: XSS },
      ],
    }),
  );
  for (const [name, value] of host.allAttributes()) {
    assert.ok(!/^on/i.test(name), `attribute ${name} looks like an inline handler`);
    assert.ok(!String(value).includes("alert("), `attribute ${name} carries package script`);
  }
});

test("markdown is built from nodes, so no HTML in it is ever parsed", () => {
  const host = render(tree({ component: "markdown", value: `**bold** ${XSS}\n- item` }));
  assert.deepEqual(innerHTMLWrites, []);
  assert.ok(host.allText().some((t) => t.includes("<img src=x")), "the raw tag should survive as literal text");
  assert.ok(host.allText().includes("bold"));
});

// ── tokenized styling ───────────────────────────────────────────────────────

test("an out-of-table style token contributes no class", () => {
  const host = render(tree({ component: "text", value: "x", tone: "evil-tone", size: "'; drop" }));
  for (const className of host.allClasses()) {
    assert.ok(!className.includes("evil-tone"), `token leaked into a class: ${className}`);
    assert.ok(!className.includes("drop"), `token leaked into a class: ${className}`);
  }
});

test("grid columns are written as a custom property, never as a style string", () => {
  const host = render(tree({ component: "grid", columns: 3, children: [] }));
  const grid = host.find((n) => n.className?.includes("xc-grid"));
  assert.equal(grid.style._props["--xc-columns"], "3");
  assert.deepEqual(grid.attributes, {}, "no style attribute should be set at all");
});

test("an out-of-range column count falls back rather than being written through", () => {
  const host = render(tree({ component: "grid", columns: 99, children: [] }));
  assert.equal(host.find((n) => n.className?.includes("xc-grid")).style._props["--xc-columns"], "2");
});

// ── unknown components ──────────────────────────────────────────────────────

test("an unknown component renders an error rather than silently vanishing", () => {
  const host = render(tree({ component: "iframe", src: "https://evil.invalid" }));
  assert.ok(host.allText().some((t) => t.includes("iframe")));
  assert.ok(host.allText().includes("Unavailable") || host.allClasses().some((c) => c.includes("xc-error")));
});

// ── media by reference ──────────────────────────────────────────────────────

test("an asset image resolves to an Orb route, never to a package URL", () => {
  const host = render(tree({ component: "image", source: { kind: "asset", path: "icons/a.png" }, alt: "a" }));
  const img = host.find((n) => n.tagName === "IMG");
  assert.equal(img.src, "/api/extensions/pkg/assets/icons/a.png");
});

test("a filename with a query character cannot become a query string", () => {
  const host = render(tree({ component: "image", source: { kind: "asset", path: "a?b#c.png" }, alt: "a" }));
  const img = host.find((n) => n.tagName === "IMG");
  assert.ok(!img.src.includes("?b"), `query character survived: ${img.src}`);
  assert.ok(!img.src.includes("#c"), `fragment character survived: ${img.src}`);
});

test("an artifact source with a non-integer id resolves to nothing", () => {
  const host = render(tree({ component: "image", source: { kind: "artifact", attachment_id: "../../etc" }, alt: "a" }));
  assert.equal(host.find((n) => n.tagName === "IMG"), null);
});

// ── value resolution ────────────────────────────────────────────────────────

test("a path never reaches a prototype", () => {
  assert.equal(renderer.resolvePath({ data: {} }, "data.__proto__.constructor"), undefined);
  assert.equal(renderer.resolvePath({ data: { a: [1, 2] } }, "data.a.1"), 2);
  assert.equal(renderer.resolvePath({ data: { a: [1] } }, "data.a.9"), undefined);
});

test("templates substitute scalars and render containers as empty", () => {
  const value = renderer.resolveValue({ $template: "x={{data.n}} y={{data.list}}" }, { data: { n: 4, list: [1] } });
  assert.equal(value, "x=4 y=");
});

test("predicates are total and type-strict", () => {
  const ns = { data: { n: 1, s: "a" } };
  assert.equal(renderer.evaluatePredicate({ eq: [{ $ref: "data.n" }, true] }, ns), false);
  assert.equal(renderer.evaluatePredicate({ lt: [{ $ref: "data.s" }, { $ref: "data.n" }] }, ns), false);
  assert.equal(renderer.evaluatePredicate({ exists: { $ref: "data.missing" } }, ns), false);
  assert.equal(renderer.evaluatePredicate({ and: [{ exists: { $ref: "data.n" } }] }, ns), true);
});

test("a false `when` removes the node entirely", () => {
  const host = render(
    tree({
      component: "stack",
      children: [
        { component: "text", value: "shown", when: { exists: { $ref: "data.present" } } },
        { component: "text", value: "hidden", when: { exists: { $ref: "data.absent" } } },
      ],
    }),
    { data: { present: 1 } },
  );
  assert.ok(host.allText().includes("shown"));
  assert.ok(!host.allText().includes("hidden"));
});

// ── forms ───────────────────────────────────────────────────────────────────

test("rendering a bound control writes no state and adds a host save bar", () => {
  let saved = null;
  const host = render(tree({ component: "text-input", bind: "config.name", label: "Name" }), {
    config: { name: "current" },
    onSaveState: async (updates) => {
      saved = updates;
    },
  });
  assert.equal(saved, null, "rendering must not write");
  const save = host.find((n) => n.className?.includes("xc-save-bar"));
  assert.ok(save, "a bound view gets a host-owned save control");
});

test("the save bar groups the draft by scope", async () => {
  let saved = null;
  const host = render(
    tree({
      component: "stack",
      children: [
        { component: "text-input", bind: "config.name", label: "Name" },
        { component: "toggle", bind: "state.conversation.on", label: "On" },
      ],
    }),
    {
      onSaveState: async (updates) => {
        saved = updates;
      },
    },
  );
  const input = host.find((n) => n.tagName === "INPUT" && n.type === "text");
  input.value = "typed";
  for (const fn of input.listeners.input || []) fn();
  const toggle = host.find((n) => n.tagName === "INPUT" && n.type === "checkbox");
  toggle.checked = true;
  for (const fn of toggle.listeners.change || []) fn();

  const button = host.find((n) => n.className?.includes("xc-button"));
  for (const fn of button.listeners.click || []) await fn();
  assert.deepEqual(saved, { config: { name: "typed" }, conversation: { on: true } });
});

test("a lines textarea shows an array one per line and saves it back as an array", async () => {
  let saved = null;
  const host = render(
    tree({ component: "textarea", bind: "config.vocabulary", label: "Vocabulary", value_kind: "lines" }),
    { config: { vocabulary: ["noir", "detective"] }, onSaveState: async (updates) => (saved = updates) },
  );
  const field = host.find((n) => n.tagName === "TEXTAREA");
  assert.equal(field.value, "noir\ndetective", "the stored array renders one member per line");

  field.value = "noir\n  detective  \n\nheist\n";
  for (const fn of field.listeners.input || []) fn();
  const button = host.find((n) => n.className?.includes("xc-button"));
  for (const fn of button.listeners.click || []) await fn();
  // Trimmed, blanks dropped: the flow reading this key runs list.join over it.
  assert.deepEqual(saved, { config: { vocabulary: ["noir", "detective", "heist"] } });
});

test("a view with no bound controls gets no save bar", () => {
  const host = render(tree({ component: "text", value: "read only" }));
  assert.equal(host.find((n) => n.className?.includes("xc-save-bar")), null);
});

test("a button dispatches only its declared input, never the form draft", async () => {
  const calls = [];
  const host = render(
    tree({
      component: "stack",
      children: [
        { component: "text-input", bind: "config.secret", label: "Secret" },
        { component: "button", label: "Go", action: "run", input: { mode: "fast" } },
      ],
    }),
    { onAction: async (action, input) => calls.push([action, input]) },
  );
  const input = host.find((n) => n.tagName === "INPUT" && n.type === "text");
  input.value = "typed";
  for (const fn of input.listeners.input || []) fn();
  const button = host.find((n) => n.className?.includes("xc-button") && n.textContent === "Go");
  for (const fn of button.listeners.click || []) await fn();
  assert.deepEqual(calls, [["run", { mode: "fast" }]]);
});

// ── trees ───────────────────────────────────────────────────────────────────

const NODES = [
  { id: 1, parent_id: null, role: "user", child_count: 2, preview: XSS },
  { id: 2, parent_id: 1, role: "assistant", child_count: 0, preview: "a" },
  { id: 3, parent_id: 1, role: "assistant", child_count: 0, preview: "b" },
];

test("the host computes tree depth from parent_id, not from the package", () => {
  const host = render(
    tree({
      component: "conversation-tree",
      nodes: { $ref: "data.nodes" },
      active_path: { $ref: "data.path" },
      select_action: "select",
      show_previews: true,
    }),
    { data: { nodes: NODES, path: [1, 2] } },
  );
  const rows = [];
  const collect = (node) => {
    if (node.className?.includes("xc-tree-row")) rows.push(node);
    for (const child of node.children) collect(child);
  };
  collect(host);
  assert.equal(rows.length, 3);
  assert.deepEqual(
    rows.map((r) => r.style._props["--xc-depth"]),
    ["0", "1", "1"],
  );
  assert.equal(rows.filter((r) => r.className.includes("xc-tree-active")).length, 2);
});

test("a tree preview is text even when it is a tag", () => {
  const host = render(
    tree({
      component: "conversation-tree",
      nodes: { $ref: "data.nodes" },
      active_path: [],
      select_action: "select",
      show_previews: true,
    }),
    { data: { nodes: NODES } },
  );
  assert.ok(host.allText().includes(XSS));
  assert.deepEqual(innerHTMLWrites, []);
});

test("a node whose parent is unknown is treated as a root rather than dropped", () => {
  const orphaned = [{ id: 9, parent_id: 404, role: "assistant", child_count: 0 }];
  const host = render(
    tree({ component: "tree", nodes: { $ref: "data.nodes" }, select_action: null }),
    { data: { nodes: orphaned } },
  );
  assert.ok(host.allText().includes("assistant"));
});

// ── draft disposal ──────────────────────────────────────────────────────────

test("drafts keyed to a departed digest are dropped on disposal", () => {
  const host = render(tree({ component: "text-input", bind: "config.name", label: "Name" }), { digest: "old" });
  const input = host.find((n) => n.tagName === "INPUT");
  input.value = "typed";
  for (const fn of input.listeners.input || []) fn();

  renderer.disposeStaleDrafts(["new"]);

  let saved = null;
  const again = render(tree({ component: "text-input", bind: "config.name", label: "Name" }), {
    digest: "old",
    onSaveState: async (updates) => {
      saved = updates;
    },
  });
  const bar = again.find((n) => n.className?.includes("xc-save-bar"));
  const button = bar.find((n) => n.className?.includes("xc-button"));
  for (const fn of button.listeners.click || []) fn();
  assert.equal(saved, null, "a dropped draft must not resubmit a departed revision's values");
});

// ── icons ───────────────────────────────────────────────────────────────────

test("an unknown icon name yields nothing rather than passing through", () => {
  assert.equal(renderer.iconGlyph("git-branch").length > 0, true);
  assert.equal(renderer.iconGlyph("../../evil"), "");
  assert.equal(renderer.iconGlyph("constructor"), "");
});
