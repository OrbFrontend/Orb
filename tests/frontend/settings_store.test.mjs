import assert from "node:assert/strict";
import { test } from "node:test";
import { saveSettings } from "../../frontend/settings_store.js";
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
