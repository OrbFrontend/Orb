// The assistant transcript as data: what may be stored, what is replayed to the server, how a run's events change it,
// and its markup. No DOM lookups and no requests, so every rule here can be tested on its own.
import { esc, escAttr, sentenceDiff } from "./utils.js";

export const MAX_ITEM_CHARS = 8000;
const MAX_ITEMS = 100;
const MAX_LABEL_CHARS = 200;
const MAX_NOTE_CHARS = 500;
const MAX_IDS_CHARS = 2000;
// A run proposes at most one edit per tool run, so one card holds every edit a run can propose.
const MAX_PROPOSALS = 12;
// Unchanged text shown on each side of a change; the whole new text stays one click away.
const DIFF_CONTEXT = 120;
const MODES = new Set(["chat", "document", "library"]);
const OUTCOMES = new Set(["stopped", "limit", "error", "interrupted", "applied", "handoff"]);
const NO_RESULT = "No result was received; check before asking again.";
const INTERRUPTED = "The connection ended before the assistant finished.";
const HANDOFF_TOOLS = new Set(["regenerate_reply", "rewrite_reply"]);
const MAX_DIRECTION_CHARS = 500;
// A proposed edit's states besides `pending`. Only a pending edit on the card this page holds can be applied; a stored
// card's pending edits load as historical.
const PROPOSAL_NOTES = {
  applied: "Applied.",
  cancelled: "Cancelled; nothing was changed.",
  stale: "Not applied: the text changed after this preview.",
  missing: "Not applied: what it edits no longer exists.",
  expired: "Not applied: this proposal expired.",
  historical: "Not applied, and no longer open to Apply.",
};
const PROPOSAL_STATES = new Set(["pending", ...Object.keys(PROPOSAL_NOTES)]);
// The verdicts Apply answers that settle an edit; `busy` leaves it pending, to try again.
const VERDICTS = new Set(["applied", "stale", "missing", "expired"]);
const ASK_AGAIN = new Set(["stale", "missing", "expired", "historical"]);

const text = (value, cap) => (typeof value === "string" ? value.slice(0, cap) : "");
const id = (value) => (typeof value === "string" && value && value.length <= 64 ? value : null);
const rowId = (value) => (Number.isSafeInteger(value) && value > 0 ? value : null);
const count = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : 0);

