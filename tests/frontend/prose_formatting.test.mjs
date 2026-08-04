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
