// chat_composer.js — the message composer: everything the user types or attaches
// before a send.
//
//   • the composer textarea (auto-grow, Enter-to-send)
//   • image attachments (file picker, preview chips)
//
// app.js calls initComposer() once at startup and bridges triggerAttachImage
// onto window for the inline "Attach Image" handler. chat_stream.js imports
// updateAttachmentPreview to clear the chips after a send.

import { sendMessage } from "./chat_stream.js";
import { S } from "./state.js";
import { $, formatBytes, toast } from "./utils.js";
import { validate } from "./validate.js";

// ── Image attachments
export function triggerAttachImage() {
  $("attach-image-input").click();
}

function handleAttachmentSelect(e) {
  const files = Array.from(e.target.files);
  if (files.length === 0) return;

  const validation = validate.validateImageFiles(files, 10, 10 * 1024 * 1024, 20 * 1024 * 1024);
  if (!validation.valid) {
    toast(validation.error, true);
    e.target.value = "";
    return;
  }

  for (const file of files) {
    const fileValidation = validate.validateImageFile(file, 10 * 1024 * 1024, [
      "image/png",
      "image/jpeg",
      "image/webp",
      "image/gif",
    ]);
    if (!fileValidation.valid) {
      toast(fileValidation.error, true);
      continue;
    }
    const reader = new FileReader();
    reader.onload = (event) => {
      const b64 = event.target.result.split(",")[1]; // strip data:image/...;base64,
      S.attachments.push({
        b64,
        mime: file.type,
        filename: file.name,
        size: file.size,
      });
      updateAttachmentPreview();
    };
    reader.readAsDataURL(file);
  }
  e.target.value = ""; // allow re-selecting same file
}

export function updateAttachmentPreview() {
  const container = $("attachment-preview");
  container.innerHTML = "";
  S.attachments.forEach((att, idx) => {
    const item = document.createElement("div");
    item.className = "attachment-item";
    const img = document.createElement("img");
    img.src = `data:${att.mime};base64,${att.b64}`;
    const info = document.createElement("div");
    info.className = "attachment-info";
    const name = document.createElement("div");
    name.className = "attachment-name";
    name.textContent = att.filename || "image";
    const size = document.createElement("div");
    size.className = "attachment-size";
    size.textContent = formatBytes(att.size);
    info.appendChild(name);
    info.appendChild(size);
    const removeBtn = document.createElement("button");
    removeBtn.className = "attachment-remove";
    removeBtn.innerHTML = "×";
    removeBtn.title = "Remove";
    removeBtn.onclick = () => {
      S.attachments.splice(idx, 1);
      updateAttachmentPreview();
    };
    item.appendChild(img);
    item.appendChild(info);
    item.appendChild(removeBtn);
    container.appendChild(item);
  });
}

// ── Composer textarea
// Auto-grow the composer. Reading scrollHeight right after writing height
// forces a synchronous reflow; doing that on every keystroke (against a long
// chat DOM) is what makes typing feel laggy. Defer it to an animation frame so
// the keypress paints first and bursts of input coalesce into one layout pass.
let _resizeScheduled = false;
function _resizeChatInput() {
  _resizeScheduled = false;
  const el = $("chat-input");
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
}

// ── Inline autocomplete (ghost text)
// A debounced POST to /autocomplete predicts a short continuation, shown as gray
// ghost text and accepted with Tab. Only fires at the end of a non-empty draft
// and never mid-generation. A server 503 (ML extra not installed) disables it
// for the session so we stop polling.
const GHOST_DEBOUNCE_MS = 180;
let _ghostText = "";
let _ghostTimer = null;
let _ghostAbort = null;
let _ghostDisabled = false;

function _renderGhost() {
  const g = $("chat-ghost");
  const inp = $("chat-input");
  if (!g) return;
  const chip = $("chat-ghost-chip");
  if (chip) {
    chip.textContent = _ghostText;
    chip.hidden = !_ghostText;
  }
  g.textContent = "";
  if (!_ghostText) return;
  g.appendChild(document.createTextNode(inp.value)); // transparent prefix pushes the suggestion to the caret
  const s = document.createElement("span");
  s.className = "ghost-suggestion";
  s.textContent = _ghostText;
  g.appendChild(s);
  g.scrollTop = inp.scrollTop;
}

