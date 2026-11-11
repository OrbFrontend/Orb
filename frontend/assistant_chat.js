// The right-panel assistant: one dock that follows whichever utility panel is open, one transcript kept in this
// browser, one run at a time owned through S.operations, and Apply for the edits its latest run proposed.
import { registerActions } from "./actions.js";
import { api } from "./api.js";
import {
  applyStreamEvent,
  applyVerdicts,
  handoffOutcome,
  isSendKey,
  MAX_ITEM_CHARS,
  parseTranscript,
  pendingIds,
  runItems,
  serializeTranscript,
  settleRun,
  transcriptHtml,
  trimTranscript,
  wireContext,
} from "./assistant_view.js";
import { loadConversations } from "./chat_conversations.js";
import { showBranchChange } from "./chat_messages.js";
import { regenerateCapturedReply, rewriteCapturedReply } from "./chat_stream.js";
import { refreshConversationMessages } from "./chat_workflow.js";
import { sseError } from "./errors.js";
import { loadCharacters } from "./library_sidebar.js";
import { loadWorlds, refreshChatWorlds, refreshWorld } from "./lorebooks.js";
import { renderMessageHtml } from "./message_html.js";
import { begin, beginStream, finish, settle, streamEvents } from "./operations.js";
import { renderToolsPanel } from "./settings.js";
import { loadPersonas } from "./settings_personas.js";
import { reloadSettingKeys } from "./settings_store.js";
import { charactersView, S, subscribe } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import { $, toast } from "./utils.js";

const LS_TRANSCRIPT = "orb-assistant-transcript";
const LS_OPEN = "orb-assistant-open";
const FOLLOW_SLACK_PX = 24;
const LABEL_TITLE_CHARS = 120;
// The events after which the stored transcript changes in a way worth keeping; progress and notes are not among them.
const PERSISTED_EVENTS = new Set(["tool_start", "tool_result", "handoff", "done", "error"]);
const APPLY_KIND = "assistant-apply";
// Every settings key a pipeline toggle can write, read again after a run whose changes may not all have been reported.
const SWITCH_KEYS = [
  "enable_agent",
  "enabled_tools",
  "director_individual_fragments",
  "length_guard_enabled",
  "agentic_lorebook_enabled",
  "workflows_globally_enabled",
  "workflow_enabled",
];

// The run this tab is streaming, with the view it captured at Send. The view on screen may have moved on since.
let run = null;
// Whether the log keeps to its newest entry. Read from the reader's own scrolling, since a hidden log measures nothing.
let following = true;
// A Tools panel repaint held back while the reader is typing in it.
let toolsRepaintHeld = false;

function activeOperation() {
  return [...S.operations.values()].find((op) => op.kind === "assistant" || op.kind === APPLY_KIND) || null;
}

// Storage can be full, blocked or absent; the assistant then keeps working from memory for this page load.
function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {}
}

