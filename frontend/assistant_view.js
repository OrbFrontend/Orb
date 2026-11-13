// The assistant transcript as data: what may be stored, how a run's events change it,
// and its markup. No DOM lookups and no requests, so every rule here can be tested on its own.
import { EDIT_ICON } from "./icons.js";
import { esc, escAttr, sentenceDiff } from "./utils.js";

export const MAX_ITEM_CHARS = 8000;
const MAX_ITEMS = 100;
const TRIMMED_ITEMS = 80;
const MAX_LABEL_CHARS = 200;
const MAX_NOTE_CHARS = 500;
const MAX_IDS_CHARS = 2000;
// A run proposes at most one change per tool run, so one card holds every change a run can propose.
const MAX_PROPOSALS = 12;
const MAX_DETAIL_CHARS = 500;
// Unchanged text shown on each side of a change; the whole new text stays one click away.
const DIFF_CONTEXT = 120;
const MODES = new Set(["chat", "document", "library"]);
// `opened` is a chat the assistant opened, drawn as the view change it is; `reset` is where the server's copy of the
// conversation starts, when it no longer holds the entries above.
const OUTCOMES = new Set(["stopped", "limit", "error", "interrupted", "applied", "handoff", "opened", "reset"]);
const SEPARATORS = new Set(["opened", "reset"]);
const RESET = "The assistant no longer remembers the messages above.";
const NO_RESULT = "No result was received; check before asking again.";
const INTERRUPTED = "The connection ended before the assistant finished.";
const HANDOFF_TOOLS = new Set(["regenerate_reply", "rewrite_reply", "continue_chat", "send_as_user", "open_chat"]);
// A proposed change's states besides `pending`. A change is live while this page holds its proposal id: a pending one can be
// applied, and an applied edit undone. A stored card keeps no ids, so its pending changes load as historical.
const PROPOSAL_NOTES = {
  applied: "Applied.",
  cancelled: "Cancelled; nothing was changed.",
  superseded: "Replaced by a later proposal for the same change.",
  stale: "Not applied: the text changed after this preview.",
  missing: "Not applied: what it edits no longer exists.",
  expired: "Not applied: this proposal expired.",
  historical: "Not applied, and no longer open to Apply.",
};
const PROPOSAL_STATES = new Set(["pending", ...Object.keys(PROPOSAL_NOTES)]);
// The verdicts Apply answers that settle an edit; `busy` leaves it pending, to try again.
const VERDICTS = new Set(["applied", "stale", "missing", "expired"]);
const ASK_AGAIN = new Set(["stale", "missing", "expired", "historical"]);
// What Undo answers that leaves an applied edit as it is, and why; `undone` makes it pending again.
const UNDO_NOTES = {
  stale: "Not undone: the text changed after the edit was applied.",
  missing: "Not undone: what it edited no longer exists.",
  expired: "Not undone: this edit can no longer be undone here.",
};

const text = (value, cap) => (typeof value === "string" ? value.slice(0, cap) : "");
const id = (value) => (typeof value === "string" && value && value.length <= 64 ? value : null);
const count = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : 0);

function storedContext(value) {
  if (!value || typeof value !== "object" || !MODES.has(value.mode)) return null;
  return {
    mode: value.mode,
    conversation_id: id(value.conversation_id),
    character_id: id(value.character_id),
    document_id: id(value.document_id),
    label: text(value.label, MAX_LABEL_CHARS),
    // The lorebook drawer's focus, kept only when there was one, so a view without it reads as it always did.
    ...(id(value.world_id) ? { world_id: id(value.world_id) } : {}),
    ...(id(value.world_id) && Number.isSafeInteger(value.entry_id) && value.entry_id > 0
      ? { entry_id: value.entry_id }
      : {}),
  };
}

function storedIds(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  try {
    return JSON.stringify(value).length <= MAX_IDS_CHARS ? JSON.parse(JSON.stringify(value)) : null;
  } catch {
    return null;
  }
}

// One proposed edit as stored: its labels, lengths and state, never its texts or id, so it can never be applied again.
function storedProposal(value) {
  const tool = text(value?.tool, 64);
  const status = value?.status === "pending" ? "historical" : value?.status;
  if (!tool || !PROPOSAL_STATES.has(status)) return null;
  const entry = {
    tool,
    label: text(value.label, MAX_LABEL_CHARS),
    field: text(value.field, 64),
    beforeChars: count(value.beforeChars),
    afterChars: count(value.afterChars),
    status,
  };
  if (value.list === true) entry.list = true;
  if (value.action === "delete") {
    entry.action = "delete";
    entry.detail = text(value.detail, MAX_DETAIL_CHARS);
  }
  const target = storedIds(value.target);
  if (target) entry.target = target;
  return entry;
}

