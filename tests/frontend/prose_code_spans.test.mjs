import assert from "node:assert/strict";
import { test } from "node:test";
import { formatProse } from "../../frontend/utils.js";
import { escapeUnknownTags, trimIncompleteMarkup } from "../../frontend/message_html.js";

// formatProse hands its output to DOMPurify, so nothing here is the last line of
// defence — but "the sanitiser will catch it" is only true for markup the
// sanitiser refuses, and it happily keeps an `<img src=…>`. What these pin is
// the parser's own promise: text the model marked as code stays text, and a
// fence that never closes never becomes live markup.

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

test("a tag inside a code span is printed, not parsed", () => {
  installEscapingDocument();
  const html = formatProse("try `<img src=x>` here");
  assert.match(html, /<code class="inline-code">&lt;img src=x&gt;<\/code>/);
  // The point of the ordering: before the fix the tag was lifted out and put
  // back verbatim, so `<img>` reached the page as an element inside <code>.
  assert.ok(!/<img/.test(html));
});

test("a code span is not emphasised, quoted or turned into a heading", () => {
  installEscapingDocument();
  assert.match(formatProse("`*x*`"), /<code class="inline-code">\*x\*<\/code>/);
  assert.match(formatProse("`**x**`"), /<code class="inline-code">\*\*x\*\*<\/code>/);
  const quoted = formatProse('`say "hi"`');
  assert.match(quoted, /<code class="inline-code">say &amp;quot;hi&amp;quot;<\/code>|say "hi"/);
  assert.ok(!quoted.includes('class="quoted"'));
});

test("emphasis still works around a code span", () => {
  installEscapingDocument();
  const html = formatProse("*before* `code` **after**");
  assert.match(html, /<em>before<\/em>/);
  assert.match(html, /<code class="inline-code">code<\/code>/);
  assert.match(html, /<strong>after<\/strong>/);
});

test("a fence that never closes renders as code, not as markup", () => {
  installEscapingDocument();
  // The generation was cut off mid-block. Previously nothing matched the
  // unterminated fence, so everything after it was rendered as prose — live,
  // and permanently so once the turn ended.
  const html = formatProse("intro\n```html\n<img src=https://evil.test/p.png>");
  assert.match(html, /<div class="code-block">/);
  assert.match(html, /&lt;img src=https:\/\/evil\.test\/p\.png&gt;/);
  assert.ok(!/<img/.test(html));
});

test("a closed fence still wins over the unterminated reading", () => {
  installEscapingDocument();
  const html = formatProse("```js\nlet a = 1;\n```\nafter");
  assert.match(html, /<code class="language-js">let a = 1;\n<\/code>/);
  assert.match(html, /after/);
  assert.equal((html.match(/code-block/g) || []).length, 1);
});

test("the code block carries no data attribute for the sanitiser to strip", () => {
  installEscapingDocument();
  // The toolbar is rebuilt after sanitising (message_html.js), which is what
  // lets ALLOW_DATA_ATTR be off. If formatProse emitted the buttons again, the
  // sanitiser would silently strip their actions and the toolbar would be dead.
  const html = formatProse("```\nx\n```");
  assert.ok(!html.includes("data-orb-action"));
  assert.ok(!html.includes("code-block-btn"));
});

test("model text cannot forge a parser slot", () => {
  installEscapingDocument();
  // The inline passes lift tags and code spans into numbered slots. A message
  // containing the slot characters itself would otherwise be able to name one.
  const html = formatProse("￼0� and ￹0￻ and `x` and <b>y</b>");
  assert.match(html, /<code class="inline-code">x<\/code>/);
  assert.match(html, /<b>y<\/b>/);
  assert.ok(!/￼|�|￹|￻/.test(html));
});

const isKnownTag = (name) => ["b", "i", "img", "style", "svg", "div"].includes(name.toLowerCase());

test("the unknown-tag escaper leaves an unterminated fence alone", () => {
  // formatProse escapes a fence's body itself; escaping it here too would show
  // the reader `&amp;lt;gasp&amp;gt;` inside the code block.
  const text = "a <gasp> b\n```\n<gasp>\n";
  const out = escapeUnknownTags(text, isKnownTag);
  assert.match(out, /a &lt;gasp> b/);
  assert.match(out, /```\n<gasp>\n$/);
});

test("streaming trims an unfinished tag but leaves an open fence to the parser", () => {
  assert.equal(trimIncompleteMarkup('hello <div class="ca'), "hello ");
  assert.equal(trimIncompleteMarkup("hello <style>.a{"), "hello ");
  // The fence is safe as-is: formatProse renders it as escaped code either way,
  // so trimming would only make the block flicker as it fills.
  assert.equal(trimIncompleteMarkup("hello ```\ncode"), "hello ```\ncode");
});
