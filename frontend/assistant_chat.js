// The assistant's own right panel: one chat per character, the one in view shown from this browser while the server holds
// the conversation the model reads; one run at a time owned through S.operations, and the cards of changes its runs
// proposed, each applied, discarded, edited or undone on its own. A card stays live across later requests, which are told
// what it still holds. A run and its cards outlive a closed panel.
import { registerActions } from "./actions.js";
import { api } from "./api.js";
import {
  applyStreamEvent,
  applyVerdicts,
  cleanSuggestions,
  decisionText,
  defaultSuggestions,
  findProposal,
  handoffOutcome,
  isSendKey,
  MAX_ITEM_CHARS,
  newId,
  parseSuggestions,
  parseTranscript,
  pendingProposalIds,
  reviseEntry,
  settleRun,
  storedTranscript,
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
import { refreshDocument, saveOpenDocument } from "./document.js";
import { offsetOfPosition, serializeEditor } from "./document_editor.js";
import { reconcileChildren } from "./dom_reconcile.js";
import { initDragReorder } from "./drag_reorder.js";
import { sseError } from "./errors.js";
import { loadInteractiveFragments, loadMoodFragments } from "./library_fragments.js";
import { loadCharacters } from "./library_sidebar.js";
import { forgetWorld, loadWorlds, lorebookFocus, refreshChatWorlds, refreshWorld } from "./lorebooks.js";
import { renderMessageHtml } from "./message_html.js";
import { rangeInMessage } from "./message_selection.js";
import { begin, beginStream, finish, settle, streamEvents } from "./operations.js";
import {
  closeUtilityPanel,
  isUtilityPanelOpen,
  openUtilityPanel,
  restoreUtilityPanel,
  watchUtilityPanel,
} from "./panels.js";
import { renderToolsPanel } from "./settings.js";
import { loadPersonas } from "./settings_personas.js";
import { persistSettings, reloadSettingKeys } from "./settings_store.js";
import { charactersView, S, subscribe } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import { $, toast } from "./utils.js";

// One key per chat, by the key of the character it is about.
const LS_CHAT = "orb-assistant-chat:";
// The reader's own suggestion lists, by view.
const LS_SUGGESTIONS = "orb-assistant-suggestions";
// As many chats as the server keeps conversations.
const MAX_CHATS = 32;
const PANEL = "assistant-panel";
// The layout where panels slide over the page and an on-screen keyboard may rise with focus.
const NARROW = "(max-width: 900px)";
const FOLLOW_SLACK_PX = 24;
const LABEL_TITLE_CHARS = 120;
// The events after which the stored transcript changes in a way worth keeping; progress and notes are not among them.
const PERSISTED_EVENTS = new Set(["tool_start", "tool_result", "handoff", "done", "error"]);
const APPLY_KIND = "assistant-apply";
// The longest selection a request carries; the server shows the model less and says how much it left out.
const MAX_SELECTION_CHARS = 20000;
// How much of the selection the label under the input shows, and a request's bubble keeps.
const SCOPE_SELECTION_CHARS = 40;
const SHOWN_SELECTION_CHARS = 200;
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
// The chats this page has read, by character key ("" for a view with none): `{ key, session, items }`. A run or an Apply
// writes to the chat it started in, whichever is on screen by then.
const chats = new Map();
// The chat on screen, whose items S.assistant.items is.
let shown = null;
// The reader's own suggestion lists by view, read once.
let customSuggestions = null;
// The suggestions being edited: `{ view, rows }`. The rows are what the editor was last drawn with; typing and reordering
// save without a repaint, and anything that changes the rows reads them back off the page first.
let suggestionEdit = null;
// The view the empty log's suggestions were drawn for, null once the log holds entries.
let suggestedFor = null;
// The reader's last selection in a chat message or the document page. Typing in the panel keeps it, since that is how a
// request about it gets written; a selection or a click anywhere else on the page replaces or drops it.
let selectedRange = null;
let scopeFrame = 0;

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

function chatFor(key) {
  let chat = chats.get(key);
  if (chat) return chat;
  let saved = null;
  try {
    saved = JSON.parse(stored(LS_CHAT + key) || "null");
  } catch {}
  const session = typeof saved?.session === "string" ? saved.session.slice(0, 64) : "";
  chat = { key, session: session || newId(), items: parseTranscript(saved?.items) };
  chats.set(key, chat);
  return chat;
}

/** Put the chat of the character in view on screen; true when that changed which chat is shown. */
function showChat() {
  const chat = chatFor(characterInView() || "");
  if (chat === shown) return false;
  shown = chat;
  S.assistant.items = chat.items;
  following = true;
  return true;
}

function persist(chat) {
  const key = LS_CHAT + chat.key;
  if (!chat.items.length) {
    try {
      localStorage.removeItem(key);
    } catch {}
    return;
  }
  const fresh = stored(key) === null;
  store(key, JSON.stringify({ session: chat.session, at: Date.now(), items: storedTranscript(chat.items) }));
  if (fresh) evictChats();
}

// The one chat a browser kept before chats were per character, moved under its character at load.
const LEGACY_KEYS = ["orb-assistant-transcript", "orb-assistant-session", "orb-assistant-character"];

function adoptLegacyChat() {
  const [transcript, session, character] = LEGACY_KEYS.map(stored);
  if (transcript === null && session === null && character === null) return;
  let items = null;
  try {
    items = JSON.parse(transcript || "null");
  } catch {}
  const key = LS_CHAT + (character || "");
  if (Array.isArray(items) && items.length && stored(key) === null)
    store(key, JSON.stringify({ session: session || "", at: Date.now(), items }));
  try {
    for (const legacy of LEGACY_KEYS) localStorage.removeItem(legacy);
  } catch {}
}

/** Drop the chats stored longest ago past the cap. */
function evictChats() {
  try {
    const kept = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(LS_CHAT)) continue;
      let at = 0;
      try {
        at = Number(JSON.parse(localStorage.getItem(key))?.at) || 0;
      } catch {}
      kept.push([at, key]);
    }
    kept.sort((a, b) => b[0] - a[0]);
    for (const [, key] of kept.slice(MAX_CHATS)) localStorage.removeItem(key);
  } catch {}
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

