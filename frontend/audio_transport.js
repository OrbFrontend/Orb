// On-screen transport for the workflow audio engine: a channel selector plus one
// shared control surface (play/pause, repeat, draggable seek, time readout,
// volume, stop) bound to the selected channel. The bar floats just above the
// composer, anchored inside #chat-input-area and out of the message flow so
// #chat-messages keeps its full height and scrollbar; the chat re-render rebuilds
// only #chat-messages's contents, so the bar survives it.
//
// The engine owns playback truth; this module renders it and forwards control
// gestures back as engine calls. It repaints when the engine invokes the
// registered change hook and animates the selected channel off channelState while
// audio plays. It does not use the author-facing onChannel bus: that hook is
// per-channel, so it cannot observe a never-seen channel's first play nor
// enumerate channels, which is exactly what the selector needs.

import {
  activeChannels,
  channelState,
  channelUserVolume,
  isContextSuspended,
  pauseChannel,
  replayChannel,
  resumeChannel,
  resumeContext,
  seekChannel,
  setBarChangeHook,
  setChannelRepeat,
  setChannelUserVolume,
  stopChannel,
} from "./audio_player.js";
import { scrollToBottom } from "./utils.js";

let _barEl = null;
let _reopenEl = null;
let _inited = false;
let _rafId = null;
let _geomRaf = null;
let _ro = null;
let _selectedChannel = null;
let _dismissed = false;

let _dragging = false;
let _dragChannel = null;
let _dragProgressEl = null;
let _dragFraction = 0;

function _mmss(sec) {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m + ":" + (r < 10 ? "0" + r : "" + r);
}

// Whole-stream "m:ss / m:ss", plus the current chunk's index and timing when the
// stream has more than one chunk.
function _formatTime(st) {
  let out = _mmss(st.stream.elapsedSec) + " / " + _mmss(st.stream.durationSec);
  if (st.segmentCount > 1 && st.segmentIndex >= 0) {
    out +=
      "  " +
      (st.segmentIndex + 1) +
      "/" +
      st.segmentCount +
      " " +
      _mmss(st.segment.elapsedSec) +
      "/" +
      _mmss(st.segment.durationSec);
  }
  return out;
}

function _streamPct(st) {
  return st.stream.durationSec > 0 ? (st.stream.elapsedSec / st.stream.durationSec) * 100 : 0;
}

function _anyAudible() {
  for (const name of activeChannels()) {
    const s = channelState(name);
    if (s && s.playing && !s.paused) return true;
  }
  return false;
}

// Sync the floating bar against the message column. Two effects: (1) align its
// side margins so its left meets the assistant bubbles' left edge (the scroller's
// left padding) and its right meets the user bubbles' right edge (right padding
// plus the vertical scrollbar's width -- the bar sits outside the scroller, so it
// would otherwise overhang that gutter); (2) reserve bottom scroll space equal to
// the bar's margin box so the newest message clears it instead of hiding behind
// it. Cleared while the bar is hidden.
function _syncBarLayout() {
  const cm = document.getElementById("chat-messages");
  if (!cm) return;
  if (!_barEl || _barEl.classList.contains("hidden")) {
    // Closing: animate the reserved space away so the newest message settles to
    // the new (higher) bottom smoothly -- the same feel as opening -- rather than
    // snapping when the reserve drops. As padding-bottom shrinks the browser
    // re-clamps scroll to the bottom each frame, so the view rides down with it
    // (and stays put if the user had scrolled up, since then nothing clamps).
    // Nothing reserved means nothing to animate.
    if (cm.style.paddingBottom) {
      cm.classList.add("audio-reserve-animating");
      cm.style.paddingBottom = "";
      setTimeout(() => cm.classList.remove("audio-reserve-animating"), 300);
    }
    return;
  }
  // Opening or resyncing while open: padding here must change instantly (open's
  // smooth feel comes from scrollToBottom, not the padding), so drop the collapse
  // transition first.
  cm.classList.remove("audio-reserve-animating");
  const ccs = getComputedStyle(cm);
  const padL = parseFloat(ccs.paddingLeft) || 0;
  const padR = parseFloat(ccs.paddingRight) || 0;
  const scrollbar = cm.offsetWidth - cm.clientWidth;
  _barEl.style.marginLeft = padL + "px";
  _barEl.style.marginRight = padR + scrollbar + "px";
  // Read height after the side margins settle the bar's width (they drive control
  // wrapping); top/bottom margins still come from the stylesheet.
  const bcs = getComputedStyle(_barEl);
  const my = (parseFloat(bcs.marginTop) || 0) + (parseFloat(bcs.marginBottom) || 0);
  cm.style.paddingBottom = _barEl.offsetHeight + my + "px";
}

