import { api } from "./api.js";
import { initDocAudit, onGenerationEnd, renderDocAuditPane } from "./document_audit.js";
import {
  caretAfter,
  computeCaretOffset,
  ensureTrailingFiller,
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
import { CLOSE_ICON, EDIT_ICON } from "./icons.js";
import { confirmDelete, showConfirmModal } from "./modal.js";
import { isUtilityPanelOpen } from "./panels.js";
import { createScrollFollow } from "./scroll_follow.js";
import { renderToolsPanel } from "./settings.js";
import { sseEvents, streamPost, unescapeSSE } from "./sse.js";
import { S } from "./state.js";
import { $, esc, escAttr, formatRelativeDate, toast } from "./utils.js";

const LS_MODE = "orb-doc-mode";
const LS_ACTIVE = "orb-active-doc";
const LS_ASSISTED = "orb-doc-assisted"; // Raw (0) or Assisted (1)
const LS_PROBS = "orb-doc-probs"; // capture token alternatives
const SAVE_DEBOUNCE_MS = 1500;
const STREAM_FLUSH_MS = 5000; // save interval during streaming
const HISTORY_DEBOUNCE_MS = 800; // one undo step per typing burst
const HISTORY_MAX = 100;
const MOBILE = window.matchMedia("(max-width: 900px)"); // document breakpoint
const DOC_LIMIT = 10; // documents shown before "show all"

let _docSearch = "";
let _docsExpanded = false;
let saveTimer = null;
let flushInterval = null;
let anchorTextNode = null; // text node receiving generated tokens
let docAssisted = false; // Raw vs Assisted mode
let docProbsOn = false; // capture token alternatives

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
  if (el) el.textContent = `~${Math.round(len / 4)} tokens`;
}

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

function docCheckpoint() {
  clearTimeout(docHistoryTimer);
  docHistoryTimer = null;
  const page = $("doc-page");
  if (!page || !S.activeDocId) return;
  const { content, spans } = serializeEditor(page);
  const cur = docHistory[docHistoryIndex];
  if (!cur || cur.content !== content || JSON.stringify(cur.spans) !== JSON.stringify(spans)) {
    docHistory.length = docHistoryIndex + 1;
    docHistory.push({ content, spans });
    if (docHistory.length > HISTORY_MAX) docHistory.shift();
    docHistoryIndex = docHistory.length - 1;
  }
  updateUndoButton();
}

function docRestore(snap) {
  const page = $("doc-page");
  const before = serializeEditor(page).content;
  let caret = 0;
  const max = Math.min(before.length, snap.content.length);
  while (caret < max && before[caret] === snap.content[caret]) caret++;
  renderEditor(page, snap.content, snap.spans);
  if (S.activeDocId) syncContent(S.activeDocId, snap.content);
  setCaretOffset(page, caret);
  if (MOBILE.matches) page.blur();
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();
  scheduleSave();
  updateUndoButton();
}

export function docUndo() {
  if (S.docStreaming || !S.activeDocId) return;
  docCheckpoint();
  if (docHistoryIndex <= 0) return;
  docHistoryIndex--;
  docRestore(docHistory[docHistoryIndex]);
}

export function docRedo() {
  if (S.docStreaming || !S.activeDocId) return;
  docCheckpoint();
  if (docHistoryIndex >= docHistory.length - 1) return;
  docHistoryIndex++;
  docRestore(docHistory[docHistoryIndex]);
}

