// Document mode — everything stateful: list/CRUD, mode toggle, autosave,
// generation, shortcuts. Imports the pure editor model (document_editor.js) for
// all DOM↔string work so the invariant-heavy core stays separable/testable.

import { api } from "./api.js";
import {
  caretAfter,
  computeCaretOffset,
  installPlainTextGuards,
  renderEditor,
  serializeEditor,
  setCaretOffset,
} from "./document_editor.js";
import {
  addDelta,
  addToken,
  beginRun,
  clearPending,
  commitRun,
  hideProbPopup,
  initDocProbs,
  runAt,
  swapRunToken,
  syncContent,
} from "./document_probs.js";
import { showConfirmModal } from "./modal.js";
import { S } from "./state.js";
import { $, esc, escAttr, formatRelativeDate, toast } from "./utils.js";

const LS_MODE = "orb-doc-mode";
const LS_ACTIVE = "orb-active-doc";
const LS_ASSISTED = "orb-doc-assisted"; // Raw (0) ⇄ Assisted (1) prompting strategy
const LS_PROBS = "orb-doc-probs"; // capture per-token alternatives (0/1)
const SAVE_DEBOUNCE_MS = 1500;
const STREAM_FLUSH_MS = 5000; // interval flush while streaming → tab crash loses ≤5s
const HISTORY_DEBOUNCE_MS = 800; // typing pause → one undo step per burst
const HISTORY_MAX = 100;
const MOBILE = window.matchMedia("(max-width: 900px)"); // matches document.css breakpoint
const DOC_LIMIT = 10; // documents shown before the list collapses behind "show all"

let _docSearch = "";
let _docsExpanded = false;
let saveTimer = null;
let flushInterval = null;
let anchorTextNode = null; // text node tokens stream into during generation
let docAssisted = false; // false = Raw (verbatim), true = Assisted (### macros → chat template)
let docProbsOn = false; // capture per-token alternatives (mikupad-style token swapping)

// ── Small DOM helpers ────────────────────────────────────────────────────────
function setSaveState(text) {
  const el = $("doc-save-state");
  if (el) el.textContent = text;
}
function setUndoEnabled(on) {
  const b = $("doc-undo-btn");
  if (b) b.disabled = !on;
}
function swapGenButtons(streaming) {
  $("doc-generate-btn")?.classList.toggle("hidden", streaming);
  $("doc-stop-btn")?.classList.toggle("hidden", !streaming);
}
function updateTokenCount() {
  const page = $("doc-page");
  const len = page ? serializeEditor(page).content.length : 0;
  const el = $("doc-token-count");
  if (el) el.textContent = `~${Math.round(len / 4)} tokens`; // mirrors CHARS_PER_TOKEN=4
}

// ── Undo history. One chronological timeline for typing AND generation, since
// native contenteditable undo can't survive renderEditor rebuilds and never sees
// streamed tokens. ponytail: O(doc) snapshots {content, spans}, cap 100;
// switch to diffs if docs get huge.
let docHistory = [];
let docHistoryIndex = -1;
let docHistoryTimer = null;

function updateUndoButton() {
  setUndoEnabled(!S.docStreaming && (docHistoryIndex > 0 || docHistoryTimer !== null));
}

function docHistoryReset() {
  clearTimeout(docHistoryTimer);
  docHistoryTimer = null;
  docHistory = [];
  docHistoryIndex = -1;
  updateUndoButton();
}

// Snapshot the current editor state; no-op if content/spans are unchanged.
function docCheckpoint() {
  clearTimeout(docHistoryTimer);
  docHistoryTimer = null;
  const page = $("doc-page");
  if (!page || !S.activeDocId) return;
  const { content, spans } = serializeEditor(page);
  const cur = docHistory[docHistoryIndex];
  if (!cur || cur.content !== content || JSON.stringify(cur.spans) !== JSON.stringify(spans)) {
    docHistory.length = docHistoryIndex + 1; // truncate the redo tail
    docHistory.push({ content, spans });
    if (docHistory.length > HISTORY_MAX) docHistory.shift();
    docHistoryIndex = docHistory.length - 1;
  }
  updateUndoButton();
}

