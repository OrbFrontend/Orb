import assert from "node:assert/strict";
import { test } from "node:test";
import {
  applyCardScripts,
  cardScriptTurnedOff,
  configureCardScriptGuard,
  messageDisplaySource,
  projectCardDisplay,
  setCardScriptRepaint,
} from "../../frontend/card_scripts.js";
import { Worker as ThreadWorker } from "node:worker_threads";
import { projectCardScripts } from "../../frontend/card_script_worker.js";
import { S } from "../../frontend/state.js";

// Exercise the same pure projection that production executes in its worker.
const clearAll = () => configureCardScriptGuard({ project: projectCardScripts, storage: null, announce: () => {} });
clearAll();

const script = (extra = {}) => ({ findRegex: "/secret/g", replaceString: "visible", placement: [2], ...extra });

test("display flags, role, disabled, malformed scripts and ordering", () => {
  const scripts = [null, script({ disabled: true }), script({ promptOnly: true }), script(), script({ findRegex: "/visible/g", replaceString: "done" }), script({ findRegex: "/[/g" })];
  assert.equal(applyCardScripts("secret", scripts, "assistant"), "done");
  assert.equal(applyCardScripts("secret", scripts, "user"), "secret");
  assert.equal(applyCardScripts("secret", scripts, "system"), "secret");
  assert.equal(applyCardScripts("secret", [script({ promptOnly: true, markdownOnly: true })], "assistant"), "visible");
});

// Keep the browser and prompt projections in lockstep.
test("pattern flags and replacement tokens match the prompt channel", () => {
  const cases = [
    ["/^a.(b)$/gims", "$1", "A\nb\naXb", "b\nb"],
    ["/(a)(b)?/g", "$1|$2|$$|$&|$9|$12|\\n", "a", "a||$|a|$9|a2|\\n"],
    ["/a/", "X", "aa", "Xa"],
    ["/a/g", "X", "aa", "XX"],
    ["/<\\/div>/g", "", "hello</div>", "hello"],
    ["/b/", "$`-$'", "abc", "aa-cc"],
    ["/b(c)/g", "$0|{{MATCH}}", "abc", "abc|bc"],
    ["/(?<word>\\w+)/g", "[$<word>]", "hi there", "[hi] [there]"],
    ["/b/g", "[$<nope>]", "abc", "a[]c"],
  ];
  for (const [findRegex, replaceString, source, expected] of cases)
    assert.equal(applyCardScripts(source, [script({ findRegex, replaceString })], "assistant"), expected, findRegex);
});

test("a pattern without usable flags is its own first-match-only pattern", () => {
  const cases = [
    ["plain", "plain plain", "M plain"],
    ["/missing", "x /missing y", "x M y"],
    ["/a/y", "a /a/y b", "a M b"],
    ["/a/gg", "z /a/gg z", "z M z"],
  ];
  for (const [findRegex, source, expected] of cases)
    assert.equal(applyCardScripts(source, [script({ findRegex, replaceString: "M" })], "assistant"), expected, findRegex);
  for (const findRegex of ["/[broken/g", "/(unclosed/g", "/a/x"])
    assert.equal(applyCardScripts("abc", [script({ findRegex, replaceString: "X" })], "assistant"), "abc", findRegex);
});

test("input and output caps retain canonical text", () => {
  const scripts = [script({ findRegex: "/a/g", replaceString: "aa" })];
  for (const length of [60_000, 100_001]) {
    const text = "a".repeat(length);
    assert.equal(applyCardScripts(text, scripts, "assistant"), text);
  }
});

test("message projection selects solo and group card without changing stored text", () => {
  S.activeConvId = "conversation";
  S.conversations = [{ id: "conversation", character_card_id: "a", character_name: "Amy" }];
  S.allCharacters = [{ id: "a", display_scripts: [script({ findRegex: "/Amy/g" })] }, { id: "b", display_scripts: [script({ replaceString: "other" })] }];
  S.groupCast = null;
  const message = { role: "assistant", content: "{{char}}" };
  assert.equal(messageDisplaySource(message), "visible");
  assert.equal(message.content, "{{char}}");
  S.groupCast = { members: [{ id: "member", character_card_id: "b" }] };
  assert.equal(messageDisplaySource({ role: "assistant", content: "secret", speaker_member_id: "member" }), "other");
  assert.equal(messageDisplaySource({ role: "user", content: "secret" }), "secret");
  S.groupCast = { members: [], speakerCardIds: new Map([["former-member", "b"]]) };
  assert.equal(messageDisplaySource({ role: "assistant", content: "secret", speaker_member_id: "former-member" }), "other");
  S.groupCast = null;
});