function setDocumentMode(on) {
  S.documentMode = on;
  document.getElementById("app")?.classList.toggle("document-mode", on);
  localStorage.setItem(LS_MODE, on ? "1" : "0");
  if (on) {
    const body = $("documents-section");
    body?.classList.remove("collapsed");
    body?.previousElementSibling?.querySelector(".arrow")?.classList.remove("collapsed");
  }
  const btn = $("mode-switch-btn");
  if (btn) {
    btn.textContent = on ? "📄" : "💬";
    btn.title = on ? "Switch to Chat mode" : "Switch to Document mode";
  }
  if (isUtilityPanelOpen("tools-panel")) {
    if (on) renderDocAuditPane();
    else renderToolsPanel();
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

function reflectAssistedToggle() {
  $("doc-mode-raw")?.classList.toggle("active", !docAssisted);
  $("doc-mode-assisted")?.classList.toggle("active", docAssisted);
  const assisted = $("doc-help-assisted");
  if (assisted) assisted.hidden = !docAssisted;
  const raw = $("doc-help-raw");
  if (raw) raw.hidden = docAssisted;
  const summary = $("doc-help-summary");
  if (summary) summary.textContent = `How to prompt (${docAssisted ? "Assisted" : "Raw"})`;
  const cap = $("doc-help-maxtok");
  if (cap) {
    const cfg = S.modelConfigs?.find((m) => m.id === S.activeModelConfigId);
    cap.textContent = cfg?.max_tokens || 512;
  }
}

export function setDocAssisted(on) {
  docAssisted = !!on;
  localStorage.setItem(LS_ASSISTED, docAssisted ? "1" : "0");
  reflectAssistedToggle();
}

function reflectProbsToggle() {
  $("doc-probs-btn")?.classList.toggle("active", docProbsOn);
}

export function setDocProbs(on) {
  docProbsOn = !!on;
  localStorage.setItem(LS_PROBS, docProbsOn ? "1" : "0");
  reflectProbsToggle();
}

const _docItemHtml = (
  d,
) => `<div class="doc-item${S.activeDocId === d.id ? " active" : ""}" onclick="openDocument('${d.id}')">
      <div class="doc-item-info">
        <div class="doc-item-name">${esc(d.title)}</div>
        <div class="doc-item-meta">${formatRelativeDate(d.updated_at)}</div>
      </div>
      <div class="doc-item-actions">
        <button onclick="event.stopPropagation();renameDocument('${d.id}')" title="Rename" aria-label="Rename document">${EDIT_ICON}</button>
        <button class="del-btn" onclick="event.stopPropagation();deleteDocument('${d.id}')" title="Delete" aria-label="Delete document">${CLOSE_ICON}</button>
      </div>
    </div>`;

export function renderDocuments() {
  const list = $("documents-list");
  if (!list) return;

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
  hideProbPopup();
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
  $("app")?.classList.add("doc-open");
  if (!S.documentMode) setDocumentMode(true);

  const page = $("doc-page");
  renderEditor(page, doc.content, doc.generated_spans || []);
  syncContent(id, doc.content);
  page.setAttribute("contenteditable", "true");
  $("doc-generate-btn").disabled = false;
  $("doc-title-text").textContent = doc.title;
  docHistoryReset();
  docCheckpoint();
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
  confirmDelete("Document", `Delete "${esc(doc ? doc.title : "this document")}"? This cannot be undone.`, async () => {
    try {
      await api.del(`/documents/${id}`);
      S.documents = S.documents.filter((d) => d.id !== id);
      if (S.activeDocId === id) clearEditor();
      renderDocuments();
      toast("Deleted");
    } catch (e) {
      toast(e.message, true);
    }
  });
}

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
    S.docDirty = true;
    setSaveState("Save failed");
  }
}

function onEditorInput() {
  ensureTrailingFiller($("doc-page"));
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();
  if (S.activeDocId) syncContent(S.activeDocId, serializeEditor($("doc-page")).content);
  scheduleSave();
  clearTimeout(docHistoryTimer);
  docHistoryTimer = setTimeout(docCheckpoint, HISTORY_DEBOUNCE_MS);
  updateUndoButton();
}

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

let docScrollFollow = null;
function initDocAutoscroll() {
  const scroll = $("doc-editor-scroll");
  if (!scroll) return;
  docScrollFollow = createScrollFollow(scroll, { threshold: 40, debounceMs: 0, twoWayScroll: true });
}
function scrollAnchorIntoView() {
  docScrollFollow?.toBottom();
}

let genRunStart = 0;
let stopRequested = false;
let genFinish = ""; // finish reason from the SSE done event
let genErrored = false;

