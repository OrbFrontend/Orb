// Dynamic Worlds — the review surface, as pure functions.
//
// A world changeset is the Agent asking permission: it never touches the World
// until the user acts on it. Everything in this module is string-in/string-out
// so the same helpers paint the card under a reply, the drawer's Pending and
// History lists, and the edit modal — and so they stay testable without a DOM.
//
// Actions live in lorebooks.js (which owns every world mutation). Buttons here
// carry `data-wc-action` + `data-wc-id` + `data-wc-world` and are dispatched by
// one delegated listener, so this module adds no inline handlers.
import { esc, escAttr } from "./utils.js";

// Every status the backend can put on a changeset, with the word the user sees.
// `stale` is deliberately not called "failed": nothing went wrong, the world
// simply moved on and the proposal has to be re-judged against it.
export const STATUS_LABELS = {
  pending: "Proposed world change",
  stale: "Needs re-evaluation",
  applied: "Applied to the world",
  rejected: "Rejected",
  superseded: "Superseded by re-evaluation",
  reverted: "Undone",
};

const OP_VERBS = {
  create: "Add",
  replace: "Replace",
  suppress: "Remove",
  update: "Revise",
  archive: "Retire",
  // Never proposed — a hard delete from the drawer, recorded so History says
  // what happened to the lorebook rather than only what the Agent did to it.
  delete: "Delete",
};

// Operations with no inverse. Everything the applier writes is an overlay row
// it can archive or roll back; a deleted row is simply gone, so a changeset
// made only of deletions is history to read, not a change to take back.
const IRREVERSIBLE_OPS = new Set(["delete"]);

// A pending proposal is decidable; a stale one must be re-evaluated first. Both
// are "open", which is what the drawer's Pending section and the card's
// actions-vs-status split key on.
export const isOpen = (cs) => cs?.status === "pending" || cs?.status === "stale";
export const isStale = (cs) => cs?.status === "stale";

/** Whether an applied changeset has anything an undo could compensate for. */
export const isUndoable = (cs) => (cs?.operations || []).some((op) => !IRREVERSIBLE_OPS.has(op?.op));

/** The open changesets attached to a message, newest first. */
export function openProposals(msg) {
  return (msg?.world_changesets || []).filter(isOpen);
}

/** How an operation's entry activates, phrased for a reader rather than a schema. */
export function activationLabel(op) {
  if (op?.op === "suppress" || op?.op === "archive") return "";
  if (op?.activation === "constant") return "Always in context";
  const kws = (op?.keywords || []).filter(Boolean);
  return kws.length ? `On: ${kws.join(", ")}` : "Keyword-activated";
}

/** One line naming what an operation does and to what. */
export function operationTitle(op) {
  const verb = OP_VERBS[op?.op] || op?.op || "Change";
  const subject = op?.op === "create" ? op?.name : op?.target_name || op?.name;
  return subject ? `${verb} “${subject}”` : verb;
}

/**
 * The before/after pair a reviewer needs. `before` is the target's text as it
 * read when the proposal was made, snapshotted on the operation itself; `after`
 * is empty for the two operations that remove rather than rewrite.
 */
export function operationDiff(op) {
  const before = op?.op === "create" ? "" : op?.target_content || "";
  const after = op?.op === "suppress" || op?.op === "archive" ? "" : op?.content || "";
  return { before, after };
}

function _diffHtml(op) {
  const { before, after } = operationDiff(op);
  const rows = [];
  if (before) rows.push(`<div class="wc-before">${esc(before)}</div>`);
  if (after) rows.push(`<div class="wc-after">${esc(after)}</div>`);
  if (!rows.length) rows.push(`<div class="wc-before wc-empty">(nothing)</div>`);
  return `<div class="wc-diff">${rows.join("")}</div>`;
}

/** One operation, rendered for review. */
export function operationHtml(op) {
  const meta = [activationLabel(op)].filter(Boolean);
  return `
    <li class="wc-op wc-op-${escAttr(op?.op || "")}">
      <div class="wc-op-title">${esc(operationTitle(op))}</div>
      ${_diffHtml(op)}
      <div class="wc-op-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
      ${op?.rationale ? `<div class="wc-op-why">${esc(op.rationale)}</div>` : ""}
    </li>`;
}

function _button(action, label, cls = "") {
  return `<button type="button" class="btn btn-sm ${cls}" data-wc-action="${escAttr(action)}">${esc(label)}</button>`;
}

/** The actions a changeset offers in its current state. A stale one offers
 * Re-evaluate and never Apply — nothing is force-applied or silently rebased. */
