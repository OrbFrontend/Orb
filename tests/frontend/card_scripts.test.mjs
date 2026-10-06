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
  await created[0].ended;
  assert.equal(applyCardScripts("abcd", scripts, "assistant"), "safe");
  const malicious = "abcd".repeat(32) + "!";
  assert.equal(applyCardScripts(malicious, scripts, "assistant"), malicious);
  let responsive = false;
  setTimeout(() => { responsive = true; }, 20);
  await created[1].ended;
  assert.ok(responsive, "the page's event loop stays responsive while the worker backtracks");
  assert.ok(cardScriptTurnedOff(scripts[0].findRegex));
  assert.match(notices[0], /too long to render/);
  configureCardScriptGuard({ storage, announce: () => {} });
  assert.equal(applyCardScripts(malicious, scripts, "assistant"), malicious);
  assert.equal(created.length, 2, "a persisted disabled pattern starts no worker after reload");
});
