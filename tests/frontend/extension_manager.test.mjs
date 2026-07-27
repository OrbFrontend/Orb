// The one property the extension manager exists to preserve: package-authored
// strings become *text*, never markup, never a handler, never an attribute.
//
// A minimal DOM shim rather than jsdom (this repo has no test deps): the shim
// records exactly which sink each value reached. That is stronger than checking
// the rendered output for entities -- it fails if a future edit switches a field
// to innerHTML even when the value it happened to be given was harmless.
import assert from "node:assert/strict";
import { test } from "node:test";

// ── DOM shim ────────────────────────────────────────────────────────────────

const innerHTMLWrites = [];

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = {};
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
  querySelector() {
    return null;
  }
  addEventListener(name, fn) {
    (this.listeners[name] ||= []).push(fn);
  }
  click() {}
  /** Every string this subtree would show a user, from textContent only. */
  allText() {
    return [this._text, ...this.children.flatMap((c) => c.allText())].filter(Boolean);
  }
  /** Every attribute value in this subtree, so a handler-looking one is visible. */
  allAttributes() {
    return [...Object.entries(this.attributes), ...this.children.flatMap((c) => c.allAttributes())];
  }
}

const byId = new Map();
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => byId.get(id) ?? null,
  addEventListener() {},
  body: new FakeElement("body"),
};
// Every fetch answers with a *stale* generation. Two things fall out: the
// tests never touch the network, and the manager's stale-response guard is
// exercised on every render -- a background refetch must never clobber the
// model a newer generation already replaced.
let lastFetchPath = null;
globalThis.fetch = async (path) => {
  lastFetchPath = path;
  return {
    ok: true,
    json: async () => ({ runtime_generation: -1, extensions: [], orphaned_data: [] }),
    text: async () => "",
  };
};

const manager = await import("../../frontend/extension_manager.js");
const { S } = await import("../../frontend/state.js");

// ── fixtures ────────────────────────────────────────────────────────────────

const XSS = '<img src=x onerror="alert(1)">';

function catalogEntry(over = {}) {
  return {
    id: "scene-meter",
    name: XSS,
    version: "1.0.0",
    author: XSS,
    description: XSS,
    source_kind: "archive",
    source_url: "",
    active_digest: "a".repeat(64),
    previous_digest: null,
    enabled: true,
    load_status: "available",
    diagnostic: "",
    blocked_entry_points: [],
    permissions: [],
    can_rollback: false,
    ...over,
  };
}

/** The first `<input>` in a rendered sidebar — each row has exactly one. */
function toggleOf(host) {
  const find = (node) =>
    node.tagName === "INPUT" ? node : node.children.reduce((hit, c) => hit ?? find(c), undefined);
  const box = find(host);
  assert.ok(box, "the row should render a toggle");
  return box;
}

function render(entries, orphans = []) {
  innerHTMLWrites.length = 0;
  byId.clear();
  const host = new FakeElement("div");
  byId.set("extensions-list", host);
  S.extensionCatalog = entries;
  S.extensionOrphanedData = orphans;
  manager.renderExtensionSidebar();
  return host;
}

// ── tests ───────────────────────────────────────────────────────────────────

test("a hostile package name is rendered as text, not markup", () => {
  const host = render([catalogEntry()]);
  assert.ok(host.allText().includes(XSS), "the name should be shown verbatim as text");
  assert.deepEqual(innerHTMLWrites, [], "nothing package-derived may reach innerHTML");
});

test("no package string becomes an event-handler attribute", () => {
  const host = render([catalogEntry({ diagnostic: XSS })]);
  for (const [name, value] of host.allAttributes()) {
    assert.ok(!/^on/i.test(name), `attribute ${name} looks like an inline handler`);
    assert.ok(!String(value).includes("alert("), `attribute ${name} carries package script`);
  }
});

test("the status chip reports only what the toggle cannot show", () => {
  const broken = render([catalogEntry({ load_status: "missing_content", diagnostic: "gone" })]);
  assert.ok(broken.allText().includes("missing content"));

  const limited = render([catalogEntry({ blocked_entry_points: ["hook post_pipeline"] })]);
  assert.ok(limited.allText().includes("limited"));

  // Enablement is the row's own toggle to display. A chip repeating it is a
  // second element for one fact, and the two drift when either renders stale --
  // the sidebar said "on" for an extension whose toggle was already off.
  const off = render([catalogEntry({ enabled: false })]);
  assert.ok(!off.allText().includes("disabled"), "off state must not become a chip");
  assert.equal(toggleOf(off).checked, false, "off state is shown by the row's toggle");

  const fine = render([catalogEntry()]);
  assert.ok(!fine.allText().includes("on"), "a healthy enabled row carries no chip at all");
  assert.equal(toggleOf(fine).checked, true);
});

