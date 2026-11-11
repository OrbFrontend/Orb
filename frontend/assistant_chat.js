// The right-panel assistant: one dock that follows whichever utility panel is open, one transcript kept in this
// browser, one run at a time owned through S.operations, and Apply for the one edit it has proposed.
import { registerActions } from "./actions.js";
import { api } from "./api.js";
import {
  affectedResources,
  applyRefusal,
  applyStreamEvent,
  handoffOutcome,
  isSendKey,
  MAX_ITEM_CHARS,
  parseTranscript,
  resources,
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
// The events after which the stored transcript changes in a way worth keeping; progress and notes are not among them. A
// write's `tool_start` is kept too, so a reload in the middle of it shows an unknown outcome instead of nothing.
const PERSISTED_EVENTS = new Set(["tool_result", "mutation", "confirm", "handoff", "done", "error"]);
const APPLY_KIND = "assistant-apply";

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
  log.innerHTML = transcriptHtml(S.assistant.items, renderMessageHtml, { active: S.assistant.proposal, busy });
  toEnd();
}

/**
 * Read again what a run or an Apply changed, by id, through the loaders that own each resource. Nothing here moves focus or
 * opens anything: lists refresh in place, and a chat repaints only while it is the one in view, each loader checking the
 * view token again once its fetch returns.
 */
