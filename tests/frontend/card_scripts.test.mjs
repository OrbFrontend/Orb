import assert from "node:assert/strict";
import { test } from "node:test";
import { applyCardScripts, messageDisplaySource, projectCardDisplay } from "../../frontend/card_scripts.js";
import { S } from "../../frontend/state.js";

const script = (extra = {}) => ({ findRegex: "/secret/g", replaceString: "visible", placement: [2], ...extra });

test("display flags, role, disabled, malformed scripts and ordering", () => {
  const scripts = [null, script({ disabled: true }), script({ promptOnly: true }), script(), script({ findRegex: "/visible/g", replaceString: "done" }), script({ findRegex: "/[/g" })];
  assert.equal(applyCardScripts("secret", scripts, "assistant"), "done");
  assert.equal(applyCardScripts("secret", scripts, "user"), "secret");
  assert.equal(applyCardScripts("secret", scripts, "system"), "secret");
  assert.equal(applyCardScripts("secret", [script({ promptOnly: true, markdownOnly: true })], "assistant"), "visible");
});

// Mirrors the table in tests/unit/test_card_scripts.py: the two channels project
// one declaration set, so a divergence here is a bug in one of them.
test("pattern flags and replacement tokens match the prompt channel", () => {
  const cases = [
    ["/^a.(b)$/gims", "$1", "A\nb\naXb", "b\nb"],
    ["/(a)(b)?/g", "$1|$2|$$|$&|$9|$12|\\n", "a", "a||$|a|$9|a2|\\n"],
    ["/a/", "X", "aa", "Xa"],
    ["/a/g", "X", "aa", "XX"],
    ["/<\\/div>/g", "", "hello</div>", "hello"],
    ["/b/", "$`-$'", "abc", "aa-cc"],
    ["/b(c)/g", "$0|{{MATCH}}", "abc", "abc|bc"],
    ["/(?<word>\\w+)/g", "[$<word>]", "hi there", "[hi] [there]"],
    ["/b/g", "[$<nope>]", "abc", "a[]c"],
  ];
  for (const [findRegex, replaceString, source, expected] of cases)
    assert.equal(applyCardScripts(source, [script({ findRegex, replaceString })], "assistant"), expected, findRegex);
});

test("a pattern without usable flags is its own first-match-only pattern", () => {
  const cases = [
    ["plain", "plain plain", "M plain"],
    ["/missing", "x /missing y", "x M y"],
    ["/a/y", "a /a/y b", "a M b"],
    ["/a/gg", "z /a/gg z", "z M z"],
  ];
  for (const [findRegex, source, expected] of cases)
    assert.equal(applyCardScripts(source, [script({ findRegex, replaceString: "M" })], "assistant"), expected, findRegex);
  for (const findRegex of ["/[broken/g", "/(unclosed/g", "/a/x"])
    assert.equal(applyCardScripts("abc", [script({ findRegex, replaceString: "X" })], "assistant"), "abc", findRegex);
});

test("input and output caps retain canonical text", () => {
  const scripts = [script({ findRegex: "/a/g", replaceString: "aa" })];
  for (const length of [60_000, 100_001]) {
    const text = "a".repeat(length);
    assert.equal(applyCardScripts(text, scripts, "assistant"), text);
  }
});

test("message projection selects solo and group card without changing stored text", () => {
  S.activeConvId = "conversation";
  S.conversations = [{ id: "conversation", character_card_id: "a", character_name: "Amy" }];
  S.allCharacters = [{ id: "a", display_scripts: [script({ findRegex: "/Amy/g" })] }, { id: "b", display_scripts: [script({ replaceString: "other" })] }];
  S.groupCast = null;
  const message = { role: "assistant", content: "{{char}}" };
  assert.equal(messageDisplaySource(message), "visible");
  assert.equal(message.content, "{{char}}");
  S.groupCast = { members: [{ id: "member", character_card_id: "b" }] };
  assert.equal(messageDisplaySource({ role: "assistant", content: "secret", speaker_member_id: "member" }), "other");
  assert.equal(messageDisplaySource({ role: "user", content: "secret" }), "secret");
  S.groupCast = { members: [], speakerCardIds: new Map([["former-member", "b"]]) };
  assert.equal(messageDisplaySource({ role: "assistant", content: "secret", speaker_member_id: "former-member" }), "other");
  S.groupCast = null;
});

test("CSS only attaches to assistant messages and cannot break out of its element", () => {
  const card = { display_css: '</style><p>injected</p>' };
  assert.equal(projectCardDisplay("hello", card, "user"), "hello");
  assert.ok(projectCardDisplay("hello", card, "assistant").startsWith('<style><\\/style>'));
});

test("CSS pasted with its style tags from a creator's note is unwrapped", () => {
  const face = "@font-face { font-family: board; src: url(a.ttf); }";
  const wrapped = { display_css: `Prose first.\n<style>${face}</style>\n<STYLE media="x">q { color: red; }` };
  assert.equal(projectCardDisplay("hi", wrapped, "assistant"), `<style>${face}\nq { color: red; }</style>\nhi`);
  assert.equal(projectCardDisplay("hi", { display_css: "<style></style>" }, "assistant"), "hi");
});
