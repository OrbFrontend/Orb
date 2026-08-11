// Dynamic Worlds review surface. world_proposals.js is string-in/string-out
// (it imports only utils.js, also DOM-free), so it loads under node --test.
//
// What matters here is what a reviewer is told: which changeset states offer
// which actions, what a before/after actually shows for each operation, and
// that a pending proposal never leaks into anything that reads as applied lore.
import assert from "node:assert/strict";
import { before, test } from "node:test";

// `esc` escapes through a detached DOM node, so node needs the same minimal
// stand-in the utils escaping test installs. Escaping *behaviour* is that
// test's subject; here it only has to not throw.
before(() => {
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
});

import {
  actionsHtml,
  activationLabel,
  changesetRowHtml,
  isOpen,
  isStale,
  messageProposalsHtml,
  openProposals,
  operationDiff,
  operationEditHtml,
  operationHtml,
  operationTitle,
  proposalCardHtml,
  readOperationEdit,
  STATUS_LABELS,
} from "../../frontend/world_proposals.js";

const op = (over = {}) => ({
  op: "create",
  name: "Collapsed Bridge",
  content: "The bridge has fallen.",
  activation: "keywords",
  keywords: ["bridge"],
  rationale: "It fell during the crossing.",
  ...over,
});

const changeset = (over = {}) => ({
  id: 7,
  world_id: "w1",
  status: "pending",
  summary: "The bridge fell.",
  operations: [op()],
  created_at: "2026-08-10T12:00:00+00:00",
  ...over,
});

// ── state predicates

test("pending and stale are the open states; decided ones are not", () => {
  assert.equal(isOpen(changeset()), true);
  assert.equal(isOpen(changeset({ status: "stale" })), true);
  for (const status of ["applied", "rejected", "superseded", "reverted"]) {
    assert.equal(isOpen(changeset({ status })), false);
  }
  assert.equal(isOpen(null), false);
});

test("isStale names exactly one state", () => {
  assert.equal(isStale(changeset({ status: "stale" })), true);
  assert.equal(isStale(changeset()), false);
});

test("openProposals ignores decided changesets", () => {
  const list = [changeset(), changeset({ id: 8, status: "applied" }), changeset({ id: 9, status: "stale" })];
  assert.deepEqual(
    openProposals({ world_changesets: list }).map((c) => c.id),
    [7, 9],
  );
  assert.deepEqual(openProposals({}), []);
});

// ── operation rendering

test("activation is phrased for a reader, not as a schema value", () => {
  assert.equal(activationLabel(op({ activation: "constant" })), "Always in context");
  assert.equal(activationLabel(op({ activation: "keywords", keywords: ["bridge", "gorge"] })), "On: bridge, gorge");
  assert.equal(activationLabel(op({ activation: "keywords", keywords: [] })), "Keyword-activated");
});

test("the two removing operations have no activation to show", () => {
  assert.equal(activationLabel(op({ op: "suppress" })), "");
  assert.equal(activationLabel(op({ op: "archive" })), "");
});

test("an operation's title names the entry it acts on", () => {
  assert.equal(operationTitle(op()), "Add “Collapsed Bridge”");
  assert.equal(operationTitle(op({ op: "replace", target_name: "The Bridge" })), "Replace “The Bridge”");
  assert.equal(operationTitle(op({ op: "suppress", target_name: "The Bridge" })), "Remove “The Bridge”");
  assert.equal(operationTitle(op({ op: "archive", target_name: "Rumour" })), "Retire “Rumour”");
});

test("create has no before; suppress and archive have no after", () => {
  assert.deepEqual(operationDiff(op({ target_content: "ignored" })), {
    before: "",
    after: "The bridge has fallen.",
  });
  assert.deepEqual(operationDiff(op({ op: "replace", target_content: "It stands." })), {
    before: "It stands.",
    after: "The bridge has fallen.",
  });
  assert.deepEqual(operationDiff(op({ op: "suppress", target_content: "It stands.", content: "" })), {
    before: "It stands.",
    after: "",
  });
});

test("operation html carries the title, both sides and the rationale", () => {
  const html = operationHtml(op({ op: "replace", target_content: "It stands." }));
  assert.match(html, /Replace/);
  assert.match(html, /wc-before[^>]*>It stands\./);
  assert.match(html, /wc-after[^>]*>The bridge has fallen\./);
  assert.match(html, /It fell during the crossing\./);
});

test("rendered operation text is escaped", () => {
  const html = operationHtml(op({ name: "<img src=x onerror=1>", content: "a & b" }));
  assert.ok(!html.includes("<img"));
  assert.match(html, /&amp;/);
});

// ── actions