function docRestore(snap) {
  const page = $("doc-page");
  // Caret goes to where the current content diverges from the target — the edit
  // being undone/redone — not a stored position (which drifted to end-of-doc for
  // snapshots taken while focus was off the editor).
  const before = serializeEditor(page).content;
  let caret = 0;
  const max = Math.min(before.length, snap.content.length);
  while (caret < max && before[caret] === snap.content[caret]) caret++;
  renderEditor(page, snap.content, snap.spans);
  if (S.activeDocId) syncContent(S.activeDocId, snap.content); // remap token-runs across the undo/redo jump
  setCaretOffset(page, caret);
  if (MOBILE.matches) page.blur(); // addRange refocuses the box → keyboard pops while reading; kill it on mobile
  // Programmatic render fires no input event → same bookkeeping as onEditorInput.
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();
  scheduleSave();
  updateUndoButton();
}

export function docUndo() {
  if (S.docStreaming || !S.activeDocId) return;
  docCheckpoint(); // pending typing becomes its own (redoable) step
  if (docHistoryIndex <= 0) return;
  docHistoryIndex--;
  docRestore(docHistory[docHistoryIndex]);
}

export function docRedo() {
  if (S.docStreaming || !S.activeDocId) return;
  docCheckpoint(); // pending typing truncates the redo tail (standard behavior)
  if (docHistoryIndex >= docHistory.length - 1) return;
  docHistoryIndex++;
  docRestore(docHistory[docHistoryIndex]);
}

// ── Mode toggle (class on #app; no router). ──────────────────────────────────
function setDocumentMode(on) {
  S.documentMode = on;
  document.getElementById("app")?.classList.toggle("document-mode", on);
  localStorage.setItem(LS_MODE, on ? "1" : "0");
  if (on) {
    // Documents is the primary section here; expand it (ships collapsed for chat).
    const body = $("documents-section");
    body?.classList.remove("collapsed");
    body?.previousElementSibling?.querySelector(".arrow")?.classList.remove("collapsed");
  }
  const btn = $("mode-switch-btn");
  if (btn) {
    btn.textContent = on ? "📄" : "💬";
    btn.title = on ? "Switch to Chat mode" : "Switch to Document mode";
  }
}

export function toggleDocumentMode() {
  if (S.docStreaming) {
    toast("Stop generation first", true);
    return;
  }
  const entering = !S.documentMode;
  if (!entering && S.docDirty) flushSave();
  setDocumentMode(entering);
}

// ── Prompting-strategy toggle (Raw ⇄ Assisted), persisted like documentMode. ──
// Raw sends the document verbatim (text mode) — the user types chat-template
// tokens. Assisted interprets ### SYSTEM/USER/ASSISTANT line macros and renders
// through the model's own template. Sent as `assisted` in the generate POST.
function reflectAssistedToggle() {
  $("doc-mode-raw")?.classList.toggle("active", !docAssisted);
  $("doc-mode-assisted")?.classList.toggle("active", docAssisted);
  // Show only the help for the active mode + fill the real token cap.
  const assisted = $("doc-help-assisted");
  if (assisted) assisted.hidden = !docAssisted;
  const raw = $("doc-help-raw");
  if (raw) raw.hidden = docAssisted;
  const summary = $("doc-help-summary");
  if (summary) summary.textContent = `How to prompt (${docAssisted ? "Assisted" : "Raw"})`;
  const cap = $("doc-help-maxtok");
  if (cap) {
    const cfg = S.modelConfigs?.find((m) => m.id === S.activeModelConfigId);
    cap.textContent = cfg?.max_tokens || 512; // 512 = server fallback in DocumentContinuer
  }
}

