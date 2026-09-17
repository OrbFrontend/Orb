import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { url: "https://orb.invalid/" });
globalThis.window = dom.window;
for (const key of ["document", "Node", "NodeFilter", "Element", "HTMLElement", "DOMParser", "MutationObserver"]) {
  globalThis[key] = dom.window[key];
}
const { cardGeneratorToolHtml, mountCardGenerator } = await import("../../frontend/library_card_generator.js");
const tick = () => new Promise((resolve) => setImmediate(resolve));
let requests;
let dispose;
beforeEach(() => {
  document.body.innerHTML = "";
  requests = [];
  globalThis.fetch = async (url, options) => {
    let writer;
    const body = new ReadableStream({ start(controller) { writer = controller; } });
    requests.push({ url, ...options, writer });
    return { ok: true, body };
  };
});
afterEach(() => dispose?.());

function mount(onOpenDraft = () => {}) {
  const container = document.createElement("div");
  container.innerHTML = cardGeneratorToolHtml();
  document.body.append(container);
  const root = container.firstElementChild;
  dispose = mountCardGenerator(root, { onOpenDraft });
  const idea = root.querySelector("[data-cardgen-idea]");
  idea.value = "A harbour fence";
  idea.dispatchEvent(new window.Event("input", { bubbles: true }));
  return root;
}
function send(request, event, data) {
  request.writer.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${typeof data === "string" ? data : JSON.stringify(data)}\n\n`));
}
function generate(root) { root.querySelector('[data-cardgen-action="generate"]').click(); }

test("isolated markup and a double press produce one request, with thinking off", async () => {
  const root = mount();
  assert.equal(root.querySelectorAll("[id], [data-action]").length, 0);
  generate(root);
  root.querySelector('[data-cardgen-action="generate"]').dispatchEvent(new window.Event("click", { bubbles: true }));
  await tick();
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(requests[0].body), { idea: "A harbour fence", reasoning: false, tailoring: "off" });
});

function tailor(root, mode) {
  const select = root.querySelector("[data-cardgen-tailoring]");
  select.value = mode;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
}

for (const mode of ["summary", "deep"]) {
  test(`${mode} tailoring sends its mode`, async () => {
    const root = mount();
    tailor(root, mode);
    generate(root);
    await tick();
    const body = JSON.parse(requests[0].body);
    assert.equal(body.tailoring, mode);
    assert.equal(body.reasoning, mode === "deep");
  });
}

test("Deep locks thinking on and restores the user's choice when switched away", () => {
  const root = mount();
  const reasoning = root.querySelector("[data-cardgen-reasoning]");
  for (const choice of [false, true]) {
    tailor(root, "off");
    reasoning.checked = choice;
    tailor(root, "deep");
    assert.equal(reasoning.checked, true);
    assert.equal(reasoning.disabled, true);
    tailor(root, "deep");
    tailor(root, "summary");
    assert.equal(reasoning.checked, choice);
    assert.equal(reasoning.disabled, false);
  }
});

test("only Deep shows the data warning", () => {
  const root = mount();
  const note = root.querySelector("[data-cardgen-tailoring-note]");
  for (const mode of ["off", "summary", "deep", "summary"]) {
    tailor(root, mode);
    const deep = mode === "deep";
    assert.equal(note.classList.contains("is-warning"), deep);
    assert.equal(/Agent endpoint/.test(note.textContent), deep);
  }
});

test("running locks every control, and Deep keeps thinking locked afterwards", async () => {
  const root = mount();
  tailor(root, "deep");
  generate(root);
  await tick();
  const controls = ["[data-cardgen-idea]", "[data-cardgen-tailoring]", "[data-cardgen-reasoning]"].map((s) => root.querySelector(s));
  assert.deepEqual(controls.map((control) => control.disabled), [true, true, true]);
  root.querySelector('[data-cardgen-action="cancel"]').click();
  await tick();
  assert.deepEqual(controls.map((control) => control.disabled), [false, false, true]);
});

test("progress and multiline drafts reach the editor callback", async () => {
  let opened;
  const root = mount((card) => { opened = card; });
  tailor(root, "summary");
  generate(root);
  await tick();
  assert.equal(JSON.parse(requests[0].body).tailoring, "summary");
  send(requests[0], "progress", { label: "Reading preferences…" });
  await tick();
  assert.equal(root.querySelector("[data-cardgen-progress]").textContent, "Reading preferences…");
  const card = { name: "Mara", first_mes: "Hello.\nCome inside." };
  send(requests[0], "done", { card });
  requests[0].writer.close();
  await tick();
  assert.deepEqual(opened, card);
});

test("leaving the panel aborts the request and suppresses a queued draft", async () => {
  let opened = false;
  const root = mount(() => { opened = true; });
  generate(root);
  await tick();
  send(requests[0], "done", { card: { name: "Late" } });
  root.remove();
  await tick();
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(opened, false);
});

test("remount aborts the old run, whose completion cannot change the new run", async () => {
  let opened = false;
  const old = mount(() => { opened = true; });
  generate(old);
  await tick();
  const fresh = mount();
  generate(fresh);
  await tick();
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(requests[1].signal.aborted, false);
  assert.equal(fresh.querySelector('[data-cardgen-action="generate"]').disabled, true);
  assert.equal(opened, false);
});

test("Cancel restores controls and never opens a card", async () => {
  let opened = false;
  const root = mount(() => { opened = true; });
  generate(root);
  await tick();
  root.querySelector('[data-cardgen-action="cancel"]').click();
  await tick();
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(opened, false);
  assert.equal(root.querySelector('[data-cardgen-action="generate"]').disabled, false);
  assert.match(root.querySelector("[data-cardgen-progress]").textContent, /cancelled/);
  assert.equal(root.querySelector("[data-cardgen-progress]").classList.contains("is-error"), false);
  assert.equal(root.classList.contains("ml-busy"), false);
});

test("provider errors display as text and allow another attempt", async () => {
  const root = mount();
  generate(root);
  await tick();
  send(requests[0], "error", "Provider unavailable <script>");
  requests[0].writer.close();
  await tick();
  const progress = root.querySelector("[data-cardgen-progress]");
  assert.equal(progress.textContent, "Provider unavailable <script>");
  assert.equal(progress.classList.contains("is-error"), true);
  assert.equal(root.querySelectorAll("script").length, 0);
  generate(root);
  await tick();
  assert.equal(requests.length, 2);
  assert.equal(root.classList.contains("ml-busy"), true);
  assert.equal(progress.textContent, "");
  assert.equal(progress.classList.contains("is-error"), false);
});

test("an incomplete stream reports failure without opening an editor", async () => {
  const root = mount(() => assert.fail("No draft was returned"));
  generate(root);
  await tick();
  requests[0].writer.close();
  await tick();
  assert.match(root.querySelector("[data-cardgen-progress]").textContent, /without a draft/);
});