function trackSelection() {
  const selection = window.getSelection();
  const node = selection?.anchorNode;
  const el = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
  if (!el || el.closest(`#${PANEL}`)) return;
  const held = !selection.isCollapsed && el.closest(".message .msg-body, #doc-page");
  selectedRange = held ? selection.getRangeAt(0).cloneRange() : null;
  // A drag reports every step; the label is drawn once a frame, and only while there is a panel to draw it in.
  if (scopeFrame) return;
  scopeFrame = requestAnimationFrame(() => {
    scopeFrame = 0;
    if (isUtilityPanelOpen(PANEL)) paintScope();
  });
}

/** The selection still standing in the page *context* names: a range in its document or its chat, else null. */
function selectionIn(context) {
  const range = selectedRange;
  const root = context.mode === "document" ? $("doc-page") : context.mode === "chat" ? $("chat-messages") : null;
  return range && !range.collapsed && root?.contains(range.commonAncestorContainer) ? range : null;
}

const oneLine = (text, cap) => {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > cap ? `${line.slice(0, cap)}…` : line;
};

/**
 * The text selected in the view, for the request to carry: a document's as its editor holds it, a chat message's as it is
 * saved, with the message's id, so the model can copy it into an edit. A selection across messages is sent as it reads.
 */
function captureSelection(context) {
  const range = selectionIn(context);
  if (!range) return null;
  let text = range.toString();
  let messageId = null;
  if (context.mode === "document") {
    const page = $("doc-page");
    const start = offsetOfPosition(page, range.startContainer, range.startOffset);
    text = serializeEditor(page).content.slice(start, offsetOfPosition(page, range.endContainer, range.endOffset));
  } else {
    const node = range.commonAncestorContainer;
    const message = (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement)?.closest(".message[data-msg-id]");
    messageId = Number(message?.dataset.msgId) || null;
    text = (messageId && rangeInMessage(range, messageId)?.text) || text;
  }
  if (!text.trim()) return null;
  return { text: text.slice(0, MAX_SELECTION_CHARS), ...(messageId ? { message_id: messageId } : {}) };
}

/** The kind of view the empty log suggests requests for. */
function shownView() {
  const context = captureContext();
  return context.entry_id ? "entry" : context.world_id ? "world" : context.character_id ? "character" : context.mode;
}