export function setDocAssisted(on) {
  docAssisted = !!on;
  localStorage.setItem(LS_ASSISTED, docAssisted ? "1" : "0");
  reflectAssistedToggle();
}

// ── Per-token alternatives toggle (mikupad-style token swapping), persisted. ──
// Opt-in: logprobs cost generation speed on llama.cpp, and providers that can't
// supply them degrade to no-popup. Sent as `token_probs` in the generate POST.
function reflectProbsToggle() {
  $("doc-probs-btn")?.classList.toggle("active", docProbsOn);
}

export function setDocProbs(on) {
  docProbsOn = !!on;
  localStorage.setItem(LS_PROBS, docProbsOn ? "1" : "0");
  reflectProbsToggle();
}

// ── Documents list. ──────────────────────────────────────────────────────────
const _docItemHtml = (
  d,
) => `<div class="doc-item${S.activeDocId === d.id ? " active" : ""}" onclick="openDocument('${d.id}')">
      <div class="doc-item-info">
        <div class="doc-item-name">${esc(d.title)}</div>
        <div class="doc-item-meta">${formatRelativeDate(d.updated_at)}</div>
      </div>
      <div class="doc-item-actions">
        <button onclick="event.stopPropagation();renameDocument('${d.id}')" title="Rename">✏</button>
        <button class="del-btn" onclick="event.stopPropagation();deleteDocument('${d.id}')" title="Delete">✕</button>
      </div>
    </div>`;

export function renderDocuments() {
  const list = $("documents-list");
  if (!list) return;

  // Search box only appears once the list outgrows the default view (mirrors Worlds).
  const searchWrap = $("documents-search-wrap");
  if (searchWrap) {
    searchWrap.style.display = S.documents.length > DOC_LIMIT || _docSearch.trim() ? "" : "none";
  }
  const searchInp = $("documents-search");
  if (searchInp && searchInp.value !== _docSearch) searchInp.value = _docSearch;

  if (!S.documents.length) {
    list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No documents yet.</div>';
    return;
  }

  const q = _docSearch.trim().toLowerCase();
  const matched = q ? S.documents.filter((d) => d.title.toLowerCase().includes(q)) : S.documents;
  if (q && !matched.length) {
    list.innerHTML = `<div class="worlds-empty">No documents match “${esc(_docSearch.trim())}”</div>`;
    return;
  }

  const collapsed = !q && !_docsExpanded && matched.length > DOC_LIMIT;
  const shown = collapsed ? matched.slice(0, DOC_LIMIT) : matched;
  let html = shown.map(_docItemHtml).join("");
  if (!q) {
    if (collapsed) {
      html += `<button type="button" class="worlds-more" onclick="expandDocs()">+${matched.length - DOC_LIMIT} more — show all</button>`;
    } else if (_docsExpanded && matched.length > DOC_LIMIT) {
      html += `<button type="button" class="worlds-more" onclick="collapseDocs()">Show less</button>`;
    }
  }
  list.innerHTML = html;
}

export function onDocSearch(value) {
  _docSearch = value;
  renderDocuments();
}

export function expandDocs() {
  _docsExpanded = true;
  renderDocuments();
}

export function collapseDocs() {
  _docsExpanded = false;
  renderDocuments();
}

// Upsert a document into the sidebar list and re-sort by updated_at DESC (mirrors
// the backend order), from a full row returned by create/update.
function updateDocInList(row) {
  const entry = { id: row.id, title: row.title, created_at: row.created_at, updated_at: row.updated_at };
  const i = S.documents.findIndex((d) => d.id === row.id);
  if (i >= 0) S.documents[i] = entry;
  else S.documents.unshift(entry);
  S.documents.sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0));
  renderDocuments();
}

