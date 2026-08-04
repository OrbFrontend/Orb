import assert from "node:assert/strict";
import { test } from "node:test";
import { boolFlag, escAttr, escHandlerArg } from "../../frontend/utils.js";

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

test("boolFlag accepts SQLite and optimistic-update true values only", () => {
  assert.equal(boolFlag(true), true);
  assert.equal(boolFlag(1), true);
  assert.equal(boolFlag(false), false);
  assert.equal(boolFlag(0), false);
  assert.equal(boolFlag("1"), false);
  assert.equal(boolFlag("0"), false);
  assert.equal(boolFlag(null), false);
});

test("escHandlerArg preserves a single-quoted inline-handler argument", () => {
  assert.equal(escHandlerArg(`my'cast\\line\n"<&`), `my\\'cast\\\\line\\n&quot;&lt;&amp;`);
});

test("escAttr prevents quote-delimited attribute injection", () => {
  installEscapingDocument();
  assert.equal(
    escAttr(`" autofocus onfocus="alert(document.domain)'<&`),
    "&quot; autofocus onfocus=&quot;alert(document.domain)&#39;&lt;&amp;",
  );
});