test("CSS only attaches to assistant messages and cannot break out of its element", () => {
  const card = { display_css: '</style><p>injected</p>' };
  assert.equal(projectCardDisplay("hello", card, "user"), "hello");
  assert.ok(projectCardDisplay("hello", card, "assistant").startsWith('<style><\\/style>'));
});

test("CSS pasted with its style tags from a creator's note is unwrapped", () => {
  const face = "@font-face { font-family: board; src: url(a.ttf); }";
  const wrapped = { display_css: `Prose first.\n<style>${face}</style>\n<STYLE media="x">q { color: red; }` };
  assert.equal(projectCardDisplay("hi", wrapped, "assistant"), `<style>${face}\nq { color: red; }</style>\nhi`);
  assert.equal(projectCardDisplay("hi", { display_css: "<style></style>" }, "assistant"), "hi");
});

function memoryStorage() {
  const items = new Map();
  return { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, String(v)), removeItem: (k) => items.delete(k) };
}

test("only the worker result for this exact input is displayed, then the message list repaints", async () => {
  const pending = new Map();
  configureCardScriptGuard({
    project: (job) => new Promise((resolve) => pending.set(job.text, () => resolve(projectCardScripts(job)))),
    storage: null, announce: () => {},
  });
  let repaints = 0;
  setCardScriptRepaint(() => repaints++);
  const scripts = [script({ findRegex: "/held/g", replaceString: "shown" })];
  assert.equal(applyCardScripts("held", scripts, "assistant"), "held");
  assert.equal(applyCardScripts("held", scripts, "assistant"), "held");
  assert.equal(applyCardScripts("held back", scripts, "assistant"), "held back");
  assert.equal(pending.size, 2);
  pending.get("held")();
  await new Promise(setImmediate);
  assert.equal(repaints, 1);
  assert.equal(applyCardScripts("held", scripts, "assistant"), "shown");
  assert.equal(applyCardScripts("held back", scripts, "assistant"), "held back");
  setCardScriptRepaint(() => {});
  clearAll();
});

test("changed replacements and identity context invalidate the projection, and macros resolve between scripts", () => {
  clearAll();
  S.activeConvId = "c1";
  S.conversations = [{ id: "c1", character_name: "Amy" }];
  const scripts = [script({ replaceString: "{{char}}" }), script({ findRegex: "/Amy/", replaceString: "first" })];
  assert.equal(applyCardScripts("secret", scripts, "assistant"), "first");
  scripts[1].replaceString = "edited";
  assert.equal(applyCardScripts("secret", scripts, "assistant"), "edited");
  S.conversations[0].character_name = "Bea";
  assert.equal(applyCardScripts("secret", scripts, "assistant"), "Bea");
  S.conversations[0].character_name = "Amy";
});

function controlledWorkers(t) {
  const created = [];
  const active = new Set();
  let peak = 0;
  const previousWorker = globalThis.Worker;
  globalThis.Worker = class {
    constructor() {
      this.listeners = new Map();
      created.push(this);
      active.add(this);
      peak = Math.max(peak, active.size);
    }
    addEventListener(event, listener) { this.listeners.set(event, listener); }
    postMessage(job) { this.job = job; }
    complete() { this.listeners.get("message")({ data: projectCardScripts(this.job) }); }
    terminate() { active.delete(this); }
  };
  t.after(() => {
    clearAll();
    globalThis.Worker = previousWorker;
  });
  configureCardScriptGuard({ storage: null, announce: () => {} });
  return { created, active, get peak() { return peak; } };
}

async function finishWorkers(workers) {
  for (let i = 0; i < 100; i++) {
    await new Promise(setImmediate);
    if (!workers.active.size) return;
    for (const worker of [...workers.active]) worker.complete();
  }
  assert.fail("workers did not finish");
}

