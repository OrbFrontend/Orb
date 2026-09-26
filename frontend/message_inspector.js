// A reply's Inspector sections -- moods, reasoning, decisions, feedback, state,
// tool calls, injection block and latency -- built from one "turn view" so the
// Inspector panel and the in-chat blocks above each reply's text render them
// alike. In the chat, reasoning gets its own block first; the rest share the
// Inspector block under it.
//
// The in-chat blocks read a per-conversation cache filled by one batched
// director-logs request per render, and the reply still streaming reads the
// live turn state. Open states are shared: toggling a section on one reply
// toggles it on every reply and in the panel, and the choice is saved. The
// Reasoning block is the exception: the panel's Reasoning section holds the
// next turn's controls, so the two open and close apart.
import { api } from "./api.js";
import { decisionOutcomes, decisionsHtml } from "./chat_decisions.js";
import { CHEVRON_RIGHT_ICON } from "./icons.js";
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

// `data-inspect-section` key → the `S` flag it reads and the saved
// `inspector_open_states` key it writes.
const OPEN_STATE_FIELDS = {
  inline: "inlineInspectorOpen",
  inline_reasoning: "inlineReasoningOpen",
  reasoning: "reasoningOpen",
  tool_calls: "toolCallsOpen",
  injection_block: "injectionBlockOpen",
  context_size: "contextSizeOpen",
  decisions: "decisionsOpen",
};

export function saveInspectorOpenStates() {
  const states = {};
  for (const [key, field] of Object.entries(OPEN_STATE_FIELDS)) states[key] = S[field];
  api.put("/settings", { inspector_open_states: states }).catch(() => {});
}

export function loadInspectorOpenStates(saved) {
  if (!saved || typeof saved !== "object") return;
  for (const [key, field] of Object.entries(OPEN_STATE_FIELDS)) {
    if (typeof saved[key] === "boolean") S[field] = saved[key];
  }
}

const openAttr = (key) => (S[OPEN_STATE_FIELDS[key]] ? " open" : "");

// Whoever owns the message list repaints it; this module sits below it.
let repaintMessages = () => {};
export function setInlineInspectorRepaint(fn) {
  repaintMessages = fn;
}

// `toggle` does not bubble, so sections are watched in the capture phase.
//
// Firefox fires `toggle` for a `<details open>` that arrives through innerHTML,
// and both the panel and the chat rebuild sections on repaint -- so the state
// is compared before it is written. Sections are rendered *from* these flags,
// which makes a repaint's event always a no-op and a click always a real change.
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

// Repaint the chat so every reply's copy of the section follows, holding the
// reply that was clicked still on screen while the ones around it resize.
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

/** A saved reply's view, from its `director-log` payload. */
export function turnViewFromLog(msgId, data) {
  const known = data?.mood_data_available !== false && Array.isArray(data?.active_moods);
  return {
    msgId,
    live: false,
    moods: { known, activeIds: known ? data.active_moods : [], resting: restingCooldowns(msgId) },
    reasoning: {
      director: data?.reasoning_director || "",
      writer: data?.reasoning_writer || "",
      editor: data?.reasoning_editor || "",
    },
    decisions: data?.decision_evaluations,
    storedDecisions: true,
    feedback: data?.feedback,
    state: data?.state,
    toolCalls: data?.tool_calls || [],
    injection: data?.injection_block || "",
    latency: data?.agent_latency_ms || 0,
  };
}

/** The streaming turn's view, from the SSE state gathered so far. */
export function liveTurnView() {
  const ld = S.lastDirectorData || {};
  const history = S.messages.slice(0, S.streamCutoffIndex ?? S.messages.length);
  const lastAssistant = history.findLast((message) => message.role === "assistant");
  const known = ld.mood_data_available !== false && Array.isArray(ld.active_moods);
  return {
    msgId: null,
    live: true,
    // The reply before this turn carries the cooldowns the Director read.
    moods: { known, activeIds: known ? ld.active_moods : [], resting: lastAssistant?.fragment_cooldowns || {} },
    reasoning: { director: S.reasoningDirector, writer: S.reasoningWriter, editor: S.reasoningEditor },
    decisions: S.lastDecisions,
    storedDecisions: false,
    feedback: S.lastFeedback?.values,
    state: S.lastState,
    toolCalls: ld.tool_calls || [],
    injection: ld.injection_block || "",
    latency: ld.agent_latency_ms || 0,
  };
}

// ── Sections ──

