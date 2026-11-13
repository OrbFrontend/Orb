// The assistant's own right panel: one transcript, about one character at a time, shown from this browser while the server
// holds the conversation the model reads; one run at a time owned through
// S.operations, and the cards of changes its runs proposed, each applied, discarded, edited or undone on its own. A card stays
// live across later requests, which are told what it still holds. A run and its cards outlive a closed panel.
import { registerActions } from "./actions.js";
import { api } from "./api.js";
import {
  applyStreamEvent,
  applyVerdicts,
  findProposal,
  handoffOutcome,
  isSendKey,
  MAX_ITEM_CHARS,
  newId,
  parseTranscript,
  pendingProposalIds,
  reviseEntry,
  serializeTranscript,
  settleRun,
  transcriptEntries,
  trimTranscript,
  undoVerdicts,
  wireContext,
} from "./assistant_view.js";
import { forgetConversation, loadConversations, resetChatUI, selectConversation } from "./chat_conversations.js";
import { showBranchChange } from "./chat_messages.js";
import {
  continueCapturedChat,
  regenerateCapturedReply,
  rewriteCapturedReply,
  sendCapturedMessage,
} from "./chat_stream.js";
import { refreshConversationMessages } from "./chat_workflow.js";
import { reconcileChildren } from "./dom_reconcile.js";
import { sseError } from "./errors.js";
import { loadInteractiveFragments, loadMoodFragments } from "./library_fragments.js";
import { loadCharacters } from "./library_sidebar.js";
import { forgetWorld, loadWorlds, lorebookFocus, refreshChatWorlds, refreshWorld } from "./lorebooks.js";
import { renderMessageHtml } from "./message_html.js";
import { begin, beginStream, finish, settle, streamEvents } from "./operations.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel, watchUtilityPanel } from "./panels.js";
import { renderToolsPanel } from "./settings.js";
import { loadPersonas } from "./settings_personas.js";
import { reloadSettingKeys } from "./settings_store.js";
import { charactersView, S, subscribe } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import { $, toast } from "./utils.js";

const LS_TRANSCRIPT = "orb-assistant-transcript";
const LS_SESSION = "orb-assistant-session";
const LS_OPEN = "orb-assistant-open";
const LS_THINK = "orb-assistant-think";
const PANEL = "assistant-panel";
// The layout where panels slide over the page and an on-screen keyboard may rise with focus.
const NARROW = "(max-width: 900px)";
const FOLLOW_SLACK_PX = 24;
const LABEL_TITLE_CHARS = 120;
// The events after which the stored transcript changes in a way worth keeping; progress and notes are not among them.
const PERSISTED_EVENTS = new Set(["tool_start", "tool_result", "handoff", "done", "error"]);
const APPLY_KIND = "assistant-apply";
// Every settings key a tool can write, read again after a run whose changes may not all have been reported.
const SWITCH_KEYS = [
  "enable_agent",
  "enabled_tools",
  "director_individual_fragments",
  "length_guard_enabled",
  "agentic_lorebook_enabled",
  "workflows_globally_enabled",
  "workflow_enabled",
  "active_persona_id",
];

// The run this tab is streaming, with the view it captured at Send. The view on screen may have moved on since.
let run = null;
// Whether the log keeps to its newest entry. Read from the reader's own scrolling, since a hidden log measures nothing.
let following = true;
// A Tools panel repaint held back while the reader is typing in it.
let toolsRepaintHeld = false;
// The character the transcript is about, null until a view names one.
let character = null;
// The view the empty log's suggestions were drawn for, null once the log holds entries.
let suggestedFor = null;

const narrow = () => Boolean(window.matchMedia?.(NARROW).matches);

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

// A title is the reader's own text and can be any length; the label is stored with every request in the log.
const titled = (prefix, title) =>
  `${prefix}: ${title.length > LABEL_TITLE_CHARS ? `${title.slice(0, LABEL_TITLE_CHARS)}…` : title}`;

