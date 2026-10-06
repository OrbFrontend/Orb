import assert from "node:assert/strict";
import { test } from "node:test";
import { applySettings, loadSettingsRow, saveSettings } from "../../frontend/settings_store.js";
import { S, subscribe } from "../../frontend/state.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

/** Answer each PUT /settings from the queue, in the order the saves were sent. */
function queuedPuts(t) {
  const answers = [];
  t.mock.method(globalThis, "fetch", async () => {
    const { promise } = answers.shift();
    return promise;
  });
  return answers;
}

test("a failed save restores its keys while a later save in flight keeps its edit", async (t) => {
  S.settings = { show_editor_diff: 1, inspector_inline: 0 };
  const answers = queuedPuts(t);
  const first = deferred();
  const second = deferred();
  answers.push(first, second);
  const seen = [];
  const off = subscribe("settings", (patch) => seen.push(patch));
  t.after(off);

  const failing = saveSettings({ show_editor_diff: false });
  const saving = saveSettings({ inspector_inline: true });
  assert.equal(S.showEditorDiff, false);
  assert.equal(S.inspectorInline, true);
  assert.throws(() => {
    S.showEditorDiff = true;
  }, TypeError);

  first.resolve(Response.json({ detail: "nope" }, { status: 500 }));
  await assert.rejects(failing);
  assert.equal(S.showEditorDiff, true);
  assert.equal(S.inspectorInline, true);
  assert.deepEqual(seen, [{ show_editor_diff: false }, { inspector_inline: true }, { show_editor_diff: 1 }]);

  second.resolve(Response.json({ show_editor_diff: 1, inspector_inline: 1 }));
  await saving;
  assert.deepEqual(S.settings, { show_editor_diff: 1, inspector_inline: 1 });
});

test("an earlier save's server row does not undo a later save still in flight", async (t) => {
  S.settings = { show_chat_avatars: 0, inspector_inline: 0 };
  const answers = queuedPuts(t);
  const first = deferred();
  const second = deferred();
  answers.push(first, second);

  const a = saveSettings({ show_chat_avatars: true });
  const b = saveSettings({ inspector_inline: true });
  first.resolve(Response.json({ show_chat_avatars: 1, inspector_inline: 0 }));
  await a;
  assert.equal(S.showChatAvatars, true);
  assert.equal(S.inspectorInline, true);

  second.resolve(Response.json({ show_chat_avatars: 1, inspector_inline: 1 }));
  await b;
  assert.deepEqual(S.settings, { show_chat_avatars: 1, inspector_inline: 1 });
});

test("settings requests are ordered even when a later response is ready first", async (t) => {
  S.settings = { show_chat_avatars: 0, inspector_inline: 0 };
  const first = deferred();
  const second = deferred();
  const calls = [];
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    calls.push(JSON.parse(options.body));
    return calls.length === 1 ? first.promise : second.promise;
  });
  const a = saveSettings({ show_chat_avatars: true });
  const b = saveSettings({ inspector_inline: true });
  second.resolve(Response.json({ show_chat_avatars: 1, inspector_inline: 1 }));
  await new Promise(setImmediate);
  assert.deepEqual(calls, [{ show_chat_avatars: true }]);
  assert.equal(S.inspectorInline, true);
  first.resolve(Response.json({ show_chat_avatars: 1, inspector_inline: 0 }));
  await Promise.all([a, b]);
  assert.deepEqual(S.settings, { show_chat_avatars: 1, inspector_inline: 1 });
});

test("overlapping failures on the same setting restore its last confirmed value", async (t) => {
  S.settings = { inspector_inline: 0 };
  const answers = queuedPuts(t);
  answers.push(
    { promise: Promise.resolve(Response.json({}, { status: 500 })) },
    { promise: Promise.resolve(Response.json({}, { status: 500 })) },
  );
  const results = await Promise.allSettled([
    saveSettings({ inspector_inline: true }),
    saveSettings({ inspector_inline: false }),
  ]);
  assert.deepEqual(results.map((result) => result.status), ["rejected", "rejected"]);
  assert.equal(S.settings.inspector_inline, 0);
});

test("a pending settings response preserves local edits made before their save", async (t) => {
  S.settings = { inspector_inline: 0, reasoning_prefill: "old" };
  const answers = queuedPuts(t);
  const response = deferred();
  answers.push(response);
  const saving = saveSettings({ inspector_inline: true });
  applySettings({ reasoning_prefill: "typing" });
  response.resolve(Response.json({ inspector_inline: 1, reasoning_prefill: "old" }));
  await saving;
  assert.equal(S.settings.reasoning_prefill, "typing");
});

test("a settings reload waits for earlier saves and retains later optimistic edits", async (t) => {
  S.settings = { inspector_inline: 0, show_chat_avatars: 0 };
  const responses = [deferred(), deferred(), deferred()];
  const calls = [];
  t.mock.method(globalThis, "fetch", (_url, options) => {
    calls.push(options.method || "GET");
    return responses[calls.length - 1].promise;
  });
  const first = saveSettings({ inspector_inline: true });
  const loading = loadSettingsRow();
  const last = saveSettings({ show_chat_avatars: true });
  responses[0].resolve(Response.json({ inspector_inline: 1, show_chat_avatars: 0 }));
  await first;
  await new Promise(setImmediate);
  assert.deepEqual(calls, ["PUT", "GET"]);
  responses[1].resolve(Response.json({ inspector_inline: 1, show_chat_avatars: 0 }));
  await loading;
  assert.equal(S.showChatAvatars, true);
  responses[2].resolve(Response.json({ inspector_inline: 1, show_chat_avatars: 1 }));
  await last;
  assert.deepEqual(S.settings, { inspector_inline: 1, show_chat_avatars: 1 });
});