// #chat-messages changes geometry from several sources -- window resize, the
// sidebar or inspector panels toggling, and the scrollbar appearing or vanishing
// as the conversation grows -- each shifting the bar's target margins and reserved
// height. A ResizeObserver on the scroller catches them all; the rAF gate
// coalesces bursts.
function _onGeomChange() {
  if (_geomRaf != null) return;
  _geomRaf = requestAnimationFrame(() => {
    _geomRaf = null;
    _syncBarLayout();
  });
}

// Build an inline icon via createElementNS (the module avoids innerHTML so a
// channel name can never break out of an attribute; static icons follow the same
// rule). children is a list of [tagName, attrs] for the svg's shapes.
const _SVG_NS = "http://www.w3.org/2000/svg";
function _icon(viewBox, children) {
  const svg = document.createElementNS(_SVG_NS, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const [tag, attrs] of children) {
    const el = document.createElementNS(_SVG_NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    svg.appendChild(el);
  }
  return svg;
}

function _setReopenVisible(on) {
  if (_reopenEl) _reopenEl.classList.toggle("hidden", !on);
}

// Rebuilds the selector and the selected channel's control surface. Built with
// createElement/textContent (never innerHTML) so an arbitrary channel name cannot
// break out of an attribute, and so the delegated listeners on the stable bar
// element keep working.
function _refreshBar() {
  if (!_barEl) return;
  const wasHidden = _barEl.classList.contains("hidden");
  const names = activeChannels();
  if (_selectedChannel && !names.includes(_selectedChannel)) _selectedChannel = null;
  if (!_selectedChannel && names.length) _selectedChannel = names[0];

  _barEl.innerHTML = "";
  _barEl.classList.toggle("audio-transport-suspended", isContextSuspended());

  // Nothing playing clears a prior dismissal, so the next audio session opens the
  // bar normally instead of staying hidden behind the reopen button.
  if (!names.length) _dismissed = false;
  _setReopenVisible(names.length > 0 && _dismissed);

  if (!names.length || _dismissed) {
    _barEl.classList.add("hidden");
    _syncBarLayout();
    _syncRaf();
    return;
  }
  _barEl.classList.remove("hidden");

  const tabs = document.createElement("div");
  tabs.className = "audio-transport-tabs";
  for (const name of names) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "audio-transport-tab" + (name === _selectedChannel ? " selected" : "");
    tab.dataset.channel = name;
    tab.setAttribute("aria-pressed", name === _selectedChannel ? "true" : "false");
    tab.textContent = name;
    tabs.appendChild(tab);
  }
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "audio-transport-close";
  closeBtn.setAttribute("aria-label", "Close audio player (keeps playing)");
  closeBtn.appendChild(
    _icon("0 0 24 24", [
      ["line", { x1: "6", y1: "6", x2: "18", y2: "18" }],
      ["line", { x1: "18", y1: "6", x2: "6", y2: "18" }],
    ]),
  );
  tabs.appendChild(closeBtn);
  _barEl.appendChild(tabs);

  const st = channelState(_selectedChannel);
  if (st) _barEl.appendChild(_buildControls(_selectedChannel, st));

  _syncBarLayout();
  // Opening the bar lengthens the scroll content (its reserved bottom space),
  // which would leave the newest message tucked behind it; ride the scroll to the
  // new bottom. Goes through the app's autoscroll, so a user who scrolled up to
  // read history is left where they are.
  if (wasHidden) scrollToBottom();
  _syncRaf();
}

