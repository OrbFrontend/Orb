import { sendMessage } from "./chat_stream.js";
import { refreshCastRailIntent } from "./group_setup.js";
import { CLOSE_ICON } from "./icons.js";
import { S } from "./state.js";
import { $, formatBytes, toast } from "./utils.js";
import { validate } from "./validate.js";

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
  e.target.value = "";
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
    removeBtn.innerHTML = CLOSE_ICON;
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
  refreshCastRailIntent();
}

let _resizeScheduled = false;
function _resizeChatInput() {
  _resizeScheduled = false;
  const el = $("chat-input");
  el.style.height = "auto";
  const ghost = $("chat-ghost");
  const ghostH = _ghostText && ghost ? ghost.scrollHeight : 0;
  el.style.height = `${Math.min(Math.max(el.scrollHeight, ghostH), 150)}px`;
}

function _scheduleResize() {
  if (!_resizeScheduled) {
    _resizeScheduled = true;
    requestAnimationFrame(_resizeChatInput);
  }
}

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
  if (!_ghostText) {
    _scheduleResize();
    return;
  }
  g.appendChild(document.createTextNode(inp.value));
  const s = document.createElement("span");
  s.className = "ghost-suggestion";
  s.textContent = _ghostText;
  g.appendChild(s);
  g.scrollTop = inp.scrollTop;
  _scheduleResize();
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
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
  if (!document.execCommand("insertText", false, _ghostText)) {
    inp.value += _ghostText;
  }
  _ghostText = "";
  _renderGhost();
  onComposerInput();
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
      _ghostDisabled = true;
      return;
    }
    if (!r.ok) return;
    const { completion } = await r.json();
    if (inp.value !== draft) return;
    _ghostText = completion || "";
    _renderGhost();
  } catch {}
}

function onComposerInput() {
  _scheduleResize();
  clearGhost();
  scheduleGhost();
  refreshCastRailIntent();
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

export function initComposer() {
  $("attach-image-input").addEventListener("change", handleAttachmentSelect);

  const input = $("chat-input");
  input.addEventListener("input", onComposerInput);
  input.addEventListener("keydown", onComposerKeydown);
  input.addEventListener("blur", clearGhost);
  input.addEventListener("scroll", () => {
    const g = $("chat-ghost");
    if (g) g.scrollTop = input.scrollTop;
  });

  $("chat-ghost-chip").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    acceptGhost();
  });
}