// One entry rebuilt from its known fields only: anything else a stored or forged entry carries is dropped, so loading a
// transcript can never bring back live state such as a running tool or a control that acts.
function storedItem(value) {
  if (!value || typeof value !== "object") return null;
  const body = text(value.text, MAX_ITEM_CHARS);
  if (value.kind === "user") {
    const context = storedContext(value.context);
    if (!body.trim() || !context) return null;
    // The request's name on the server, which an edit of it sends back; an open edit box is not kept.
    return { kind: "user", text: body, context, ...(id(value.requestId) ? { requestId: id(value.requestId) } : {}) };
  }
  if (value.kind === "assistant") return body.trim() ? { kind: "assistant", text: body } : null;
  if (value.kind === "tool") {
    const tool = text(value.tool, 64);
    if (!tool) return null;
    const running = value.running === true;
    const item = { kind: "tool", tool, text: running ? NO_RESULT : body, ok: !running && value.ok === true };
    const note = text(value.note, MAX_NOTE_CHARS);
    const ids = storedIds(value.ids);
    if (note) item.note = note;
    if (ids) item.ids = ids;
    return item;
  }
  if (value.kind === "proposals") {
    const proposals = (Array.isArray(value.proposals) ? value.proposals : [])
      .slice(0, MAX_PROPOSALS)
      .map(storedProposal)
      .filter(Boolean);
    if (!proposals.length) return null;
    const card = { kind: "proposals", proposals };
    const request = text(value.request, MAX_ITEM_CHARS);
    const note = text(value.note, MAX_NOTE_CHARS);
    if (request) card.request = request;
    if (note) card.note = note;
    return card;
  }
  if (value.kind === "outcome" && OUTCOMES.has(value.reason))
    return { kind: "outcome", reason: value.reason, text: body };
  return null;
}

/** A name for a request or a conversation that the server has never seen. */
export function newId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** A `handoff` event's checked target with its tool, or null when it names nothing this page can start. */
function handoffTarget(d) {
  return HANDOFF_TOOLS.has(d.tool) && d.handoff?.conversation_id ? { tool: d.tool, ...d.handoff } : null;
}

// What the transcript says once the chat was asked to start a handoff, by why it did not ("" when it did).
const HANDOFF_NOTES = {
  "": "Started {what} in the chat. The chat writes the reply, and its own Stop ends it.",
  not_open:
    "Not started: the chat for {what} is no longer the one open. Open it and ask again, or use the reply's own button.",
  moved: "Not started: that reply is no longer on the branch the chat shows. Ask again for the reply you mean.",
  busy: "Not started: the chat is busy, or this tab cannot send now. Try again once it is free.",
  stopped:
    "Not started: the assistant was stopped before the chat began {what}. Ask again, or use the reply's own button.",
};

// A continue or a send follows on from the chat's last message, so a newer one there is what moves it.
const CONTINUE_MOVED = "Not started: the chat has a newer message than the one the assistant checked. Ask again.";

// What the transcript says about a chat the assistant was asked to open, by why it did not ("" when it did).
const OPEN_NOTES = {
  document: "Not opened: Document mode is on. Switch to Chat mode and pick {what} in the chat list.",
  moved: "Not opened: the view changed while the assistant worked. Pick {what} in the chat list.",
  stopped: "Not opened: the assistant was stopped first. Pick {what} in the chat list.",
};

/** The outcome entry for a handoff the browser was asked to start; *status* is its answer, "" once it started. */
export function handoffOutcome(handoff, status) {
  if (handoff.tool === "open_chat") {
    const chat = handoff.title ? `“${handoff.title}”` : "the chat";
    if (status === "") return { kind: "outcome", reason: "opened", text: `Opened ${chat}` };
    const note = OPEN_NOTES[status] ?? OPEN_NOTES.moved;
    return { kind: "outcome", reason: "handoff", text: note.replace("{what}", chat) };
  }
  const followsLast = handoff.tool === "continue_chat" || handoff.tool === "send_as_user";
  const doing =
    handoff.tool === "continue_chat"
      ? "the next reply"
      : handoff.tool === "send_as_user"
        ? "sending your message"
        : `${handoff.tool === "rewrite_reply" ? "rewriting" : "regenerating"} message ${handoff.message_id}`;
  const note = followsLast && status === "moved" ? CONTINUE_MOVED : (HANDOFF_NOTES[status] ?? HANDOFF_NOTES.busy);
  return { kind: "outcome", reason: "handoff", text: note.replace("{what}", doing) };
}