function toEnd() {
  const log = $("assistant-log");
  if (log && following) log.scrollTop = log.scrollHeight;
  $("assistant-jump")?.classList.toggle("hidden", following);
}

function jumpToEnd() {
  following = true;
  toEnd();
}

function paintLog() {
  const log = $("assistant-log");
  if (!log) return;
  showChat();
  const busy = Boolean(activeOperation());
  const view = shownView();
  suggestedFor = S.assistant.items.length ? null : view;
  if (suggestionEdit && (S.assistant.items.length || suggestionEdit.view !== view)) suggestionEdit = null;
  const suggestions = {
    list: suggestionEdit ? suggestionEdit.rows : suggestionsFor(view),
    editing: Boolean(suggestionEdit),
    custom: view in suggestionLists(),
  };
  // Keyed, so a stream event rebuilds only the rows it changed and a disclosure the reader opened stays open.
  reconcileChildren(log, transcriptEntries(S.assistant.items, renderMessageHtml, { busy, suggestions }));
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
  // The linked world leads the sidebar, and chats with these characters may now use it.
  character_world: () => [
    "character_world",
    async () => {
      await loadCharacters();
      await Promise.all([loadWorlds(), refreshChatWorlds(S.activeConvId)]);
    },
  ],
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
  // The list row, and the page while the document is the one open in the editor.
  document: (r) => [`document:${r.document_id}`, () => refreshDocument(r.document_id)],
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
  if (showChat()) paintLog();
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
  paintScope();
  // The suggestions follow the view, which can change without a notice. Only a changed view redraws them: pressing one
  // focuses it, and redrawing then would replace the button before its click lands.
  if (!S.assistant.items.length && shownView() !== suggestedFor) paintLog();
}

