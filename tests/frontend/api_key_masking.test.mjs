// Saved endpoint keys reach the page only through the key field's eye button, and an untouched hidden key never writes over
// another endpoint's key when the URL changes.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(readFileSync(new URL("../../frontend/index.html", import.meta.url), "utf8"), {
  url: "https://orb.invalid",
});
globalThis.window = dom.window;
for (const name of ["document", "Node", "NodeFilter", "Element", "DocumentFragment", "HTMLElement", "DOMParser", "Event"])
  globalThis[name] = dom.window[name];
window.matchMedia = () => ({ matches: false, addEventListener() {} });
window.Element.prototype.scrollTo = () => {};
window.Element.prototype.scrollIntoView = () => {};
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = clearTimeout;

const KEYS = { 1: "sk-a-0123456789aaaa", 2: "sk-b-0123456789bbbb" };
const row = (id, url) => ({
  id,
  url,
  api_key_hint: KEYS[id] ? `••••••••${KEYS[id].slice(-4)}` : "",
  active_model_config_id: null,
  agent_active_model_config_id: null,
  completion_mode: "chat",
  proxy: "",
  kind: "chat",
});

const calls = [];
globalThis.fetch = async (path, opts = {}) => {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : undefined;
  calls.push({ method, path, body });
  const reveal = path.match(/^\/api\/endpoints\/(\d+)\/api-key$/);
  if (reveal) return Response.json({ api_key: KEYS[reveal[1]] });
  if (method === "POST" && path === "/api/endpoints") return Response.json({ ...row(1, body.url), id: 3 });
  if (method === "PUT" && path === "/api/settings") return Response.json({ ...S.settings, ...body });
  return Response.json([]);
};

const { S } = await import("../../frontend/state.js");
const { renderEndpoints } = await import("../../frontend/settings_models.js");

async function until(check) {
  for (let i = 0; i < 500; i++) {
    if (check()) return;
    await new Promise(setImmediate);
  }
  assert.fail("condition never held");
}

function showEndpointA() {
  S.endpoints = [row(1, "https://a.test/v1"), row(2, "https://b.test/v1")];
  S.judgeEndpoints = [];
  S.activeEndpointId = 1;
  S.settings = { active_endpoint_id: 1, endpoint_url: "https://a.test/v1", agent_same_as_writer: true };
  renderEndpoints();
  calls.length = 0;
  return document.querySelector('[data-key="api_key"]');
}

test("a saved key reaches the page only when the eye asks for it", async () => {
  const key = showEndpointA();

  assert.equal(key.value, "");
  assert.equal(key.placeholder, S.endpoints[0].api_key_hint);
  assert.ok(!document.documentElement.outerHTML.includes(KEYS[1]));

  key.closest(".api-key-wrap").querySelector(".api-key-toggle").click();
  await until(() => key.value);

  assert.deepEqual(calls, [{ method: "GET", path: "/api/endpoints/1/api-key", body: undefined }]);
  assert.equal(key.value, KEYS[1]);
});

test("switching URLs keeps a saved row's key and carries the shown key to a new row", async () => {
  const key = showEndpointA();
  const url = document.querySelector('[data-key="endpoint_url"]');

  url.value = "https://b.test/v1";
  url.dispatchEvent(new Event("change", { bubbles: true }));
  await until(() => calls.some((c) => c.path === "/api/settings" && c.body.active_endpoint_id === 2));

  assert.ok(!calls.some((c) => c.method === "PUT" && c.path.startsWith("/api/endpoints/") && "api_key" in c.body));
  assert.equal(key.placeholder, S.endpoints[1].api_key_hint);

  url.value = "https://c.test/v1";
  url.dispatchEvent(new Event("change", { bubbles: true }));
  await until(() => calls.some((c) => c.method === "POST" && c.path === "/api/endpoints"));

  assert.equal(calls.find((c) => c.method === "POST").body.api_key, KEYS[2]);
});
