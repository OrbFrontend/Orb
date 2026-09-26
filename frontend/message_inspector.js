// A reply's Inspector sections, built from one "turn view" so the Inspector panel
// and the in-chat blocks above each reply render them alike. Saved replies read a
// per-conversation cache of director logs; the streaming reply reads the live
// turn state. Open states are shared by every reply and the panel, except the
// chat's Reasoning block, which opens apart from the panel's next-turn controls.
import { api } from "./api.js";
import { decisionOutcomes, decisionsHtml } from "./chat_decisions.js";
import { CHEVRON_RIGHT_ICON } from "./icons.js";
import { sectionHtml } from "./inspector_section.js";
import { preserveScroll } from "./scroll_follow.js";
import { interactiveFragmentsView, moodFragmentsView, restingCooldowns, S } from "./state.js";
import { convUrl, esc, escAttr } from "./utils.js";

export const REASONING_PASSES = [
  { key: "director", label: "Director", color: "var(--accent-dim)" },
  { key: "writer", label: "Writer", color: "var(--accent-dim)" },
  { key: "editor", label: "Editor", color: "var(--accent-dim)" },
];

export const REASONING_BOTTOM_THRESHOLD = 20;

// ── Open states ──

// `data-inspect-section` key (also the saved `inspector_open_states` key) → its `S` flag.
const OPEN_STATE_FIELDS = {
  inline: "inlineInspectorOpen",
  inline_reasoning: "inlineReasoningOpen",
  reasoning: "reasoningOpen",
  tool_calls: "toolCallsOpen",
  injection_block: "injectionBlockOpen",
  context_size: "contextSizeOpen",
  decisions: "decisionsOpen",
  feedback: "feedbackOpen",
  state_changes: "stateChangesOpen",
};

function saveInspectorOpenStates() {
  const states = {};
  for (const [key, field] of Object.entries(OPEN_STATE_FIELDS)) states[key] = S[field];
  api.put("/settings", { inspector_open_states: states }).catch(() => {});
}

export function loadInspectorOpenStates(saved) {
  for (const [key, field] of Object.entries(OPEN_STATE_FIELDS)) {
    if (typeof saved?.[key] === "boolean") S[field] = saved[key];
  }
}

const isOpen = (key) => Boolean(S[OPEN_STATE_FIELDS[key]]);
const openAttr = (key) => (isOpen(key) ? " open" : "");

// Whoever owns the message list repaints it; this module sits below it.
let repaintMessages = () => {};
export function setInlineInspectorRepaint(fn) {
  repaintMessages = fn;
}

// `toggle` does not bubble, so it is caught in the capture phase. Firefox also
// fires it for a `<details open>` set through innerHTML; sections render from
// these flags, so comparing first makes a repaint's event a no-op.
document.addEventListener(
  "toggle",
  (e) => {
    const el = e.target;
    const field = OPEN_STATE_FIELDS[el?.dataset?.inspectSection];
    if (!field || S[field] === el.open) return;
    S[field] = el.open;
    saveInspectorOpenStates();
    syncChatSections(el, el.dataset.inspectSection);
  },
  true,
);

// Repaint every reply's copy of the section, holding the clicked reply still.
function syncChatSections(toggled, key) {
  const chat = document.getElementById("chat-messages");
  if (!chat?.querySelector(`[data-inspect-section="${key}"]`)) return;
  const anchor = chat.contains(toggled) ? toggled.closest(".message[data-msg-id]") : null;
  const anchorId = anchor?.dataset.msgId;
  const before = anchor?.getBoundingClientRect().top;
  repaintMessages();
  renderLiveInspector();
  if (anchorId == null) return;
  const now = chat.querySelector(`.message[data-msg-id="${CSS.escape(anchorId)}"]`);
  if (now) chat.scrollTop += now.getBoundingClientRect().top - before;
}

// ── Turn views ──

/** Moods of a `director-log` payload or the streaming turn's director data. */
export function moodsOf(data, resting) {
  const known = data?.mood_data_available !== false && Array.isArray(data?.active_moods);
  return { known, activeIds: known ? data.active_moods : [], resting };
}

// *msgId* is null for the streaming reply.
function turnView(msgId, data, resting, rest) {
  return {
    msgId,
    live: msgId == null,
    moods: moodsOf(data, resting),
    toolCalls: data?.tool_calls || [],
    injection: data?.injection_block || "",
    latency: data?.agent_latency_ms || 0,
    ...rest,
  };
}

/** A saved reply's view, from its `director-log` payload. */
export function turnViewFromLog(msgId, data) {
  return turnView(msgId, data, restingCooldowns(msgId), {
    reasoning: Object.fromEntries(REASONING_PASSES.map(({ key }) => [key, data?.[`reasoning_${key}`] || ""])),
    decisions: data?.decision_evaluations,
    feedback: data?.feedback,
    state: data?.state,
  });
}

