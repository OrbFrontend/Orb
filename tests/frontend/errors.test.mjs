import assert from "node:assert/strict";
import { test } from "node:test";
import { responseError, sseError } from "../../frontend/errors.js";

test("HTTP failures preserve conflict details and status for callers", async () => {
  const body = JSON.stringify({ detail: { message: "The document changed", document: { content: "new" } } });
  const error = await responseError(new Response(body, { status: 409 }));
  assert.equal(error.message, "The document changed");
  assert.equal(error.status, 409);
  assert.equal(error.body, body);
  assert.equal(JSON.parse(error.body).detail.document.content, "new");
});

test("HTTP failures with empty bodies still explain the status", async () => {
  assert.equal((await responseError(new Response("", { status: 502 }))).message, "Orb returned HTTP 502.");
});

test("reading a cancelled HTTP error body keeps AbortError", async () => {
  const abort = new DOMException("Aborted", "AbortError");
  await assert.rejects(responseError({ status: 409, text: async () => { throw abort; } }), (error) => error === abort);
});

test("structured SSE failures retain metadata and parse JSON before unescaping", () => {
  const failure = {
    headline: "The provider rejected the request.",
    sentence: "Try another\nmodel; literal \\n remains literal.",
    kind: "provider",
    stage: "writer pass",
    status: 400,
    host: "provider.invalid",
    body: '{"error":"invalid\\nmodel"}',
  };
  const error = sseError(JSON.stringify(failure));
  assert.equal(error.message, `${failure.headline} ${failure.sentence}`);
  assert.deepEqual(error.failure, failure);
  assert.equal(error.status, undefined, "a provider status inside SSE is not an HTTP refusal of this request");
  assert.equal(sseError(failure).message, error.message);
});

test("legacy SSE errors unescape text, while HTTP text stays literal", async () => {
  assert.equal(sseError("first\\nsecond").message, "first\nsecond");
  assert.equal((await responseError(new Response("first\\nsecond", { status: 500 }))).message, "first\\nsecond");
});

test("structured validation and workflow errors produce readable messages", () => {
  assert.equal(sseError({ detail: [{ msg: "field required" }, { msg: "bad value" }] }).message, "field required; bad value");
  assert.equal(sseError({ detail: { message: "Pick a model" } }).message, "Pick a model");
  assert.equal(sseError({ error: { message: "No credits" } }).message, "No credits");
  assert.equal(sseError('"Quoted failure"').message, "Quoted failure");
  assert.equal(sseError("", "Tagging failed").message, "Tagging failed");
});
