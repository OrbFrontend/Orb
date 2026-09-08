import assert from "node:assert/strict";
import { test } from "node:test";
import { attachmentDataUrl, attachmentMime } from "../../frontend/utils.js";
import { renderDefaultWidget } from "../../frontend/default_widget.js";

// Attachment markup is built by string interpolation and appended outside
// renderMessageHtml, so it never meets DOMPurify. The filename and MIME come
// from whatever the client POSTed, which makes this the one place in the message
// row where quoting is the whole defence.

function installEscapingDocument() {
  globalThis.document = {
    createElement() {
      return {
        innerHTML: "",
        set textContent(value) {
          this.innerHTML = String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        },
      };
    },
  };
}

test("a well-formed attachment gets its data URL", () => {
  assert.equal(attachmentDataUrl("image/png", "aGk="), "data:image/png;base64,aGk=");
  assert.equal(attachmentDataUrl(" image/PNG ", "aGk="), "data:image/PNG;base64,aGk=");
  assert.equal(attachmentMime(" IMAGE/PNG "), "image/png");
});

test("a MIME type that is not one yields no URL at all", () => {
  // `image/png" onerror="…` used to be interpolated straight into src=".
  for (const mime of [
    'image/png" onerror="alert(1)',
    "image/png;charset=x",
    "javascript:alert(1)",
    "image/png,<svg>",
    "",
    null,
    undefined,
    42,
  ]) {
    assert.equal(attachmentDataUrl(mime, "aGk="), "", `mime: ${String(mime)}`);
    assert.equal(attachmentMime(mime), "", `mime: ${String(mime)}`);
  }
});

test("a payload that is not base64 yields no URL", () => {
  // The regex guards the alphabet, not the arithmetic: what matters is that
  // nothing outside base64's character set can reach the URL. A payload that is
  // merely undecodable ends as a broken image, which has its own fallback.
  for (const b64 of ['aGk=" onload="alert(1)', "aGk<", "", null, "aGk=);x:url(https://evil.test"]) {
    assert.equal(attachmentDataUrl("image/png", b64), "", `b64: ${String(b64)}`);
  }
  // Whitespace inside a real payload is just transport noise and is stripped.
  assert.equal(attachmentDataUrl("image/png", "aG\nk="), "data:image/png;base64,aGk=");
});

test("the default widget quotes every attribute it interpolates", () => {
  installEscapingDocument();
  const html = renderDefaultWidget({
    mime: "image/png",
    b64: "aGk=",
    filename: '" onload="alert(1)',
  });
  // The quote has to be entity-encoded, or the alt attribute ends early and
  // what follows it becomes an event handler.
  assert.ok(!html.includes('" onload='), html);
  assert.match(html, /alt="&quot; onload=&quot;alert\(1\)"/);
  assert.match(html, /src="data:image\/png;base64,aGk="/);
});

test("the default widget refuses to build a src from a forged MIME type", () => {
  installEscapingDocument();
  const html = renderDefaultWidget({ mime: 'image/png" onerror="alert(1)', b64: "aGk=", filename: "x.png" });
  assert.ok(!html.includes("onerror"), html);
  // The forged type is replaced, not repaired: the bytes stay reachable as an
  // opaque download, and nothing the model wrote reaches the `data:` URL.
  assert.ok(!html.includes("image/png"), html);
  assert.match(html, /class="workflow-artifact-link"/);
  assert.match(html, /href="data:application\/octet-stream;base64,aGk="/);
});

test("a filename is escaped in the link text as well as in its attributes", () => {
  installEscapingDocument();
  const html = renderDefaultWidget({
    mime: "application/pdf",
    b64: "aGk=",
    filename: "<script>alert(1)</script>",
  });
  assert.ok(!html.includes("<script>"), html);
  assert.match(html, /&lt;script&gt;/);
});
