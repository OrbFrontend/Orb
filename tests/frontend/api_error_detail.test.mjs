// The failure half of frontend/api.js. `_req` throws with the *body* as the
// message, so every `toast(e.message, true)` in the app showed a raw
// `{"detail":"..."}` — a backend that took care to word a provider's rejection
// well, read through a JSON wrapper. Unwrapped once, in the client.
import assert from "node:assert/strict";
import { test } from "node:test";

import { api } from "../../frontend/api.js";

const realFetch = globalThis.fetch;

function respondWith(status, body) {
  globalThis.fetch = async () => ({
    ok: false,
    status,
    text: async () => body,
  });
}

async function failureOf(status, body) {
  respondWith(status, body);
  try {
    await api.post("/whatever", {});
    assert.fail("expected a throw");
  } catch (e) {
    return e;
  } finally {
    globalThis.fetch = realFetch;
  }
}

test("a FastAPI detail string becomes the message", async () => {
  const said = "OpenRouter rejected the request (HTTP 400): Google AI Studio: User location is not supported.";
  const e = await failureOf(502, JSON.stringify({ detail: said }));
  assert.equal(e.message, said);
  assert.equal(e.status, 502);
});

test("the raw body stays available for anything that wants it", async () => {
  const body = JSON.stringify({ detail: "nope" });
  const e = await failureOf(500, body);
  assert.equal(e.body, body);
});

test("a 422 validation list is joined rather than shown as [object Object]", async () => {
  const e = await failureOf(
    422,
    JSON.stringify({ detail: [{ loc: ["body", "size"], msg: "unexpected value" }, { msg: "field required" }] }),
  );
  assert.equal(e.message, "unexpected value; field required");
});

test("a non-JSON body falls through to the raw text", async () => {
  // A proxy error page, or an empty 502 from something in front of Orb.
  const e = await failureOf(502, "<html>Bad Gateway</html>");
  assert.equal(e.message, "<html>Bad Gateway</html>");
});

test("a body with no detail key falls through rather than becoming empty", async () => {
  const e = await failureOf(500, JSON.stringify({ error: "something" }));
  assert.equal(e.message, JSON.stringify({ error: "something" }));
});