function moodTags({ known, activeIds, resting }) {
  if (!known) return [];
  const fragments = [...moodFragmentsView()];
  for (const id of activeIds) {
    if (!fragments.some((fragment) => fragment.id === id)) fragments.push({ id, label: id });
  }
  return fragments.map((fragment) => {
    const active = activeIds.includes(fragment.id);
    const rests = !active && Number(resting[fragment.id]) >= 1;
    return { label: fragment.label, active, className: active ? " active" : rests ? " resting" : "" };
  });
}

/**
 * The Moods section. The panel keeps its "None" placeholder (*showNone*); a
 * reply's block leaves the section out when there are no moods to show.
 */
export function moodsHtml(moods, { showNone = false } = {}) {
  const tags = moodTags(moods);
  if (!tags.length && !showNone) return "";
  const badges = tags.map((tag) => `<span class="style-tag${tag.className}">${esc(tag.label)}</span>`).join("");
  const none = moods.known ? '<span style="color:var(--text-muted);font-size:12px">None</span>' : "";
  return `<div class="inspector-block"><h4>Moods</h4>
    <div>${badges || none}</div>
  </div>`;
}

export function feedbackRows(values) {
  if (!values || typeof values !== "object") return [];
  const frags = interactiveFragmentsView();
  return Object.entries(values)
    .filter(([, v]) => v && (Array.isArray(v) ? v.length : true))
    .map(([id, v]) => {
      const frag = frags.find((f) => f.id === id);
      const label = frag?.injection_label || frag?.label || id;
      return { label, value: v };
    });
}

export function buildFeedbackHtml(values) {
  const rows = feedbackRows(values);
  if (!rows.length) return "";
  const body = rows
    .map(({ label, value }) => {
      const valHtml = Array.isArray(value)
        ? `<ul>${value.map((it) => `<li>${esc(String(it))}</li>`).join("")}</ul>`
        : esc(String(value));
      return `<div class="feedback-row">
        <span class="feedback-row-label">${esc(label)}</span>
        <div class="feedback-row-value">${valHtml}</div>
      </div>`;
    })
    .join("");
  return `<div class="inspector-block">
    <h4>Feedback</h4>
    <div class="feedback-card">${body}</div>
  </div>`;
}

const STATE_OP_LABELS = { add: "Added", revise: "Changed", retire: "Retired" };
// What an operation that did not apply tried to do.
const STATE_ATTEMPT_LABELS = { set: "Set", add: "Add", revise: "Change", retire: "Retire", clear: "Clear" };

function stateFragmentLabel(fragmentId, fallback) {
  if (fallback) return fallback;
  return interactiveFragmentsView().find((f) => f.id === fragmentId)?.label || fragmentId || "State";
}

// Changes the Agent did not make carry a badge naming who did.
const STATE_SOURCE_BADGES = { user: "You", carried: "Carried" };

function stateRowHtml(label, op, text, { source = "agent", detail = "" } = {}) {
  const who = STATE_SOURCE_BADGES[source];
  const badge = who ? ` <span class="state-badge">${who}</span>` : "";
  const body = text ? `: ${esc(String(text))}` : "";
  const why = detail ? `<div class="state-change-detail">${esc(detail)}</div>` : "";
  return `<div class="feedback-row${who ? " user-note" : ""}">
    <span class="feedback-row-label">${esc(label)}${badge}</span>
    <div class="feedback-row-value"><span class="state-change-op">${esc(op)}</span>${body}${why}</div>
  </div>`;
}

/**
 * One turn's state: the changes it made, the operations it refused, and the
 * carried corrections that could no longer apply. ``state`` is the ``state``
 * SSE payload or the director log's ``state``.
 */
export function buildStateHtml(state) {
  const changes = Array.isArray(state?.changes) ? state.changes : [];
  const rejected = Array.isArray(state?.rejected) ? state.rejected : [];
  const dropped = Array.isArray(state?.dropped) ? state.dropped : [];
  if (!changes.length && !rejected.length && !dropped.length) return "";
  const blocks = [];
  if (changes.length) {
    const rows = changes
      .map((c) =>
        stateRowHtml(stateFragmentLabel(c.fragment_id, c.fragment_label), STATE_OP_LABELS[c.op] || c.op, c.text, {
          source: c.source,
        }),
      )
      .join("");
    blocks.push(`<div class="feedback-card">${rows}</div>`);
  }
  if (rejected.length) {
    const rows = rejected
      .map((r) =>
        stateRowHtml(stateFragmentLabel(r.fragment_id), STATE_ATTEMPT_LABELS[r.op] || r.op || "Rejected", r.text, {
          detail: r.detail || r.reason,
        }),
      )
      .join("");
    blocks.push(`<h4>Rejected</h4><div class="feedback-card state-rejected">${rows}</div>`);
  }
  if (dropped.length) {
    const rows = dropped
      .map((d) =>
        stateRowHtml(
          stateFragmentLabel(d.fragment_id, d.fragment_label),
          d.op === "retire" ? "Retire" : "Change",
          d.text,
        ),
      )
      .join("");
    blocks.push(
      `<h4>Corrections not carried over</h4>
       <div class="state-note">They changed entries from the discarded reply.</div>
       <div class="feedback-card state-rejected">${rows}</div>`,
    );
  }
  return `<div class="inspector-block">
    <h4>State (this reply)</h4>
    ${blocks.join("")}
  </div>`;
}

