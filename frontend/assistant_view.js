// The assistant transcript as data: what may be stored, what is replayed to the server, how a run's events change it,
// and its markup. No DOM lookups and no requests, so every rule here can be tested on its own.
import { esc, escAttr, sentenceDiff } from "./utils.js";

export const MAX_ITEM_CHARS = 8000;
const MAX_ITEMS = 100;
const MAX_LABEL_CHARS = 200;
const MAX_NOTE_CHARS = 500;
const MAX_IDS_CHARS = 2000;
const MAX_FIELDS = 8;
// Unchanged text shown on each side of a change; the whole new text stays one click away.
const DIFF_CONTEXT = 120;
const MODES = new Set(["chat", "document", "library"]);
const OUTCOMES = new Set(["stopped", "limit", "error", "interrupted", "applied", "handoff"]);
const NO_RESULT = "No result was received.";
// A write that started and was never answered may or may not have committed; nothing resends it.
export const UNKNOWN_OUTCOME = "Outcome unknown; check the target.";
const COMMITTED_UNANSWERED = "Done; its result was not received.";
const INTERRUPTED = "The connection ended before the assistant finished.";
const RESOURCE_KINDS = new Set([
  "lorebook_entry",
  "settings",
  "character",
  "persona",
  "message",
  "world",
  "conversation",
  "chat_worlds",
  "branch",
  "media",
]);
const HANDOFF_TOOLS = new Set(["regenerate_reply", "rewrite_reply"]);
const MAX_DIRECTION_CHARS = 500;
// A proposal's states. Only `pending` and `unconfirmed` (an Apply that was never answered) can be applied, and only while
// the proposal is the one this page is holding; a stored card is always one of the inert states.
const PROPOSAL_STATES = new Set([
  "pending",
  "applying",
  "unconfirmed",
  "applied",
  "cancelled",
  "superseded",
  "stale",
  "missing",
  "expired",
  "historical",
  "unknown",
]);
const PROPOSAL_NOTES = {
  applying: "Applying…",
  unconfirmed: "Apply was not confirmed. Retry it, or check the target.",
  applied: "Applied.",
  cancelled: "Cancelled; nothing was changed.",
  superseded: "Not applied: a newer request replaced it.",
  stale: "Not applied: the text changed after this preview.",
  missing: "Not applied: what it edits no longer exists.",
  expired: "Not applied: this proposal expired.",
  historical: "From an earlier visit; it can no longer be applied.",
  unknown: "Apply outcome unknown; check the target.",
};
// What survives a reload: anything still open becomes historical, and an Apply in flight becomes an unknown outcome.
const STORED_STATE = { pending: "historical", superseded: "historical", applying: "unknown", unconfirmed: "unknown" };
const ASK_AGAIN = new Set(["stale", "missing", "expired", "historical", "superseded"]);

const text = (value, cap) => (typeof value === "string" ? value.slice(0, cap) : "");
const id = (value) => (typeof value === "string" && value && value.length <= 64 ? value : null);
const rowId = (value) => (Number.isSafeInteger(value) && value > 0 ? value : null);

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
    const result = running ? unanswered(value) : body;
    const item = { kind: "tool", tool, text: result, ok: running ? !!value.committed?.length : value.ok === true };
    const note = text(value.note, MAX_NOTE_CHARS);
    const ids = storedIds(value.ids);
    const target = resource(value.target);
    if (note) item.note = note;
    if (ids) item.ids = ids;
    // An unknown outcome keeps what it may have changed, so a later request can be told to check it rather than redo it.
    if (result === UNKNOWN_OUTCOME || value.unknown === true) item.unknown = true;
    if (target && item.unknown) item.target = target;
    return item;
  }
  if (value.kind === "proposal") {
    const tool = text(value.tool, 64);
    const status = STORED_STATE[value.status] || value.status;
    if (!tool || !PROPOSAL_STATES.has(status)) return null;
    const item = { kind: "proposal", tool, label: text(value.label, MAX_LABEL_CHARS), status };
    const fields = Array.isArray(value.fields) ? value.fields : [];
    item.fields = fields.slice(0, MAX_FIELDS).map((field) => ({ label: text(field?.label, 64) }));
    const request = text(value.request, MAX_ITEM_CHARS);
    const target = resource(value.target);
    if (request) item.request = request;
    if (target) item.target = target;
    return item;
  }
  if (value.kind === "outcome" && OUTCOMES.has(value.reason))
    return { kind: "outcome", reason: value.reason, text: body };
  return null;
}