async function refreshResources(changed) {
  const worlds = new Set();
  const keys = new Set();
  const chats = new Set();
  const chatWorlds = new Set();
  const branches = new Set();
  let characters = false;
  let personas = false;
  let worldList = false;
  let conversations = false;
  for (const item of changed) {
    if (item.kind === "lorebook_entry") worlds.add(item.world_id);
    else if (item.kind === "settings") for (const key of item.keys) keys.add(key);
    // A media job's artifacts arrive with the chat's messages, read again through the chat's own loader.
    else if (item.kind === "message" || item.kind === "media") chats.add(item.conversation_id);
    else if (item.kind === "character") characters = true;
    else if (item.kind === "persona") personas = true;
    else if (item.kind === "world" && item.world_id) worlds.add(item.world_id);
    else if (item.kind === "world") worldList = true;
    else if (item.kind === "conversation") conversations = true;
    else if (item.kind === "chat_worlds") chatWorlds.add(item.conversation_id);
    else if (item.kind === "branch") branches.add(item.conversation_id);
  }
  const jobs = [...worlds].map((worldId) => refreshWorld(worldId));
  if (worldList) jobs.push(loadWorlds());
  if (keys.size) jobs.push(refreshSettings([...keys]));
  if (characters) jobs.push(loadCharacters());
  if (personas) jobs.push(loadPersonas());
  if (conversations) jobs.push(loadConversations());
  for (const cid of chats) if (S.activeConvId === cid) jobs.push(refreshConversationMessages(null, cid));
  for (const cid of chatWorlds) jobs.push(refreshChatWorlds(cid));
  for (const cid of branches) jobs.push(showBranchChange(cid));
  await Promise.allSettled(jobs);
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

/**
 * Keep *media* to the run's media jobs still running: one starts with its tool, as `media` work in its chat, and ends with
 * its result, or once the run has settled (*event* `settled`).
 */
function trackMedia(media, current, record, event) {
  if (event === "tool_start") {
    const item = current.items.at(-1);
    if (item?.kind === "tool" && item.running && item.target?.kind === "media") {
      const op = begin("media", { conversationId: item.target.conversation_id, messageId: item.target.message_id });
      op.stop = () => record.stop();
      media.set(item, op);
    }
  }
  for (const [item, op] of media) {
    if (event !== "settled" && item.running) continue;
    finish(op);
    media.delete(item);
  }
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

/** A new request ends the held proposal's Apply: it becomes inert, or an unknown outcome if its Apply was never answered. */
function releaseProposal() {
  const held = S.assistant.proposal;
  S.assistant.proposal = null;
  if (held?.status === "pending") held.status = "superseded";
  else if (held?.status === "unconfirmed") held.status = "unknown";
}

/**
 * Stream one run over *body* and settle it. *context* is the view it is about and *request* the user's words, which a
 * proposal it makes remembers for Ask again.
 */
async function stream(body, context, request) {
  const items = S.assistant.items;
  const current = { items, context, request, note: "", status: "", terminal: null, writes: [] };
  run = current;
  const record = beginStream("assistant", wireContext(context), "/assistant/stop");
  // A media job this run is rendering, as media work in its chat, so that chat's delete dialog names it. Its Stop is the run's.
  const media = new Map();
  persist();
  paint();
  let failure = "";
  try {
    for await (const { event, data } of streamEvents(record, "/assistant/run", body, "Assistant")) {
      const failed = event === "error" ? sseError(data, "The assistant failed.").message : "";
      const committed = applyStreamEvent(current, event, data, failed);
      trackMedia(media, current, record, event);
      if (event === "confirm" && current.proposal) S.assistant.proposal = current.proposal;
      // Committed now, so shown now; the stream carries on meanwhile.
      if (committed.length) refreshResources(committed);
      const startedWrite = event === "tool_start" && current.items.at(-1)?.write;
      const answered = event === "assistant_message" && current.items.at(-1)?.kind === "assistant";
      if (PERSISTED_EVENTS.has(event) || startedWrite || answered) persist();
      paint();
    }
  } catch (error) {
    failure = error?.message || "The assistant failed.";
  } finally {
    settleRun(current, { stopped: record.stream.stopping, failure });
    persist();
    paint();
    await settle(record);
    trackMedia(media, current, record, "settled");
    if (run === current) run = null;
    // Only after the assistant's own stream has settled, and never after a Stop, which the transcript then says.
    if (current.terminal === "handoff" && current.handoff) {
      if (record.stream.stopping) current.items.push(handoffOutcome(current.handoff, "stopped"));
      else launchHandoff(current);
      trimTranscript(current.items);
      persist();
    }
    paint();
    // Only once the server has settled: what it committed is then final, including a write whose result never came.
    // Nothing is sent again; the reader checks the target.
    if (current.terminal !== "reply" && current.terminal !== "handoff")
      await refreshResources(affectedResources(current));
    else {
      // A media job that ended short may have saved as it ended without saying so, and the run can still answer after it.
      const unsaid = current.writes.filter((item) => item.target?.kind === "media" && !item.committed?.length);
      if (unsaid.length) await refreshResources(affectedResources({ writes: unsaid }));
    }
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
  releaseProposal();
  const items = S.assistant.items;
  items.push({ kind: "user", text, context });
  trimTranscript(items);
  input.value = "";
  await stream({ items: runItems(items), context: wireContext(context) }, context, text);
}

/**
 * Commit the held proposal by its id alone. A refusal leaves it inert, except a busy chat, which may be tried again; an
 * Apply that was never answered keeps its id for a retry and reads its target again. Only a confirmed commit continues the
 * request, once, in the view it was sent from.
 */
async function apply(proposalId) {
  const item = S.assistant.proposal;
  if (!item || item.proposalId !== proposalId || activeOperation()) return;
  if (item.status !== "pending" && item.status !== "unconfirmed") return;
  if (!requestSendPermission()) return;
  // A chat edit is chat work while it commits: the chat's delete dialog names it, and the server makes a delete wait for it.
  const chat = item.target?.kind === "message" ? { conversationId: item.target.conversation_id } : {};
  const op = begin(APPLY_KIND, { ...chat, proposalId });
  const previous = item.status;
  item.status = "applying";
  delete item.note;
  persist();
  paint();
  let result = null;
  try {
    result = await api.post("/assistant/apply", { proposal_id: proposalId });
    item.status = "applied";
  } catch (error) {
    const refusal = applyRefusal(error, previous);
    item.status = refusal.status;
    if (refusal.note) item.note = refusal.note;
  } finally {
    finish(op);
  }
  if (item.status !== "pending" && item.status !== "unconfirmed") S.assistant.proposal = null;
  if (result) {
    S.assistant.items.push({ kind: "outcome", reason: "applied", text: String(result.summary || "Applied.") });
    trimTranscript(S.assistant.items);
  }
  persist();
  paint();
  if (result) await refreshResources(resources(result.resources));
  else if ((item.status === "unconfirmed" || item.status === "unknown") && item.target)
    await refreshResources([item.target]);
  if (!result || !item.context || activeOperation()) return;
  const body = {
    items: runItems(S.assistant.items),
    context: wireContext(item.context),
    continue_proposal_id: proposalId,
  };
  await stream(body, item.context, item.request || "");
}

function cancel(proposalId) {
  const item = S.assistant.proposal;
  if (!item || item.proposalId !== proposalId || activeOperation()) return;
  item.status = "cancelled";
  S.assistant.proposal = null;
  persist();
  paint();
}

/** Put a proposal's request back in the input, for the reader to send again for a fresh preview. Nothing is sent. */
function askAgain(index) {
  const item = S.assistant.items[index];
  if (item?.kind !== "proposal" || !item.request) return;
  const input = $("assistant-input");
  input.value = item.request;
  input.focus();
}

function clear() {
  if (activeOperation()) return;
  S.assistant.items.length = 0;
  S.assistant.proposal = null;
  persist();
  paint();
}

export function initAssistant() {
  const dock = $("assistant-dock");
  if (!dock) return;
  S.assistant.items = parseTranscript(stored(LS_TRANSCRIPT));
  S.assistant.proposal = null;
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
    apply: (el) => apply(el.dataset.proposalId),
    cancel: (el) => cancel(el.dataset.proposalId),
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
