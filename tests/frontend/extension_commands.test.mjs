// The host command model, the fixed effect mapping, and the renderer-driven
// library sweep.
//
// Three properties get asserted here because they come from the *loop* and the
// *table*, not from any single invocation: built-ins always outrank community
// placements, `character.card` effects coalesce into a bounded number of
// refetches, and a runtime-generation bump stops a sweep instead of letting the
// server commit writes whose envelopes this tab would discard.
import assert from "node:assert/strict";
import { test } from "node:test";

// ── DOM + network shims ─────────────────────────────────────────────────────

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.style = { _props: {}, setProperty: () => {} };
    this.classList = { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false };
    this._text = "";
    this.className = "";
  }
  set textContent(v) {
    this._text = String(v);
  }
  get textContent() {
    return this._text;
  }
  setAttribute(n, v) {
    this.attributes[n] = v;
  }
  appendChild(c) {
    this.children.push(c);
    return c;
  }
  replaceChildren(...n) {
    this.children = n;
  }
  querySelectorAll() {
    return [];
  }
  addEventListener(n, fn) {
    (this.listeners[n] ||= []).push(fn);
  }
  allText() {
    return [this._text, ...this.children.flatMap((c) => c.allText())].filter(Boolean);
  }
}

const byId = new Map();
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => byId.get(id) ?? null,
  addEventListener() {},
  querySelectorAll: () => [],
  body: new FakeElement("body"),
};
globalThis.BroadcastChannel = undefined;
globalThis.setTimeout = ((fn) => {
  // Synchronous, so the debounce window is observable without waiting on a
  // real timer. Every scheduled callback is recorded rather than run.
  scheduled.push(fn);
  return scheduled.length;
}).bind(null);
const scheduled = [];
globalThis.clearTimeout = () => {};

const requests = [];
let responder = async () => ({});
globalThis.fetch = async (path, init) => {
  requests.push({ path, method: init?.method || "GET", body: init?.body ? JSON.parse(init.body) : null });
  const payload = await responder(path, init);
  return { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) };
};

const commands = await import("../../frontend/extension_commands.js");
const { S } = await import("../../frontend/state.js");

function reset() {
  requests.length = 0;
  scheduled.length = 0;
  byId.clear();
  S.extensionCatalog = [];
  S.extensionRuntimeGeneration = 0;
  S.activeConvId = "conv-1";
  S.settings = {};
}

function entry(over = {}) {
  return {
    id: "pkg",
    name: "Pkg",
    enabled: true,
    load_status: "available",
    active_digest: "d",
    commands: [],
    placements: [],
    ...over,
  };
}

// ── command model ───────────────────────────────────────────────────────────

test("built-ins form the first band and extensions the second", () => {
  reset();
  S.extensionCatalog = [
    entry({
      id: "zeta",
      commands: [{ id: "z", label: "Zeta thing", action: "run" }],
      placements: [{ slot: "composer.menu", command: "z" }],
    }),
    entry({
      id: "alpha",
      commands: [{ id: "a", label: "Alpha thing", action: "run" }],
      placements: [{ slot: "composer.menu", command: "a" }],
    }),
  ];
  commands.initExtensionCommands({ builtins: { "composer.menu": [{ id: "new", label: "New conversation" }] } });
  const labels = commands.slotCommands("composer.menu").map((c) => c.label);
  // Ordering is band, then extension id — never anything a manifest chooses.
  assert.deepEqual(labels, ["New conversation", "Alpha thing", "Zeta thing"]);
});

test("a disabled or unavailable extension contributes no placement", () => {
  reset();
  const placed = { commands: [{ id: "c", label: "X", action: "run" }], placements: [{ slot: "tools", command: "c" }] };
  S.extensionCatalog = [entry({ id: "off", enabled: false, ...placed })];
  commands.initExtensionCommands({});
  assert.deepEqual(commands.slotCommands("tools"), []);

  S.extensionCatalog = [entry({ id: "broken", load_status: "invalid", ...placed })];
  assert.deepEqual(commands.slotCommands("tools"), []);
});

test("a command's availability predicate is evaluated against the host projection", () => {
  reset();
  S.extensionCatalog = [
    entry({
      commands: [{ id: "c", label: "X", action: "run", when: { exists: { $ref: "host.active_conversation_id" } } }],
      placements: [{ slot: "composer.menu", command: "c" }],
    }),
  ];
  commands.initExtensionCommands({});
  const host = new FakeElement("div");
  byId.set("burger-dropdown", host);

  S.activeConvId = "conv-1";
  commands.renderComposerMenu();
  assert.equal(host.children.length, 1);

  S.activeConvId = null;
  commands.renderComposerMenu();
  assert.equal(host.children.length, 0, "the predicate must gate the rendered item, not just the model");
});

// ── the fixed effect mapping ────────────────────────────────────────────────

test("an unknown effect resource is dropped rather than dispatched", async () => {
  reset();
  let touched = false;
  commands.initExtensionCommands({ refetch: { conversation: async () => (touched = true) } });
  await commands.applyEffects({ runtime_generation: 1, effects: [{ resource: "settings.everything" }] });
  assert.equal(touched, false);
});

test("branch effects coalesce into one conversation refetch", async () => {
  reset();
  let conversationRefetches = 0;
  let notesRefetches = 0;
  commands.initExtensionCommands({
    refetch: {
      conversation: async () => {
        conversationRefetches += 1;
      },
      directionNotes: async () => {
        notesRefetches += 1;
      },
    },
  });
  await commands.applyEffects({
    runtime_generation: 1,
    effects: [
      { resource: "conversation.messages", conversation_id: "c" },
      { resource: "conversation.director", conversation_id: "c" },
      { resource: "conversation.direction_notes", conversation_id: "c" },
    ],
  });
  assert.equal(conversationRefetches, 1, "messages and director share one refetch path");
  assert.equal(notesRefetches, 1);
});