test("history projections share a bounded worker pool and queued messages still render", async (t) => {
  const workers = controlledWorkers(t);
  const owners = Array.from({ length: 20 }, () => ({}));
  const scripts = [script()];
  for (const [i, owner] of owners.entries())
    assert.equal(applyCardScripts(`secret ${i}`, scripts, "assistant", owner), `secret ${i}`);
  await new Promise(setImmediate);
  assert.equal(workers.active.size, 2);
  assert.equal(workers.created.length, 2);
  await finishWorkers(workers);
  assert.equal(workers.created.length, owners.length);
  assert.equal(workers.peak, 2);
  for (const [i, owner] of owners.entries())
    assert.equal(applyCardScripts(`secret ${i}`, scripts, "assistant", owner), `visible ${i}`);
});

test("streaming cancels evicted projections and ignores results from terminated workers", async (t) => {
  const workers = controlledWorkers(t);
  const owner = {};
  const scripts = [script()];
  applyCardScripts("secret 0", scripts, "assistant", owner);
  applyCardScripts("secret 1", scripts, "assistant", owner);
  await new Promise(setImmediate);
  const obsolete = [...workers.active];
  for (let i = 2; i < 100; i++) applyCardScripts(`secret ${i}`, scripts, "assistant", owner);
  assert.ok(obsolete.every((worker) => !workers.active.has(worker)), "eviction terminates running workers");
  for (const worker of obsolete)
    worker.listeners.get("message")({ data: { text: "stale", disabled: ["/secret/g"] } });
  await finishWorkers(workers);
  assert.equal(workers.peak, 2);
  assert.equal(workers.created.length, 6, "only the two original and four retained inputs start workers");
  assert.equal(cardScriptTurnedOff("/secret/g"), false);
  assert.equal(applyCardScripts("secret 99", scripts, "assistant", owner), "visible 99");
});

test("resetting the guard cancels both active and queued projections", async (t) => {
  const workers = controlledWorkers(t);
  for (let i = 0; i < 10; i++) applyCardScripts(`secret ${i}`, [script()], "assistant", {});
  await new Promise(setImmediate);
  configureCardScriptGuard({ storage: null, announce: () => {} });
  await new Promise(setImmediate);
  assert.equal(workers.active.size, 0);
  assert.equal(workers.created.length, 2);
  const owner = {};
  applyCardScripts("secret new", [script()], "assistant", owner);
  await finishWorkers(workers);
  assert.equal(applyCardScripts("secret new", [script()], "assistant", owner), "visible new");
});

test("a later catastrophic input is terminated off the page and its exact pattern stays disabled after reload", async (t) => {
  const created = [];
  const previousWorker = globalThis.Worker;
  globalThis.Worker = class {
    constructor(url) {
      this.thread = new ThreadWorker(new URL("./card_script_worker_fixture.mjs", import.meta.url), { workerData: url.href });
      this.ended = new Promise((resolve) => this.thread.once("exit", resolve));
      created.push(this);
    }
    addEventListener(event, fn) { this.thread.on(event, event === "message" ? (data) => fn({ data }) : fn); }
    postMessage(data) { this.thread.postMessage(data); }
    terminate() { void this.thread.terminate(); }
  };
  t.after(() => { globalThis.Worker = previousWorker; clearAll(); });
  const storage = memoryStorage();
  const notices = [];
  configureCardScriptGuard({ storage, announce: (message) => notices.push(message) });
  const scripts = [script({ findRegex: "/^(?:(?:abcd)+)+$/", replaceString: "safe" })];
  assert.equal(applyCardScripts("abcd", scripts, "assistant"), "abcd");
  await new Promise(setImmediate);
  await created[0].ended;
  assert.equal(applyCardScripts("abcd", scripts, "assistant"), "safe");
  const malicious = "abcd".repeat(32) + "!";
  assert.equal(applyCardScripts(malicious, scripts, "assistant"), malicious);
  let responsive = false;
  setTimeout(() => { responsive = true; }, 20);
  await new Promise(setImmediate);
  await created[1].ended;
  assert.ok(responsive, "the page's event loop stays responsive while the worker backtracks");
  assert.ok(cardScriptTurnedOff(scripts[0].findRegex));
  assert.match(notices[0], /too long to render/);
  configureCardScriptGuard({ storage, announce: () => {} });
  assert.equal(applyCardScripts(malicious, scripts, "assistant"), malicious);
  assert.equal(created.length, 2, "a persisted disabled pattern starts no worker after reload");
});