export async function loadDocuments() {
  S.documents = await api.get("/documents");
  renderDocuments();
  // Restore persisted mode + active doc on boot.
  if (localStorage.getItem(LS_MODE) === "1") {
    const savedId = localStorage.getItem(LS_ACTIVE);
    if (savedId && S.documents.some((d) => d.id === savedId)) await openDocument(savedId);
    else setDocumentMode(true);
  }
}

export async function createDocument() {
  try {
    const doc = await api.post("/documents", {});
    updateDocInList(doc);
    await openDocument(doc.id);
  } catch (e) {
    toast(`Create failed: ${e.message}`, true);
  }
}

export async function openDocument(id) {
  if (S.docStreaming) {
    toast("Stop generation first", true);
    return;
  }
  hideProbPopup(); // switching docs → drop any popup from the previous one
  if (S.activeDocId && S.activeDocId !== id && S.docDirty) await flushSave();
  let doc;
  try {
    doc = await api.get(`/documents/${id}`);
  } catch (e) {
    toast(`Failed to open: ${e.message}`, true);
    return;
  }
  S.activeDocId = id;
  localStorage.setItem(LS_ACTIVE, id);
  $("app")?.classList.add("doc-open"); // gates empty-state text + rename button
  if (!S.documentMode) setDocumentMode(true);

  const page = $("doc-page");
  renderEditor(page, doc.content, doc.generated_spans || []);
  syncContent(id, doc.content); // realign any session token-runs to the loaded content
  page.setAttribute("contenteditable", "true");
  $("doc-generate-btn").disabled = false;
  $("doc-title-text").textContent = doc.title;
  docHistoryReset();
  docCheckpoint(); // baseline snapshot
  S.docDirty = false;
  setSaveState("Saved");
  updateTokenCount();
  renderDocuments();
}

function clearEditor() {
  S.activeDocId = null;
  localStorage.removeItem(LS_ACTIVE);
  $("app")?.classList.remove("doc-open");
  const page = $("doc-page");
  if (page) {
    page.textContent = "";
    page.setAttribute("contenteditable", "false");
  }
  $("doc-title-text").textContent = "No document";
  $("doc-generate-btn").disabled = true;
  docHistoryReset();
  S.docDirty = false;
  setSaveState("");
  updateTokenCount();
}