/**
 * The stored transcript as valid entries; malformed JSON or a wrong shape is an empty transcript. A request with no answer
 * after it (the page closed mid-run) gets an interrupted outcome, so it reads as unanswered rather than as still pending.
 */
export function parseTranscript(stored) {
  let raw = null;
  try {
    raw = JSON.parse(stored || "null");
  } catch {}
  if (!Array.isArray(raw)) return [];
  const items = [];
  const kept = raw.map(storedItem).filter(Boolean);
  kept.forEach((item, index) => {
    items.push(item);
    if (item.kind === "user" && (index === kept.length - 1 || kept[index + 1].kind === "user"))
      items.push({ kind: "outcome", reason: "interrupted", text: INTERRUPTED });
  });
  return items.slice(-MAX_ITEMS);
}

/** What is kept between page loads: summaries and ids, never a live run's state or an actionable proposal. */
export function serializeTranscript(items) {
  return JSON.stringify(items.map(storedItem).filter(Boolean).slice(-MAX_ITEMS));
}

/** Drop the oldest entries past the cap, in place. The log is the reader's record only: the server holds the conversation. */
export function trimTranscript(items) {
  if (items.length > MAX_ITEMS) items.splice(0, items.length - TRIMMED_ITEMS);
}

const edited = (entry) =>
  entry.action === "delete"
    ? `deletion of ${entry.label}`
    : `${entry.field ? `${entry.field.toLowerCase()} of ` : ""}${entry.label}`;

/** The view fields the server checks; the label is the browser's own and is not sent as context. */
export function wireContext(context) {
  const out = { mode: context.mode };
  for (const key of ["conversation_id", "character_id", "document_id", "world_id", "entry_id"])
    if (context[key]) out[key] = context[key];
  return out;
}

const sameView = (a, b) => !!a && !!b && JSON.stringify(wireContext(a)) === JSON.stringify(wireContext(b));

/** Enter sends; Shift+Enter is a newline, and an Enter that commits an IME composition is neither. */
export function isSendKey(e) {
  return e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229;
}

