import { api } from "./api.js";
import { confirmDelete } from "./modal.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { interactiveFragmentsView, S } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import { $, convUrl, esc, escAttr, toast } from "./utils.js";

const PANEL_ID = "state-panel";
const BUTTON_ID = "state-panel-btn";
const BUTTON_IDS = [BUTTON_ID, "mobile-state-btn"];

const SOURCE_LABELS = { agent: "Agent", user: "You", carried: "Carried" };
const UPDATE_LABELS = {
  after_reply: "updated after the reply",
  before_writer: "updated before the Writer",
  manual: "updated by hand only",
};
const INJECT_LABELS = { off: "not injected", director: "to the Director", writer: "to the Writer", both: "to both" };
const HISTORY_VERBS = { add: "added", revise: "revised", retire: "retired" };

// The last GET /state reply, for the conversation it was read for.
let panel = null;
let panelConvId = null;
let loadSeq = 0;
// The one open inline editor: { fragmentId, op: "set" | "add" | "revise", entryId, text }.
// ``text`` is the live draft, so a re-render (a finished turn, a history load)
// keeps what the user typed.
let editing = null;
// Set when an editor opens, so only that render takes focus.
let focusEditor = false;
// A manual write in flight; further clicks wait for it.
let writing = false;
// Fragments whose history is expanded, and their loaded history. ``historyGen``
// retires history reads that started before the cache was cleared.
const historyOpen = new Set();
const historyCache = new Map();
let historyGen = 0;

export function toggleStatePanel() {
  if (isUtilityPanelOpen(PANEL_ID)) closeUtilityPanel(PANEL_ID, BUTTON_ID);
  else openUtilityPanel(PANEL_ID, BUTTON_ID, renderStatePanel);
}

document.addEventListener("state-panel-request", toggleStatePanel);

function hasEnabledStateFragment() {
  return interactiveFragmentsView().some((f) => f.field_type === "state" && f.enabled !== 0 && f.enabled !== false);
}

/**
 * Show the State button whenever an enabled state fragment exists or the
 * conversation holds saved state, and close the panel when neither holds.
 */
export function updateStateButton() {
  const hasState = panelConvId === S.activeConvId && Boolean(panel?.has_state);
  const on = Boolean(S.activeConvId) && (hasEnabledStateFragment() || hasState);
  for (const id of BUTTON_IDS) {
    const el = $(id);
    if (el) el.classList.toggle("hidden", !on);
  }
  if (!on && isUtilityPanelOpen(PANEL_ID)) closeUtilityPanel(PANEL_ID, BUTTON_ID);
}

function resetForConversation(cid) {
  if (panelConvId === cid) return;
  panel = null;
  panelConvId = cid;
  editing = null;
  historyOpen.clear();
  clearHistoryCache();
}

function clearHistoryCache() {
  historyCache.clear();
  historyGen++;
}

/** Close the editor when the refreshed state no longer offers its target. */
function dropStaleEditor() {
  if (!editing) return;
  const f = panel?.fragments.find((x) => x.fragment_id === editing.fragmentId);
  const { op, entryId } = editing;
  const offered =
    op === "revise" ? f?.entries.some((e) => e.entry_id === entryId) : f?.mode === (op === "add" ? "entries" : "value");
  if (!offered || f.read_only) editing = null;
}

/**
 * Re-read the active branch's state: after a turn, a branch switch, a message
 * deletion, or a conversation switch. Updates the button and, when the panel is
 * open, the panel.
 */
export async function refreshState() {
  const cid = S.activeConvId;
  const seq = ++loadSeq;
  resetForConversation(cid);
  if (!cid) {
    updateStateButton();
    if (isUtilityPanelOpen(PANEL_ID)) render();
    return;
  }
  let data;
  try {
    data = await api.get(convUrl(cid, "state"));
  } catch (e) {
    if (seq !== loadSeq) return;
    if (isUtilityPanelOpen(PANEL_ID)) renderMessage(e.message);
    return;
  }
  if (seq !== loadSeq) return;
  panel = data;
  dropStaleEditor();
  clearHistoryCache();
  updateStateButton();
  if (isUtilityPanelOpen(PANEL_ID)) {
    render();
    await Promise.all([...historyOpen].map(loadHistory));
  }
}

