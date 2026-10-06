import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, test } from "node:test";
import { loadDom } from "./dom_fixture.mjs";

await loadDom({ html: readFileSync(new URL("../../frontend/index.html", import.meta.url), "utf8") });
window.matchMedia = () => ({ matches: false, addEventListener() {} });
window.Element.prototype.scrollTo = () => {};
window.Element.prototype.scrollIntoView = () => {};
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = clearTimeout;
globalThis.fetch = async () => Response.json({});

const { S } = await import("../../frontend/state.js");
const { updateUserBtn } = await import("../../frontend/settings_personas.js");
const { renderMessages } = await import("../../frontend/chat_core.js");
const calls = [];

beforeEach((t) => {
  S.settings = { active_persona_id: 1, show_chat_avatars: true };
  S.personas = [{ id: 1, name: "Ada" }, { id: 2, name: "Bea" }];
  S.activeConvId = "persona-chat";
  S.conversations = [{ id: "persona-chat", character_card_id: "card", character_name: "Vale" }];
  S.allCharacters = [{ id: "card", name: "Vale" }];
  S.groupCast = null;
  S.messages = [{ id: 1, role: "user", content: "Hello" }];
  document.getElementById("toast-stack").replaceChildren();
  calls.length = 0;
  t.mock.method(globalThis, "fetch", async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({ path, method: options.method || "GET", body });
    return Response.json(path === "/api/settings" ? { ...S.settings, ...body } : {});
  });
  updateUserBtn();
  renderMessages();
  document.getElementById("user-profile-btn").click();
});

async function until(check) {
  for (let i = 0; i < 500; i++) {
    if (check()) return;
    await new Promise(setImmediate);
  }
  assert.fail(`persona UI did not refresh: ${document.getElementById("toast-stack").textContent}`);
}

function action(name) {
  return document.querySelector(`[data-wf-action="personas:${name}"][data-persona-id="2"]`);
}

function assertPersonaShown(name) {
  assert.equal(document.querySelector(".message.user .msg-avatar").textContent, name[0]);
  assert.ok(document.getElementById("user-profile-btn").textContent.includes(name));
  assert.equal(document.querySelector("#toast-stack .error"), null);
}

test("selecting a persona refreshes the modal and chat without a false failure toast", async () => {
  action("activate").click();
  await until(() => document.querySelector('.persona-item-active[data-persona-id="2"]'));
  assert.equal(S.activePersonaId, 2);
  assertPersonaShown("Bea");
  assert.ok(calls.some((call) => call.path === "/api/settings" && call.body.active_persona_id === 2));
});

for (const [name, path, target] of [
  ["conversationLock", "/api/conversations/persona-chat", () => S.conversations[0]],
  ["characterLock", "/api/characters/card", () => S.allCharacters[0]],
]) {
  test(`pinning and unpinning a persona through ${name} refreshes the chat and modal`, async () => {
    action(name).click();
    await until(() => action(name).getAttribute("aria-pressed") === "true");
    assert.equal(target().persona_lock_id, 2);
    assertPersonaShown("Bea");
    action(name).click();
    await until(() => action(name).getAttribute("aria-pressed") === "false");
    assert.equal(target().persona_lock_id, null);
    assertPersonaShown("Ada");
    assert.deepEqual(calls.filter((call) => call.path === path).map((call) => call.body), [
      { persona_lock_id: 2 }, { persona_lock_id: null },
    ]);
  });
}