test("many character.card effects debounce into one scheduled library refetch", async () => {
  reset();
  let libraryRefetches = 0;
  commands.initExtensionCommands({
    refetch: {
      characters: () => {
        libraryRefetches += 1;
      },
    },
  });
  for (let i = 0; i < 50; i += 1) {
    await commands.applyEffects({ runtime_generation: 1, effects: [{ resource: "character.card", card_id: `c${i}` }] });
  }
  assert.equal(libraryRefetches, 0, "nothing fires until the debounce window closes");
  // Exactly one callback survives; the rest were superseded before running.
  scheduled.at(-1)();
  assert.equal(libraryRefetches, 1);
});

test("an envelope from an older generation is discarded whole", async () => {
  reset();
  S.extensionRuntimeGeneration = 5;
  let touched = false;
  commands.initExtensionCommands({ refetch: { catalog: async () => (touched = true) } });
  await commands.applyEffects({ runtime_generation: 4, effects: [{ resource: "extension.catalog" }] });
  assert.equal(touched, false);
  assert.equal(S.extensionRuntimeGeneration, 5);
});

// ── the library sweep ───────────────────────────────────────────────────────

function sweepView() {
  return {
    view_version: 1,
    data: {},
    root: { component: "library-sweep", action: "classify", label: "Run", unclassified_key: "tagged" },
  };
}

async function mountSweep(pages) {
  reset();
  S.extensionCatalog = [entry()];
  commands.initExtensionCommands({});
  let pageIndex = 0;
  responder = async (path) => {
    if (path.includes("/views/")) {
      return { runtime_generation: 0, id: "w", view: sweepView(), data: {}, config: {}, state: {}, errors: {} };
    }
    if (path.includes("/resources/library.cards")) return pages[pageIndex++] ?? { cards: [], next_cursor: null };
    // Same generation throughout: nothing installed, updated, or was disabled
    // while this sweep ran, so the loop must not stop.
    return { runtime_generation: 0, effects: [], toasts: [] };
  };
  const host = new FakeElement("div");
  await commands.mountView(host, "pkg", "w");
  const button = host.children
    .flatMap((n) => [n, ...n.children])
    .find((n) => n.tagName === "BUTTON" || n.className?.includes("xc-button"));
  return { host, button };
}

test("a sweep walks every page and dispatches one action per card", async () => {
  const { button } = await mountSweep([
    { cards: [{ id: "a", name: "A", state: {} }], next_cursor: "cur1" },
    { cards: [{ id: "b", name: "B", state: {} }], next_cursor: null },
  ]);
  for (const fn of button.listeners.click || []) await fn();
  const dispatched = requests.filter((r) => r.path.includes("/actions/classify"));
  assert.deepEqual(
    dispatched.map((r) => r.body.input.card_id),
    ["a", "b"],
  );
});

test("a sweep skips cards its own state already marks classified", async () => {
  const { button } = await mountSweep([
    {
      cards: [
        { id: "a", name: "A", state: { tagged: true } },
        { id: "b", name: "B", state: {} },
      ],
      next_cursor: null,
    },
  ]);
  for (const fn of button.listeners.click || []) await fn();
  const dispatched = requests.filter((r) => r.path.includes("/actions/classify"));
  assert.deepEqual(
    dispatched.map((r) => r.body.input.card_id),
    ["b"],
  );
});

test("a generation bump mid-sweep stops the loop and dispatches nothing after it", async () => {
  reset();
  S.extensionCatalog = [entry()];
  commands.initExtensionCommands({});
  let dispatched = 0;
  responder = async (path) => {
    if (path.includes("/views/")) {
      return { runtime_generation: 0, id: "w", view: sweepView(), data: {}, config: {}, state: {}, errors: {} };
    }
    if (path.includes("/resources/library.cards")) {
      return { cards: [{ id: "a", state: {} }, { id: "b", state: {} }, { id: "c", state: {} }], next_cursor: null };
    }
    dispatched += 1;
    // The second card's response arrives from a newer generation: an update or
    // a disable landed while the sweep was running.
    return { runtime_generation: dispatched >= 2 ? 9 : 0, effects: [], toasts: [] };
  };
  const host = new FakeElement("div");
  await commands.mountView(host, "pkg", "w");
  const button = host.children.flatMap((n) => [n, ...n.children]).find((n) => n.tagName === "BUTTON");
  for (const fn of button.listeners.click || []) await fn();

  assert.equal(dispatched, 2, "the third card must not be dispatched after the generation moved");
  const status = host.children.flatMap((n) => [n, ...n.children]).find((n) => n.className?.includes("xc-sweep-status"));
  assert.ok(status.textContent.includes("2"), `the completed count should be reported, got: ${status.textContent}`);
});

test("a sweep sends back only cursors the server issued", async () => {
  const { button } = await mountSweep([
    { cards: [{ id: "a", state: {} }], next_cursor: "opaque-token" },
    { cards: [], next_cursor: null },
  ]);
  for (const fn of button.listeners.click || []) await fn();
  const walks = requests.filter((r) => r.path.includes("/resources/library.cards"));
  assert.equal(walks.length, 2);
  assert.ok(walks[1].path.endsWith("cursor=opaque-token"));
});