function liveTurnView() {
  // The reply before this turn carries the cooldowns the Director read.
  const history = S.messages.slice(0, S.streamCutoffIndex ?? S.messages.length);
  const resting = history.findLast((message) => message.role === "assistant")?.fragment_cooldowns || {};
  return turnView(null, S.lastDirectorData, resting, {
    reasoning: { director: S.reasoningDirector, writer: S.reasoningWriter, editor: S.reasoningEditor },
    decisions: S.lastDecisions,
    feedback: S.lastFeedback?.values,
    state: S.lastState,
  });
}

// ── Sections ──

function moodTags({ known, activeIds, resting }) {
  if (!known) return [];
  const fragments = [...moodFragmentsView()];
  for (const id of activeIds) if (!fragments.some((fragment) => fragment.id === id)) fragments.push({ id, label: id });
  return fragments.map(({ id, label }) => {
    const active = activeIds.includes(id);
    return { label, active, className: active ? " active" : Number(resting[id]) >= 1 ? " resting" : "" };
  });
}

/** The Moods section; only the panel (*showNone*) shows it with no moods. */
export function moodsHtml(moods, { showNone = false } = {}) {
  const tags = moodTags(moods);
  if (!tags.length && !showNone) return "";
  const badges = tags.map((tag) => `<span class="inspect-chip${tag.className}">${esc(tag.label)}</span>`).join("");
  const none = moods.known ? '<span class="inspect-empty">None</span>' : "";
  return sectionHtml({ title: "Moods", body: badges ? `<div class="inspect-chips">${badges}</div>` : none });
}

export function buildFeedbackHtml(values) {
  const frags = interactiveFragmentsView();
  const rows = Object.entries(values && typeof values === "object" ? values : {})
    .filter(([, v]) => v && (!Array.isArray(v) || v.length))
    .map(([id, value]) => {
      const frag = frags.find((f) => f.id === id);
      const valHtml = Array.isArray(value)
        ? `<ul>${value.map((it) => `<li>${esc(String(it))}</li>`).join("")}</ul>`
        : esc(String(value));
      return `<div class="feedback-row">
        <span class="feedback-row-label">${esc(frag?.injection_label || frag?.label || id)}</span>
        <div class="feedback-row-value">${valHtml}</div>
      </div>`;
    });
  if (!rows.length) return "";
  return sectionHtml({ key: "feedback", open: isOpen("feedback"), title: "Feedback", body: rows.join("") });
}

const STATE_OP_LABELS = { add: "Added", revise: "Changed", retire: "Retired" };
// What an operation that did not apply tried to do.
const STATE_ATTEMPT_LABELS = { set: "Set", add: "Add", revise: "Change", retire: "Retire", clear: "Clear" };
// Changes the Agent did not make carry a badge naming who did.
const STATE_SOURCE_BADGES = { user: "You", carried: "Carried" };

function stateFragmentLabel(fragmentId, fallback) {
  return fallback || interactiveFragmentsView().find((f) => f.id === fragmentId)?.label || fragmentId || "State";
}

function stateRowHtml(label, op, text, { source = "agent", detail = "" } = {}) {
  const who = STATE_SOURCE_BADGES[source];
  const badge = who ? ` <span class="inspect-chip pill">${who}</span>` : "";
  const body = text ? `: ${esc(String(text))}` : "";
  const why = detail ? `<div class="state-change-detail">${esc(detail)}</div>` : "";
  return `<div class="feedback-row${who ? " user-note" : ""}">
    <span class="feedback-row-label">${esc(label)}${badge}</span>
    <div class="feedback-row-value"><span class="state-change-op">${esc(op)}</span>${body}${why}</div>
  </div>`;
}

/**
 * One turn's state: the changes it made, the operations it refused, and the
 * carried corrections that could no longer apply. *state* is the `state` SSE
 * payload or the director log's `state`.
 */