function collapsibleHtml(key, title, body) {
  return `<details class="inspector-block" data-inspect-section="${key}"${openAttr(key)}>
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4>${title}</h4>
    </summary>
    ${body}
  </details>`;
}

export function toolCallsHtml(toolCalls) {
  if (!toolCalls?.length) return "";
  const text = toolCalls.map((c) => JSON.stringify(c)).join("\n\n");
  return collapsibleHtml("tool_calls", "Tool Calls", `<div class="injection-box">${esc(text)}</div>`);
}

export function injectionHtml(injection) {
  if (!injection) return "";
  return collapsibleHtml("injection_block", "Injection Block", `<div class="injection-box">${esc(injection)}</div>`);
}

export function latencyHtml(latency) {
  if (!latency) return "";
  return `<div class="inspector-block inspector-latency"><h4>Agent Latency</h4>
    <div style="font-size:12px;color:var(--text-secondary)">${latency}ms</div></div>`;
}

// ── The in-chat Reasoning block ──

// The pass a saved reply's block shows, by message id. Unset means its last pass.
const selectedPassByMsg = new Map();

// A saved reply lists the passes that wrote something. The streaming reply also
// lists the pass running now, so its box is on screen for the first delta.
function reasoningPassIndexes(view) {
  return REASONING_PASSES.flatMap((pass, i) => {
    if (view.reasoning[pass.key]) return [i];
    const running = view.live && S.isStreaming && i === S.reasoningPassActive && S.reasoningEnabled[pass.key] !== false;
    return running ? [i] : [];
  });
}

/** A reply's Reasoning block, or "" when no pass has reasoning to show. */
export function reasoningBlockHtml(view) {
  const shown = reasoningPassIndexes(view);
  if (!shown.length) return "";
  const wanted = view.live ? S.reasoningPassSelected : selectedPassByMsg.get(view.msgId);
  const selected = shown.includes(wanted) ? wanted : shown[shown.length - 1];
  const tabs =
    shown.length > 1
      ? `<div class="msg-inspect-passes">${shown
          .map(
            (i) =>
              `<button type="button" class="msg-inspect-pass${i === selected ? " active" : ""}" data-inspect-pass="${i}">${esc(REASONING_PASSES[i].label)}</button>`,
          )
          .join("")}</div>`
      : `<div class="msg-inspect-passes"><span class="msg-inspect-pass active">${esc(REASONING_PASSES[selected].label)}</span></div>`;
  // The streaming reply's box is the one reasoning deltas append to.
  const boxId = view.live ? ' id="reasoning-box"' : "";
  return `<details class="msg-inspect msg-reasoning" data-inspect-section="inline_reasoning"${openAttr("inline_reasoning")}>
    <summary class="msg-inspect-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="msg-inspect-title">Reasoning</span>
    </summary>
    <div class="msg-reasoning-body">
      ${tabs}<div class="reasoning-box"${boxId}>${esc(view.reasoning[REASONING_PASSES[selected].key])}</div>
    </div>
  </details>`;
}

// ── The in-chat block ──

function summaryChips(view) {
  const chips = moodTags(view.moods)
    .filter((tag) => tag.active)
    .map((tag) => `<span class="style-tag active">${esc(tag.label)}</span>`);
  for (const { label, outcome } of decisionOutcomes(view.decisions, { stored: view.storedDecisions })) {
    // The value alone: the user named the decision. Hovering says which one.
    chips.push(`<span class="msg-inspect-chip" title="${escAttr(`${label}: ${outcome}`)}">${esc(outcome)}</span>`);
  }
  return chips.join("");
}