export function renameDocument(id) {
  const doc = S.documents.find((d) => d.id === id);
  if (!doc) return;
  showConfirmModal(
    {
      title: "Rename Document",
      message: "",
      confirmText: "Save",
      confirmClass: "",
      extraHtml: `<div class="field"><input id="doc-rename-input" type="text" autofocus maxlength="200" value="${escAttr(doc.title)}" style="width:100%;padding:8px"></div>`,
    },
    async () => {
      const val = $("doc-rename-input")?.value.trim();
      if (!val) return;
      try {
        const row = await api.put(`/documents/${id}`, { title: val });
        updateDocInList(row);
        if (S.activeDocId === id) $("doc-title-text").textContent = row.title;
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

export function renameActiveDocument() {
  if (S.activeDocId) renameDocument(S.activeDocId);
}

export function deleteDocument(id) {
  if (S.docStreaming) {
    toast("Stop generation first", true);
    return;
  }
  const doc = S.documents.find((d) => d.id === id);
  showConfirmModal(
    {
      title: "Delete Document",
      message: `Delete "${esc(doc ? doc.title : "this document")}"? This cannot be undone.`,
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del(`/documents/${id}`);
        S.documents = S.documents.filter((d) => d.id !== id);
        if (S.activeDocId === id) clearEditor();
        renderDocuments();
        toast("Deleted");
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

// ── Autosave. Content + spans always travel together (backend validator). ────
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => flushSave(), SAVE_DEBOUNCE_MS);
}

async function flushSave({ keepalive = false } = {}) {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!S.activeDocId || !S.docDirty) return;
  const page = $("doc-page");
  const { content, spans } = serializeEditor(page);
  S.docDirty = false;
  if (keepalive) {
    // beforeunload: fire-and-forget so tokens/edits aren't lost on tab close.
    fetch(`/api/documents/${S.activeDocId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, generated_spans: spans }),
      keepalive: true,
    }).catch(() => {});
    return;
  }
  setSaveState("Saving…");
  try {
    const row = await api.put(`/documents/${S.activeDocId}`, { content, generated_spans: spans });
    setSaveState("Saved");
    updateDocInList(row);
  } catch {
    S.docDirty = true; // let the next debounce retry
    setSaveState("Save failed");
  }
}

function onEditorInput() {
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();
  if (S.activeDocId) syncContent(S.activeDocId, serializeEditor($("doc-page")).content); // keep token offsets aligned
  scheduleSave();
  clearTimeout(docHistoryTimer);
  docHistoryTimer = setTimeout(docCheckpoint, HISTORY_DEBOUNCE_MS);
  updateUndoButton(); // pending burst is already undoable
}

// ── Generation. ──────────────────────────────────────────────────────────────
function startFlushInterval() {
  stopFlushInterval();
  flushInterval = setInterval(() => {
    if (!S.activeDocId) return;
    const { content, spans } = serializeEditor($("doc-page"));
    api.put(`/documents/${S.activeDocId}`, { content, generated_spans: spans }).catch(() => {});
  }, STREAM_FLUSH_MS);
}
function stopFlushInterval() {
  if (flushInterval) {
    clearInterval(flushInterval);
    flushInterval = null;
  }
}

// Smart autoscroll (mirrors chat's): follow the stream while the caret's at the
// bottom; wheel/touch-up cuts it instantly, scrolling back to the bottom re-arms.
let docAutoscroll = true;
function initDocAutoscroll() {
  const scroll = $("doc-editor-scroll");
  if (!scroll) return;
  const THRESHOLD = 40;
  let touchY = 0;
  scroll.addEventListener(
    "wheel",
    (e) => {
      if (e.deltaY < 0) docAutoscroll = false;
    },
    { passive: true },
  );
  scroll.addEventListener(
    "touchstart",
    (e) => {
      touchY = e.touches[0].clientY;
    },
    { passive: true },
  );
  scroll.addEventListener(
    "touchmove",
    (e) => {
      if (e.touches[0].clientY > touchY) docAutoscroll = false;
    },
    { passive: true },
  );
  scroll.addEventListener("scroll", () => {
    docAutoscroll = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight <= THRESHOLD;
  });
}
function scrollAnchorIntoView() {
  const scroll = $("doc-editor-scroll");
  if (scroll && docAutoscroll) scroll.scrollTop = scroll.scrollHeight;
}

// Dedicated SSE reader (do NOT reuse the chat-coupled processSSEStream). Handles
// the wire facts of the backend's _sse_stream: string data has \n escaped (token /
// error), dict data is raw JSON (probs — must NOT be unescaped or the JSON breaks),
// ": keepalive" comment frames appear during silent stretches, and errors arrive
// in-band as `event: error`.
async function readDocSSE(resp, onToken, onProbs, onError) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    while (true) {
      const idx = buf.indexOf("\n\n");
      if (idx === -1) break;
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (!frame || frame.startsWith(":")) continue; // keepalive comment
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      // Unescape ONLY string-data channels; probs data is JSON where a real
      // newline inside a token would already be `\n`-escaped by json.dumps —
      // unescaping it here would corrupt the payload.
      if (event === "token") onToken(data.replace(/\\n/g, "\n"));
      else if (event === "probs") {
        try {
          onProbs(JSON.parse(data));
        } catch {
          /* malformed probs frame → skip, never break the text stream */
        }
      } else if (event === "error") {
        onError(data.replace(/\\n/g, "\n"));
        return;
      } else if (event === "done") return;
    }
  }
}

export async function docGenerate() {
  if (!S.activeDocId || S.docStreaming) return;
  const page = $("doc-page");
  hideProbPopup(); // no stale alternatives popup over a regenerating region
  if (S.docDirty) await flushSave();
  docCheckpoint(); // pre-generation state — Ctrl+Z after gen lands here

  // Split in the string domain: caret offset → prompt is the prefix before it.
  const caret = computeCaretOffset(page);
  const { content, spans } = serializeEditor(page);
  const prompt = content.slice(0, caret);
  beginRun(S.activeDocId, caret); // token records (if any) collect against this run

  // Re-render with an empty streaming anchor at the caret (splits a straddling span).
  const anchor = renderEditor(page, content, spans, caret);
  anchorTextNode = anchor.firstChild;

  page.setAttribute("contenteditable", "false");
  page.classList.add("generating");
  docAutoscroll = true; // each generation starts by following the stream
  S.docStreaming = true;
  S.docAbortController = new AbortController();
  swapGenButtons(true);
  updateUndoButton(); // greyed while streaming
  startFlushInterval();

  try {
    const resp = await fetch(`/api/documents/${S.activeDocId}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, assisted: docAssisted, token_probs: docProbsOn }),
      signal: S.docAbortController.signal,
    });
    if (!resp.ok) throw new Error(await resp.text());
    await readDocSSE(
      resp,
      (delta) => {
        anchorTextNode.appendData(delta);
        addDelta(delta); // positions the chunk's probs records within the run
        updateTokenCount(); // live count instead of a static "Generating" label
        scrollAnchorIntoView();
      },
      (rec) => addToken(rec), // per-token alternatives → side-store
      (msg) => toast(msg || "Generation error", true),
    );
  } catch (e) {
    if (e.name !== "AbortError") toast(`Generation failed: ${e.message}`, true);
  } finally {
    finalizeGeneration();
  }
}

function finalizeGeneration() {
  stopFlushInterval();
  S.docStreaming = false;
  S.docAbortController = null;
  anchorTextNode = null;
  const page = $("doc-page");
  page.setAttribute("contenteditable", "true");
  page.classList.remove("generating");
  swapGenButtons(false);

  const anchor = page.querySelector(".gen-active");
  let committedText = null;
  if (anchor) {
    anchor.classList.remove("gen-active");
    if (!anchor.textContent) {
      anchor.remove(); // empty span (immediate EOS / abort before any token)
      clearPending();
      toast("No text was generated");
    } else {
      committedText = anchor.textContent;
      caretAfter(anchor);
    }
  } else {
    clearPending();
  }
  // Sync the side-store to the post-generation content FIRST (shifts any
  // pre-existing runs for the inserted text), THEN commit the fresh run in those
  // same coordinates — committing first would let the remap double-shift it.
  if (S.activeDocId) {
    syncContent(S.activeDocId, serializeEditor(page).content);
    if (committedText != null) commitRun(S.activeDocId, committedText);
  }
  if (MOBILE.matches) $("doc-page").blur(); // no keyboard pop on Stop / gen-end while reading
  S.docDirty = true;
  flushSave(); // immediate save at stream end
  updateTokenCount();
  docCheckpoint(); // post-generation snapshot (no-op if nothing streamed)
}

export function docStop() {
  if (!S.docStreaming) return;
  S.docAbortController?.abort();
  fetch(`/api/documents/${S.activeDocId}/stop`, { method: "POST" }).catch(() => {});
}

// Swap a generated token for one of its alternatives (mikupad-style), then
// auto-continue from that point. Passed to initDocProbs as ctx.requestSwap.
// Everything after the swapped token is deleted — the continuation is being
// rewritten from the swap point, so stale tail text (even user-typed) goes;
// docCheckpoint makes the whole swap one Ctrl+Z step.
function docSwapToken(run, tokenIndex, alt) {
  if (S.docStreaming || !S.activeDocId) return;
  const page = $("doc-page");
  const { content, spans } = serializeEditor(page);

  // Token start = run start + the lengths of the tokens before it.
  let tokStart = run.start;
  for (let i = 0; i < tokenIndex; i++) tokStart += run.tokens[i].text.length;
  // Revalidate: the run must still tile the current content (no edit since hover).
  // runAt drops the run and returns null on a mismatch → bail without touching text.
  if (runAt(S.activeDocId, tokStart, content) !== run) {
    hideProbPopup();
    return;
  }
  hideProbPopup();
  docCheckpoint(); // pre-swap undo step

  // Truncate at the swap point: keep content before the token, then the
  // alternative; everything after is deleted. Spans clip at tokStart and the
  // swapped token itself stays tinted as generated text.
  const newContent = content.slice(0, tokStart) + alt.t;
  const newSpans = spans
    .filter((s) => s.start < tokStart)
    .map((s) => ({ start: s.start, end: Math.min(s.end, tokStart) }));
  newSpans.push({ start: tokStart, end: newContent.length });
  swapRunToken(S.activeDocId, run, tokenIndex, alt, newContent);

  renderEditor(page, newContent, newSpans);
  setCaretOffset(page, tokStart + alt.t.length); // caret right after the swapped token
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();

  // Continue generation: docGenerate slices prompt = content up to the caret, so
  // the swapped token is in the prompt and a fresh run begins exactly at the cut.
  docGenerate();
}

// ── Shortcuts: Ctrl/Cmd+Enter generates, Esc stops, Ctrl/Cmd+Z / +Shift+Z / +Y
// undo/redo. Scoped to document mode and no open modal so they can't collide
// with modal.js / mobile.js Esc handlers.
function isOtherEditableTarget(t) {
  return t instanceof Element && t.id !== "doc-page" && (t.matches("input, textarea, select") || t.isContentEditable);
}

function onDocKeydown(e) {
  if (!S.documentMode) return;
  if ($("modal-root")?.innerHTML) return;
  const key = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    docGenerate();
  } else if (e.key === "Escape" && S.docStreaming) {
    e.preventDefault();
    docStop();
  } else if ((e.ctrlKey || e.metaKey) && !e.altKey && (key === "z" || key === "y")) {
    if (isOtherEditableTarget(e.target)) return; // other text boxes keep native undo
    e.preventDefault();
    if (key === "y" || e.shiftKey) docRedo();
    else docUndo();
  }
}