// What a tool still waiting for its result says once the run is over: a read got nothing, a write may have committed.
function unanswered(item) {
  if (item.committed?.length) return COMMITTED_UNANSWERED;
  return item.write === true ? UNKNOWN_OUTCOME : NO_RESULT;
}

/**
 * A resource the server named, rebuilt from the ids its kind carries. Anything else is dropped. A create's target names only
 * its kind, since the id is the server's to assign; whatever that list shows is read again.
 */
function resource(value) {
  if (!value || typeof value !== "object" || !RESOURCE_KINDS.has(value.kind)) return null;
  if (value.kind === "lorebook_entry") {
    const world = id(value.world_id);
    if (!world) return null;
    const entry = rowId(value.entry_id) ? { entry_id: value.entry_id } : {};
    return { kind: "lorebook_entry", world_id: world, ...entry };
  }
  const optional = { character: "character_id", world: "world_id", conversation: "conversation_id" }[value.kind];
  if (optional) {
    const named = id(value[optional]);
    return named ? { kind: value.kind, [optional]: named } : { kind: value.kind };
  }
  if (value.kind === "persona") {
    const persona = rowId(value.persona_id);
    return persona ? { kind: "persona", persona_id: persona } : { kind: "persona" };
  }
  if (value.kind === "chat_worlds") {
    const chat = id(value.conversation_id);
    const world = id(value.world_id);
    return chat && world ? { kind: "chat_worlds", conversation_id: chat, world_id: world } : null;
  }
  if (value.kind === "branch") {
    const chat = id(value.conversation_id);
    return chat ? { kind: "branch", conversation_id: chat } : null;
  }
  if (value.kind === "message" || value.kind === "media") {
    const chat = id(value.conversation_id);
    const message = rowId(value.message_id);
    return chat && message ? { kind: value.kind, conversation_id: chat, message_id: message } : null;
  }
  const keys = Array.isArray(value.keys) ? value.keys.filter((key) => typeof key === "string" && key.length <= 64) : [];
  return keys.length ? { kind: "settings", keys: keys.slice(0, 16) } : null;
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
export function resources(values) {
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
  applying: "proposed; Apply outcome unknown, check the target",
  unconfirmed: "proposed; Apply outcome unknown, check the target",
  unknown: "proposed; Apply outcome unknown, check the target",
  applied: "applied by the user",
  cancelled: "cancelled by the user; nothing changed",
  superseded: "proposed, never applied",
  historical: "proposed, never applied",
  stale: "not applied: the text changed first",
  missing: "not applied: the target was gone",
  expired: "not applied: the proposal expired",
};

/** The entries a run replays to the server. Tool and outcome entries go as text records; the server quotes them. */
export function runItems(items) {
  const out = [];
  for (const item of items.slice(-MAX_ITEMS)) {
    if (item.kind === "user") out.push({ kind: "user", text: item.text, view: item.context.label });
    else if (item.kind === "assistant") out.push({ kind: "assistant", text: item.text });
    else if (item.kind === "tool" && !item.running) {
      // An unknown outcome is not a failure: "failed" would invite doing it again.
      const state = item.unknown
        ? "outcome unknown, may have happened; check before redoing"
        : item.ok
          ? "ok"
          : "failed";
      const target = item.unknown && item.target ? ` Target: ${JSON.stringify(item.target)}` : "";
      out.push({ kind: "tool", tool: item.tool, text: `${state}: ${item.text}${target}` });
    } else if (item.kind === "proposal")
      out.push({ kind: "tool", tool: item.tool, text: `${PROPOSAL_RECORDS[item.status]}: edit to ${item.label}` });
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

function sameView(a, b) {
  return (
    !!a &&
    !!b &&
    a.mode === b.mode &&
    a.conversation_id === b.conversation_id &&
    a.character_id === b.character_id &&
    a.document_id === b.document_id
  );
}

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

/** A `confirm` event's preview as a live proposal entry, or null. Field texts are kept whole: they are what Apply commits. */
function proposalItem(tool, value, run) {
  const proposalId = id(value?.proposal_id);
  const fields = (Array.isArray(value?.fields) ? value.fields : [])
    .slice(0, MAX_FIELDS)
    .flatMap((field) =>
      typeof field?.before === "string" && typeof field?.after === "string"
        ? [{ label: text(field.label, 64) || text(field.field, 64), before: field.before, after: field.after }]
        : [],
    );
  if (!proposalId || !fields.length) return null;
  return {
    kind: "proposal",
    tool: text(tool, 64) || "edit",
    proposalId,
    label: text(value.label, MAX_LABEL_CHARS),
    fields,
    target: resource(value.target),
    request: text(run.request, MAX_ITEM_CHARS),
    context: run.context || null,
    status: "pending",
  };
}

/**
 * Apply one stream event to *run* (`{ items, note, status, terminal, writes?, request?, context? }`). `terminal` is set
 * only by `done` or `error`: a stream that simply ends leaves it null, which `settleRun` reports as interrupted, never as an
 * answer. `writes` collects this run's write entries, so settlement knows what to reconcile, `proposal` the edit it
 * proposed, and `handoff` the reply it asks the chat to generate once it has settled. Returns the resources a `mutation` committed, for the caller to read again, and otherwise an empty list.
 */
export function applyStreamEvent(run, event, data, errorMessage) {
  const d = payload(data);
  let committed = [];
  run.writes ||= [];
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
    if (d.write === true) {
      item.write = true;
      item.target = resource(d.target);
      run.writes.push(item);
    }
    run.note = "";
    run.items.push(item);
  } else if (event === "mutation") {
    const item = run.writes.findLast((entry) => entry.callId === d.call_id);
    if (item) {
      item.committed = resources(d.resources);
      // Kept even if the result itself never arrives: what was committed outlives the run.
      item.ids ||= storedIds({ committed: item.committed });
      committed = item.committed;
    }
  } else if (event === "tool_result") {
    const item = run.items.findLast((entry) => entry.kind === "tool" && entry.running && entry.callId === d.call_id);
    if (item) {
      delete item.running;
      item.ok = d.ok === true;
      item.text = text(d.summary, MAX_ITEM_CHARS);
      const ids = storedIds(d.ids);
      if (ids && Object.keys(ids).length) item.ids = ids;
    }
  } else if (event === "confirm") {
    const item = proposalItem(d.tool, d.proposal, run);
    if (item) {
      run.items.push(item);
      run.proposal = item;
    }
  } else if (event === "handoff") {
    const target = handoffTarget(d);
    if (target) run.handoff = target;
  } else if (event === "done") {
    run.terminal = OUTCOMES.has(d.reason) || d.reason === "awaiting_apply" ? d.reason : "reply";
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
 * Close *run* once its stream is over: tools still waiting get no result, and a run without a verdict gets one. A write
 * still waiting is marked as committed when its `mutation` came, and as an unknown outcome when it did not.
 */
export function settleRun(run, { stopped = false, failure = "" } = {}) {
  for (const item of run.items) {
    if (item.kind !== "tool" || !item.running) continue;
    delete item.running;
    item.ok = !!item.committed?.length;
    item.text = unanswered(item);
    if (item.text === UNKNOWN_OUTCOME) item.unknown = true;
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
  run.status = "";
  trimTranscript(run.items);
}

/**
 * What *run* may have changed and the browser should read again: every resource its writes committed, the target of each
 * write whose outcome never arrived, and the message of every media job, which may have saved an artifact as it was stopped
 * or failed. Deduplicated, in the order they were written.
 */
export function affectedResources(run) {
  const seen = new Map();
  for (const item of run.writes || []) {
    const found = item.committed?.length
      ? item.committed
      : item.target && (item.text === UNKNOWN_OUTCOME || item.target.kind === "media")
        ? [item.target]
        : [];
    for (const entry of found) seen.set(JSON.stringify(entry), entry);
  }
  return [...seen.values()];
}

/**
 * The proposal state an Apply failure leaves, from the server's refusal; anything unexplained is unconfirmed. *previous* is
 * the card's state before this Apply: after an Apply that was never answered, "expired" or "stale" may only mean the first
 * one committed (the server lost its record, or now holds the approved text), so the outcome is unknown, never "not applied".
 */
export function applyRefusal(error, previous = "pending") {
  let detail = null;
  try {
    detail = JSON.parse(error?.body || "null")?.detail;
  } catch {}
  const code = detail && typeof detail === "object" ? detail.code : null;
  const message = typeof detail?.message === "string" ? detail.message.slice(0, MAX_NOTE_CHARS) : "";
  const retried = previous === "unconfirmed" || previous === "applying";
  if (retried && (error?.status === 410 || (error?.status === 409 && code === "stale")))
    return { status: "unknown", note: "" };
  if (error?.status === 409 && code === "busy") return { status: "pending", note: message };
  if (error?.status === 409 && code === "stale") return { status: "stale", note: "" };
  if (error?.status === 404) return { status: "missing", note: "" };
  if (error?.status === 410) return { status: "expired", note: "" };
  return { status: "unconfirmed", note: "" };
}

function clipped(value, { head, tail }) {
  if (value.length <= head + tail + 1) return esc(value);
  return `${head ? esc(value.slice(0, head)) : ""}<span class="asst-gap">…</span>${tail ? esc(value.slice(-tail)) : ""}`;
}

/**
 * One field's change as escaped markup: removed and added text marked, unchanged text around a change shortened to its
 * nearest context. The texts are the server's exact before and after.
 */
export function diffHtml(before, after) {
  const ops =
    before && after
      ? sentenceDiff(before, after)
      : [...(before ? [{ type: "delete", text: before }] : []), ...(after ? [{ type: "insert", text: after }] : [])];
  return ops
    .map((op, index) => {
      if (op.type === "insert") return `<ins class="asst-ins">${esc(op.text)}</ins>`;
      if (op.type === "delete") return `<del class="asst-del">${esc(op.text)}</del>`;
      const head = index === ops.length - 1 ? DIFF_CONTEXT : index === 0 ? 0 : DIFF_CONTEXT;
      const tail = index === 0 ? DIFF_CONTEXT : index === ops.length - 1 ? 0 : DIFF_CONTEXT;
      return clipped(op.text, { head, tail });
    })
    .join("");
}

function proposalHtml(item, index, { active, busy }) {
  const live = item === active && (item.status === "pending" || item.status === "unconfirmed");
  const fields = (item.fields || [])
    .map((field) =>
      field.before === undefined
        ? `<div class="asst-diff-label">${esc(field.label)}</div>`
        : `<div class="asst-diff-label">${esc(field.label)}</div>` +
          `<div class="asst-diff">${diffHtml(field.before, field.after)}</div>` +
          `<details class="asst-diff-full"><summary>Full new text</summary><div class="asst-diff-text">${esc(field.after)}</div></details>`,
    )
    .join("");
  const note = item.note || PROPOSAL_NOTES[item.status] || "";
  const attr = `data-proposal-id="${escAttr(item.proposalId || "")}"`;
  const disabled = busy ? " disabled" : "";
  let controls = "";
  if (live) {
    const apply = item.status === "unconfirmed" ? "Retry Apply" : "Apply";
    controls =
      `<button type="button" class="btn btn-accent btn-sm" data-wf-action="assistant:apply" ${attr}${disabled}>${apply}</button>` +
      `<button type="button" class="btn btn-sm" data-wf-action="assistant:cancel" ${attr}${disabled}>Cancel</button>`;
  } else if (ASK_AGAIN.has(item.status) && item.request) {
    controls = `<button type="button" class="btn btn-sm" data-wf-action="assistant:askAgain" data-index="${index}">Ask again</button>`;
  }
  return (
    `<div class="asst-proposal asst-proposal-${item.status}" role="group" aria-label="Proposed edit">` +
    `<div class="asst-proposal-head">Proposed edit to ${esc(item.label || "an item")}</div>${fields}` +
    (note ? `<div class="asst-proposal-note">${esc(note)}</div>` : "") +
    (controls ? `<div class="asst-proposal-actions">${controls}</div>` : "") +
    "</div>"
  );
}

const EMPTY =
  '<div class="asst-empty">Ask about your characters, chats, worlds, personas, documents or settings.</div>';

/**
 * The log's markup. User text, labels, tool lines, outcomes and proposed edits are escaped; only an assistant reply goes
 * through *renderProse*, inside `.msg-body`, where no element can name an action. Apply and Cancel are drawn only on
 * *active*, the one proposal this page holds, and are disabled while *busy*.
 */
export function transcriptHtml(items, renderProse, { active = null, busy = false } = {}) {
  if (!items.length) return EMPTY;
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
    } else if (item.kind === "proposal" && PROPOSAL_STATES.has(item.status)) {
      parts.push(proposalHtml(item, index, { active, busy }));
    } else if (item.kind === "outcome" && OUTCOMES.has(item.reason)) {
      parts.push(`<div class="asst-outcome asst-outcome-${item.reason}">${esc(item.text)}</div>`);
    }
  });
  return parts.join("");
}