/** The view a request sent now would be about, with the lorebook drawer's world and entry over it. */
function captureContext() {
  const page = capturePage();
  const lore = lorebookFocus();
  if (!lore) return page;
  const what = lore.entryId
    ? titled("Lore entry", lore.entryName || "Untitled")
    : titled("World", lore.worldName || "Untitled");
  return { ...page, world_id: lore.worldId, entry_id: lore.entryId, label: `${page.label} · ${what}` };
}

/** The page behind any drawer. A group names no character: the cast has several. */
function capturePage() {
  const none = { conversation_id: null, character_id: null, document_id: null, world_id: null, entry_id: null };
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

/** The kind of view the empty log suggests requests for. */
function shownView() {
  const context = captureContext();
  return context.entry_id ? "entry" : context.world_id ? "world" : context.character_id ? "character" : context.mode;
}

function toEnd() {
  const log = $("assistant-log");
  if (log && following) log.scrollTop = log.scrollHeight;
}

function paintLog() {
  const log = $("assistant-log");
  if (!log) return;
  const busy = Boolean(activeOperation());
  const view = shownView();
  suggestedFor = S.assistant.items.length ? null : view;
  // Keyed, so a stream event rebuilds only the rows it changed and a disclosure the reader opened stays open.
  reconcileChildren(log, transcriptEntries(S.assistant.items, renderMessageHtml, { busy, suggestFor: view }));
  toEnd();
}

// How each kind of committed resource is read again, keyed so that each is read once per refresh.
const chatMessages = (r) => [
  `chat:${r.conversation_id}`,
  () => S.activeConvId === r.conversation_id && refreshConversationMessages(null, r.conversation_id),
];
// A deleted resource is let go of as the app's own delete does: the chat or world in view closes.
const REFRESHERS = {
  lorebook_entry: (r) => [`world:${r.world_id}`, () => refreshWorld(r.world_id)],
  world: (r) =>
    r.deleted
      ? [`world-gone:${r.world_id}`, () => forgetWorld(r.world_id)]
      : r.world_id
        ? [`world:${r.world_id}`, () => refreshWorld(r.world_id)]
        : ["worlds", loadWorlds],
  character: (r) =>
    r.deleted
      ? [
          "characters",
          async () => {
            if (S.activeCharId === r.character_id) resetChatUI();
            await Promise.all([loadCharacters(), loadConversations()]);
          },
        ]
      : ["characters", loadCharacters],
  persona: () => ["personas", loadPersonas],
  mood_fragment: () => ["mood_fragments", loadMoodFragments],
  // Its loader also reads the open chat's state again, which a state fragment's change can alter.
  interactive_fragment: () => ["interactive_fragments", loadInteractiveFragments],
  conversation: (r) =>
    r.deleted
      ? [`chat-gone:${r.conversation_id}`, () => forgetConversation(r.conversation_id)]
      : ["conversations", loadConversations],
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
    else if (REFRESHERS[item.kind]) jobs.set(...REFRESHERS[item.kind](item));
  }
  // The active persona's refresh paints the persona list itself, after the key it marks has loaded.
  if (keys.has("active_persona_id")) jobs.delete("personas");
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
    { kind: "mood_fragment" },
    { kind: "interactive_fragment" },
    { kind: "conversation" },
    ...(S.activeConvId ? [{ kind: "message", conversation_id: S.activeConvId }] : []),
    { kind: "settings", keys: SWITCH_KEYS },
  ]);
}

/**
 * Open chat *cid* as the chat list does, only while the reader is still on the view the request was sent from: a run that
 * ends after they moved on never pulls them back. Returns why it did not open, or "" once it is opening.
 */
function openCapturedChat(cid, context) {
  if (S.documentMode) return "document";
  if (JSON.stringify(wireContext(captureContext())) !== JSON.stringify(wireContext(context))) return "moved";
  selectConversation(cid).catch((error) => console.error("Failed to open the chat:", error));
  return "";
}

/**
 * Start what a settled run handed off. A reply goes through the chat's own helpers and checks, only in the chat the run
 * named while it is open and its target unmoved; once started, the chat's stream owns Stop and the reply. A chat to open
 * opens only from the view the request was sent from. Otherwise the transcript says why, and nothing starts or switches.
 */