/** A reply's Inspector block, or "" when none of its sections has anything to show. */
export function inspectorBlockHtml(view) {
  // The panel's order, except latency: it is short, so it shares the first row with the moods.
  const sections = [
    moodsHtml(view.moods),
    latencyHtml(view.latency),
    decisionsHtml(view.decisions, { stored: view.storedDecisions }),
    buildFeedbackHtml(view.feedback),
    buildStateHtml(view.state),
    toolCallsHtml(view.toolCalls),
    injectionHtml(view.injection),
  ].join("");
  if (!sections.trim()) return "";
  return `<details class="msg-inspect" data-inspect-section="inline"${openAttr("inline")}>
    <summary class="msg-inspect-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="msg-inspect-title">Inspector</span>
      <span class="msg-inspect-chips">${summaryChips(view)}</span>
    </summary>
    <div class="msg-inspect-body">${sections}</div>
  </details>`;
}

// ── Cache of saved replies' director logs ──

// Keyed by message id for one conversation. `null` marks a reply the server
// had no data for, so it is not asked for again.
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

/** Store a reply's `director-log` payload, fetched by the Inspector or the batch read. */
export function rememberInspection(msgId, data) {
  syncCacheConversation();
  inspections.set(msgId, data ?? null);
}

// A cached reply's turn view, or null when the setting is off or there is no log.
function cachedTurnView(m) {
  if (!S.inspectorInline || m.role !== "assistant" || !m.id) return null;
  syncCacheConversation();
  const data = inspections.get(m.id);
  return data ? turnViewFromLog(m.id, data) : null;
}

/** The in-chat Inspector block for message *m*, or "" when the setting is off or there is nothing to show. */
export function inlineInspectorHtml(m) {
  const view = cachedTurnView(m);
  return view ? inspectorBlockHtml(view) : "";
}

/** The in-chat Reasoning block for message *m*, or "" when the setting is off or there is none. */
export function inlineReasoningHtml(m) {
  const view = cachedTurnView(m);
  return view ? reasoningBlockHtml(view) : "";
}

/** Fetch the logs of the rendered replies not cached yet, then repaint the ones that have a block. */
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
          const view = data ? turnViewFromLog(id, data) : null;
          if (view && (inspectorBlockHtml(view) || reasoningBlockHtml(view))) visible = true;
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
 * Show a freshly stored reply's blocks. A bubble baked from the stream is not
 * part of the list's reconciled rows yet, so its live slots are replaced in
 * place rather than repainting the list under it.
 */
export function refreshInlineInspector(msgId) {
  if (!S.inspectorInline) return;
  const row = document.querySelector(`#chat-messages .message[data-msg-id="${msgId}"]`);
  const baked = row && !row.dataset.rkey;
  const inspectSlot = baked ? row.querySelector(".msg-inspect-baked") : null;
  const reasoningSlot = baked ? row.querySelector(".msg-reasoning-baked") : null;
  if (!inspectSlot || !reasoningSlot) {
    repaintMessages();
    return;
  }
  const message = S.messages.find((m) => m.id === msgId);
  inspectSlot.innerHTML = message ? inlineInspectorHtml(message) : "";
  reasoningSlot.innerHTML = message ? inlineReasoningHtml(message) : "";
}

// ── The streaming reply's blocks ──

/**
 * Repaint the streaming bubble's blocks from the live turn state. Returns
 * whether the Reasoning block rendered, so its box holds the full text.
 */
export function renderLiveInspector() {
  const bubble = S.streamingBodyEl?.closest(".message");
  const inspectSlot = bubble?.querySelector(".msg-inspect-live");
  const reasoningSlot = bubble?.querySelector(".msg-reasoning-live");
  const view = S.inspectorInline && (inspectSlot || reasoningSlot) ? liveTurnView() : null;
  const reasoning = view ? reasoningBlockHtml(view) : "";
  preserveScroll(
    () => document.getElementById("reasoning-box"),
    REASONING_BOTTOM_THRESHOLD,
    () => {
      if (inspectSlot) inspectSlot.innerHTML = view ? inspectorBlockHtml(view) : "";
      if (reasoningSlot) reasoningSlot.innerHTML = reasoning;
    },
  );
  return reasoningSlot != null && reasoning !== "";
}

// A saved reply's pass tabs switch only its own box. The streaming reply's tabs
// follow the panel's pass selection, which the Inspector module handles.
document.addEventListener("click", (e) => {
  const tab = e.target.closest?.("button[data-inspect-pass]");
  if (!tab || tab.closest(".msg-reasoning-live")) return;
  const row = tab.closest(".message[data-msg-id]");
  const msgId = Number(row?.dataset.msgId);
  const data = inspections.get(msgId);
  if (!data) return;
  selectedPassByMsg.set(msgId, Number(tab.dataset.inspectPass));
  const section = tab.closest('[data-inspect-section="inline_reasoning"]');
  if (section) section.outerHTML = reasoningBlockHtml(turnViewFromLog(msgId, data));
});