export function buildStateHtml(state) {
  const list = (key) => (Array.isArray(state?.[key]) ? state[key] : []);
  const rows = (items, row) => items.map(row).join("");
  const blocks = [];
  const changes = list("changes");
  if (changes.length) {
    const row = (c) =>
      stateRowHtml(stateFragmentLabel(c.fragment_id, c.fragment_label), STATE_OP_LABELS[c.op] || c.op, c.text, {
        source: c.source,
      });
    blocks.push(rows(changes, row));
  }
  const rejected = list("rejected");
  if (rejected.length) {
    const row = (r) =>
      stateRowHtml(stateFragmentLabel(r.fragment_id), STATE_ATTEMPT_LABELS[r.op] || r.op || "Rejected", r.text, {
        detail: r.detail || r.reason,
      });
    blocks.push(`<div class="inspect-subhead">Rejected</div><div class="state-rejected">${rows(rejected, row)}</div>`);
  }
  const dropped = list("dropped");
  if (dropped.length) {
    const row = (d) =>
      stateRowHtml(
        stateFragmentLabel(d.fragment_id, d.fragment_label),
        d.op === "retire" ? "Retire" : "Change",
        d.text,
      );
    blocks.push(`<div class="inspect-subhead">Corrections not carried over</div>
       <div class="state-note">They changed entries from the discarded reply.</div>
       <div class="state-rejected">${rows(dropped, row)}</div>`);
  }
  return blocks.length
    ? sectionHtml({
        key: "state_changes",
        open: isOpen("state_changes"),
        title: "State (this reply)",
        body: blocks.join(""),
      })
    : "";
}

// Raw text the turn sent or received, shown as sent.
function rawSectionHtml(key, title, text, meta = "") {
  return sectionHtml({ key, open: isOpen(key), title, meta, body: `<div class="inspect-raw">${esc(text)}</div>` });
}

export function toolCallsHtml(toolCalls) {
  if (!toolCalls?.length) return "";
  const text = toolCalls.map((c) => JSON.stringify(c)).join("\n\n");
  return rawSectionHtml("tool_calls", "Tool Calls", text, String(toolCalls.length));
}

export function injectionHtml(injection) {
  return injection ? rawSectionHtml("injection_block", "Injection Block", injection) : "";
}

export function latencyHtml(latency) {
  if (!latency) return "";
  return sectionHtml({
    title: "Agent Latency",
    meta: `${latency.toLocaleString()} ms`,
    className: "inspector-latency",
  });
}

// ── The in-chat blocks ──

// The pass a saved reply's Reasoning block shows, by message id. Unset means its last pass.
const selectedPassByMsg = new Map();

/** A reply's Reasoning block, or "" when no pass has reasoning to show. */
export function reasoningBlockHtml(view) {
  // The streaming reply also lists the running pass, so its box is there for the first delta.
  const shown = REASONING_PASSES.flatMap(({ key }, i) =>
    view.reasoning[key] ||
    (view.live && S.isStreaming && i === S.reasoningPassActive && S.reasoningEnabled[key] !== false)
      ? [i]
      : [],
  );
  if (!shown.length) return "";
  const wanted = view.live ? S.reasoningPassSelected : selectedPassByMsg.get(view.msgId);
  const selected = shown.includes(wanted) ? wanted : shown[shown.length - 1];
  const tabs = shown.map((i) => {
    const label = esc(REASONING_PASSES[i].label);
    return shown.length > 1
      ? `<button type="button" class="inspect-chip${i === selected ? " active" : ""}" data-inspect-pass="${i}">${label}</button>`
      : `<span class="inspect-chip active">${label}</span>`;
  });
  // The streaming reply's box is the one reasoning deltas append to.
  const boxId = view.live ? ' id="reasoning-box"' : "";
  return `<details class="msg-inspect msg-reasoning" data-inspect-section="inline_reasoning"${openAttr("inline_reasoning")}>
    <summary class="msg-inspect-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="msg-inspect-title">Reasoning</span>
    </summary>
    <div class="msg-reasoning-body">
      <div class="inspect-chips">${tabs.join("")}</div>
      <div class="inspect-raw"${boxId}>${esc(view.reasoning[REASONING_PASSES[selected].key])}</div>
    </div>
  </details>`;
}

/** A reply's Inspector block, or "" when none of its sections has anything to show. */
export function inspectorBlockHtml(view) {
  const stored = !view.live;
  // The panel's order, except latency: it is short, so it shares the first row with the moods.
  const sections = [
    moodsHtml(view.moods),
    latencyHtml(view.latency),
    decisionsHtml(view.decisions, { stored }),
    buildFeedbackHtml(view.feedback),
    buildStateHtml(view.state),
    toolCallsHtml(view.toolCalls),
    injectionHtml(view.injection),
  ].join("");
  if (!sections) return "";
  // The collapsed line shows active moods and decision values; hovering a value names its decision.
  const chips = [
    ...moodTags(view.moods)
      .filter((tag) => tag.active)
      .map((tag) => `<span class="inspect-chip active">${esc(tag.label)}</span>`),
    ...decisionOutcomes(view.decisions, { stored }).map(
      ({ label, outcome }) =>
        `<span class="inspect-chip" title="${escAttr(`${label}: ${outcome}`)}">${esc(outcome)}</span>`,
    ),
  ];
  return `<details class="msg-inspect" data-inspect-section="inline"${openAttr("inline")}>
    <summary class="msg-inspect-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="msg-inspect-title">Inspector</span>
      <span class="msg-inspect-chips">${chips.join("")}</span>
    </summary>
    <div class="msg-inspect-body">${sections}</div>
  </details>`;
}

