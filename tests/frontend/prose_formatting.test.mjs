import assert from "node:assert/strict";
import { test } from "node:test";
import { formatProse, formatProseWithDiff } from "../../frontend/utils.js";

function installEscapingDocument() {
  globalThis.document = {
    createElement() {
      return {
        innerHTML: "",
        set textContent(value) {
          this.innerHTML = String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
        },
      };
    },
  };
}

test("normal and diff prose share all supported quote formatting", () => {
  installEscapingDocument();
  const text = '"a" “b” ‘c’ «d» ‹e› 「f」 『g』 „h“ ‚i‘';
  const normal = formatProse(text);
  const diff = formatProseWithDiff([{ type: "equal", text }]);
  assert.equal((normal.match(/class="quoted"/g) || []).length, 9);
  assert.equal(diff, normal);
});

test("tag interiors are not treated as prose", () => {
  installEscapingDocument();
  // Without tag extraction INLINE_QUOTE_RE wraps the attribute value and the
  // `*` in a class name becomes an <em>.
  const html = formatProse('<img src="x.png" alt="a * b" class="c*d">');
  assert.equal(html, '<img src="x.png" alt="a * b" class="c*d">');
  assert.ok(!html.includes("quoted"));
  assert.ok(!html.includes("<em>"));
});

test("inline formatting still applies around a tag", () => {
  installEscapingDocument();
  assert.equal(formatProse('*soft* <b>x</b> "q"'), '<em>soft</em> <b>x</b> <span class="quoted">"q"</span>');
});

test("a style block is encoded into custom-style", () => {
  installEscapingDocument();
  const html = formatProse("<style>.a { content: '*' }</style>tail");
  assert.equal(html, `<custom-style>${encodeURIComponent(".a { content: '*' }")}</custom-style>tail`);
  // The CSS must survive percent-decoding byte for byte.
  const encoded = html.slice("<custom-style>".length, html.indexOf("</custom-style>"));
  assert.equal(decodeURIComponent(encoded), ".a { content: '*' }");
});

test("newlines are left for the DOM layout pass", () => {
  installEscapingDocument();
  const html = formatProse("one\ntwo\n\nthree");
  assert.equal(html, "one\ntwo\n\nthree");
  assert.ok(!html.includes("<br>"));
});

test("model text cannot forge a tag slot", () => {
  installEscapingDocument();
  // The slot characters are stripped from the input before tags are lifted out,
  // so a hand-written slot cannot have a tag substituted into it.
  assert.equal(formatProse("￼0� <b>x</b>"), "0 <b>x</b>");
});

test("fenced code is still escaped whole", () => {
  installEscapingDocument();
  const html = formatProse("```js\nconst a = 1 < 2 && '*x*';\n```");
  assert.ok(html.includes("const a = 1 &lt; 2 &amp;&amp; '*x*';"));
  assert.ok(!html.includes("<em>"));
});