function _buildControls(channel, st) {
  const row = document.createElement("div");
  row.className = "audio-transport-row";
  row.dataset.channel = channel;

  const playpause = document.createElement("button");
  playpause.type = "button";
  playpause.className = "audio-transport-playpause";
  playpause.dataset.channel = channel;
  // A finished channel offers Replay (a fresh life) in place of resume; a paused
  // one offers Play; a live one offers Pause.
  if (!st.playing) {
    playpause.dataset.action = "replay";
    playpause.textContent = "Replay";
    playpause.setAttribute("aria-label", "Replay " + channel);
  } else if (st.paused) {
    playpause.dataset.action = "resume";
    playpause.textContent = "Play";
    playpause.setAttribute("aria-label", "Play " + channel);
  } else {
    playpause.dataset.action = "pause";
    playpause.textContent = "Pause";
    playpause.setAttribute("aria-label", "Pause " + channel);
  }

  const repeat = document.createElement("button");
  repeat.type = "button";
  repeat.className = "audio-transport-repeat" + (st.loop ? " on" : "");
  repeat.dataset.channel = channel;
  repeat.setAttribute("aria-pressed", st.loop ? "true" : "false");
  repeat.textContent = "Repeat";

  const progress = document.createElement("div");
  progress.className = "audio-transport-progress";
  progress.dataset.channel = channel;
  const fill = document.createElement("div");
  fill.className = "audio-transport-fill";
  fill.style.width = _streamPct(st) + "%";
  progress.appendChild(fill);

  const time = document.createElement("span");
  time.className = "audio-transport-time";
  time.textContent = _formatTime(st);

  const vol = document.createElement("input");
  vol.type = "range";
  vol.min = "0";
  vol.max = "100";
  vol.className = "audio-transport-vol";
  vol.dataset.channel = channel;
  vol.value = String(Math.round(channelUserVolume(channel) * 100));

  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "audio-transport-stop";
  stop.dataset.channel = channel;
  stop.setAttribute("aria-label", "Stop " + channel);
  stop.textContent = "Stop";

  // Seek bar on its own full-width line above a wrapping control strip, so the
  // scrubber owns the width and the controls never push each other off-screen.
  const controls = document.createElement("div");
  controls.className = "audio-transport-controls";
  controls.append(playpause, repeat, time, vol, stop);
  row.append(progress, controls);
  return row;
}