test("the row toggle is the one on/off control, and nothing else gates it", () => {
  S.settings = {};
  const host = render([catalogEntry()]);
  const box = toggleOf(host);
  assert.equal(box.type, "checkbox");
  assert.equal(box.disabled, undefined);

  // No package string may reach an attribute, so the toggle must carry a real
  // listener rather than the inline `onchange="fn('<id>')"` the mood fragments
  // use -- the id in that string is package-authored.
  assert.ok(box.listeners.change?.length, "the toggle wires a listener, not an attribute");
  for (const [name] of host.allAttributes()) assert.ok(!/^on/i.test(name));

  // The Secondary master is a built-in-tier switch: an extension row stays live
  // and checked while it is off, rather than greying out for a reason that is
  // only visible in another panel.
  S.settings = { workflows_globally_enabled: false };
  const ungated = render([catalogEntry()]);
  assert.equal(toggleOf(ungated).disabled, undefined);
  assert.equal(toggleOf(ungated).checked, true);
  S.settings = {};
});

test("an empty catalog still offers a way into the manager", () => {
  const host = render([]);
  const text = host.allText();
  assert.ok(text.includes("No extensions installed"));
  assert.ok(text.some((t) => t.includes("Manage Extensions")));
});

/** Render the manager modal body against a pre-seeded root. */
function renderManager(entries, orphans = []) {
  innerHTMLWrites.length = 0;
  byId.clear();
  const root = new FakeElement("div");
  byId.set("modal-root", new FakeElement("div"));
  byId.set("ext-manager-root", root);
  S.extensionCatalog = entries;
  S.extensionOrphanedData = orphans;
  manager.showExtensionManagerModal(entries[0]?.id ?? null);
  return root;
}

test("the expanded card renders hostile package fields as text", () => {
  const root = renderManager([
    catalogEntry({
      diagnostic: XSS,
      blocked_entry_points: [XSS],
      permissions: [
        { value: { capability: "model.call", lane: "agent" }, capability: "model.call", parameters: { lane: XSS }, description: XSS, emphasis: "high", granted: true },
      ],
    }),
  ]);
  const text = root.allText();
  assert.ok(text.includes(XSS), "package strings must appear, as text");
  // showModal writes Orb's own static shell; no *package* string may ride along.
  for (const written of innerHTMLWrites) {
    assert.ok(!written.includes("onerror"), "package data reached innerHTML");
  }
  for (const [name, value] of root.allAttributes()) {
    assert.ok(!/^on/i.test(name), `attribute ${name} looks like an inline handler`);
    assert.ok(!String(value).includes("alert("));
  }
});

test("a permission's parameters read as part of its sentence, not as a column", () => {
  // The row is a flex line of [checkbox, sentence]. Appending the detail as a
  // third child made it a third *column*, so `(lane: agent)` was right-aligned
  // beside the wrapped sentence rather than following it.
  const root = renderManager([
    catalogEntry({
      permissions: [
        {
          value: { capability: "model.call", lane: "agent" },
          capability: "model.call",
          parameters: { lane: "agent" },
          description: "Make its own model calls.",
          granted: true,
        },
      ],
    }),
  ]);
  const rows = [];
  const walk = (node) => {
    if (node.className?.includes("ext-permission ") || node.className === "ext-permission") rows.push(node);
    for (const child of node.children) walk(child);
  };
  walk(root);
  const row = rows.find((r) => r.allText().some((t) => t.includes("Make its own model calls")));
  assert.ok(row, "the grant row was not rendered");
  const detailOwner = row.children.find((c) => c.children.some((g) => g.className === "ext-permission-detail"));
  assert.ok(detailOwner, "the detail must be nested inside the sentence span");
  assert.equal(detailOwner.textContent, "Make its own model calls.");
});

test("orphaned data from an uninstalled extension is listed so it stays purgeable", () => {
  // Uninstall preserves namespaced state on purpose; if the manager did not
  // list it, that data would be neither visible nor purgeable.
  const root = renderManager([], [{ id: "gone-extension", records: 3 }]);
  const text = root.allText().join("\n");
  assert.match(text, /Data left behind by uninstalled extensions/);
  assert.match(text, /gone-extension — 3 record\(s\)/);
  assert.match(text, /Purge/);
});

test("a response from an older generation is discarded, not merged", async () => {
  byId.clear();
  byId.set("extensions-list", new FakeElement("div"));
  S.extensionRuntimeGeneration = 7;
  S.extensionCatalog = [catalogEntry()];
  await manager.loadExtensionCatalog();
  assert.equal(lastFetchPath, "/api/extensions");
  // The shim answers with generation -1; merging it would resurrect a catalog
  // the user has already replaced.
  assert.equal(S.extensionRuntimeGeneration, 7);
  assert.equal(S.extensionCatalog.length, 1);
});

test("the catalog state keys exist on S with safe defaults", () => {
  assert.ok(Array.isArray(S.extensionCatalog));
  assert.ok(Array.isArray(S.extensionOrphanedData));
  assert.equal(typeof S.extensionRuntimeGeneration, "number");
});
