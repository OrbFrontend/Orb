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
} from "./document_editor.js";
import { showConfirmModal } from "./modal.js";
import { S } from "./state.js";
import { $, esc, escAttr, formatRelativeDate, toast } from "./utils.js";

const LS_MODE = "orb-doc-mode";
const LS_ACTIVE = "orb-active-doc";
const LS_ASSISTED = "orb-doc-assisted"; // Raw (0) ⇄ Assisted (1) prompting strategy
const SAVE_DEBOUNCE_MS = 1500;
const STREAM_FLUSH_MS = 5000; // interval flush while streaming → tab crash loses ≤5s

let saveTimer = null;
let flushInterval = null;
let lastGenSpanEl = null; // the finalized generated span "Undo generation" removes
let anchorTextNode = null; // text node tokens stream into during generation
let docAssisted = false; // false = Raw (verbatim), true = Assisted (### macros → chat template)

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
function showGenStatus(on) {
  $("doc-gen-status")?.classList.toggle("hidden", !on);
}
function updateTokenCount() {
  const page = $("doc-page");
  const len = page ? serializeEditor(page).content.length : 0;
  const el = $("doc-token-count");
  if (el) el.textContent = `~${Math.round(len / 4)} tokens`; // mirrors CHARS_PER_TOKEN=4
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

// ── Documents list. ──────────────────────────────────────────────────────────
export function renderDocuments() {
  const list = $("documents-list");
  if (!list) return;
  if (!S.documents.length) {
    list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No documents yet.</div>';
    return;
  }
  list.innerHTML = S.documents
    .map(
      (d) => `<div class="doc-item${S.activeDocId === d.id ? " active" : ""}" onclick="openDocument('${d.id}')">
      <div class="doc-item-info">
        <div class="doc-item-name">${esc(d.title)}</div>
        <div class="doc-item-meta">${formatRelativeDate(d.updated_at)}</div>
      </div>
      <div class="doc-item-actions">
        <button onclick="event.stopPropagation();renameDocument('${d.id}')" title="Rename">✏</button>
        <button class="del-btn" onclick="event.stopPropagation();deleteDocument('${d.id}')" title="Delete">✕</button>
      </div>
    </div>`,
    )
    .join("");
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
  page.setAttribute("contenteditable", "true");
  $("doc-generate-btn").disabled = false;
  $("doc-title-text").textContent = doc.title;
  lastGenSpanEl = null;
  setUndoEnabled(false);
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
  lastGenSpanEl = null;
  setUndoEnabled(false);
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
  // First edit after a finalized generation invalidates the undo target — the
  // user may have typed inside the span, and removing it would eat their words.
  if (lastGenSpanEl) {
    lastGenSpanEl = null;
    setUndoEnabled(false);
  }
  S.docDirty = true;
  setSaveState("Unsaved…");
  updateTokenCount();
  scheduleSave();
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

function scrollAnchorIntoView() {
  const scroll = $("doc-editor-scroll");
  if (!scroll) return;
  if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140) scroll.scrollTop = scroll.scrollHeight;
}

// Dedicated SSE reader (do NOT reuse the chat-coupled processSSEStream). Handles
// the three wire facts of the backend's _sse_stream: \n is escaped in data,
// ": keepalive" comment frames appear during silent stretches, and errors arrive
// in-band as `event: error`.
async function readDocSSE(resp, onToken, onError) {
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
      data = data.replace(/\\n/g, "\n"); // newlines are load-bearing in the editor
      if (event === "token") onToken(data);
      else if (event === "error") {
        onError(data);
        return;
      } else if (event === "done") return;
    }
  }
}

export async function docGenerate() {
  if (!S.activeDocId || S.docStreaming) return;
  const page = $("doc-page");
  if (S.docDirty) await flushSave();

  // Split in the string domain: caret offset → prompt is the prefix before it.
  const caret = computeCaretOffset(page);
  const { content, spans } = serializeEditor(page);
  const prompt = content.slice(0, caret);

  // Re-render with an empty streaming anchor at the caret (splits a straddling span).
  const anchor = renderEditor(page, content, spans, caret);
  anchorTextNode = anchor.firstChild;

  page.setAttribute("contenteditable", "false");
  page.classList.add("generating");
  S.docStreaming = true;
  S.docAbortController = new AbortController();
  swapGenButtons(true);
  showGenStatus(true);
  startFlushInterval();

  try {
    const resp = await fetch(`/api/documents/${S.activeDocId}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, assisted: docAssisted }),
      signal: S.docAbortController.signal,
    });
    if (!resp.ok) throw new Error(await resp.text());
    await readDocSSE(
      resp,
      (delta) => {
        anchorTextNode.appendData(delta);
        scrollAnchorIntoView();
      },
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
  showGenStatus(false);

  const anchor = page.querySelector(".gen-active");
  if (anchor) {
    anchor.classList.remove("gen-active");
    if (!anchor.textContent) {
      anchor.remove(); // empty span (immediate EOS / abort before any token)
      lastGenSpanEl = null;
      setUndoEnabled(false);
      toast("No text was generated");
    } else {
      lastGenSpanEl = anchor;
      setUndoEnabled(true);
      caretAfter(anchor);
    }
  }
  S.docDirty = true;
  flushSave(); // immediate save at stream end
  updateTokenCount();
}

export function docStop() {
  if (!S.docStreaming) return;
  S.docAbortController?.abort();
  fetch(`/api/documents/${S.activeDocId}/stop`, { method: "POST" }).catch(() => {});
}

export async function docUndoLastGen() {
  if (!lastGenSpanEl) return;
  lastGenSpanEl.remove();
  lastGenSpanEl = null;
  setUndoEnabled(false);
  S.docDirty = true;
  updateTokenCount();
  await flushSave();
}

// ── Shortcuts: Ctrl/Cmd+Enter generates, Esc stops. Scoped to document mode and
// no open modal so they can't collide with modal.js / mobile.js Esc handlers.
function onDocKeydown(e) {
  if (!S.documentMode) return;
  if ($("modal-root")?.innerHTML) return;
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    docGenerate();
  } else if (e.key === "Escape" && S.docStreaming) {
    e.preventDefault();
    docStop();
  }
}

export function initDocumentMode() {
  const page = $("doc-page");
  if (!page) return;
  docAssisted = localStorage.getItem(LS_ASSISTED) === "1";
  reflectAssistedToggle();
  // Re-read the token cap on open — modelConfigs may load / change after init.
  $("doc-help")?.addEventListener("toggle", (e) => e.target.open && reflectAssistedToggle());
  installPlainTextGuards(page);
  page.addEventListener("input", onEditorInput);
  page.addEventListener("blur", () => {
    if (S.docDirty) flushSave();
  });
  document.addEventListener("keydown", onDocKeydown);
  window.addEventListener("beforeunload", () => {
    if (S.docDirty && S.activeDocId) flushSave({ keepalive: true });
  });
}