/** The label under the input: the view the next request is about, led by the text selected there, if any. */
function paintScope() {
  const scope = $("assistant-scope");
  if (!scope) return;
  const busy = Boolean(activeOperation());
  const context = busy && run ? run.context : captureContext();
  const selected = busy ? null : selectionIn(context);
  const quote = selected ? oneLine(selected.toString(), SCOPE_SELECTION_CHARS) : "";
  scope.textContent = quote ? `“${quote}” · ${context.label}` : context.label;
  scope.title = busy ? `Working on: ${context.label}` : `Your next message is about: ${context.label}`;
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
 * card of proposed edits remembers for Ask again. *removed* is what an edit cut from the log, its old wording first.
 */
async function stream(chat, body, context, request, removed = []) {
  const items = chat.items;
  let started = false;
  const current = { items, context, request, note: "", status: "", terminal: null };
  run = current;
  const record = beginStream("assistant", wireContext(context), "/assistant/stop");
  persist(chat);
  paint();
  let failure = "";
  try {
    // The options are read at Send, and a document's tools read its saved text: both are as the reader left them first.
    await Promise.all([optionsSaved, context.mode === "document" && saveOpenDocument()]);
    for await (const { event, data } of streamEvents(record, "/assistant/run", body, "Assistant")) {
      started ||= event === "start";
      const failed = event === "error" ? sseError(data, "The assistant failed.").message : "";
      const committed = applyStreamEvent(current, event, data, failed);
      // Committed now, so shown now; the stream carries on meanwhile.
      if (committed.length) refreshResources(committed);
      const answered = event === "assistant_message" && current.items.at(-1)?.kind === "assistant";
      if (PERSISTED_EVENTS.has(event) || answered) persist(chat);
      paint();
    }
  } catch (error) {
    failure = error?.message || "The assistant failed.";
  } finally {
    settleRun(current, { stopped: record.stream.stopping, failure });
    if (removed.length && !started) {
      // The server never took the conversation back, so the log does not either: the edit is open again as typed.
      items.splice(
        items.findLastIndex((item) => item.kind === "user"),
        items.length,
        ...removed,
      );
      Object.assign(removed[0], { editing: true, draft: request });
      if (failure) toast(failure, true);
    } else if (!started) {
      // The server never held this request, so a retry or an edit of it sends it as new.
      const unsent = items.findLast((item) => item.kind === "user");
      if (unsent) unsent.unsent = true;
    }
    persist(chat);
    paint();
    await settle(record);
    if (run === current) run = null;
    // Only after the assistant's own stream has settled, and never after a Stop, which the transcript then says.
    if (current.terminal === "handoff" && current.handoff) {
      if (record.stream.stopping) current.items.push(handoffOutcome(current.handoff, "stopped"));
      else launchHandoff(current);
      trimTranscript(current.items);
      persist(chat);
    }
    paint();
    // Only once the server has settled, so whatever it committed is final: a write whose result never came, or a media job
    // that failed after saving.
    if (
      (current.terminal !== "reply" && current.terminal !== "handoff" && current.terminal !== "proposal") ||
      current.failed
    )
      await refreshEverything();
  }
}

/**
 * Send *text* as a request: a new one, or the new wording of *replaced*, an earlier request of the log. An edit takes the
 * conversation back to where that request began, here and on the server, and goes on from there; only the conversation
 * goes back. What the removed requests changed stays changed, and the cards among them go with their entries, so a
 * change still waiting there is discarded. Either way the request is about the view open now, since the tools act on what
 * is there now, and goes to the chat of the character in view. Returns the run once it has started, or null when nothing
 * was sent.
 */
function submit(text, replaced = null) {
  if (activeOperation() || !text) return null;
  if (text.length > MAX_ITEM_CHARS) {
    toast(`Keep a message to the assistant under ${MAX_ITEM_CHARS} characters.`, true);
    return null;
  }
  if (!requestSendPermission()) return null;
  // Both captured once, here: switching chats or documents while this runs changes the next request, not this one.
  if (showChat()) paintLog();
  const chat = shown;
  const context = captureContext();
  const items = chat.items;
  // Gone when the view turned to another character's chat: the text is then a request of its own.
  const at = replaced ? items.indexOf(replaced) : -1;
  const removed = at >= 0 ? items.splice(at) : [];
  const edit = at >= 0 && !replaced.unsent;
  // A new message takes over any earlier checkpoint, including feedback requesting a revision.
  for (const item of items) if (item.kind === "proposals") delete item.context;
  const requestId = (edit && replaced.requestId) || newId();
  const selection = captureSelection(context);
  const quote = selection ? { selection: oneLine(selection.text, SHOWN_SELECTION_CHARS) } : {};
  items.push({ kind: "user", text, context, requestId, ...quote });
  trimTranscript(items);
  following = true;
  // The changes earlier cards still hold, so this request can build on them; the server reads their texts itself.
  const pending = pendingProposalIds(items);
  // Only the request goes out: the server holds the conversation this session names, every call and result in it.
  const body = { text, context: wireContext(context), session: chat.session, request_id: requestId, edit };
  return stream(chat, { ...body, pending, ...(selection ? { selection } : {}) }, context, text, removed);
}

// Read from the server when the options first open; until then the box shows only a saved prompt.
let defaultInstructions = null;

// Empty when the box holds the default, which the setting stores as empty.
function customInstructions() {
  const text = $("assistant-instructions").value.trim();
  return text && text !== defaultInstructions ? text : "";
}

// The options are settings, read by the server at Send: a run waits for the last change to them to be saved.
let optionsSaved = Promise.resolve();

const offGroups = (switches) =>
  Object.entries(switches || {})
    .flatMap(([id, on]) => (on ? [] : [id]))
    .sort();

function paintOptions() {
  $("assistant-think").checked = Boolean(S.settings?.assistant_reasoning);
  const off = offGroups(S.settings?.assistant_tool_groups);
  for (const box of categoryBoxes()) box.checked = !off.includes(box.dataset.category);
  const searx = $("assistant-searxng-url");
  if (document.activeElement !== searx) searx.value = S.settings?.searxng_url || "";
  const prompt = $("assistant-instructions");
  if (document.activeElement !== prompt) prompt.value = S.settings?.assistant_instructions || defaultInstructions || "";
}

async function loadDefaultInstructions() {
  if (defaultInstructions !== null) return;
  try {
    defaultInstructions = (await api.get("/assistant/instructions")).default.trim();
  } catch {
    return;
  }
  paintOptions();
}

function resetInstructions() {
  $("assistant-instructions").value = defaultInstructions ?? "";
  storeOptions();
}

const categoryBoxes = () => [...$("assistant-options").querySelectorAll("input[data-category]")];

// A group missing from the setting is on, so only those switched off are stored.
function storeOptions() {
  const patch = {};
  const think = $("assistant-think").checked;
  if (think !== Boolean(S.settings?.assistant_reasoning)) patch.assistant_reasoning = think;
  const off = categoryBoxes()
    .flatMap((box) => (box.checked ? [] : [box.dataset.category]))
    .sort();
  if (off.join() !== offGroups(S.settings?.assistant_tool_groups).join()) {
    patch.assistant_tool_groups = Object.fromEntries(off.map((id) => [id, false]));
  }
  const searxngUrl = $("assistant-searxng-url").value.trim();
  if (searxngUrl !== (S.settings?.searxng_url || "")) patch.searxng_url = searxngUrl;
  const instructions = customInstructions();
  if (instructions !== (S.settings?.assistant_instructions || "")) patch.assistant_instructions = instructions;
  if (Object.keys(patch).length) optionsSaved = persistSettings(patch, paintOptions);
}

function toggleOptions() {
  const hidden = $("assistant-options").classList.toggle("hidden");
  $("assistant-gear").setAttribute("aria-expanded", String(!hidden));
  if (hidden) return;
  paintOptions();
  loadDefaultInstructions();
}

function send() {
  const input = $("assistant-input");
  if (!submit(input.value.trim())) return;
  input.value = "";
  fitInput();
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
 * Run one change request over proposals *entries* (`[{ entry, card }]`) of *chat*: Apply, Undo or a revision. Message
 * changes are chat work while they commit, so the chat's delete dialog names them and the server makes a delete wait for them.
 */
async function changing(chat, entries, work) {
  if (!entries.length || activeOperation()) return;
  if (!requestSendPermission()) return;
  const conversations = new Set(
    entries.flatMap(({ entry }) => (entry.target?.kind === "message" ? [entry.target.conversation_id] : [])),
  );
  const ops = conversations.size
    ? [...conversations].map((conversationId) => begin(APPLY_KIND, { conversationId }))
    : [begin(APPLY_KIND, {})];
  paint();
  try {
    await work();
  } finally {
    for (const op of ops) finish(op);
  }
  persist(chat);
  paint();
}

const DOCUMENT_UNSAVED =
  "Nothing was changed: the open document is generating, or its text could not be saved. Try again once it shows Saved.";

/**
 * Whether the stored text of *entries*' targets is the text on screen: a document open in the editor is saved first, so
 * a change is compared with what the reader sees and the editor's next save does not meet it as a conflict.
 */
async function documentsSaved(entries) {
  const open = entries.some(
    ({ entry }) => entry.target?.kind === "document" && entry.target.document_id === S.activeDocId,
  );
  return !open || (await saveOpenDocument());
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

function logOutcome(chat, outcome) {
  if (!outcome?.summaries.length) return;
  chat.items.push({ kind: "outcome", reason: "applied", text: outcome.summaries.join(" ") });
  trimTranscript(chat.items);
  persist(chat);
}

/**
 * Record what a change request settled: its summary in the transcript, unless the request resuming the run says it
 * (*resuming*), and the resources it changed read again.
 */
async function settled(chat, outcome, fallback, resuming = false) {
  if (!resuming) logOutcome(chat, outcome);
  await refreshResources(outcome ? outcome.resources : fallback);
}

/**
 * Resume the same chat and captured view, with the user's decision as an ordinary, visible request. Returns null when
 * nothing was sent.
 */
function continueProposal(chat, card, decision, end = false) {
  if (!card || activeOperation()) return null;
  if (!requestSendPermission()) return null;
  const continuing = !!card.context && !end;
  const context = card.context || captureContext();
  delete card.context;
  // The prompt says to carry on with the request or, with nothing left, answer briefly; only an end needs saying.
  const text = continuing ? decision : `${decision} That's all for this request.`;
  const requestId = newId();
  chat.items.push({ kind: "user", text, context, requestId });
  trimTranscript(chat.items);
  following = true;
  return stream(
    chat,
    {
      text,
      context: wireContext(context),
      session: chat.session,
      request_id: requestId,
      pending: pendingProposalIds(chat.items),
    },
    context,
    card.request,
  );
}

/**
 * Apply pending changes by their ids alone, each answered with its own verdict. A busy chat leaves its change pending to try
 * again, as does an Apply that ends without verdicts, which also reads their targets again.
 */
async function apply(el) {
  const chat = shown;
  const entries = pendingOf(el);
  const card = entries[0]?.card;
  let applied = null;
  let decision = "";
  await changing(chat, entries, async () => {
    delete card.note;
    if (!(await documentsSaved(entries))) {
      card.note = DOCUMENT_UNSAVED;
      return;
    }
    try {
      const results = (
        await api.post("/assistant/apply", { proposal_ids: entries.map(({ entry }) => entry.proposalId) })
      )?.results;
      applied = applyVerdicts(card, results);
      if (Array.isArray(results) && results.length && !card.proposals.some((entry) => entry.status === "pending"))
        decision = decisionText(
          entries.map(({ entry }) => entry),
          results,
        );
    } catch {
      card.note = "Apply did not finish. Try again.";
    }
  });
  if (card)
    await settled(
      chat,
      applied,
      entries.flatMap(({ entry }) => (entry.target ? [entry.target] : [])),
      !!decision,
    );
  if (decision) {
    // A deleted target may have been the captured view itself; resume from a view the server can still resolve.
    if (
      card.context &&
      applied.resources.some(
        (resource) =>
          resource.deleted &&
          ["conversation_id", "character_id", "document_id", "world_id", "entry_id"].some(
            (key) => resource[key] && resource[key] === card.context[key],
          ),
      )
    )
      card.context = { mode: "library", label: "Library" };
    const resumed = continueProposal(chat, card, decision);
    if (!resumed) {
      logOutcome(chat, applied);
      paint();
    }
    await resumed;
  }
}

/** Put back the text an applied edit replaced; it is pending again afterwards, to apply or discard. */
async function undo(el) {
  const chat = shown;
  const found = findProposal(chat.items, el.dataset.proposal || "");
  if (found?.entry.status !== "applied") return;
  let undone = null;
  await changing(chat, [found], async () => {
    if (!(await documentsSaved([found]))) {
      found.entry.note = DOCUMENT_UNSAVED;
      return;
    }
    try {
      undone = undoVerdicts(
        found.card,
        (await api.post("/assistant/undo", { proposal_ids: [found.entry.proposalId] }))?.results,
      );
    } catch {
      found.entry.note = "Undo did not finish. Try again.";
    }
  });
  await settled(chat, undone, found.entry.target ? [found.entry.target] : []);
}

/** Skip a proposal and report the decision immediately, or end the remaining request. */
function cancel(el, end = false) {
  if (activeOperation()) return;
  const chat = shown;
  const entries = pendingOf(el);
  const card = entries[0]?.card;
  const decision = decisionText(entries.map(({ entry }) => entry));
  for (const { entry } of entries) {
    entry.status = "cancelled";
    delete entry.editing;
    delete entry.proposalId;
  }
  persist(chat);
  paint();
  return continueProposal(chat, card, decision, end);
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
  const chat = shown;
  const found = findProposal(chat.items, el.dataset.proposal || "");
  if (found?.entry.status !== "pending") return;
  const { entry } = found;
  await changing(chat, [found], async () => {
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

function suggestionLists() {
  customSuggestions ??= parseSuggestions(stored(LS_SUGGESTIONS));
  return customSuggestions;
}

function suggestionsFor(view) {
  return suggestionLists()[view] ?? defaultSuggestions(view);
}

/** Keep *rows* as *view*'s suggestions; a list that matches the defaults is dropped, so it follows them. */
function saveSuggestions(view, rows) {
  const lists = suggestionLists();
  const list = cleanSuggestions(rows);
  if (JSON.stringify(list) === JSON.stringify(cleanSuggestions(defaultSuggestions(view)))) delete lists[view];
  else lists[view] = list;
  if (Object.keys(lists).length) store(LS_SUGGESTIONS, JSON.stringify(lists));
  else {
    try {
      localStorage.removeItem(LS_SUGGESTIONS);
    } catch {}
  }
  const reset = $("assistant-log")?.querySelector('[data-wf-action="assistant:suggestionsReset"]');
  if (reset) reset.disabled = !(view in lists);
}

const suggestionBoxes = () => [...($("assistant-log")?.querySelectorAll(".asst-suggest-text") || [])];

/** The editor's rows as the page holds them now, blanks included. */
function editedRows() {
  return suggestionBoxes().map((box) => box.value);
}

function editSuggestions() {
  const view = shownView();
  const rows = [...suggestionsFor(view)];
  suggestionEdit = { view, rows: rows.length ? rows : [""] };
  paint();
}

/** Redraw the editor with its rows changed by *change*, saving the result. */
function changeSuggestions(change) {
  if (!suggestionEdit) return;
  const rows = editedRows();
  change(rows);
  suggestionEdit.rows = rows;
  saveSuggestions(suggestionEdit.view, rows);
  paint();
}

function addSuggestion() {
  changeSuggestions((rows) => rows.push(""));
  suggestionBoxes().at(-1)?.focus();
}

function removeSuggestion(el) {
  const row = el.closest(".asst-suggest-row");
  const index = [...row.parentElement.querySelectorAll(".asst-suggest-row")].indexOf(row);
  changeSuggestions((rows) => rows.splice(index, 1));
}

function resetSuggestions() {
  if (!suggestionEdit) return;
  suggestionEdit.rows = [...defaultSuggestions(suggestionEdit.view)];
  saveSuggestions(suggestionEdit.view, suggestionEdit.rows);
  // The defaults may be the very rows the editor was drawn with, which a repaint would keep, typing and all.
  $("assistant-log")?.querySelector(".asst-suggest-editor")?.remove();
  paint();
}

function finishSuggestions() {
  if (!suggestionEdit) return;
  saveSuggestions(suggestionEdit.view, editedRows());
  suggestionEdit = null;
  paint();
}

function suggestionDraft(e) {
  if (!suggestionEdit) return;
  if (e.type === "input") saveSuggestions(suggestionEdit.view, editedRows());
  else if (e.key === "Escape") {
    // The edit is what Escape closes, not the panel behind it.
    e.stopPropagation();
    finishSuggestions();
  }
}

/** Empty the chat on screen, here and on the server; its next request starts a conversation the server has never seen. */
function clear() {
  if (activeOperation()) return;
  showChat();
  const session = shown.session;
  shown.items.length = 0;
  shown.session = newId();
  persist(shown);
  paint();
  api
    .post("/assistant/forget", { session })
    .catch((error) => console.warn("Failed to clear the assistant chat:", error));
}

/** The character the page is about: a solo chat's or the library's, a group as one of its own, null where there is none. */
function characterInView() {
  if (S.documentMode) return null;
  const conv = S.conversations.find((item) => item.id === S.activeConvId);
  if (conv) return conv.kind === "group" ? `group:${conv.id}` : conv.character_card_id || null;
  return S.activeCharId || null;
}

export function initAssistant() {
  const panel = $(PANEL);
  if (!panel) return;
  adoptLegacyChat();
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
    endRequest: (el) => cancel(el, true),
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
    editSuggestions: () => editSuggestions(),
    suggestionDraft: (_el, e) => suggestionDraft(e),
    suggestionAdd: () => addSuggestion(),
    suggestionRemove: (el) => removeSuggestion(el),
    suggestionsReset: () => resetSuggestions(),
    suggestionsDone: () => finishSuggestions(),
    retry: (el) => {
      const item = requestOf(el);
      if (item) submit(item.text, item);
    },
    jump: () => jumpToEnd(),
    focusInput: () => $("assistant-input").focus(),
    options: () => toggleOptions(),
    option: () => storeOptions(),
    resetInstructions: () => resetInstructions(),
  });
  const log = $("assistant-log");
  initDragReorder(log, {
    itemSelector: ".asst-suggest-row",
    handleSelector: ".asst-suggest-grip",
    onReorder: () => suggestionEdit && saveSuggestions(suggestionEdit.view, editedRows()),
  });
  log.addEventListener("scroll", () => {
    if (!log.clientHeight) return;
    following = log.scrollHeight - log.clientHeight - log.scrollTop <= FOLLOW_SLACK_PX;
    $("assistant-jump").classList.toggle("hidden", following);
  });
  document.addEventListener("selectionchange", trackSelection);
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
  subscribe("operations", paintControls);
  subscribe("conversations", paintControls);
  subscribe("documents", paintControls);
  subscribe("lorebook", paintControls);
  // Opening Tools or the Inspector closes the panel, as do mobile Back and a tap beside it; each lands here.
  watchUtilityPanel(PANEL, (open) => {
    S.assistant.open = open;
  });
  paint();
  restoreUtilityPanel(PANEL, revealPanel);
}
