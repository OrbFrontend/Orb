// The State panel's inline editor, driven through the real DOM against a stubbed API.
//
// The panel re-renders whenever the branch's state is re-read (a finished turn,
// a fragment toggle, a history load), so these pin what a re-render must not
// do to an edit in progress, and what a write must not race.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><body>
    <button id="state-panel-btn" class="hidden"></button>
    <div id="state-panel" class="open"><div id="state-panel-content"></div></div>
    <textarea id="composer"></textarea>
  </body>`,
  { url: "https://orb.invalid/" },
);
globalThis.window = dom.window;
for (const key of ["document", "Node", "Element", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent"]) {
  globalThis[key] = dom.window[key];
}

const { S } = await import("../../frontend/state.js");
const { refreshState } = await import("../../frontend/state_panel.js");

const CID = "conv-1";

function panelState(entries) {
  return {
    fragments: [
      {
        fragment_id: "threads",
        label: "Open threads",
        mode: "entries",
        update: "after_reply",
        inject: "both",
        origin: "global",
        enabled: true,
        configured: true,
        read_only: false,
        full: false,
        several_values: false,
        entries,
      },
    ],
    has_state: entries.length > 0,
    updates_on: true,
    limits: { text: 800, entries: 12 },
  };
}

const ENTRY = { entry_id: "e1", text: "The key is missing.", source: "agent", message_id: 1, turn_index: 1 };

// Each GET /state resolves when the test says so, so reads can be interleaved with writes.
let gets = [];
let posts = [];

function respond(body) {
  return { ok: true, json: async () => body, text: async () => JSON.stringify(body) };
}

globalThis.fetch = (url, opts = {}) => {
  if ((opts.method || "GET") === "POST") {
    const body = JSON.parse(opts.body);
    return new Promise((resolve) => posts.push({ body, resolve }));
  }
  return new Promise((resolve) => gets.push({ url, resolve }));
};

const content = () => document.getElementById("state-panel-content");
const click = (selector) => content().querySelector(selector).dispatchEvent(new MouseEvent("click", { bubbles: true }));
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function load(entries) {
  const pending = refreshState();
  gets.shift().resolve(respond(panelState(entries)));
  await pending;
}

function type(text) {
  const input = content().querySelector(".state-editor-input");
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(async () => {
  gets = [];
  posts = [];
  S.activeConvId = CID;
  // An enabled state fragment keeps the button, and so the panel, open without saved state.
  S.interactiveFragments = [{ id: "threads", field_type: "state", enabled: 1 }];
  await load([ENTRY]);
  if (content().querySelector('[data-state-action="cancel"]')) click('[data-state-action="cancel"]');
});

test("a re-read keeps the typed draft and does not take focus from the composer", async () => {
  click('[data-state-action="add"]');
  assert.equal(document.activeElement, content().querySelector(".state-editor-input"));
  type("The lie about the key.");
  document.getElementById("composer").focus();

  await load([ENTRY]);

  assert.equal(content().querySelector(".state-editor-input").value, "The lie about the key.");
  assert.equal(content().querySelector(".state-editor-count").textContent, "22/800");
  assert.equal(document.activeElement.id, "composer");
});

test("editing an entry starts from its text, and closes when a re-read retires it", async () => {
  click('[data-state-action="revise"][data-entry-id="e1"]');
  assert.equal(content().querySelector(".state-editor-input").value, ENTRY.text);

  await load([]);

  assert.equal(content().querySelector(".state-editor-input"), null);
});

test("a second save while one is in flight sends nothing", async () => {
  click('[data-state-action="add"]');
  type("A new thread.");
  click('[data-state-action="save"]');
  click('[data-state-action="save"]');
  await tick();
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { fragment_id: "threads", op: "add", text: "A new thread.", entry_id: "" });
  posts[0].resolve(respond({ changes: [], state: panelState([ENTRY]) }));
  await tick();
});

test("a read that started before a write cannot overwrite the write's state", async () => {
  const staleRead = refreshState();
  click('[data-state-action="add"]');
  type("Added after the read began.");
  click('[data-state-action="save"]');
  await tick();
  const added = { entry_id: "e2", text: "Added after the read began.", source: "user", message_id: 1, turn_index: 1 };
  posts[0].resolve(respond({ changes: [], state: panelState([ENTRY, added]) }));
  await tick();

  gets.shift().resolve(respond(panelState([ENTRY])));
  await staleRead;

  assert.match(content().textContent, /Added after the read began\./);
});