const blocksHtml = (view) => reasoningBlockHtml(view) + inspectorBlockHtml(view);

// ── Cache of saved replies' director logs ──

// By message id, for one conversation. `null` marks a reply the server had no data for.
const inspections = new Map();
const inflight = new Set();
let cacheConvId = null;
const MAX_BATCH = 100; // the endpoint's own cap

function syncCacheConversation() {
  if (cacheConvId === S.activeConvId) return;
  inspections.clear();
  inflight.clear();
  selectedPassByMsg.clear();
  cacheConvId = S.activeConvId;
}

/** Store a reply's `director-log` payload. */
export function rememberInspection(msgId, data) {
  syncCacheConversation();
  inspections.set(msgId, data ?? null);
}

/** Message *m*'s in-chat blocks, or "" when the setting is off or there is nothing to show. */
export function inlineInspectorHtml(m) {
  if (!S.inspectorInline || m.role !== "assistant" || !m.id) return "";
  syncCacheConversation();
  const data = inspections.get(m.id);
  return data ? blocksHtml(turnViewFromLog(m.id, data)) : "";
}

/** Fetch the logs of the rendered replies not cached yet, then repaint if any has a block. */
export function ensureInspections(msgs) {
  if (!S.inspectorInline || !S.activeConvId || !msgs?.length) return;
  syncCacheConversation();
  const ids = msgs
    .filter((m) => m.role === "assistant" && m.id && !inspections.has(m.id) && !inflight.has(m.id))
    .map((m) => m.id);
  const convId = S.activeConvId;
  for (let start = 0; start < ids.length; start += MAX_BATCH) {
    const batch = ids.slice(start, start + MAX_BATCH);
    for (const id of batch) inflight.add(id);
    api
      .get(`${convUrl(convId, "director-logs")}?ids=${batch.join(",")}`)
      .then((logs) => {
        if (cacheConvId !== convId) return;
        let visible = false;
        for (const id of batch) {
          inflight.delete(id);
          const data = logs?.[id] ?? null;
          inspections.set(id, data);
          visible ||= Boolean(data && blocksHtml(turnViewFromLog(id, data)));
        }
        if (visible) repaintMessages();
      })
      .catch(() => {
        // Left uncached, so the next repaint asks again.
        if (cacheConvId === convId) for (const id of batch) inflight.delete(id);
      });
  }
}

/**
 * Show a freshly stored reply's blocks. A bubble baked from the stream is not a
 * reconciled row yet, so its slot is filled in place rather than repainting.
 */
export function refreshInlineInspector(msgId) {
  if (!S.inspectorInline) return;
  const row = document.querySelector(`#chat-messages .message[data-msg-id="${msgId}"]`);
  const slot = row && !row.dataset.rkey ? row.querySelector(".msg-inspect-baked") : null;
  if (!slot) return repaintMessages();
  const message = S.messages.find((m) => m.id === msgId);
  slot.innerHTML = message ? inlineInspectorHtml(message) : "";
}

/** Repaint the streaming bubble's blocks. Returns whether its Reasoning block rendered. */
export function renderLiveInspector() {
  const slot = S.streamingBodyEl?.closest(".message")?.querySelector(".msg-inspect-live");
  if (!slot) return false;
  const view = S.inspectorInline ? liveTurnView() : null;
  const reasoning = view ? reasoningBlockHtml(view) : "";
  preserveScroll(
    () => document.getElementById("reasoning-box"),
    REASONING_BOTTOM_THRESHOLD,
    () => {
      slot.innerHTML = view ? reasoning + inspectorBlockHtml(view) : "";
    },
  );
  return reasoning !== "";
}

// A saved reply's pass tabs switch only its own box; the streaming reply's tabs
// follow the panel's pass selection (chat_inspector.js).
document.addEventListener("click", (e) => {
  const tab = e.target.closest?.("button[data-inspect-pass]");
  if (!tab || tab.closest(".msg-inspect-live")) return;
  const msgId = Number(tab.closest(".message[data-msg-id]")?.dataset.msgId);
  const data = inspections.get(msgId);
  if (!data) return;
  selectedPassByMsg.set(msgId, Number(tab.dataset.inspectPass));
  const section = tab.closest('[data-inspect-section="inline_reasoning"]');
  if (section) section.outerHTML = reasoningBlockHtml(turnViewFromLog(msgId, data));
});
