// The post-processing "Run only when" field in the shared interactive-fragment
// form, driven through the real DOM for both the global and the card editor.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM('<!doctype html><body><div id="modal-root"></div><div id="modal-sub-root"></div></body>', {
  url: "https://orb.invalid/",
});
globalThis.window = dom.window;
for (const key of ["document", "Node", "Element", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent"]) {
  globalThis[key] = dom.window[key];
}

// /api/decisions/config answers when the test resolves it, so the form can be
// seen before and after the config arrives.
let pendingConfig = null;
const writes = [];
globalThis.fetch = async (url, opts = {}) => {
  if (url === "/api/decisions/config") {
    const payload = await new Promise((resolve) => {
      pendingConfig = resolve;
    });
    if (payload === null) return { ok: false, status: 500, text: async () => "down" };
    return { ok: true, json: async () => payload };
  }
  writes.push({ url, method: opts.method, body: opts.body ? JSON.parse(opts.body) : null });
  // The list reload after a save returns the stored fragment.
  return { ok: true, json: async () => (opts.body ? JSON.parse(opts.body) : [structuredClone(GATED)]) };
};

const { S } = await import("../../frontend/state.js");
const { setDecisionConfig } = await import("../../frontend/decisions.js");
const { closeModal, closeSubModal } = await import("../../frontend/modal.js");
const fragments = await import("../../frontend/library_fragments.js");

const $ = (id) => document.getElementById(id);
const shown = (id) => $(id) && $(id).style.display !== "none";
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function retype(value) {
  $("interactive-frag-type").value = value;
  fragments.updateInteractiveFragmentExample(value);
}

const GATED = {
  id: "trim",
  label: "Trim",
  description: "Trim to two actions.",
  field_type: "post_processing",
  required: false,
  injection_label: "Trim",
  sort_order: 0,
  cooldown_turns: 0,
  post_processing_gate: 'Do more than two "actions" happen?',
};

const lastPut = () => writes.filter((write) => write.method === "PUT").at(-1).body;

beforeEach(async () => {
  closeModal();
  closeSubModal();
  writes.length = 0;
  // Fail any fetch left pending so the loader forgets it and fetches afresh.
  pendingConfig?.(null);
  pendingConfig = null;
  await flush();
  setDecisionConfig(null);
  S.interactiveFragments = [structuredClone(GATED)];
});

test("the gate shows for post-processing only and escapes its saved text", () => {
  fragments.showInteractiveFragmentModal("trim");

  assert.ok(shown("interactive-frag-gate-row"));
  assert.equal($("interactive-frag-gate").value, GATED.post_processing_gate);
  assert.equal($("interactive-frag-gate").getAttribute("maxlength"), "2000");
  for (const type of ["string", "array", "state", "feedback", "decision"]) {
    retype(type);
    assert.ok(!shown("interactive-frag-gate-row"), type);
  }
  retype("post_processing");
  assert.ok(shown("interactive-frag-gate-row"));
});

test("a save sends the trimmed gate, an empty one clears it, and other types send empty", async () => {
  fragments.showInteractiveFragmentModal("trim");
  $("interactive-frag-gate").value = "  Is it long?  ";
  await fragments.saveInteractiveFragment(true);
  assert.equal(lastPut().post_processing_gate, "Is it long?");

  fragments.showInteractiveFragmentModal("trim");
  $("interactive-frag-gate").value = "   ";
  await fragments.saveInteractiveFragment(true);
  assert.equal(lastPut().post_processing_gate, "");

  fragments.showInteractiveFragmentModal("trim");
  retype("feedback");
  await fragments.saveInteractiveFragment(true);
  assert.equal(lastPut().field_type, "feedback");
  assert.equal(lastPut().post_processing_gate, "");
});

// Before any successful load: the loader keeps a loaded config for good.
test("a failed config fetch is unknown, not unconfigured", async () => {
  fragments.showInteractiveFragmentModal("trim");
  pendingConfig(null);
  await flush();
  assert.ok(!shown("interactive-frag-gate-note"));
});

test("the unconfigured note waits for the config, and an unknown config shows none", async () => {
  fragments.showInteractiveFragmentModal("trim");
  assert.ok(!shown("interactive-frag-gate-note"), "loading is not unconfigured");

  pendingConfig({ configured: false });
  await flush();
  assert.ok(shown("interactive-frag-gate-note"));

  retype("feedback");
  retype("post_processing");
  assert.ok(shown("interactive-frag-gate-note"), "type changes repaint from the loaded config");

  closeModal();
  setDecisionConfig({ configured: true });
  fragments.showInteractiveFragmentModal("trim");
  assert.ok(!shown("interactive-frag-gate-note"));
});

test("a card fragment retyped away from post-processing cannot bring its old gate back", () => {
  fragments.initCardFragments({ interactive: [structuredClone(GATED)] });

  fragments.showCardInteractiveFragmentModal("trim");
  assert.equal($("interactive-frag-gate").value, GATED.post_processing_gate);
  retype("string");
  $("card-frag-save").click();
  assert.equal(fragments.readCardFragments().interactive[0].post_processing_gate, "");

  fragments.showCardInteractiveFragmentModal("trim");
  retype("post_processing");
  assert.equal($("interactive-frag-gate").value, "");
  $("card-frag-save").click();

  const saved = fragments.readCardFragments().interactive[0];
  assert.equal(saved.field_type, "post_processing");
  assert.equal(saved.post_processing_gate, "");

  // Reopened from the saved card data, as the card editor does on load.
  fragments.initCardFragments(structuredClone(fragments.readCardFragments()));
  fragments.showCardInteractiveFragmentModal("trim");
  assert.equal($("interactive-frag-gate").value, "");
});

test("a card gate is saved and reopened", () => {
  fragments.initCardFragments({ interactive: [{ ...structuredClone(GATED), post_processing_gate: "" }] });
  fragments.showCardInteractiveFragmentModal("trim");
  $("interactive-frag-gate").value = "Is it long?";
  $("card-frag-save").click();

  fragments.initCardFragments(structuredClone(fragments.readCardFragments()));
  fragments.showCardInteractiveFragmentModal("trim");
  assert.equal($("interactive-frag-gate").value, "Is it long?");
});