export function initDocumentMode() {
  const page = $("doc-page");
  if (!page) return;
  docAssisted = localStorage.getItem(LS_ASSISTED) === "1";
  reflectAssistedToggle();
  docProbsOn = localStorage.getItem(LS_PROBS) === "1";
  reflectProbsToggle();
  // Re-read the token cap on open — modelConfigs may load / change after init.
  $("doc-help")?.addEventListener("toggle", (e) => e.target.open && reflectAssistedToggle());
  installPlainTextGuards(page);
  initDocAutoscroll();
  // Hover-to-inspect / click-to-swap per-token alternatives. Context is injected
  // (S-free module): current doc, streaming guard, and the swap action.
  initDocProbs(page, {
    getDocId: () => S.activeDocId,
    isStreaming: () => S.docStreaming,
    requestSwap: docSwapToken,
  });
  page.addEventListener("input", onEditorInput);
  // Context-menu Undo/Redo must hit our history, never the orphaned native stack.
  page.addEventListener("beforeinput", (e) => {
    if (e.inputType === "historyUndo" || e.inputType === "historyRedo") {
      e.preventDefault();
      if (e.inputType === "historyUndo") docUndo();
      else docRedo();
    }
  });
  page.addEventListener("blur", () => {
    if (S.docDirty) flushSave();
  });
  document.addEventListener("keydown", onDocKeydown);
  window.addEventListener("beforeunload", () => {
    if (S.docDirty && S.activeDocId) flushSave({ keepalive: true });
  });
}
