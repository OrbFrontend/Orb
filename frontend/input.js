// input.js — everything between the user's keyboard/pointer and the message list.
//
//   • the composer textarea (auto-grow, Enter-to-send)
//   • image attachments (file picker, preview chips)
//   • smart autoscroll (follow the stream until the user scrolls up)
//   • the document-level chat keyboard navigation hook
//
// app.js calls initInput() once at startup. triggerAttachImage and
// updateAttachmentPreview are also bridged onto window for inline handlers —
// and chat_stream.js reads updateAttachmentPreview off the global after a send.

import { handleChatKeyNav, sendMessage } from "./chat.js";
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

function onComposerInput() {
  if (_resizeScheduled) return;
  _resizeScheduled = true;
  requestAnimationFrame(_resizeChatInput);
}

function onComposerKeydown(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const validation = validate.validateChatInput(this.value);
    if (!validation.valid) {
      toast(validation.error, true);
      return;
    }
    sendMessage();
  }
}

// ── Smart autoscroll: disable on upward scroll, re-enable when back at bottom
function initAutoscroll() {
  const ct = $("chat-messages");
  if (!ct) return;
  const THRESHOLD = 20;
  let scrollDebounce = null;

  // Wheel: immediately cut autoscroll on any upward scroll intent
  ct.addEventListener(
    "wheel",
    (e) => {
      if (e.deltaY < 0) S.autoscrollEnabled = false;
    },
    { passive: true },
  );

  // Touch: disable on upward swipe
  let touchStartY = 0;
  ct.addEventListener(
    "touchstart",
    (e) => {
      touchStartY = e.touches[0].clientY;
    },
    { passive: true },
  );
  ct.addEventListener(
    "touchmove",
    (e) => {
      if (e.touches[0].clientY > touchStartY) S.autoscrollEnabled = false;
    },
    { passive: true },
  );

  // Re-enable only once the user has scrolled back to the bottom (debounced to
  // avoid false positives from rapid programmatic scroll events during streaming)
  ct.addEventListener("scroll", () => {
    if (S._programmaticScroll) return;
    clearTimeout(scrollDebounce);
    scrollDebounce = setTimeout(() => {
      const atBottom = ct.scrollHeight - ct.scrollTop - ct.clientHeight <= THRESHOLD;
      if (atBottom) S.autoscrollEnabled = true;
    }, 100);
  });
}

// ── Wiring: register every input/scroll listener. Call once at startup.
export function initInput() {
  $("attach-image-input").addEventListener("change", handleAttachmentSelect);

  const input = $("chat-input");
  input.addEventListener("input", onComposerInput);
  input.addEventListener("keydown", onComposerKeydown);

  document.addEventListener("keydown", handleChatKeyNav);

  initAutoscroll();
}