function storedContext(value) {
  if (!value || typeof value !== "object" || !MODES.has(value.mode)) return null;
  return {
    mode: value.mode,
    conversation_id: id(value.conversation_id),
    character_id: id(value.character_id),
    document_id: id(value.document_id),
    label: text(value.label, MAX_LABEL_CHARS),
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
  const target = resource(value.target);
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
    return body.trim() && context ? { kind: "user", text: body, context } : null;
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

// The ids each resource kind carries, each with its check and whether it is required.
const MESSAGE_IDS = { conversation_id: [id, true], message_id: [rowId, true] };
const RESOURCE_IDS = {
  lorebook_entry: { world_id: [id, true], entry_id: [rowId, false] },
  character: { character_id: [id, false] },
  world: { world_id: [id, false] },
  conversation: { conversation_id: [id, false] },
  persona: { persona_id: [rowId, false] },
  chat_worlds: { conversation_id: [id, true], world_id: [id, true] },
  branch: { conversation_id: [id, true] },
  message: MESSAGE_IDS,
  media: MESSAGE_IDS,
};

/** A resource the server named, rebuilt from the ids its kind carries. Anything else is dropped. */
function resource(value) {
  if (!value || typeof value !== "object") return null;
  if (value.kind === "settings") {
    const keys = Array.isArray(value.keys)
      ? value.keys.filter((key) => typeof key === "string" && key.length <= 64)
      : [];
    return keys.length ? { kind: "settings", keys: keys.slice(0, 16) } : null;
  }
  if (!Object.hasOwn(RESOURCE_IDS, value.kind)) return null;
  const out = { kind: value.kind };
  for (const [key, [check, required]] of Object.entries(RESOURCE_IDS[value.kind])) {
    const valid = check(value[key]);
    if (valid) out[key] = valid;
    else if (required) return null;
  }
  return out;
}

/** A `handoff` event's checked target, or null: which reply in which chat, and a rewrite's direction. */
function handoffTarget(value) {
  const tool = text(value?.tool, 64);
  const target = value?.handoff;
  const chat = id(target?.conversation_id);
  const message = rowId(target?.message_id);
  if (!HANDOFF_TOOLS.has(tool) || !chat || !message) return null;
  const out = { tool, conversation_id: chat, message_id: message };
  if (tool !== "rewrite_reply") return out;
  const direction = text(target.direction, MAX_DIRECTION_CHARS).trim();
  return direction ? { ...out, direction } : null;
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

/** The outcome entry for a handoff the chat was asked to start; *status* is the chat's answer, "" once it started. */
export function handoffOutcome(handoff, status) {
  const doing = handoff.tool === "rewrite_reply" ? "rewriting" : "regenerating";
  const note = HANDOFF_NOTES[status] ?? HANDOFF_NOTES.busy;
  return { kind: "outcome", reason: "handoff", text: note.replace("{what}", `${doing} message ${handoff.message_id}`) };
}

/** Validate resources a server response named, for the caller to read again. */
function resources(values) {
  return (Array.isArray(values) ? values : []).map(resource).filter(Boolean);
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

/** Drop the oldest entries past the cap, in place. */
export function trimTranscript(items) {
  if (items.length > MAX_ITEMS) items.splice(0, items.length - MAX_ITEMS);
}

const PROPOSAL_RECORDS = {
  pending: "proposed, not applied",
  applied: "applied by the user",
  cancelled: "cancelled by the user; nothing changed",
  historical: "proposed, never applied",
  stale: "not applied: the text changed first",
  missing: "not applied: the target was gone",
  expired: "not applied: the proposal expired",
};

const edited = (entry) => `${entry.field ? `${entry.field.toLowerCase()} of ` : ""}${entry.label}`;

/** The entries a run replays to the server. Tool and outcome entries go as text records; the server quotes them. */
export function runItems(items) {
  const out = [];
  for (const item of items.slice(-MAX_ITEMS)) {
    if (item.kind === "user") out.push({ kind: "user", text: item.text, view: item.context.label });
    else if (item.kind === "assistant") out.push({ kind: "assistant", text: item.text });
    else if (item.kind === "tool" && !item.running)
      out.push({ kind: "tool", tool: item.tool, text: `${item.ok ? "ok" : "failed"}: ${item.text}` });
    else if (item.kind === "proposals")
      for (const entry of item.proposals)
        out.push({
          kind: "tool",
          tool: entry.tool,
          text: `${PROPOSAL_RECORDS[entry.status]}: edit to the ${edited(entry)}`,
        });
    else if (item.kind === "outcome") out.push({ kind: "outcome", text: `${item.reason}: ${item.text}` });
  }
  return out.map((item) => ({ ...item, text: item.text.slice(0, MAX_ITEM_CHARS) }));
}

/** The view fields the server checks; the label is the browser's own and is not sent as context. */
export function wireContext(context) {
  const out = { mode: context.mode };
  for (const key of ["conversation_id", "character_id", "document_id"]) if (context[key]) out[key] = context[key];
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
  const proposalId = id(value?.proposal_id);
  const field = Array.isArray(value?.fields) ? value.fields[0] : null;
  if (!proposalId || typeof field?.before !== "string" || typeof field?.after !== "string") return null;
  return {
    proposalId,
    tool: text(tool, 64) || "edit",
    label: text(value.label, MAX_LABEL_CHARS),
    field: text(field.label, 64) || text(field.field, 64),
    before: field.before,
    after: field.after,
    beforeChars: field.before.length,
    afterChars: field.after.length,
    target: resource(value.target),
    status: "pending",
  };
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
  if (event === "progress") {
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
    committed = resources(d.resources);
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
    run.proposals ||= [];
    const index = entry ? run.proposals.findIndex((item) => item.proposalId === entry.proposalId) : -1;
    if (index >= 0) run.proposals[index] = entry;
    else if (entry && run.proposals.length < MAX_PROPOSALS) run.proposals.push(entry);
  } else if (event === "handoff") {
    const target = handoffTarget(d);
    if (target) run.handoff = target;
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
      out.resources.push(...resources(result.resources));
      out.summaries.push(text(result.summary, MAX_NOTE_CHARS) || `Applied the edit to the ${edited(entry)}.`);
    } else if (entry.status !== "pending") skipped.push(edited(entry));
  }
  card.note =
    skipped.length && out.summaries.length
      ? `Applied ${out.summaries.length}; skipped ${skipped.length}: ${skipped.join("; ")}.`.slice(0, MAX_NOTE_CHARS)
      : "";
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

/** The same change as two columns: what the field holds now with its removals, and what it will hold with its additions. */
export function splitDiffHtml(before, after) {
  const ops = diffOps(before, after);
  const side = (drop) => ops.map((op, index) => (op.type === drop ? "" : opHtml(op, index, ops))).join("");
  return { before: side("insert"), after: side("delete") };
}

// The stylesheet shows this in place of the inline diff once the panel is wide enough for two columns.
function splitHtml(entry) {
  const split = splitDiffHtml(entry.before, entry.after);
  return (
    '<div class="asst-diff-split" aria-hidden="true">' +
    `<div class="asst-diff-side"><div class="asst-diff-side-label">Now</div><div class="asst-diff">${split.before}</div></div>` +
    `<div class="asst-diff-side"><div class="asst-diff-side-label">Proposed</div><div class="asst-diff">${split.after}</div></div>` +
    "</div>"
  );
}

function entryHtml(entry) {
  const size = `${entry.beforeChars.toLocaleString("en-US")} → ${entry.afterChars.toLocaleString("en-US")} characters`;
  const diff =
    entry.before === undefined
      ? ""
      : `<div class="asst-diff asst-diff-inline">${diffHtml(entry.before, entry.after)}</div>` +
        splitHtml(entry) +
        `<details class="asst-diff-full"><summary>Full new text</summary><div class="asst-diff-text">${esc(entry.after)}</div></details>`;
  const note = entry.note || PROPOSAL_NOTES[entry.status] || "";
  return (
    `<div class="asst-edit asst-edit-${entry.status}">` +
    `<div class="asst-diff-label">${esc(entry.field || "Text")} <span class="asst-diff-size">${size}</span></div>${diff}` +
    (note ? `<div class="asst-proposal-note">${esc(note)}</div>` : "") +
    "</div>"
  );
}

function cardHtml(card, index, { active, busy }) {
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
        `<div class="asst-proposal-target">${esc(entries[0].label || "an item")}</div>${entries.map(entryHtml).join("")}`,
    )
    .join("");
  const pending = card.proposals.some((entry) => entry.status === "pending");
  const disabled = busy ? " disabled" : "";
  let controls = "";
  if (card === active && pending) {
    controls =
      `<button type="button" class="btn btn-accent btn-sm" data-wf-action="assistant:apply"${disabled}>Apply</button>` +
      `<button type="button" class="btn btn-sm" data-wf-action="assistant:cancel"${disabled}>Cancel</button>`;
  } else if (card.request && card.proposals.some((entry) => ASK_AGAIN.has(entry.status))) {
    controls = `<button type="button" class="btn btn-sm" data-wf-action="assistant:askAgain" data-index="${escAttr(String(index))}">Ask again</button>`;
  }
  const total = card.proposals.length;
  return (
    `<div class="asst-proposal${pending && card === active ? " asst-proposal-live" : ""}" role="group" aria-label="Proposed edits">` +
    `<div class="asst-proposal-head">${total === 1 ? "Proposed edit" : `${total} proposed edits`}</div>${body}` +
    (card.note ? `<div class="asst-proposal-note">${esc(card.note)}</div>` : "") +
    (controls ? `<div class="asst-proposal-actions">${controls}</div>` : "") +
    "</div>"
  );
}

// Requests the tools can carry out from each kind of view, offered while the transcript is empty.
const SUGGESTIONS = {
  chat: [
    "Summarise what has happened in this chat.",
    "Which worlds does this chat use?",
    "Rewrite the last reply to be shorter.",
    "Save a checkpoint of this chat.",
  ],
  character: [
    "Summarise this character.",
    "Tighten this character's description.",
    "Write a persona that would make a good foil for this character.",
  ],
  document: [
    "Summarise this document.",
    "Which of my characters appear in this document?",
    "What is switched on in the pipeline?",
  ],
  library: [
    "Which characters have no scenario?",
    "Create a world called Ashfall with an entry for its capital.",
    "List my personas.",
    "What is switched on in the pipeline?",
  ],
};

function emptyHtml(view) {
  const offered = SUGGESTIONS[view] || SUGGESTIONS.library;
  return (
    '<div class="asst-empty">Ask about your characters, chats, worlds, personas, documents or settings.</div>' +
    '<div class="asst-suggestions" role="group" aria-label="Suggested requests">' +
    offered
      .map(
        (text) =>
          `<button type="button" class="asst-suggestion" data-wf-action="assistant:suggest" data-text="${escAttr(text)}">${esc(text)}</button>`,
      )
      .join("") +
    "</div>"
  );
}

/**
 * The log's markup. User text, labels, tool lines, outcomes and proposed edits are escaped; only an assistant reply goes
 * through *renderProse*, inside `.msg-body`, where no element can name an action. Apply and Cancel are drawn only on
 * *active*, the one card this page holds, and are disabled while *busy*. An empty log suggests requests for *suggestFor*
 * (`chat`, `character`, `document` or `library`).
 */
export function transcriptHtml(items, renderProse, { active = null, busy = false, suggestFor = "library" } = {}) {
  if (!items.length) return emptyHtml(suggestFor);
  let view = null;
  const parts = [];
  items.forEach((item, index) => {
    if (item.kind === "user") {
      if (!sameView(view, item.context)) {
        parts.push(`<div class="asst-sep" role="separator">${esc(item.context.label || item.context.mode)}</div>`);
      }
      view = item.context;
      parts.push(`<div class="asst-msg asst-user">${esc(item.text)}</div>`);
    } else if (item.kind === "assistant") {
      parts.push(`<div class="asst-msg asst-reply"><div class="msg-body">${renderProse(item.text)}</div></div>`);
    } else if (item.kind === "tool") {
      const state = item.running ? " asst-tool-running" : item.ok ? "" : " asst-tool-failed";
      const note = item.note ? `<div class="asst-tool-note">${esc(item.note)}</div>` : "";
      const result = item.running ? "Working…" : item.text;
      parts.push(
        `<div class="asst-tool${state}">${note}<div class="asst-tool-line"><span class="asst-tool-name">${esc(item.tool)}</span>` +
          `<span class="asst-tool-text">${esc(result)}</span></div></div>`,
      );
    } else if (item.kind === "proposals") {
      parts.push(cardHtml(item, index, { active, busy }));
    } else if (item.kind === "outcome" && OUTCOMES.has(item.reason)) {
      parts.push(`<div class="asst-outcome asst-outcome-${item.reason}">${esc(item.text)}</div>`);
    }
  });
  return parts.join("");
}
