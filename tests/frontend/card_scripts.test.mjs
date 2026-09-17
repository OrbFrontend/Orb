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

test("native JS flags and replacement tokens, including non-global replacement", () => {
  assert.equal(applyCardScripts("A\nb\naXb", [script({ findRegex: "/^a.(b)$/gims", replaceString: "$1" })], "assistant"), "b\nb");
  assert.equal(applyCardScripts("aa", [script({ findRegex: "/(a)/", replaceString: "$1$$" })], "assistant"), "a$a");
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