function launchHandoff(current) {
  const target = current.handoff;
  const { conversation_id: cid, message_id: msgId } = target;
  const status =
    target.tool === "open_chat"
      ? openCapturedChat(cid, current.context)
      : target.tool === "continue_chat"
        ? continueCapturedChat(cid, msgId, target.speaker_member_id || null)
        : target.tool === "send_as_user"
          ? sendCapturedMessage(cid, msgId, target.content)
          : target.tool === "rewrite_reply"
            ? rewriteCapturedReply(cid, msgId, target.direction)
            : regenerateCapturedReply(cid, msgId);
  // A slide-over would cover the reply the chat now writes.
  if (status === "" && narrow()) closeUtilityPanel(PANEL);
  current.items.push(handoffOutcome(target, status));
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
    if (keys.includes("active_persona_id")) await loadPersonas();
  } catch (error) {
    console.error("Failed to refresh settings:", error);
  }
  repaintToolsPanel();
}

/** Mark every button that opens the panel, through the root, so a button rendered later is marked too. */
function paintLaunchers(busy) {
  const app = $("app");
  const pending = pendingProposalIds(S.assistant.items).length;
  // Written only when changed: these sit on the root, where a style change reaches the whole page.
  if (app.hasAttribute("data-asst-running") !== busy) app.toggleAttribute("data-asst-running", busy);
  if (app.hasAttribute("data-asst-pending") !== pending > 0) app.toggleAttribute("data-asst-pending", pending > 0);
  const count = pending ? String(pending) : "";
  if (app.style.getPropertyValue("--asst-pending") === count) return;
  if (count) app.style.setProperty("--asst-pending", count);
  else app.style.removeProperty("--asst-pending");
}

function paintControls() {
  const operation = activeOperation();
  const busy = Boolean(operation);
  paintLaunchers(busy);
  const applying = operation?.kind === APPLY_KIND;
  const stopping = Boolean(operation?.stream?.stopping);
  const send = $("assistant-send");
  if (!send) return;
  send.dataset.state = applying ? "applying" : busy ? (stopping ? "stopping" : "stop") : "send";
  const label = applying ? "Saving the change" : busy ? "Stop the assistant" : "Send to the assistant";
  send.setAttribute("aria-label", label);
  send.title = applying ? label : busy ? "Stop" : "Send (Enter)";
  send.disabled = stopping || applying;
  send.classList.toggle("asst-stop", busy && !applying);
  // Clearing mid-run would drop the entries the run is still writing to.
  $("assistant-clear").disabled = busy || !S.assistant.items.length;
  $("assistant-status").textContent = applying ? "Saving the change…" : busy ? run?.status || "Working…" : "";
  const context = busy && run ? run.context : captureContext();
  const scope = $("assistant-scope");
  scope.textContent = context.label;
  scope.title = busy ? `Working on: ${context.label}` : `Your next message is about: ${context.label}`;
  // The suggestions follow the view, which can change without a notice. Only a changed view redraws them: pressing one
  // focuses it, and redrawing then would replace the button before its click lands.
  if (!S.assistant.items.length && shownView() !== suggestedFor) paintLog();
}

function paint() {
  paintLog();
  paintControls();
}

/** The panel has just opened: show the newest entry and the view the next request is about. */
function revealPanel() {
  toEnd();
  paintControls();
}

