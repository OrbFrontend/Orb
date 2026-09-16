import {
  activeChannels,
  channelState,
  isContextSuspended,
  resumeContext,
  seekChannel,
  setBarChangeHook,
} from "./audio_player.js";
import { speakerLabel } from "./group_cast.js";
import { S } from "./state.js";
import { scrollToMessage } from "./utils.js";

let _barEl = null;
let _inited = false;
let _rafId = null;
let _geomRaf = null;
let _ro = null;
let _messageObserver = null;
let _messageMutations = null;
let _selectedChannel = null;
let _watchedMsgId = null;
let _watchedMessageEl = null;
let _messageVisibility = new Map();

let _dragging = false;
let _dragChannel = null;
let _dragProgressEl = null;
let _dragFraction = 0;

function _mmss(sec) {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r < 10 ? `0${r}` : `${r}`}`;
}

function _formatTime(st) {
  return `${_mmss(st.stream.elapsedSec)} / ${_mmss(st.stream.durationSec)}`;
}

function _streamPct(st) {
  return st.stream.durationSec > 0 ? (st.stream.elapsedSec / st.stream.durationSec) * 100 : 0;
}

// A channel can opt out of the dock and show its own progress where it was started.
function _dockAllowed(st) {
  return !!st && st.source?.dock !== false;
}

function _dockChannels() {
  return activeChannels().filter((name) => _dockAllowed(channelState(name)));
}

function _anyAudible() {
  for (const name of activeChannels()) {
    const s = channelState(name);
    if (s?.playing && !s.paused && _dockAllowed(s)) return true;
  }
  return false;
}

function _syncBarLayout() {
  const cm = document.getElementById("chat-messages");
  if (!cm) return;
  if (!_barEl || _barEl.classList.contains("hidden")) {
    if (cm.style.paddingBottom) {
      cm.classList.add("audio-reserve-animating");
      cm.style.paddingBottom = "";
      setTimeout(() => cm.classList.remove("audio-reserve-animating"), 300);
    }
    return;
  }
  cm.classList.remove("audio-reserve-animating");
  const ccs = getComputedStyle(cm);
  const padL = parseFloat(ccs.paddingLeft) || 0;
  const padR = parseFloat(ccs.paddingRight) || 0;
  const scrollbar = cm.offsetWidth - cm.clientWidth;
  _barEl.style.marginLeft = `${padL}px`;
  _barEl.style.marginRight = `${padR + scrollbar}px`;
  const bcs = getComputedStyle(_barEl);
  const my = (parseFloat(bcs.marginTop) || 0) + (parseFloat(bcs.marginBottom) || 0);
  cm.style.paddingBottom = `${_barEl.offsetHeight + my}px`;
}

function _onGeomChange() {
  if (_geomRaf != null) return;
  _geomRaf = requestAnimationFrame(() => {
    _geomRaf = null;
    _syncBarLayout();
  });
}

function _messageElement(msgId) {
  if (msgId == null) return null;
  return document.querySelector(`.message[data-msg-id="${Number(msgId)}"]`);
}

function _watchMessage(msgId) {
  const el = _messageElement(msgId);
  if (_watchedMsgId === msgId && _watchedMessageEl === el) return;
  _watchedMsgId = msgId;
  _watchedMessageEl = el;
  _messageVisibility = new Map();
  _messageObserver?.disconnect();
  if (msgId == null) return;
  if (!el) {
    _messageVisibility.set(msgId, false);
    return;
  }
  _messageObserver?.observe(el);
}

function _messageIsVisible(msgId) {
  if (msgId == null) return false;
  if (_messageVisibility.has(msgId)) return _messageVisibility.get(msgId);
  const el = _messageElement(msgId);
  const cm = document.getElementById("chat-messages");
  if (!el || !cm) return false;
  const r = el.getBoundingClientRect();
  const box = cm.getBoundingClientRect();
  return r.bottom > box.top && r.top < box.bottom;
}

function _sourceText(st) {
  const source = st.source || { label: "Audio", msgId: null };
  const msg = source.msgId == null ? null : S.messages.find((item) => item.id === source.msgId);
  const preview = (msg?.content || "").replace(/\s+/g, " ").trim();
  return {
    label:
      source.msgId != null && (!source.label || source.label === "Speech")
        ? speakerLabel(msg)
        : source.label || "Audio",
    preview: preview.length > 90 ? `${preview.slice(0, 87)}…` : preview,
    msgId: source.msgId,
  };
}

function _shouldShowDock(st) {
  const msgId = st?.source?.msgId;
  return msgId == null || !_messageIsVisible(msgId);
}

function _refreshBar() {
  if (!_barEl) return;
  const names = _dockChannels();
  if (_selectedChannel && !names.includes(_selectedChannel)) _selectedChannel = null;
  if (!_selectedChannel && names.length) _selectedChannel = names[0];

  let st = _selectedChannel ? channelState(_selectedChannel) : null;
  _watchMessage(st?.source?.msgId ?? null);
  let show = !!names.length && !!st && _shouldShowDock(st);
  if (!show && names.length > 1) {
    const fallback = names.find((name) => _shouldShowDock(channelState(name)));
    if (fallback) {
      _selectedChannel = fallback;
      st = channelState(fallback);
      _watchMessage(st?.source?.msgId ?? null);
      show = true;
    }
  }

  _barEl.innerHTML = "";
  _barEl.classList.toggle("audio-transport-suspended", isContextSuspended());
  _barEl.classList.toggle("hidden", !show);
  if (!show) {
    _syncBarLayout();
    _syncRaf();
    return;
  }

  if (names.length > 1) {
    const tabs = document.createElement("div");
    tabs.className = "audio-transport-tabs";
    for (const name of names) {
      const tab = document.createElement("button");
      const tabState = channelState(name);
      tab.type = "button";
      tab.className = `audio-transport-tab${name === _selectedChannel ? " selected" : ""}`;
      tab.dataset.channel = name;
      tab.setAttribute("aria-pressed", name === _selectedChannel ? "true" : "false");
      tab.textContent = _sourceText(tabState || {}).label;
      tabs.appendChild(tab);
    }
    _barEl.appendChild(tabs);
  }

  _barEl.appendChild(_buildDock(_selectedChannel, st));
  _syncBarLayout();
  _syncRaf();
}

function _buildDock(channel, st) {
  const row = document.createElement("div");
  row.className = "audio-transport-row";
  row.dataset.channel = channel;

  const source = _sourceText(st);
  const label = document.createElement("button");
  label.type = "button";
  label.className = "audio-transport-source";
  label.dataset.msgId = source.msgId ?? "";
  label.title = source.msgId == null ? source.label : "Jump to the playing message";
  label.textContent = source.preview ? `${source.label} · ${source.preview}` : source.label;

  const progress = document.createElement("div");
  progress.className = "audio-transport-progress";
  progress.dataset.channel = channel;
  const fill = document.createElement("div");
  fill.className = "audio-transport-fill";
  fill.style.width = `${_streamPct(st)}%`;
  progress.appendChild(fill);

  const time = document.createElement("span");
  time.className = "audio-transport-time";
  time.textContent = _formatTime(st);
  row.append(label, progress, time);
  return row;
}

function _tick() {
  const anyPlaying = _anyAudible();
  const st = _selectedChannel ? channelState(_selectedChannel) : null;
  const dragOwnsFill = _dragging && _dragChannel === _selectedChannel;
  if (st && _barEl && !_barEl.classList.contains("hidden") && !dragOwnsFill) {
    const row = _barEl.querySelector(".audio-transport-row");
    if (row) {
      const fill = row.querySelector(".audio-transport-fill");
      if (fill) fill.style.width = `${_streamPct(st)}%`;
      const time = row.querySelector(".audio-transport-time");
      if (time) time.textContent = _formatTime(st);
    }
  }
  _rafId = anyPlaying ? requestAnimationFrame(_tick) : null;
}

function _syncRaf() {
  const anyPlaying = _anyAudible();
  if (anyPlaying && _rafId == null) {
    _rafId = requestAnimationFrame(_tick);
  } else if (!anyPlaying && _rafId != null) {
    cancelAnimationFrame(_rafId);
    _rafId = null;
  }
}

function _applyDrag(e) {
  if (!_dragProgressEl) return;
  const rect = _dragProgressEl.getBoundingClientRect();
  let frac = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0;
  frac = frac < 0 ? 0 : frac > 1 ? 1 : frac;
  _dragFraction = frac;
  const fill = _dragProgressEl.querySelector(".audio-transport-fill");
  if (fill) fill.style.width = `${frac * 100}%`;
}

function _onDragMove(e) {
  if (_dragging) _applyDrag(e);
}

function _onDragUp(e) {
  if (!_dragging) return;
  _applyDrag(e);
  const channel = _dragChannel;
  const fraction = _dragFraction;
  _dragging = false;
  _dragChannel = null;
  _dragProgressEl = null;
  document.removeEventListener("pointermove", _onDragMove, true);
  document.removeEventListener("pointerup", _onDragUp, true);
  const st = channelState(channel);
  if (st) seekChannel(channel, fraction * st.stream.durationSec);
}

function _onBarClick(e) {
  const tab = e.target.closest(".audio-transport-tab");
  if (tab?.dataset.channel) {
    _selectedChannel = tab.dataset.channel;
    _refreshBar();
    return;
  }
  const source = e.target.closest(".audio-transport-source");
  if (source?.dataset.msgId) scrollToMessage(Number(source.dataset.msgId));
}

function _onBarPointerDown(e) {
  const prog = e.target.closest(".audio-transport-progress");
  if (!prog?.dataset.channel) return;
  const st = channelState(prog.dataset.channel);
  if (!st) return;
  _dragging = true;
  _dragChannel = prog.dataset.channel;
  _dragProgressEl = prog;
  _applyDrag(e);
  document.addEventListener("pointermove", _onDragMove, true);
  document.addEventListener("pointerup", _onDragUp, true);
  e.preventDefault();
}

function _bindResumeOnGesture() {
  const resume = () => {
    if (isContextSuspended()) {
      resumeContext()
        .then(() => _barEl?.classList.remove("audio-transport-suspended"))
        .catch(() => {});
    }
  };
  for (const ev of ["pointerdown", "keydown", "touchend"]) {
    document.addEventListener(ev, resume, true);
  }
}

export function initAudioPlayer() {
  if (_inited) return;
  _inited = true;
  _barEl = document.createElement("div");
  _barEl.id = "audio-transport";
  _barEl.className = "audio-transport hidden";
  _barEl.addEventListener("click", _onBarClick);
  _barEl.addEventListener("pointerdown", _onBarPointerDown);
  const main = document.getElementById("main");
  const inputArea = document.getElementById("chat-input-area");
  if (inputArea) inputArea.insertBefore(_barEl, inputArea.firstChild);
  else if (main) main.appendChild(_barEl);
  else document.body.appendChild(_barEl);
  const cm = document.getElementById("chat-messages");
  if (cm && typeof ResizeObserver !== "undefined") {
    _ro = new ResizeObserver(_onGeomChange);
    _ro.observe(cm);
  }
  if (cm && typeof IntersectionObserver !== "undefined") {
    _messageObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) _messageVisibility.set(Number(entry.target.dataset.msgId), entry.isIntersecting);
        _refreshBar();
      },
      { root: cm, threshold: 0.01 },
    );
  }
  if (cm && typeof MutationObserver !== "undefined") {
    _messageMutations = new MutationObserver(() => {
      const st = _selectedChannel ? channelState(_selectedChannel) : null;
      _watchMessage(st?.source?.msgId ?? null);
      _refreshBar();
    });
    _messageMutations.observe(cm, { childList: true, subtree: true });
  }
  setBarChangeHook(_refreshBar);
  _bindResumeOnGesture();
}