function clearGhost() {
  if (_ghostTimer) {
    clearTimeout(_ghostTimer);
    _ghostTimer = null;
  }
  if (_ghostText) {
    _ghostText = "";
    _renderGhost();
  }
}

function acceptGhost() {
  const inp = $("chat-input");
  // insertText (not `inp.value +=`) so the browser keeps this in the native
  // undo stack — a direct .value assignment wipes it and breaks Ctrl+Z.
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
  if (!document.execCommand("insertText", false, _ghostText)) {
    inp.value += _ghostText; // ponytail: fallback if execCommand unsupported
  }
  _ghostText = "";
  _renderGhost();
  onComposerInput(); // resize + schedule the next suggestion
}

function scheduleGhost() {
  if (_ghostDisabled) return;
  if (_ghostTimer) clearTimeout(_ghostTimer);
  _ghostTimer = setTimeout(_requestGhost, GHOST_DEBOUNCE_MS);
}

async function _requestGhost() {
  _ghostTimer = null;
  if (_ghostDisabled) return;
  const inp = $("chat-input");
  const draft = inp.value;
  const cid = S.activeConvId;
  if (!cid || !draft.trim() || S.isStreaming || inp.selectionStart !== draft.length) return;
  // Toggle takes effect live (no reload); the 503 path below still backstops deps/model-missing.
  if (S.settings?.local_ml_enabled?.autocomplete === false) return;
  if (_ghostAbort) _ghostAbort.abort();
  _ghostAbort = new AbortController();
  try {
    const r = await fetch(`/api/conversations/${cid}/autocomplete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft }),
      signal: _ghostAbort.signal,
    });
    if (r.status === 503) {
      _ghostDisabled = true; // ML extra not installed — stop trying this session
      return;
    }
    if (!r.ok) return;
    const { completion } = await r.json();
    if (inp.value !== draft) return; // draft moved on while we waited
    _ghostText = completion || "";
    _renderGhost();
  } catch {
    /* AbortError (superseded) or network error: ignore */
  }
}

function onComposerInput() {
  if (!_resizeScheduled) {
    _resizeScheduled = true;
    requestAnimationFrame(_resizeChatInput);
  }
  clearGhost();
  scheduleGhost();
}

function onComposerKeydown(e) {
  if (_ghostText) {
    if (e.key === "Tab") {
      e.preventDefault();
      acceptGhost();
      return;
    }
    if (e.key === "Escape") {
      clearGhost();
      return;
    }
    // Any other key (typing, caret move) invalidates the shown suggestion;
    // typing then reschedules a fresh one via onComposerInput.
    if (!["Shift", "Control", "Alt", "Meta"].includes(e.key)) clearGhost();
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const validation = validate.validateChatInput(this.value);
    if (!validation.valid) {
      toast(validation.error, true);
      return;
    }
    clearGhost();
    sendMessage();
  }
}

// ── Wiring: register the composer's input listeners. Call once at startup.
export function initComposer() {
  $("attach-image-input").addEventListener("change", handleAttachmentSelect);

  const input = $("chat-input");
  input.addEventListener("input", onComposerInput);
  input.addEventListener("keydown", onComposerKeydown);
  // Clear ghost text when focus leaves (e.g. clicking Send) and keep the
  // overlay scrolled in step with the textarea.
  input.addEventListener("blur", clearGhost);
  input.addEventListener("scroll", () => {
    const g = $("chat-ghost");
    if (g) g.scrollTop = input.scrollTop;
  });

  // Mobile tap-to-accept: pointerdown (not click) + preventDefault keeps the
  // textarea focused, so the tap neither fires blur→clearGhost nor collapses
  // the on-screen keyboard. Visible only on coarse-pointer devices (see CSS).
  $("chat-ghost-chip").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    acceptGhost();
  });
}