export async function renderStatePanel() {
  if (panel && panelConvId === S.activeConvId) render();
  else renderMessage("Loading…");
  await refreshState();
}

function renderMessage(text) {
  const el = $("state-panel-content");
  if (el) el.innerHTML = `<div class="state-empty">${esc(text)}</div>`;
}

function render() {
  const el = $("state-panel-content");
  if (!el) return;
  if (!S.activeConvId) {
    renderMessage("No conversation selected.");
    return;
  }
  if (!panel) return;
  const intro = panel.updates_on
    ? ""
    : `<div class="state-note">State updates are off. Saved state is still injected, and you can edit it here.</div>`;
  if (!panel.fragments.length) {
    el.innerHTML = `${intro}<div class="state-empty">No state fragments. Add an interactive fragment of type State to keep a value or a list across turns.</div>`;
    return;
  }
  el.innerHTML = intro + panel.fragments.map(fragmentHtml).join("");
  const input = focusEditor && el.querySelector(".state-editor-input");
  focusEditor = false;
  if (input) {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function openEditor(fragmentId, op, entryId, text) {
  editing = { fragmentId, op, entryId, text };
  focusEditor = true;
  render();
}

function button(action, label, { entryId = "", danger = false, disabled = false, title = "" } = {}) {
  const cls = `btn btn-sm${danger ? " btn-danger" : ""}`;
  const entry = entryId ? ` data-entry-id="${escAttr(entryId)}"` : "";
  const tip = title ? ` title="${escAttr(title)}"` : "";
  return `<button type="button" class="${cls}" data-state-action="${action}"${entry}${tip}${disabled ? " disabled" : ""}>${label}</button>`;
}

function badge(label, tip, cls = "") {
  return ` <span class="state-badge${cls}" title="${escAttr(tip)}">${label}</span>`;
}

function badgesHtml(f) {
  let html = "";
  if (!f.configured) html += badge("Deleted", "The fragment was deleted; its saved state is read-only.");
  else if (!f.enabled) html += badge("Disabled", "The fragment is disabled: not updated, not injected.");
  if (f.origin === "card") html += badge("Card", "Embedded in the character card.");
  if (f.full)
    html += badge("Full", "The list holds the most entries it can. Retire one before adding.", " state-badge-warn");
  return html;
}

function describe(f) {
  if (!f.configured) return "";
  const mode = f.mode === "entries" ? `List, ${f.entries.length} of ${panel.limits.entries}` : "One value";
  return [mode, UPDATE_LABELS[f.update] || f.update, INJECT_LABELS[f.inject] || f.inject].join(" · ");
}

function entryMeta(entry) {
  const parts = [];
  if (entry.turn_index != null) parts.push(`Turn ${entry.turn_index}`);
  parts.push(SOURCE_LABELS[entry.source] || entry.source);
  return parts.join(" · ");
}

function isEditing(f, op, entryId = "") {
  return editing?.fragmentId === f.fragment_id && editing.op === op && (editing.entryId || "") === entryId;
}

function editorHtml() {
  const { text } = editing;
  const limit = panel.limits.text;
  return `<div class="state-editor">
    <textarea class="state-editor-input" rows="3" maxlength="${limit}">${esc(text)}</textarea>
    <div class="state-editor-foot">
      <span class="state-editor-count">${text.length}/${limit}</span>
      ${button("cancel", "Cancel")}
      <button type="button" class="btn btn-sm btn-accent" data-state-action="save">Save</button>
    </div>
  </div>`;
}

function entryHtml(f, entry, actions) {
  if (isEditing(f, "revise", entry.entry_id) || (isEditing(f, "set") && actions === "value")) {
    return `<div class="state-entry">${editorHtml()}</div>`;
  }
  let buttons = "";
  if (!f.read_only) {
    buttons =
      actions === "value"
        ? button("edit-value", "Edit") + button("clear", "Clear", { danger: true })
        : button("revise", "Edit", { entryId: entry.entry_id }) +
          button("retire", "Retire", { entryId: entry.entry_id, danger: true });
  }
  return `<div class="state-entry${entry.source === "user" ? " user-entry" : ""}">
    <div class="state-entry-text">${esc(entry.text)}</div>
    <div class="state-entry-foot">
      <span class="state-entry-meta">${esc(entryMeta(entry))}</span>
      ${buttons ? `<span class="state-entry-actions">${buttons}</span>` : ""}
    </div>
  </div>`;
}

function bodyHtml(f) {
  const entries = f.entries;
  if (f.mode === "value" && entries.length === 1) return entryHtml(f, entries[0], "value");
  const rows = entries.map((entry) => entryHtml(f, entry, "entries")).join("");
  const parts = [];
  if (f.several_values) {
    parts.push(
      `<div class="state-note">Several values are active. The next update replaces them with one value; edit or retire them to merge them first.</div>`,
    );
  }
  if (!entries.length)
    parts.push(`<div class="state-empty">${f.mode === "value" ? "No value yet." : "No entries yet."}</div>`);
  parts.push(rows);
  if (f.read_only) return parts.join("");
  if (f.mode === "value") {
    if (isEditing(f, "set")) parts.push(editorHtml());
    else {
      const tip = entries.length > 1 ? "Replace them all with one value" : "";
      parts.push(
        `<div class="state-frag-actions">${button("set", "Set value", { title: tip })}${entries.length > 1 ? button("clear", "Clear", { danger: true }) : ""}</div>`,
      );
    }
  } else if (isEditing(f, "add")) {
    parts.push(editorHtml());
  } else {
    const tip = f.full ? "The list is full: retire an entry first" : "";
    parts.push(`<div class="state-frag-actions">${button("add", "Add entry", { disabled: f.full, title: tip })}</div>`);
  }
  return parts.join("");
}

function historyHtml(f) {
  if (!historyOpen.has(f.fragment_id)) return "";
  const events = historyCache.get(f.fragment_id);
  if (!events) return `<div class="state-history"><div class="state-empty">Loading…</div></div>`;
  if (!events.length)
    return `<div class="state-history"><div class="state-empty">No changes on this branch.</div></div>`;
  const rows = events
    .map((e) => {
      const who = SOURCE_LABELS[e.source] || e.source;
      const turn = e.turn_index != null ? `Turn ${e.turn_index} · ` : "";
      const text = e.text ? `<div class="state-history-text">${esc(e.text)}</div>` : "";
      return `<li class="state-history-row"><span class="state-history-meta">${esc(`${turn}${who} ${HISTORY_VERBS[e.op] || e.op}`)}</span>${text}</li>`;
    })
    .join("");
  return `<ol class="state-history">${rows}</ol>`;
}

function fragmentHtml(f) {
  const cls = `state-frag${f.read_only ? " read-only" : ""}${f.full ? " full" : ""}`;
  const history = historyOpen.has(f.fragment_id) ? "Hide history" : "History";
  const desc = describe(f);
  const deletion = f.configured
    ? ""
    : `<div class="state-note">This fragment was deleted. Its saved state is read-only.</div>
       <div class="state-frag-actions">${button("delete-orphan", "Delete saved state", { danger: true })}</div>`;
  return `<section class="${cls}" data-fragment-id="${escAttr(f.fragment_id)}">
    <div class="state-frag-head">
      <span class="state-frag-label">${esc(f.label || f.fragment_id)}${badgesHtml(f)}</span>
      ${button("history", history)}
    </div>
    ${desc ? `<div class="state-frag-desc">${esc(desc)}</div>` : ""}
    ${bodyHtml(f)}
    ${deletion}
    ${historyHtml(f)}
  </section>`;
}

async function loadHistory(fragmentId) {
  const cid = S.activeConvId;
  const gen = historyGen;
  try {
    const events = await api.get(`${convUrl(cid, "state", "history")}?fragment_id=${encodeURIComponent(fragmentId)}`);
    if (cid !== S.activeConvId || gen !== historyGen || !historyOpen.has(fragmentId)) return;
    historyCache.set(fragmentId, events);
  } catch (e) {
    toast(e.message, true);
    historyOpen.delete(fragmentId);
  }
  if (isUtilityPanelOpen(PANEL_ID)) render();
}

async function applyOperation(body) {
  if (writing || !requestSendPermission()) return;
  const cid = S.activeConvId;
  writing = true;
  try {
    const result = await api.post(convUrl(cid, "state"), body);
    if (cid !== S.activeConvId) return;
    // The write's own state is newer than any read still in flight.
    loadSeq++;
    panel = result.state;
    editing = null;
    clearHistoryCache();
    updateStateButton();
    render();
    await Promise.all([...historyOpen].map(loadHistory));
  } catch (e) {
    toast(e.message, true);
  } finally {
    writing = false;
  }
}

function save() {
  if (!editing) return;
  const text = editing.text.trim();
  if (!text) {
    toast("The text is empty.", true);
    return;
  }
  const { fragmentId, op, entryId } = editing;
  applyOperation({ fragment_id: fragmentId, op, text, entry_id: entryId });
}

function deleteOrphan(fragmentId) {
  const f = panel?.fragments.find((x) => x.fragment_id === fragmentId);
  const label = f?.label || fragmentId;
  confirmDelete(
    "Saved State",
    `Delete the saved state of “${esc(label)}” from this conversation, on every branch?`,
    async () => {
      if (!requestSendPermission()) return;
      try {
        await api.del(convUrl(S.activeConvId, "state", encodeURIComponent(fragmentId)));
        historyOpen.delete(fragmentId);
        await refreshState();
        toast("Saved state deleted");
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

function onClick(e) {
  const target = e.target.closest("[data-state-action]");
  if (!target || target.disabled) return;
  const fragmentId = target.closest("[data-fragment-id]")?.dataset.fragmentId;
  if (!fragmentId) return;
  const entryId = target.dataset.entryId || "";
  const entries = panel?.fragments.find((f) => f.fragment_id === fragmentId)?.entries || [];
  switch (target.dataset.stateAction) {
    case "history": {
      const opening = !historyOpen.has(fragmentId);
      if (opening) historyOpen.add(fragmentId);
      else historyOpen.delete(fragmentId);
      render();
      if (opening) loadHistory(fragmentId);
      break;
    }
    case "set":
      openEditor(fragmentId, "set", "", "");
      break;
    case "edit-value":
      openEditor(fragmentId, "set", "", entries[0]?.text || "");
      break;
    case "add":
      openEditor(fragmentId, "add", "", "");
      break;
    case "revise":
      openEditor(fragmentId, "revise", entryId, entries.find((e) => e.entry_id === entryId)?.text || "");
      break;
    case "cancel":
      editing = null;
      render();
      break;
    case "save":
      save();
      break;
    case "clear":
      applyOperation({ fragment_id: fragmentId, op: "clear" });
      break;
    case "retire":
      applyOperation({ fragment_id: fragmentId, op: "retire", entry_id: entryId });
      break;
    case "delete-orphan":
      deleteOrphan(fragmentId);
      break;
  }
}

function onKeydown(e) {
  if (!e.target.classList?.contains("state-editor-input")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    editing = null;
    render();
  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    save();
  }
}

function onInput(e) {
  if (!e.target.classList?.contains("state-editor-input") || !editing) return;
  editing.text = e.target.value;
  const count = e.target.closest(".state-editor")?.querySelector(".state-editor-count");
  if (count) count.textContent = `${e.target.value.length}/${panel.limits.text}`;
}

const content = $("state-panel-content");
if (content) {
  content.addEventListener("click", onClick);
  content.addEventListener("keydown", onKeydown);
  content.addEventListener("input", onInput);
}