export async function docGenerate() {
  if (!S.activeDocId || S.docStreaming) return;
  const page = $("doc-page");
  hideProbPopup();
  if (S.docDirty) await flushSave();
  docCheckpoint();

  const caret = computeCaretOffset(page);
  const { content, spans } = serializeEditor(page);
  const prompt = content.slice(0, caret);
  beginRun(S.activeDocId, caret);
  genRunStart = caret;
  stopRequested = false;
  genFinish = "";
  genErrored = false;

  const anchor = renderEditor(page, content, spans, caret);
  anchorTextNode = anchor.firstChild;

  page.setAttribute("contenteditable", "false");
  page.classList.add("generating");
  docScrollFollow?.setFollowing(true);
  S.docStreaming = true;
  S.docAbortController = new AbortController();
  swapGenButtons(true);
  updateUndoButton();
  startFlushInterval();

  try {
    const resp = await streamPost(
      `/documents/${S.activeDocId}/generate`,
      { prompt, assisted: docAssisted, token_probs: docProbsOn },
      S.docAbortController.signal,
    );
    if (!resp.ok) throw new Error(await resp.text());
    for await (const { event, data } of sseEvents(resp.body, { signal: S.docAbortController.signal })) {
      if (event === "token") {
        const delta = unescapeSSE(data);
        anchorTextNode.appendData(delta);
        addDelta(delta);
        updateTokenCount();
        scrollAnchorIntoView();
      } else if (event === "probs") {
        try {
          addToken(JSON.parse(data));
        } catch {}
      } else if (event === "error") {
        genErrored = true;
        toast(unescapeSSE(data) || "Generation error", true);
        break;
      } else if (event === "done") {
        try {
          genFinish = JSON.parse(data).finish || "";
        } catch {
          genFinish = "";
        }
        break;
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      genErrored = true;
      toast(`Generation failed: ${e.message}`, true);
    }
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
      anchor.remove();
      clearPending();
      toast("No text was generated");
    } else {
      committedText = anchor.textContent;
      caretAfter(anchor);
    }
  } else {
    clearPending();
  }
  if (S.activeDocId) {
    syncContent(S.activeDocId, serializeEditor(page).content);
    if (committedText != null) commitRun(S.activeDocId, committedText);
  }
  if (MOBILE.matches) $("doc-page").blur();
  S.docDirty = true;
  flushSave();
  updateTokenCount();
  docCheckpoint();
  if (committedText != null && !genErrored && S.activeDocId) {
    onGenerationEnd({
      docId: S.activeDocId,
      runStart: genRunStart,
      draft: committedText,
      truncated: stopRequested || genFinish === "length",
      assisted: docAssisted,
    });
  }
}

export function docStop() {
  if (!S.docStreaming) return;
  stopRequested = true;
  S.docAbortController?.abort();
  fetch(`/api/documents/${S.activeDocId}/stop`, { method: "POST" }).catch(() => {});
}

function applyPatchedRun(runStart, oldText, newText) {
  const page = $("doc-page");
  if (!page || !S.activeDocId || S.docStreaming) return false;
  const { content, spans } = serializeEditor(page);
  if (content.slice(runStart, runStart + oldText.length) !== oldText) return false;
  docCheckpoint();

  const oldEnd = runStart + oldText.length;
  const delta = newText.length - oldText.length;
  const newContent = content.slice(0, runStart) + newText + content.slice(oldEnd);
  const newSpans = [];
  for (const s of spans) {
    if (s.end <= runStart) newSpans.push({ start: s.start, end: s.end });
    else if (s.start >= oldEnd) newSpans.push({ start: s.start + delta, end: s.end + delta });
    else {
      if (s.start < runStart) newSpans.push({ start: s.start, end: runStart });
      if (s.end > oldEnd) newSpans.push({ start: runStart + newText.length, end: s.end + delta });
    }
  }
  newSpans.push({ start: runStart, end: runStart + newText.length });
  newSpans.sort((a, b) => a.start - b.start);

  renderEditor(page, newContent, newSpans);
  syncContent(S.activeDocId, newContent);
  setCaretOffset(page, runStart + newText.length);
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();
  flushSave();
  return true;
}

function docSwapToken(run, tokenIndex, alt) {
  if (S.docStreaming || !S.activeDocId) return;
  const page = $("doc-page");
  const { content, spans } = serializeEditor(page);

  let tokStart = run.start;
  for (let i = 0; i < tokenIndex; i++) tokStart += run.tokens[i].text.length;
  if (runAt(S.activeDocId, tokStart, content) !== run) {
    hideProbPopup();
    return;
  }
  hideProbPopup();
  docCheckpoint();

  const newContent = content.slice(0, tokStart) + alt.t;
  const newSpans = spans
    .filter((s) => s.start < tokStart)
    .map((s) => ({ start: s.start, end: Math.min(s.end, tokStart) }));
  newSpans.push({ start: tokStart, end: newContent.length });
  swapRunToken(S.activeDocId, run, tokenIndex, alt, newContent);

  renderEditor(page, newContent, newSpans);
  setCaretOffset(page, tokStart + alt.t.length);
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();

  docGenerate();
}

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
    if (isOtherEditableTarget(e.target)) return;
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
  $("doc-help")?.addEventListener("toggle", (e) => e.target.open && reflectAssistedToggle());
  installPlainTextGuards(page);
  initDocAutoscroll();
  initDocProbs(page, {
    getDocId: () => S.activeDocId,
    isStreaming: () => S.docStreaming,
    requestSwap: docSwapToken,
  });
  initDocAudit({
    getContent: () => serializeEditor($("doc-page")).content,
    applyPatchedRun,
  });
  page.addEventListener("input", onEditorInput);
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