function stored(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function persist() {
  store(LS_TRANSCRIPT, serializeTranscript(S.assistant.items));
}

// A title is the reader's own text and can be any length; the label is replayed with every later request.
const titled = (prefix, title) =>
  `${prefix}: ${title.length > LABEL_TITLE_CHARS ? `${title.slice(0, LABEL_TITLE_CHARS)}…` : title}`;

/** The view a request sent now would be about. A group names no character: the cast has several. */
function captureContext() {
  const none = { conversation_id: null, character_id: null, document_id: null };
  if (S.documentMode) {
    const doc = S.documents.find((item) => item.id === S.activeDocId);
    if (doc)
      return { ...none, mode: "document", document_id: doc.id, label: titled("Document", doc.title || "Untitled") };
    return { ...none, mode: "library", label: "Library" };
  }
  const conv = S.conversations.find((item) => item.id === S.activeConvId);
  if (conv) {
    const kind = conv.kind === "group" ? "Group chat" : "Chat";
    return { ...none, mode: "chat", conversation_id: conv.id, label: titled(kind, conv.title || "Untitled") };
  }
  const card = charactersView().find((item) => item.id === S.activeCharId);
  if (card)
    return { ...none, mode: "library", character_id: card.id, label: titled("Character", card.name || "Unnamed") };
  return { ...none, mode: "library", label: "Library" };
}

function toEnd() {
  const log = $("assistant-log");
  if (log && following) log.scrollTop = log.scrollHeight;
}

function paintLog() {
  const log = $("assistant-log");
  if (!log) return;
  const busy = Boolean(activeOperation());
  log.innerHTML = transcriptHtml(S.assistant.items, renderMessageHtml, { active: S.assistant.batch, busy });
  toEnd();
}

// How each kind of committed resource is read again, keyed so that each is read once per refresh.
const chatMessages = (r) => [
  `chat:${r.conversation_id}`,
  () => S.activeConvId === r.conversation_id && refreshConversationMessages(null, r.conversation_id),
];
const REFRESHERS = {
  lorebook_entry: (r) => [`world:${r.world_id}`, () => refreshWorld(r.world_id)],
  world: (r) => (r.world_id ? [`world:${r.world_id}`, () => refreshWorld(r.world_id)] : ["worlds", loadWorlds]),
  character: () => ["characters", loadCharacters],
  persona: () => ["personas", loadPersonas],
  conversation: () => ["conversations", loadConversations],
  // A media job's artifacts arrive with the chat's messages, read again through the chat's own loader.
  message: chatMessages,
  media: chatMessages,
  chat_worlds: (r) => [`chat_worlds:${r.conversation_id}`, () => refreshChatWorlds(r.conversation_id)],
  branch: (r) => [`branch:${r.conversation_id}`, () => showBranchChange(r.conversation_id)],
};

/**
 * Read again what a run or an Apply changed, by id, through the loaders that own each resource. Nothing here moves focus or
 * opens anything: lists refresh in place, and a chat repaints only while it is the one in view, each loader checking the
 * view token again once its fetch returns.
 */
async function refreshResources(changed) {
  const jobs = new Map();
  const keys = new Set();
  for (const item of changed) {
    if (item.kind === "settings") for (const key of item.keys) keys.add(key);
    else jobs.set(...REFRESHERS[item.kind](item));
  }
  if (keys.size) jobs.set("settings", () => refreshSettings([...keys]));
  await Promise.allSettled([...jobs.values()].map(async (job) => job()));
}

/**
 * Read again everything cheap a run could have changed, for a run that ended early or had a tool fail: what it did may not
 * all have been reported. Nothing is sent again; the reader checks what they see.
 */
function refreshEverything() {
  return refreshResources([
    { kind: "world" },
    { kind: "character" },
    { kind: "persona" },
    { kind: "conversation" },
    ...(S.activeConvId ? [{ kind: "message", conversation_id: S.activeConvId }] : []),
    { kind: "settings", keys: SWITCH_KEYS },
  ]);
}

/**
 * Ask the chat to start the reply a settled run handed off, through the chat's own helpers and checks. It starts only in
 * the chat the run named, while that chat is open and the reply still on its branch; otherwise the transcript says why and
 * nothing switches. Once started, the chat's stream owns Stop and the reply.
 */
function launchHandoff(current) {
  const target = current.handoff;
  const { conversation_id: cid, message_id: msgId } = target;
  const status =
    target.tool === "rewrite_reply"
      ? rewriteCapturedReply(cid, msgId, target.direction)
      : regenerateCapturedReply(cid, msgId);
  current.items.push(handoffOutcome(target, status));
  trimTranscript(current.items);
}

/** Repaint the Tools panel, unless the reader is typing in it: then once focus leaves it. */
function repaintToolsPanel() {
  const lists = ["tools-list", "tools-list-secondary"].map((key) => $(key)).filter(Boolean);
  const holder = lists.find((el) => el.contains(document.activeElement));
  if (!holder) {
    renderToolsPanel();
    return;
  }
  if (toolsRepaintHeld) return;
  toolsRepaintHeld = true;
  const later = (e) => {
    if (holder.contains(e.relatedTarget)) return;
    holder.removeEventListener("focusout", later);
    // After the field's own change handler, which saves what was typed.
    setTimeout(() => {
      toolsRepaintHeld = false;
      repaintToolsPanel();
    });
  };
  holder.addEventListener("focusout", later);
}

// Only the keys the toggle wrote are taken from the row, through the settings store's queue, so a save of this tab's that
// is still in flight is never overridden by an older row.
async function refreshSettings(keys) {
  try {
    await reloadSettingKeys(keys);
  } catch (error) {
    console.error("Failed to refresh settings:", error);
  }
  repaintToolsPanel();
}

function paintControls() {
  const operation = activeOperation();
  const busy = Boolean(operation);
  const applying = operation?.kind === APPLY_KIND;
  const stopping = Boolean(operation?.stream?.stopping);
  const send = $("assistant-send");
  if (!send) return;
  send.textContent = applying ? "Applying…" : busy ? (stopping ? "Stopping…" : "Stop") : "Send";
  send.setAttribute(
    "aria-label",
    applying ? "Applying the edit" : busy ? "Stop the assistant" : "Send to the assistant",
  );
  send.disabled = stopping || applying;
  send.classList.toggle("asst-stop", busy && !applying);
  // Clearing mid-run would drop the entries the run is still writing to.
  $("assistant-clear").disabled = busy || !S.assistant.items.length;
  $("assistant-status").textContent = applying ? "Applying the edit…" : busy ? run?.status || "Working…" : "";
  const context = busy && run ? run.context : captureContext();
  const scope = $("assistant-scope");
  scope.textContent = context.label;
  scope.title = busy ? `Working on: ${context.label}` : `Your next message is about: ${context.label}`;
}

function paint() {
  paintLog();
  paintControls();
}

function setOpen(open) {
  S.assistant.open = open;
  store(LS_OPEN, open ? "1" : "0");
  $("assistant-body").hidden = !open;
  $("assistant-dock").classList.toggle("asst-open", open);
  $("assistant-toggle").setAttribute("aria-expanded", String(open));
  if (open) toEnd();
}

/** Put the dock at the foot of the open panel, or hide it when there is none. Moving it stops nothing. */
function placeDock() {
  const dock = $("assistant-dock");
  const tools = $("tools-panel");
  const inspector = $("inspector");
  // Document mode has no Inspector, even if its panel was left open behind the editor.
  const inspectorShown = inspector.classList.contains("open") && !$("app").classList.contains("document-mode");
  const host = tools.classList.contains("open") ? tools : inspectorShown ? inspector : null;
  if (!host) {
    dock.hidden = true;
    return;
  }
  const shown = !dock.hidden && dock.parentElement === host;
  if (dock.parentElement !== host) {
    const log = $("assistant-log");
    const top = log.scrollTop;
    host.appendChild(dock);
    log.scrollTop = top;
  }
  dock.hidden = false;
  // A log that was hidden or moved measured nothing meanwhile; put it back where the reader left it.
  if (!shown) toEnd();
  paintControls();
}

/** A new request closes the held card: its edits still pending can no longer be applied. */
function releaseCard() {
  for (const entry of S.assistant.batch?.proposals || []) if (entry.status === "pending") entry.status = "historical";
  S.assistant.batch = null;
}

/**
 * Stream one run over *body* and settle it. *context* is the view it is about and *request* the user's words, which a
 * card of proposed edits remembers for Ask again.
 */
async function stream(body, context, request) {
  const items = S.assistant.items;
  const current = { items, context, request, note: "", status: "", terminal: null };
  run = current;
  const record = beginStream("assistant", wireContext(context), "/assistant/stop");
  persist();
  paint();
  let failure = "";
  try {
    for await (const { event, data } of streamEvents(record, "/assistant/run", body, "Assistant")) {
      const failed = event === "error" ? sseError(data, "The assistant failed.").message : "";
      const committed = applyStreamEvent(current, event, data, failed);
      // Committed now, so shown now; the stream carries on meanwhile.
      if (committed.length) refreshResources(committed);
      const answered = event === "assistant_message" && current.items.at(-1)?.kind === "assistant";
      if (PERSISTED_EVENTS.has(event) || answered) persist();
      paint();
    }
  } catch (error) {
    failure = error?.message || "The assistant failed.";
  } finally {
    settleRun(current, { stopped: record.stream.stopping, failure });
    if (current.card) S.assistant.batch = current.card;
    persist();
    paint();
    await settle(record);
    if (run === current) run = null;
    // Only after the assistant's own stream has settled, and never after a Stop, which the transcript then says.
    if (current.terminal === "handoff" && current.handoff) {
      if (record.stream.stopping) current.items.push(handoffOutcome(current.handoff, "stopped"));
      else launchHandoff(current);
      trimTranscript(current.items);
      persist();
    }
    paint();
    // Only once the server has settled, so whatever it committed is final: a write whose result never came, or a media job
    // that failed after saving.
    if ((current.terminal !== "reply" && current.terminal !== "handoff") || current.failed) await refreshEverything();
  }
}

async function send() {
  if (activeOperation()) return;
  const input = $("assistant-input");
  const text = input.value.trim();
  if (!text) return;
  if (text.length > MAX_ITEM_CHARS) {
    toast(`Keep a message to the assistant under ${MAX_ITEM_CHARS} characters.`, true);
    return;
  }
  if (!requestSendPermission()) return;
  // Captured once, here: switching chats or documents while this runs changes the next request, not this one.
  const context = captureContext();
  releaseCard();
  const items = S.assistant.items;
  items.push({ kind: "user", text, context });
  trimTranscript(items);
  input.value = "";
  await stream({ items: runItems(items), context: wireContext(context) }, context, text);
}

/**
 * Apply the held card's pending edits by their ids alone, each answered with its own verdict. A busy chat leaves its edit
 * pending to try again, as does an Apply that ends without verdicts, which also reads their targets again.
 */
async function apply() {
  const card = S.assistant.batch;
  const ids = card ? pendingIds(card) : [];
  if (!ids.length || activeOperation()) return;
  if (!requestSendPermission()) return;
  const pending = card.proposals.filter((entry) => entry.status === "pending");
  // A chat edit is chat work while it commits: the chat's delete dialog names it, and the server makes a delete wait for it.
  const chats = new Set(
    pending.flatMap((entry) => (entry.target?.kind === "message" ? [entry.target.conversation_id] : [])),
  );
  const ops = chats.size
    ? [...chats].map((conversationId) => begin(APPLY_KIND, { conversationId }))
    : [begin(APPLY_KIND, {})];
  delete card.note;
  paint();
  let applied = null;
  try {
    applied = applyVerdicts(card, (await api.post("/assistant/apply", { proposal_ids: ids }))?.results);
  } catch {
    card.note = "Apply did not finish. Try again; an edit already applied is not applied twice.";
  } finally {
    for (const op of ops) finish(op);
  }
  if (!pendingIds(card).length) S.assistant.batch = null;
  if (applied?.summaries.length) {
    S.assistant.items.push({ kind: "outcome", reason: "applied", text: applied.summaries.join(" ") });
    trimTranscript(S.assistant.items);
  }
  persist();
  paint();
  await refreshResources(
    applied ? applied.resources : pending.flatMap((entry) => (entry.target ? [entry.target] : [])),
  );
}

function cancel() {
  const card = S.assistant.batch;
  if (!card || activeOperation()) return;
  for (const entry of card.proposals) if (entry.status === "pending") entry.status = "cancelled";
  S.assistant.batch = null;
  persist();
  paint();
}

/** Put a card's request back in the input, for the reader to send again for a fresh preview. Nothing is sent. */
function askAgain(index) {
  const item = S.assistant.items[index];
  if (item?.kind !== "proposals" || !item.request) return;
  const input = $("assistant-input");
  input.value = item.request;
  input.focus();
}

function clear() {
  if (activeOperation()) return;
  S.assistant.items.length = 0;
  S.assistant.batch = null;
  persist();
  paint();
}

export function initAssistant() {
  const dock = $("assistant-dock");
  if (!dock) return;
  S.assistant.items = parseTranscript(stored(LS_TRANSCRIPT));
  S.assistant.batch = null;
  registerActions("assistant", {
    toggle: () => setOpen(!S.assistant.open),
    clear: () => clear(),
    send: () => {
      const operation = activeOperation();
      if (!operation) return send();
      if (operation.kind === APPLY_KIND) return;
      operation.stop();
      paintControls();
    },
    apply: () => apply(),
    cancel: () => cancel(),
    askAgain: (el) => askAgain(Number(el.dataset.index)),
  });
  const log = $("assistant-log");
  log.addEventListener("scroll", () => {
    if (log.clientHeight) following = log.scrollHeight - log.clientHeight - log.scrollTop <= FOLLOW_SLACK_PX;
  });
  const input = $("assistant-input");
  // On the element, so chat and mobile shortcuts on the document never see keys typed here.
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") e.stopPropagation();
    if (!isSendKey(e)) return;
    e.preventDefault();
    send();
  });
  // The view can change without a notice; refresh the label when the reader turns to the dock.
  dock.addEventListener("focusin", paintControls);
  dock.addEventListener("pointerenter", paintControls);
  subscribe("operations", paintControls);
  subscribe("conversations", paintControls);
  subscribe("documents", paintControls);

  const panels = new MutationObserver(placeDock);
  for (const el of [$("tools-panel"), $("inspector"), $("app")]) {
    panels.observe(el, { attributes: true, attributeFilter: ["class"] });
  }
  setOpen(stored(LS_OPEN) === "1");
  paint();
  placeDock();
}