test("a pending proposal offers apply, edit and reject", () => {
  const html = actionsHtml(changeset());
  for (const action of ["apply", "edit", "reject"]) {
    assert.match(html, new RegExp(`data-wc-action="${action}"`));
  }
});

test("a stale proposal offers re-evaluate and never apply", () => {
  const html = actionsHtml(changeset({ status: "stale" }));
  assert.match(html, /data-wc-action="re-evaluate"/);
  assert.ok(!html.includes('data-wc-action="apply"'));
});

test("an applied change offers undo; a rejected one offers nothing", () => {
  assert.match(actionsHtml(changeset({ status: "applied" })), /data-wc-action="undo"/);
  assert.equal(actionsHtml(changeset({ status: "rejected" })), "");
  assert.equal(actionsHtml(changeset({ status: "reverted" })), "");
});

// ── cards

test("the card carries the ids the delegated click handler dispatches on", () => {
  const html = proposalCardHtml(changeset());
  assert.match(html, /data-wc-id="7"/);
  assert.match(html, /data-wc-world="w1"/);
});

test("the card says what state it is in", () => {
  assert.match(proposalCardHtml(changeset()), new RegExp(STATUS_LABELS.pending));
  const stale = proposalCardHtml(changeset({ status: "stale" }));
  assert.match(stale, new RegExp(STATUS_LABELS.stale));
  assert.match(stale, /world changed since this was proposed/i);
});

test("a card never claims a pending change is in the world", () => {
  const html = proposalCardHtml(changeset());
  assert.ok(!/applied/i.test(html));
});

test("a card names its lorebook, since one turn can propose to several", () => {
  assert.match(proposalCardHtml(changeset({ world_name: "Gorge" })), /Gorge/);
  // Absent rather than blank when the backend projected no name.
  assert.ok(!/class="wc-world"/.test(proposalCardHtml(changeset())));
});

test("only open proposals are painted under a message", () => {
  const msg = {
    world_changesets: [changeset(), changeset({ id: 8, status: "applied", summary: "Already done" })],
  };
  const html = messageProposalsHtml(msg);
  assert.match(html, /data-wc-id="7"/);
  assert.ok(!html.includes('data-wc-id="8"'));
  assert.equal(messageProposalsHtml({}), "");
  assert.equal(proposalCardHtml(null), "");
});

test("a drawer row summarises the change, its source and its date", () => {
  const html = changesetRowHtml(
    changeset({ status: "applied", source_character_label: "Guide", applied_at: "2026-08-10T12:00:00+00:00" }),
  );
  assert.match(html, /The bridge fell\./);
  assert.match(html, /1 change/);
  assert.match(html, /Guide/);
  assert.match(html, /2026-08-10/);
  assert.match(html, /data-wc-action="undo"/);
});

test("a drawer row falls back to the first operation when there is no summary", () => {
  assert.match(changesetRowHtml(changeset({ summary: "" })), /Add “Collapsed Bridge”/);
});

// ── edit-before-apply

test("the edit form exposes the fields a reviewer may change", () => {
  const html = operationEditHtml(op(), 0);
  assert.match(html, /data-wc-index="0"/);
  for (const field of ["include", "name", "content", "activation", "keywords"]) {
    assert.match(html, new RegExp(`data-wc-field="${field}"`));
  }
});

test("a removing operation has no body to edit", () => {
  const html = operationEditHtml(op({ op: "suppress" }), 1);
  assert.match(html, /data-wc-field="include"/);
  assert.ok(!html.includes('data-wc-field="content"'));
});

test("excluding an operation drops it from the batch", () => {
  assert.equal(readOperationEdit(op(), { include: false }), null);
});

test("an edit keeps the fields the form does not expose", () => {
  const original = op({ op: "replace", target_entry_id: 3, target_name: "The Bridge", rationale: "why" });
  const edited = readOperationEdit(original, {
    include: true,
    name: " Reworded ",
    content: " New text ",
    activation: "keywords",
    keywords: "bridge, gorge ,",
  });
  assert.equal(edited.name, "Reworded");
  assert.equal(edited.content, "New text");
  assert.deepEqual(edited.keywords, ["bridge", "gorge"]);
  assert.equal(edited.target_entry_id, 3);
  assert.equal(edited.rationale, "why");
  assert.equal(edited.op, "replace");
});

test("switching an edit to constant clears its keywords", () => {
  const edited = readOperationEdit(op(), { include: true, activation: "constant", keywords: "bridge" });
  assert.equal(edited.activation, "constant");
  assert.deepEqual(edited.keywords, []);
});

test("a removing operation round-trips untouched through the editor", () => {
  const original = op({ op: "archive", target_entry_id: 9 });
  assert.deepEqual(readOperationEdit(original, { include: true }), original);
});