function payload(data) {
  try {
    const parsed = data ? JSON.parse(data) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

/** A `confirm` event's preview as a live card entry, or null. Its texts are kept whole: they are what Apply commits. */
function proposalEntry(tool, value) {
  const field = value?.fields?.[0];
  if (!value?.proposal_id || !field) return null;
  const entry = {
    proposalId: value.proposal_id,
    tool: text(tool, 64) || "edit",
    label: text(value.label, MAX_LABEL_CHARS),
    field: text(field.label, 64) || text(field.field, 64),
    before: field.before,
    after: field.after,
    beforeChars: field.before.length,
    afterChars: field.after.length,
    target: value.target,
    status: "pending",
  };
  if (Array.isArray(field.before)) entry.list = true;
  if (value.action === "delete") {
    entry.action = "delete";
    entry.detail = text(field.before, MAX_DETAIL_CHARS);
    entry.beforeChars = entry.afterChars = 0;
  }
  if (typeof value.replaces === "string") entry.replaces = value.replaces;
  return entry;
}

/** Take a revised preview into its live *entry*: what Apply now writes. */
export function reviseEntry(entry, value) {
  const field = value?.fields?.[0];
  if (!field || typeof field.after !== "string") return;
  entry.after = field.after;
  entry.afterChars = field.after.length;
}

/** Every live entry of every card in *items*, each with its card and that card's index. */
function liveEntries(items) {
  const out = [];
  items.forEach((item, index) => {
    if (item.kind !== "proposals") return;
    for (const entry of item.proposals) if (entry.proposalId) out.push({ entry, card: item, index });
  });
  return out;
}

/** The ids of every change in *items* still waiting for Apply: what the next request is told of. */
export function pendingProposalIds(items) {
  return liveEntries(items)
    .filter(({ entry }) => entry.status === "pending")
    .map(({ entry }) => entry.proposalId);
}

/** The live entry *proposalId* names in *items*, with its card, or null. */
export function findProposal(items, proposalId) {
  return liveEntries(items).find(({ entry }) => entry.proposalId === proposalId) || null;
}

/**
 * Apply one stream event to *run* (`{ items, note, status, terminal, request? }`). `terminal` is set only by `done` or
 * `error`: a stream that simply ends leaves it null, which `settleRun` reports as interrupted, never as an answer.
 * `proposals` collects the edits the run proposed, a later preview of one replacing its entry; `failed` notes a tool
 * result that came back failed; `handoff` is the reply the run asks the chat to generate once it has settled. Returns the
 * resources a `mutation` committed, for the caller to read again, and otherwise an empty list.
 */
export function applyStreamEvent(run, event, data, errorMessage) {
  const d = payload(data);
  let committed = [];
  if (event === "start") {
    // The server holds another conversation, or none: what this log shows above the request is not in the model's context.
    const request = run.items.findLastIndex((item) => item.kind === "user");
    if (d.continued === false && request > 0)
      run.items.splice(request, 0, { kind: "outcome", reason: "reset", text: RESET });
  } else if (event === "progress") {
    run.status = text(d.label, MAX_LABEL_CHARS);
  } else if (event === "assistant_message") {
    const body = text(d.text, MAX_ITEM_CHARS);
    if (d.final === true) run.items.push({ kind: "assistant", text: body });
    else run.note = body.slice(0, MAX_NOTE_CHARS);
  } else if (event === "tool_start") {
    const item = { kind: "tool", tool: text(d.tool, 64) || "tool", text: "", ok: false, running: true };
    if (typeof d.call_id === "string") item.callId = d.call_id;
    if (run.note) item.note = run.note;
    run.note = "";
    run.items.push(item);
  } else if (event === "mutation") {
    committed = Array.isArray(d.resources) ? d.resources : [];
  } else if (event === "tool_result") {
    const item = run.items.findLast((entry) => entry.kind === "tool" && entry.running && entry.callId === d.call_id);
    if (item) {
      delete item.running;
      item.ok = d.ok === true;
      item.text = text(d.summary, MAX_ITEM_CHARS);
      const ids = storedIds(d.ids);
      if (ids && Object.keys(ids).length) item.ids = ids;
    }
    if (d.ok !== true) run.failed = true;
  } else if (event === "confirm") {
    const entry = proposalEntry(d.tool, d.proposal);
    // A change an earlier card still holds, made again on top of what it proposed: that card's entry gives way.
    const replaced = entry?.replaces ? findProposal(run.items, entry.replaces) : null;
    if (replaced && replaced.entry.status === "pending") {
      replaced.entry.status = "superseded";
      delete replaced.entry.proposalId;
    }
    run.proposals ||= [];
    const index = entry ? run.proposals.findIndex((item) => item.proposalId === entry.proposalId) : -1;
    if (index >= 0) run.proposals[index] = entry;
    else if (entry && run.proposals.length < MAX_PROPOSALS) run.proposals.push(entry);
  } else if (event === "handoff") {
    run.handoff = handoffTarget(d) || run.handoff;
  } else if (event === "done") {
    run.terminal = OUTCOMES.has(d.reason) ? d.reason : "reply";
    if (run.terminal === "limit")
      run.items.push({ kind: "outcome", reason: "limit", text: text(d.message, MAX_ITEM_CHARS) });
    else if (run.terminal === "stopped")
      run.items.push({ kind: "outcome", reason: "stopped", text: text(d.message, MAX_ITEM_CHARS) || "Stopped." });
  } else if (event === "error") {
    run.terminal = "error";
    run.items.push({
      kind: "outcome",
      reason: "error",
      text: text(errorMessage, MAX_ITEM_CHARS) || "The assistant failed.",
    });
  }
  trimTranscript(run.items);
  return committed;
}

/**
 * Close *run* once its stream is over: tools still waiting get no result, a run without a verdict gets one, and the edits
 * it proposed become one card at its end, however it ended (`run.card`): proposing wrote nothing, so showing them is safe.
 */
export function settleRun(run, { stopped = false, failure = "" } = {}) {
  for (const item of run.items) {
    if (item.kind !== "tool" || !item.running) continue;
    delete item.running;
    item.ok = false;
    item.text = NO_RESULT;
  }
  if (!run.terminal) {
    const outcome = stopped
      ? { kind: "outcome", reason: "stopped", text: "Stopped." }
      : failure
        ? { kind: "outcome", reason: "error", text: failure.slice(0, MAX_ITEM_CHARS) }
        : { kind: "outcome", reason: "interrupted", text: INTERRUPTED };
    run.items.push(outcome);
    run.terminal = outcome.reason;
  }
  if (run.proposals?.length) {
    run.card = { kind: "proposals", proposals: run.proposals, request: text(run.request, MAX_ITEM_CHARS) };
    run.items.push(run.card);
  }
  run.status = "";
  trimTranscript(run.items);
}

/** The ids of *card*'s edits still waiting for Apply. */
export function pendingIds(card) {
  return card.proposals.filter((entry) => entry.status === "pending").map((entry) => entry.proposalId);
}

/**
 * Record Apply's *results* (`[{proposal_id, verdict, ...}]`) on *card*'s pending edits. `busy` leaves an edit pending with
 * its note; an unknown verdict leaves it as it was. Returns what the applied ones changed and their summaries.
 */
export function applyVerdicts(card, results) {
  const out = { resources: [], summaries: [] };
  const skipped = [];
  for (const result of Array.isArray(results) ? results : []) {
    const entry = card.proposals.find((item) => item.status === "pending" && item.proposalId === result?.proposal_id);
    if (!entry) continue;
    delete entry.note;
    if (result.verdict === "busy") entry.note = text(result.message, MAX_NOTE_CHARS) || "Busy; try again shortly.";
    else if (VERDICTS.has(result.verdict)) entry.status = result.verdict;
    if (entry.status === "applied") {
      out.resources.push(...(result.resources || []));
      out.summaries.push(text(result.summary, MAX_NOTE_CHARS) || `Applied the edit to the ${edited(entry)}.`);
    } else if (entry.status !== "pending") skipped.push(edited(entry));
  }
  card.note =
    skipped.length && out.summaries.length
      ? `Applied ${out.summaries.length}; skipped ${skipped.length}: ${skipped.join("; ")}.`.slice(0, MAX_NOTE_CHARS)
      : "";
  return out;
}

/**
 * Record Undo's *results* on *card*'s applied edits. `undone` makes an edit pending again, to apply or cancel; any other
 * verdict leaves it applied with its reason, and one that can no longer be undone loses its control. Returns what the undone
 * ones changed and their summaries.
 */
export function undoVerdicts(card, results) {
  const out = { resources: [], summaries: [] };
  for (const result of Array.isArray(results) ? results : []) {
    const entry = card.proposals.find((item) => item.status === "applied" && item.proposalId === result?.proposal_id);
    if (!entry) continue;
    if (result.verdict === "undone") {
      entry.status = "pending";
      entry.note = "Undone; the stored text is back. Apply it again or cancel it.";
      out.resources.push(...(result.resources || []));
      out.summaries.push(text(result.summary, MAX_NOTE_CHARS) || `Undid the edit to the ${edited(entry)}.`);
    } else {
      entry.note = UNDO_NOTES[result.verdict] || text(result.message, MAX_NOTE_CHARS) || "Not undone.";
      if (result.verdict !== "busy") delete entry.proposalId;
    }
  }
  return out;
}

function clipped(value, { head, tail }) {
  if (value.length <= head + tail + 1) return esc(value);
  return `${head ? esc(value.slice(0, head)) : ""}<span class="asst-gap">…</span>${tail ? esc(value.slice(-tail)) : ""}`;
}

function diffOps(before, after) {
  return before && after
    ? sentenceDiff(before, after)
    : [...(before ? [{ type: "delete", text: before }] : []), ...(after ? [{ type: "insert", text: after }] : [])];
}

// Unchanged text is shortened to its nearest context: only its tail before the first change, only its head after the last.
function opHtml(op, index, ops) {
  if (op.type === "insert") return `<ins class="asst-ins">${esc(op.text)}</ins>`;
  if (op.type === "delete") return `<del class="asst-del">${esc(op.text)}</del>`;
  const head = index === ops.length - 1 ? DIFF_CONTEXT : index === 0 ? 0 : DIFF_CONTEXT;
  const tail = index === 0 ? DIFF_CONTEXT : index === ops.length - 1 ? 0 : DIFF_CONTEXT;
  return clipped(op.text, { head, tail });
}

/**
 * One field's change as escaped markup: removed and added text marked, unchanged text around a change shortened to its
 * nearest context. The texts are the server's exact before and after.
 */
export function diffHtml(before, after) {
  const ops = diffOps(before, after);
  return ops.map((op, index) => opHtml(op, index, ops)).join("");
}

// The same change as two columns: what the field holds now with its removals, and what it will hold with its additions.
// The stylesheet shows this in place of the inline diff once the panel is wide enough for two columns.
function splitHtml(entry) {
  const ops = diffOps(entry.before, entry.after);
  const side = (label, drop) =>
    `<div class="asst-diff-side"><div class="asst-diff-side-label">${label}</div><div class="asst-diff">` +
    `${ops.map((op, index) => (op.type === drop ? "" : opHtml(op, index, ops))).join("")}</div></div>`;
  return `<div class="asst-diff-split" aria-hidden="true">${side("Now", "insert")}${side("Proposed", "delete")}</div>`;
}

/** A list field's change: the items it removes and adds, and how many it keeps. */
function listDiffHtml(before, after) {
  const removed = before.filter((item) => !after.includes(item));
  const added = after.filter((item) => !before.includes(item));
  const kept = after.length - added.length;
  const rows = [
    ...removed.map((item) => `<li><del class="asst-del">${esc(item)}</del></li>`),
    ...added.map((item) => `<li><ins class="asst-ins">${esc(item)}</ins></li>`),
  ];
  const reordered = !rows.length && before.join("\n") !== after.join("\n");
  const rest = reordered ? "Same items in a new order." : kept ? `${kept} unchanged.` : "";
  return `${rows.length ? `<ul class="asst-diff-list">${rows.join("")}</ul>` : ""}${rest ? `<div class="asst-gap">${rest}</div>` : ""}`;
}

function listEntryHtml(entry) {
  const full = entry.after.map((item) => `<li>${esc(item)}</li>`).join("");
  return (
    `<div class="asst-diff">${listDiffHtml(entry.before, entry.after)}</div>` +
    `<details class="asst-diff-full"><summary>Full new list</summary><ol class="asst-diff-text">${full}</ol></details>`
  );
}

// A preview's texts never change once received, and diffing them is the costly part of drawing a card.
const diffCache = new WeakMap();

function entryDiffHtml(entry) {
  if (entry.before === undefined) return "";
  const hit = diffCache.get(entry);
  if (hit && hit.before === entry.before && hit.after === entry.after) return hit.html;
  const html = entry.list
    ? listEntryHtml(entry)
    : `<div class="asst-diff asst-diff-inline">${diffHtml(entry.before, entry.after)}</div>` +
      splitHtml(entry) +
      `<details class="asst-diff-full"><summary>Full new text</summary><div class="asst-diff-text">${esc(entry.after)}</div></details>`;
  diffCache.set(entry, { before: entry.before, after: entry.after, html });
  return html;
}

const button = (action, label, attrs, { accent = false, disabled = false } = {}) =>
  `<button type="button" class="btn btn-xs${accent ? " btn-accent" : ""}" data-wf-action="assistant:${action}"${attrs}` +
  `${disabled ? " disabled" : ""}>${label}</button>`;

// The controls of one live entry: a pending change applies, is discarded or, for text, edited by hand first; an applied edit
// can be undone. A delete has no text to edit and no undo.
function entryControls(entry, busy) {
  if (!entry.proposalId) return "";
  const attrs = ` data-proposal="${escAttr(entry.proposalId)}"`;
  const textEdit = entry.action !== "delete" && !entry.list;
  let controls = "";
  if (entry.status === "pending" && entry.editing) {
    controls =
      button("reviseSave", "Save text", attrs, { accent: true, disabled: busy }) +
      button("reviseCancel", "Back", attrs);
  } else if (entry.status === "pending") {
    controls =
      button("apply", entry.action === "delete" ? "Delete" : "Apply", attrs, { accent: true, disabled: busy }) +
      (textEdit ? button("revise", "Edit", attrs, { disabled: busy }) : "") +
      button("cancel", "Discard", attrs, { disabled: busy });
  } else if (entry.status === "applied" && entry.action !== "delete") {
    controls = button("undo", "Undo", attrs, { disabled: busy });
  }
  return controls ? `<div class="asst-edit-actions">${controls}</div>` : "";
}

function editorHtml(entry) {
  return (
    `<textarea class="asst-revise" data-wf-action="assistant:reviseDraft" data-wf-on="input" ` +
    `data-proposal="${escAttr(entry.proposalId)}" aria-label="Your text for this edit" rows="6">${esc(entry.draft ?? entry.after)}</textarea>`
  );
}

function entryHtml(entry, busy) {
  const note = entry.note || PROPOSAL_NOTES[entry.status] || "";
  const noteHtml = note ? `<div class="asst-proposal-note">${esc(note)}</div>` : "";
  if (entry.action === "delete") {
    return (
      `<div class="asst-edit asst-edit-delete asst-edit-${entry.status}">` +
      `<div class="asst-diff-label">Delete <span class="asst-diff-size">cannot be undone</span></div>` +
      (entry.detail ? `<div class="asst-delete-detail">${esc(entry.detail)}</div>` : "") +
      noteHtml +
      entryControls(entry, busy) +
      "</div>"
    );
  }
  const unit = entry.list ? "items" : "characters";
  const size = `${entry.beforeChars.toLocaleString("en-US")} → ${entry.afterChars.toLocaleString("en-US")} ${unit}`;
  const body = entry.editing && entry.proposalId ? editorHtml(entry) : entryDiffHtml(entry);
  return (
    `<div class="asst-edit asst-edit-${entry.status}">` +
    `<div class="asst-diff-label">${esc(entry.field || "Text")} <span class="asst-diff-size">${size}</span></div>${body}` +
    noteHtml +
    entryControls(entry, busy) +
    "</div>"
  );
}

function cardHtml(card, index, { busy }) {
  const groups = new Map();
  for (const entry of card.proposals) {
    if (!PROPOSAL_STATES.has(entry.status)) continue;
    const key = JSON.stringify([entry.label, entry.target]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  const body = [...groups.values()]
    .map(
      (entries) =>
        `<div class="asst-proposal-target">${esc(entries[0].label || "an item")}</div>${entries.map((entry) => entryHtml(entry, busy)).join("")}`,
    )
    .join("");
  const live = card.proposals.filter((entry) => entry.proposalId && entry.status === "pending");
  const attrs = ` data-index="${escAttr(String(index))}"`;
  const total = card.proposals.length;
  let controls = "";
  if (live.length > 1) {
    controls =
      button("applyAll", `Apply all ${live.length}`, attrs, { accent: true, disabled: busy }) +
      button("cancelAll", "Cancel all", attrs, { disabled: busy });
  } else if (!live.length && card.request && card.proposals.some((entry) => ASK_AGAIN.has(entry.status))) {
    controls = button("askAgain", "Ask again", attrs);
  }
  const deletes = card.proposals.filter((entry) => entry.action === "delete").length;
  const noun = deletes === total ? "delete" : deletes ? "change" : "edit";
  return (
    `<div class="asst-proposal${live.length ? " asst-proposal-live" : ""}" role="group" aria-label="Proposed changes">` +
    `<div class="asst-proposal-head">${total === 1 ? `Proposed ${noun}` : `${total} proposed ${noun}s`}</div>${body}` +
    (card.note ? `<div class="asst-proposal-note">${esc(card.note)}</div>` : "") +
    (controls ? `<div class="asst-proposal-actions">${controls}</div>` : "") +
    "</div>"
  );
}

/**
 * One request: its bubble with Edit beside it, or the box its new wording is typed in. Sending that takes the conversation
 * back to this request, so the box says what goes with it: every entry below, and the changes among them still waiting.
 */
function requestHtml(item, index, items, busy) {
  const attrs = ` data-index="${escAttr(String(index))}"`;
  if (!item.editing) {
    return (
      `<div class="asst-request"><div class="asst-msg asst-user">${esc(item.text)}</div>` +
      `<button type="button" class="asst-request-edit" data-wf-action="assistant:editRequest"${attrs}` +
      ` title="Edit" aria-label="Edit message"${busy ? " disabled" : ""}>${EDIT_ICON}</button></div>`
    );
  }
  const waiting = pendingProposalIds(items.slice(index + 1)).length;
  const discarded = waiting
    ? ` ${waiting === 1 ? "The change" : `The ${waiting} changes`} still waiting for Apply ${waiting === 1 ? "is" : "are"} discarded.`
    : "";
  return (
    `<div class="asst-request asst-request-editing">` +
    `<textarea class="asst-request-box" data-wf-action="assistant:requestDraft" data-wf-on="input keydown"${attrs} ` +
    `aria-label="Your message" maxlength="${MAX_ITEM_CHARS}">${esc(item.draft ?? item.text)}</textarea>` +
    `<div class="asst-request-foot"><div class="asst-proposal-note">Sending removes everything below. ` +
    `Changes already made stay.${discarded}</div>` +
    `<div class="asst-edit-actions">${button("requestCancel", "Cancel", attrs)}` +
    `${button("requestSave", "Send", attrs, { accent: true, disabled: busy })}</div></div></div>`
  );
}

// Requests the tools can carry out from each kind of view, offered while the transcript is empty.
const SUGGESTIONS = {
  chat: [
    "Help me find a lost chat branch.",
    "How do I strategically get into her pants?",
    "Tighten entries for the active lorebooks.",
    "What are the most used 2-grams in this chat?",
    "Create an avatar image for this character. ComfyUI is up.",
  ],
  character: [
    "Summarise this character.",
    "Tighten this character's description.",
    "Write a persona that would make a good foil for this character.",
  ],
  entry: [
    "Tighten this entry without losing any facts.",
    "Suggest keywords that would bring this entry up.",
    "Does this entry contradict anything else in its world?",
  ],
  world: [
    "Which entries in this world have no keywords?",
    "Summarise this world in a paragraph.",
    "Add an entry for the world's capital.",
  ],
  document: [
    "Summarise this document.",
    "Which of my characters appear in this document?",
    "What is switched on in the pipeline?",
  ],
  library: [
    "What can you do?",
    "Make a character based on my favorite archetype, then start a chat.",
    "Create a fantasy world called India with an entry for its cursed denizens.",
    "Rate my fragments library.",
  ],
};

function emptyEntries(view) {
  const offered = SUGGESTIONS[view] || SUGGESTIONS.library;
  const buttons = offered
    .map(
      (text) =>
        `<button type="button" class="asst-suggestion" data-wf-action="assistant:suggest" data-text="${escAttr(text)}">${esc(text)}</button>`,
    )
    .join("");
  return [
    {
      key: "empty",
      html: '<div class="asst-empty">Ask about your characters, chats, worlds, personas, documents or settings.</div>',
    },
    {
      key: "suggestions",
      html: `<div class="asst-suggestions" role="group" aria-label="Suggested requests">${buttons}</div>`,
    },
  ];
}

// Each entry keeps one key for as long as it is in the transcript, so a repaint rebuilds only the rows that changed.
const rowKeys = new WeakMap();
let nextRowKey = 0;

function rowKey(item) {
  let key = rowKeys.get(item);
  if (key === undefined) {
    key = `r${nextRowKey++}`;
    rowKeys.set(item, key);
  }
  return key;
}

// A reply's text never changes once added, and rendering prose is the costly part of a repaint.
const proseCache = new WeakMap();

function proseHtml(item, renderProse) {
  const hit = proseCache.get(item);
  if (hit && hit.render === renderProse && hit.text === item.text) return hit.html;
  const html = renderProse(item.text);
  proseCache.set(item, { render: renderProse, text: item.text, html });
  return html;
}

// A stored transcript cannot point an image at another origin or route, so only a server-shaped id renders.
function imageHtml(item) {
  const imageId = item.tool === "create_image" && item.ok === true && !item.running ? item.ids?.image_id : null;
  if (typeof imageId !== "string" || !/^[a-f0-9]{32}$/.test(imageId)) return "";
  return (
    `<figure class="asst-image"><img class="workflow-artifact-image" src="/api/assistant/images/${imageId}" ` +
    `alt="Generated picture (temporary; save it to keep it)" loading="lazy" ` +
    `data-fallback="This picture expired or the server restarted. Ask for a new one.">` +
    `<figcaption>Temporary picture. Save it to keep it or set and crop an avatar.</figcaption></figure>`
  );
}

/**
 * The log's rows as `[{ key, html }]`, each one element. User text, labels, tool lines, outcomes and proposed edits are
 * escaped; only an assistant reply goes through *renderProse*, inside `.msg-body`, where no element can name an action.
 * A change's controls are drawn while this page holds its proposal, and are disabled while *busy*. An empty log
 * suggests requests for *suggestFor* (`chat`, `character`, `document` or `library`).
 */
export function transcriptEntries(items, renderProse, { busy = false, suggestFor = "library" } = {}) {
  if (!items.length) return emptyEntries(suggestFor);
  let view = null;
  const rows = [];
  items.forEach((item, index) => {
    const key = rowKey(item);
    const add = (html) => rows.push({ key, html });
    if (item.kind === "user") {
      if (!sameView(view, item.context)) {
        rows.push({
          key: `${key}v`,
          html: `<div class="asst-sep" role="separator">${esc(item.context.label || item.context.mode)}</div>`,
        });
      }
      view = item.context;
      add(requestHtml(item, index, items, busy));
    } else if (item.kind === "assistant") {
      add(`<div class="asst-msg asst-reply"><div class="msg-body">${proseHtml(item, renderProse)}</div></div>`);
    } else if (item.kind === "tool") {
      const state = item.running ? " asst-tool-running" : item.ok ? "" : " asst-tool-failed";
      const note = item.note ? `<div class="asst-tool-note">${esc(item.note)}</div>` : "";
      const result = item.running ? "Working…" : item.text;
      add(
        `<div class="asst-tool${state}">${note}<div class="asst-tool-line"><span class="asst-tool-name">${esc(item.tool)}</span>` +
          `<span class="asst-tool-text">${esc(result)}</span></div>${imageHtml(item)}</div>`,
      );
    } else if (item.kind === "proposals") {
      add(cardHtml(item, index, { busy }));
    } else if (item.kind === "outcome" && SEPARATORS.has(item.reason)) {
      add(`<div class="asst-sep" role="separator">${esc(item.text)}</div>`);
    } else if (item.kind === "outcome" && OUTCOMES.has(item.reason)) {
      add(`<div class="asst-outcome asst-outcome-${item.reason}">${esc(item.text)}</div>`);
    }
  });
  return rows;
}