// Animates the selected channel's progress fill and time readout. Runs only while
// some channel is playing and not paused, so a paused channel shows a frozen fill;
// a fill mid-drag is owned by the drag and left untouched here.
function _tick() {
  const anyPlaying = _anyAudible();
  const st = _selectedChannel ? channelState(_selectedChannel) : null;
  const dragOwnsFill = _dragging && _dragChannel === _selectedChannel;
  if (st && _barEl && !dragOwnsFill) {
    const row = _barEl.querySelector(".audio-transport-row");
    if (row) {
      const fill = row.querySelector(".audio-transport-fill");
      if (fill) fill.style.width = _streamPct(st) + "%";
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
  if (fill) fill.style.width = frac * 100 + "%";
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
  if (e.target.closest(".audio-transport-close")) {
    _dismissed = true;
    _refreshBar();
    return;
  }
  const tab = e.target.closest(".audio-transport-tab");
  if (tab && tab.dataset.channel) {
    _selectedChannel = tab.dataset.channel;
    _refreshBar();
    return;
  }
  const pp = e.target.closest(".audio-transport-playpause");
  if (pp && pp.dataset.channel) {
    if (pp.dataset.action === "pause") pauseChannel(pp.dataset.channel);
    else if (pp.dataset.action === "resume") resumeChannel(pp.dataset.channel);
    else if (pp.dataset.action === "replay") replayChannel(pp.dataset.channel);
    return;
  }
  const rep = e.target.closest(".audio-transport-repeat");
  if (rep && rep.dataset.channel) {
    const st = channelState(rep.dataset.channel);
    setChannelRepeat(rep.dataset.channel, !(st && st.loop));
    return;
  }
  const stop = e.target.closest(".audio-transport-stop");
  if (stop && stop.dataset.channel) stopChannel(stop.dataset.channel);
}

function _onBarPointerDown(e) {
  const prog = e.target.closest(".audio-transport-progress");
  if (!prog || !prog.dataset.channel) return;
  const st = channelState(prog.dataset.channel);
  // Scrubbable while playing, paused, or naturally ended: dragging an ended channel
  // re-arms playback from the drop point (seekChannel). Only a channel with no
  // retained plan -- never played or hard-stopped, so channelState is null -- has
  // nothing to seek.
  if (!st) return;
  _dragging = true;
  _dragChannel = prog.dataset.channel;
  _dragProgressEl = prog;
  _applyDrag(e);
  document.addEventListener("pointermove", _onDragMove, true);
  document.addEventListener("pointerup", _onDragUp, true);
  e.preventDefault();
}

function _onBarInput(e) {
  const slider = e.target.closest(".audio-transport-vol");
  if (slider && slider.dataset.channel) {
    setChannelUserVolume(slider.dataset.channel, Number(slider.value) / 100);
  }
}

// Resumes a context that started suspended under the browser autoplay policy. A
// bare unlock tap changes no engine state and fires no repaint, so the hint class
// is cleared here directly when the resume resolves.
function _bindResumeOnGesture() {
  const resume = () => {
    if (isContextSuspended()) {
      resumeContext()
        .then(() => _barEl && _barEl.classList.remove("audio-transport-suspended"))
        .catch(() => {});
    }
  };
  for (const ev of ["pointerdown", "keydown", "touchend"]) {
    document.addEventListener(ev, resume, true);
  }
}

// Runs once: the _inited guard makes a second call a no-op, so re-invoking at
// boot or after a re-render cannot double-mount the bar or double-bind its
// listeners.
export function initAudioPlayer() {
  if (_inited) return;
  _inited = true;
  _barEl = document.createElement("div");
  _barEl.id = "audio-transport";
  _barEl.className = "audio-transport hidden";
  _barEl.addEventListener("click", _onBarClick);
  _barEl.addEventListener("pointerdown", _onBarPointerDown);
  _barEl.addEventListener("input", _onBarInput);
  // Float it just above the composer, anchored inside #chat-input-area
  // (position:relative) so it stays out of the message flow -- #chat-messages
  // keeps its full height and scrollbar. Inserted as the composer's first child
  // so the burger menu, later in the DOM, still paints above it. Falls back to
  // in-flow, then body, if the expected layout is absent.
  const main = document.getElementById("main");
  const inputArea = document.getElementById("chat-input-area");
  if (inputArea) inputArea.insertBefore(_barEl, inputArea.firstChild);
  else if (main) main.appendChild(_barEl);
  else document.body.appendChild(_barEl);
  // Small reopen button, shown only while the bar is dismissed mid-play; clicking
  // it restores the bar. Mounted beside the bar (same positioning context) so it
  // floats in the same spot above the composer.
  _reopenEl = document.createElement("button");
  _reopenEl.type = "button";
  _reopenEl.id = "audio-reopen";
  _reopenEl.className = "audio-reopen hidden";
  _reopenEl.setAttribute("aria-label", "Show audio player");
  _reopenEl.appendChild(
    _icon("0 0 24 24", [
      ["polygon", { points: "11 5 6 9 2 9 2 15 6 15 11 19 11 5" }],
      ["path", { d: "M15.54 8.46a5 5 0 0 1 0 7.07" }],
      ["path", { d: "M19.07 4.93a10 10 0 0 1 0 14.14" }],
    ]),
  );
  _reopenEl.addEventListener("click", () => {
    _dismissed = false;
    _refreshBar();
  });
  _barEl.parentNode.insertBefore(_reopenEl, _barEl);
  const cm = document.getElementById("chat-messages");
  if (cm && typeof ResizeObserver !== "undefined") {
    _ro = new ResizeObserver(_onGeomChange);
    _ro.observe(cm);
  }
  setBarChangeHook(_refreshBar);
  _bindResumeOnGesture();
}