export function actionsHtml(cs) {
  if (cs?.status === "pending") {
    return [
      _button("apply", "Apply", "btn-accent"),
      _button("edit", "Edit"),
      _button("reject", "Reject", "wc-danger"),
    ].join("");
  }
  if (cs?.status === "stale") {
    return [_button("re-evaluate", "Re-evaluate", "btn-accent"), _button("reject", "Dismiss", "wc-danger")].join("");
  }
  // An Undo the server can only refuse is worse than no button at all.
  if (cs?.status === "applied") return isUndoable(cs) ? _button("undo", "Undo") : "";
  return "";
}

/** The compact proposal card shown beneath the assistant reply that produced it.
 * It names its own lorebook when the row carries a `world_name`, since stacked
 * cards from one turn would otherwise be indistinguishable. */
export function proposalCardHtml(cs) {
  if (!cs) return "";
  const ops = cs.operations || [];
  const status = cs.status || "pending";
  const note = isStale(cs)
    ? `<div class="wc-note">The world changed since this was proposed. Re-evaluate it against the world as it stands now.</div>`
    : "";
  const actions = actionsHtml(cs);
  return `
    <div class="wc-card wc-${escAttr(status)}" data-wc-id="${escAttr(cs.id)}" data-wc-world="${escAttr(cs.world_id)}">
      <div class="wc-head">
        <span class="wc-badge">${esc(STATUS_LABELS[status] || status)}</span>
        ${cs.world_name ? `<span class="wc-world">${esc(cs.world_name)}</span>` : ""}
        ${cs.summary ? `<span class="wc-summary">${esc(cs.summary)}</span>` : ""}
      </div>
      ${note}
      <ul class="wc-ops">${ops.map(operationHtml).join("")}</ul>
      ${actions ? `<div class="wc-actions">${actions}</div>` : ""}
    </div>`;
}

/** Every open proposal attached to a message, stacked under it. */
export function messageProposalsHtml(msg) {
  const open = openProposals(msg);
  return open.length ? open.map(proposalCardHtml).join("") : "";
}

/** A one-line summary of a changeset for the drawer's Pending / History lists. */
export function changesetRowHtml(cs) {
  const ops = cs.operations || [];
  const count = `${ops.length} change${ops.length === 1 ? "" : "s"}`;
  const source = cs.source_character_label || cs.source_conversation_label || "";
  const when = (cs.applied_at || cs.decided_at || cs.created_at || "").slice(0, 10);
  const meta = [count, source, when].filter(Boolean).join(" · ");
  return `
    <div class="wc-row wc-${escAttr(cs.status)}" data-wc-id="${escAttr(cs.id)}" data-wc-world="${escAttr(cs.world_id)}">
      <div class="wc-row-main">
        <span class="wc-row-summary">${esc(cs.summary || operationTitle(ops[0]) || "World change")}</span>
        <span class="wc-row-meta">${esc(meta)}</span>
      </div>
      <div class="wc-row-actions">${actionsHtml(cs)}</div>
    </div>`;
}

/** The edit form for one operation. Dropping one is unchecking Include rather
 * than a delete button: the whole batch commits together, so "which of these do
 * I still want" is one decision across the list, not a run of destructive clicks. */
export function operationEditHtml(op, index) {
  const isBodyless = op?.op === "suppress" || op?.op === "archive";
  const keywords = (op?.keywords || []).join(", ");
  return `
    <div class="wc-edit-op" data-wc-index="${escAttr(index)}">
      <label class="wc-edit-include">
        <input type="checkbox" data-wc-field="include" checked>
        <span>${esc(operationTitle(op))}</span>
      </label>
      ${
        isBodyless
          ? ""
          : `
      <input class="wc-edit-name" type="text" data-wc-field="name" value="${escAttr(op?.name || "")}" placeholder="Entry name">
      <textarea class="wc-edit-content" data-wc-field="content" rows="3" placeholder="What is now true">${esc(op?.content || "")}</textarea>
      <div class="wc-edit-activation">
        <select data-wc-field="activation">
          <option value="constant"${op?.activation === "constant" ? " selected" : ""}>Always in context</option>
          <option value="keywords"${op?.activation === "constant" ? "" : " selected"}>Keyword-activated</option>
        </select>
        <input type="text" data-wc-field="keywords" value="${escAttr(keywords)}" placeholder="Keywords, comma separated">
      </div>`
      }
    </div>`;
}

/** Read one edit form back into an operation, or `null` when it was excluded.
 * Spread over the original rather than built fresh, so what the form does not
 * expose (the target id, the rationale) survives the round trip. */
export function readOperationEdit(op, values) {
  if (!values.include) return null;
  if (op.op === "suppress" || op.op === "archive") return { ...op };
  const activation = values.activation === "constant" ? "constant" : "keywords";
  return {
    ...op,
    name: (values.name ?? op.name ?? "").trim(),
    content: (values.content ?? op.content ?? "").trim(),
    activation,
    keywords:
      activation === "constant"
        ? []
        : String(values.keywords ?? "")
            .split(",")
            .map((k) => k.trim())
            .filter(Boolean),
  };
}