function togglePanel() {
  if (isUtilityPanelOpen(PANEL)) {
    closeUtilityPanel(PANEL);
    return;
  }
  openUtilityPanel(PANEL, revealPanel);
  // On the narrow layout focus would raise the keyboard over the panel just opened.
  if (!narrow()) $("assistant-input").focus();
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

/**
 * Send *text* as a request: a new one, or the new wording of *replaced*, an earlier request of the log. An edit takes the
 * conversation back to where that request began, here and on the server, and goes on from there; only the conversation
 * goes back. What the removed requests changed stays changed, and the cards among them go with their entries, so a
 * change still waiting there is discarded. Either way the request is about the view open now, since the tools act on what
 * is there now. Returns the run once it has started, or null when nothing was sent.
 */
function submit(text, replaced = null) {
  if (activeOperation() || !text) return null;
  if (text.length > MAX_ITEM_CHARS) {
    toast(`Keep a message to the assistant under ${MAX_ITEM_CHARS} characters.`, true);
    return null;
  }
  if (!requestSendPermission()) return null;
  followCharacter();
  // Captured once, here: switching chats or documents while this runs changes the next request, not this one.
  const context = captureContext();
  const items = S.assistant.items;
  // Gone when the log was cleared for another character just above: the text is then a request of its own.
  const at = replaced ? items.indexOf(replaced) : -1;
  const edit = at >= 0;
  if (edit) items.length = at;
  const requestId = (edit && replaced.requestId) || newId();
  items.push({ kind: "user", text, context, requestId });
  trimTranscript(items);
  following = true;
  const reasoning = $("assistant-think").checked;
  // The changes earlier cards still hold, so this request can build on them; the server reads their texts itself.
  const pending = pendingProposalIds(items);
  // Only the request goes out: the server holds the conversation this session names, every call and result in it.
  const body = { text, context: wireContext(context), session: S.assistant.session, request_id: requestId, edit };
  return stream({ ...body, reasoning, pending }, context, text);
}

async function send() {
  const input = $("assistant-input");
  const sent = submit(input.value.trim());
  if (!sent) return;
  input.value = "";
  fitInput();
  await sent;
}

/** The request *el* names by its place in the log, or null. */
function requestOf(el) {
  const item = S.assistant.items[Number(el.dataset.index)];
  return item?.kind === "user" ? item : null;
}

/** Open a request's own text in a box, one request at a time; Send there replaces it and everything below it. */
function editOpen(el) {
  const item = requestOf(el);
  if (!item || activeOperation()) return;
  for (const other of S.assistant.items) if (other.editing) editClose(other);
  item.editing = true;
  item.draft = item.text;
  paint();
  const box = $("assistant-log").querySelector("textarea.asst-request-box");
  box?.focus();
  box?.setSelectionRange(box.value.length, box.value.length);
}

function editClose(item) {
  delete item.editing;
  delete item.draft;
}

/** Typing in a request's box: Enter sends it as the main input does, Escape puts the request back as it was. */
function editDraft(el, e) {
  const item = requestOf(el);
  if (!item?.editing) return;
  if (e.type === "input") {
    // Kept off the repaint path: a repaint mid-typing would replace the box under the caret.
    item.draft = el.value;
  } else if (e.key === "Escape") {
    // The edit is what Escape closes, not the panel behind it.
    e.stopPropagation();
    editClose(item);
    paint();
  } else if (isSendKey(e)) {
    e.preventDefault();
    editSend(el);
  }
}

async function editSend(el) {
  const item = requestOf(el);
  if (!item?.editing) return;
  await submit((item.draft ?? item.text).trim(), item);
}

/**
 * Run one change request over proposals *entries* of *card* (`[{ entry }]`): Apply, Undo or a revision. Message changes are
 * chat work while they commit, so the chat's delete dialog names them and the server makes a delete wait for them.
 */
async function changing(entries, work) {
  if (!entries.length || activeOperation()) return;
  if (!requestSendPermission()) return;
  const chats = new Set(
    entries.flatMap(({ entry }) => (entry.target?.kind === "message" ? [entry.target.conversation_id] : [])),
  );
  const ops = chats.size
    ? [...chats].map((conversationId) => begin(APPLY_KIND, { conversationId }))
    : [begin(APPLY_KIND, {})];
  paint();
  try {
    await work();
  } finally {
    for (const op of ops) finish(op);
  }
  persist();
  paint();
}

/** A card's live entries waiting for Apply, all of them or the one *el* names. */
function pendingOf(el) {
  if (el.dataset.proposal) {
    const found = findProposal(S.assistant.items, el.dataset.proposal);
    return found && found.entry.status === "pending" ? [found] : [];
  }
  const card = S.assistant.items[Number(el.dataset.index)];
  if (card?.kind !== "proposals") return [];
  return card.proposals
    .filter((entry) => entry.proposalId && entry.status === "pending")
    .map((entry) => ({ entry, card }));
}

/** Record what a change request settled: its summary in the transcript, and the resources it changed read again. */
async function settled(outcome, fallback) {
  if (outcome?.summaries.length) {
    S.assistant.items.push({ kind: "outcome", reason: "applied", text: outcome.summaries.join(" ") });
    trimTranscript(S.assistant.items);
  }
  await refreshResources(outcome ? outcome.resources : fallback);
}

/**
 * Apply pending changes by their ids alone, each answered with its own verdict. A busy chat leaves its change pending to try
 * again, as does an Apply that ends without verdicts, which also reads their targets again.
 */
async function apply(el) {
  const entries = pendingOf(el);
  const card = entries[0]?.card;
  let applied = null;
  await changing(entries, async () => {
    delete card.note;
    try {
      const results = (
        await api.post("/assistant/apply", { proposal_ids: entries.map(({ entry }) => entry.proposalId) })
      )?.results;
      applied = applyVerdicts(card, results);
    } catch {
      card.note = "Apply did not finish. Try again; a change already applied is not applied twice.";
    }
  });
  if (card)
    await settled(
      applied,
      entries.flatMap(({ entry }) => (entry.target ? [entry.target] : [])),
    );
}

/** Put back the text an applied edit replaced; it is pending again afterwards, to apply or discard. */
async function undo(el) {
  const found = findProposal(S.assistant.items, el.dataset.proposal || "");
  if (found?.entry.status !== "applied") return;
  let undone = null;
  await changing([found], async () => {
    try {
      undone = undoVerdicts(
        found.card,
        (await api.post("/assistant/undo", { proposal_ids: [found.entry.proposalId] }))?.results,
      );
    } catch {
      found.entry.note = "Undo did not finish. Try again.";
    }
  });
  await settled(undone, found.entry.target ? [found.entry.target] : []);
}

/** Discard pending changes, all of a card's or the one *el* names; nothing was written, so nothing is sent. */
function cancel(el) {
  if (activeOperation()) return;
  for (const { entry } of pendingOf(el)) {
    entry.status = "cancelled";
    delete entry.editing;
    delete entry.proposalId;
  }
  persist();
  paint();
}

/** Open, keep or close the reader's own text for a pending edit, which Save then makes what Apply writes. */
function reviseOpen(el) {
  const found = findProposal(S.assistant.items, el.dataset.proposal || "");
  if (found?.entry.status !== "pending" || activeOperation()) return;
  found.entry.editing = true;
  found.entry.draft = found.entry.after;
  paint();
  const boxes = $("assistant-log").querySelectorAll("textarea.asst-revise");
  [...boxes].find((box) => box.dataset.proposal === found.entry.proposalId)?.focus();
}

function reviseDraft(el) {
  const found = findProposal(S.assistant.items, el.dataset.proposal || "");
  // Kept off the repaint path: a repaint mid-typing would replace the box under the caret.
  if (found) found.entry.draft = el.value;
}

function reviseClose(el) {
  const found = findProposal(S.assistant.items, el.dataset.proposal || "");
  if (!found) return;
  delete found.entry.editing;
  delete found.entry.draft;
  paint();
}

async function reviseSave(el) {
  const found = findProposal(S.assistant.items, el.dataset.proposal || "");
  if (found?.entry.status !== "pending") return;
  const { entry } = found;
  await changing([found], async () => {
    try {
      const result = await api.post("/assistant/revise", {
        proposal_id: entry.proposalId,
        text: entry.draft ?? entry.after,
      });
      if (result?.verdict === "revised") {
        reviseEntry(entry, result.proposal);
        entry.note = "Your text; Apply writes it.";
        delete entry.editing;
        delete entry.draft;
      } else if (result?.verdict === "expired") {
        entry.status = "expired";
        delete entry.editing;
        delete entry.proposalId;
      } else entry.note = result?.message || "That text was not taken.";
    } catch {
      entry.note = "Saving your text did not finish. Try again.";
    }
  });
}

/** Grow the input with its text, up to the cap its stylesheet sets. */
function fitInput() {
  const input = $("assistant-input");
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}

/** Put a request in the input, for the reader to edit or send. Nothing is sent. */
function fillInput(text) {
  const input = $("assistant-input");
  input.value = text;
  fitInput();
  input.focus();
}

/** Start a conversation the server has never seen: its next request extends nothing. */
function newSession() {
  S.assistant.session = newId();
  store(LS_SESSION, S.assistant.session);
}

function clear() {
  if (activeOperation()) return;
  S.assistant.items.length = 0;
  newSession();
  persist();
  paint();
}

/** The character the page is about: a solo chat's or the library's, a group as one of its own, null where there is none. */
function characterInView() {
  if (S.documentMode) return null;
  const conv = S.conversations.find((item) => item.id === S.activeConvId);
  if (conv) return conv.kind === "group" ? `group:${conv.id}` : conv.character_card_id || null;
  return S.activeCharId || null;
}

/**
 * Start over when the reader turns to another character: the conversation is about one character at a time, so nothing
 * about the last one is carried into the next. A view with no character (a document, the bare library) changes nothing,
 * and a swap made while a run goes takes effect once it has settled.
 */
function followCharacter() {
  const now = characterInView();
  if (!now || now === character || activeOperation()) return;
  const swapped = character !== null;
  character = now;
  if (swapped && S.assistant.items.length) clear();
}

export function initAssistant() {
  const panel = $(PANEL);
  if (!panel) return;
  S.assistant.items = parseTranscript(stored(LS_TRANSCRIPT));
  S.assistant.session = (stored(LS_SESSION) || "").slice(0, 64);
  if (!S.assistant.session) newSession();
  registerActions("assistant", {
    toggle: () => togglePanel(),
    clear: () => clear(),
    send: () => {
      const operation = activeOperation();
      if (!operation) return send();
      if (operation.kind === APPLY_KIND) return;
      operation.stop();
      paintControls();
    },
    apply: (el) => apply(el),
    applyAll: (el) => apply(el),
    cancel: (el) => cancel(el),
    cancelAll: (el) => cancel(el),
    undo: (el) => undo(el),
    revise: (el) => reviseOpen(el),
    reviseDraft: (el) => reviseDraft(el),
    reviseSave: (el) => reviseSave(el),
    reviseCancel: (el) => reviseClose(el),
    editRequest: (el) => editOpen(el),
    requestDraft: (el, e) => editDraft(el, e),
    requestSave: (el) => editSend(el),
    requestCancel: (el) => {
      const item = requestOf(el);
      if (item) editClose(item);
      paint();
    },
    // A card's request goes back in the input, to send again for a fresh preview.
    askAgain: (el) => fillInput(S.assistant.items[Number(el.dataset.index)]?.request || ""),
    suggest: (el) => fillInput(el.dataset.text || ""),
    focusInput: () => $("assistant-input").focus(),
    think: () => store(LS_THINK, $("assistant-think").checked ? "1" : "0"),
  });
  $("assistant-think").checked = stored(LS_THINK) === "1";
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
  input.addEventListener("input", fitInput);
  // The view can change without a notice; refresh the label when the reader turns to the panel.
  panel.addEventListener("focusin", paintControls);
  panel.addEventListener("pointerenter", paintControls);
  subscribe("operations", () => {
    followCharacter();
    paintControls();
  });
  subscribe("conversations", () => {
    followCharacter();
    paintControls();
  });
  subscribe("documents", paintControls);
  subscribe("lorebook", paintControls);
  // Opening Tools or the Inspector closes the panel, as do mobile Back and a tap beside it; each lands here.
  watchUtilityPanel(PANEL, (open) => {
    S.assistant.open = open;
    store(LS_OPEN, open ? "1" : "0");
  });
  paint();
  // A slide-over restored at load would cover the page the reader came back to.
  if (stored(LS_OPEN) === "1" && !narrow()) openUtilityPanel(PANEL, revealPanel);
}
