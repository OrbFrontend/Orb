import {
  api,
  canMutate,
  channelState,
  clearWorkflowPhase,
  convUrl,
  getActiveConvId,
  getMessages,
  messageSegments,
  onChannel,
  pauseChannel,
  playAudio,
  refreshConversationMessages,
  registerAction,
  registerClickHandler,
  resumeChannel,
  setWorkflowPhase,
} from "/static/workflow_api.js";
import { alignmentKey, attachmentBlocks } from "./extract.js";
import { startKaraoke } from "./karaoke.js";

const WORKFLOW_ID = "tts";
const CHANNEL = "tts";
const EVICTED = "[evicted]";
const AUTOPLAY_POLL_MS = 125;
const AUTOPLAY_MAX_TRIES = 40;

const ICON_SPEAK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polygon points="3 9 3 15 7 15 12 19 12 5 7 9 3 9"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg>`;
const ICON_PLAY = `<svg class="tts-ic-play" viewBox="0 0 24 24" fill="currentColor"><polygon points="8 5 19 12 8 19 8 5"/></svg>`;
const ICON_PAUSE = `<svg class="tts-ic-pause" viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="5" width="3.5" height="14" rx="1"/><rect x="14" y="5" width="3.5" height="14" rx="1"/></svg>`;
const ICON_CARET = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="15"><polyline points="6 10 12 16 18 10"/></svg>`;

let cfg = { volume: 0.75, click_granularity: "block", click_play_scope: "unit" };

let playingAttId = null;
let channelBound = false;
let autoplayTimer = null;
let chipRaf = null;
let menuEl = null;
let menuCaret = null;

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "create", (el) => create(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "toggle", (el) => toggle(Number(el.dataset.att)));
  registerAction(WORKFLOW_ID, "menu", (el) => (menuCaret === el ? closeMenu() : openMenu(el)));
  // Menu items live on <body>, so the in-flight button handed to the shared
  // handlers is the caret back in the toolbar: it is what gets disabled, and
  // its chip is where a failure caption lands.
  registerAction(WORKFLOW_ID, "regenerate", (el) => {
    window.workflowRegenerate?.(Number(el.dataset.msgId), Number(el.dataset.att), takeMenuAnchor(el));
  });
  registerAction(WORKFLOW_ID, "reroll", (el) => {
    window.workflowReroll?.(Number(el.dataset.msgId), Number(el.dataset.att), takeMenuAnchor(el));
  });
  registerAction(WORKFLOW_ID, "step", (el) => {
    closeMenu();
    window.workflowArtifactStep?.(el.dataset.instanceId, Number(el.dataset.delta));
  });
  registerAction(WORKFLOW_ID, "delete", (el) => {
    closeMenu();
    window.workflowDeleteAttachment?.(el.dataset.instanceId);
  });
  registerAction(WORKFLOW_ID, "rehydrate", (el) => {
    window.workflowRehydrate?.(Number(el.dataset.msgId), Number(el.dataset.att), takeMenuAnchor(el));
  });
  registerClickHandler({ id: WORKFLOW_ID, label: "Speak", claims: speakClaims, onClick: speakOnClick });
}

function takeMenuAnchor(item) {
  const caret = document.querySelector(`.tts-speech-chip[data-att="${item.dataset.att}"] .tts-chip-caret`);
  closeMenu();
  return caret || item;
}

// `.message` is content-visibility:auto, which paint-contains the bubble: a
// menu positioned anywhere inside it is clipped at the bubble's edge.
function openMenu(caret) {
  closeMenu();
  const chip = caret.closest(".tts-speech-chip");
  if (!chip) return;
  const menu = document.createElement("div");
  menu.className = "wf-claim-popover tts-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = chip.querySelector("template.tts-menu-items")?.innerHTML || "";
  document.body.appendChild(menu);
  menuEl = menu;
  menuCaret = caret;
  caret.setAttribute("aria-expanded", "true");
  const anchor = chip.getBoundingClientRect();
  const box = menu.getBoundingClientRect();
  let left = anchor.right - box.width;
  let top = anchor.bottom + 4;
  if (top + box.height > window.innerHeight - 8) top = anchor.top - box.height - 4;
  left = Math.min(Math.max(8, left), window.innerWidth - box.width - 8);
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  document.addEventListener("pointerdown", onMenuOutside, true);
  document.addEventListener("keydown", onMenuKey, true);
  document.addEventListener("scroll", closeMenu, true);
  window.addEventListener("resize", closeMenu);
}

function closeMenu() {
  if (!menuEl) return;
  menuEl.remove();
  menuCaret?.setAttribute("aria-expanded", "false");
  menuEl = null;
  menuCaret = null;
  document.removeEventListener("pointerdown", onMenuOutside, true);
  document.removeEventListener("keydown", onMenuKey, true);
  document.removeEventListener("scroll", closeMenu, true);
  window.removeEventListener("resize", closeMenu);
}

function onMenuOutside(e) {
  if (menuEl?.contains(e.target) || menuCaret?.contains(e.target)) return;
  closeMenu();
}

function onMenuKey(e) {
  if (e.key !== "Escape") return;
  const caret = menuCaret;
  closeMenu();
  caret?.focus();
}

function bindChannel() {
  if (channelBound) return;
  channelBound = true;
  onChannel(CHANNEL, (ev) => {
    if (ev.type === "play") {
      armChipRaf();
    } else if (ev.type === "close") {
      if (ev.reason === "superseded") return;
      playingAttId = null;
      cancelChipRaf();
    } else if (ev.type === "pause") {
      cancelChipRaf();
    }
    applyPlayingMark();
  });
}

function applyPlayingMark() {
  for (const el of document.querySelectorAll(".tts-speech-chip.is-playing, .tts-speech-chip.is-paused")) {
    el.classList.remove("is-playing", "is-paused");
  }
  const st = channelState(CHANNEL);
  const live = playingAttId != null && st?.playing;
  if (live) {
    const el = document.querySelector(`.tts-speech-chip[data-att="${playingAttId}"]`);
    if (el) el.classList.add(st.paused ? "is-paused" : "is-playing");
  }
  for (const el of document.querySelectorAll(".tts-speech-chip[data-att]")) {
    const att = attById(Number(el.dataset.att));
    updateChipTime(el, att, live && Number(el.dataset.att) === playingAttId ? st : null);
  }
}

function formatTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const secondsPart = total % 60;
  return `${minutes}:${secondsPart < 10 ? `0${secondsPart}` : secondsPart}`;
}

function durationMs(att) {
  const cm = att?.consumption_metadata;
  const explicit = Number(cm?.duration_ms);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  const blocks = blocksOf(att);
  const summed = blocks.reduce(
    (total, block) =>
      total + Math.max(0, Number(block.duration_ms) || 0) + Math.max(0, Number(block.pause_after_ms) || 0),
    0,
  );
  return summed > 0 ? summed : 0;
}

function updateChipTime(el, att, st) {
  const time = el.querySelector(".tts-chip-time");
  const symbol = el.querySelector(".tts-chip-symbol");
  if (!time || !symbol) return;
  const duration = st?.stream?.durationSec > 0 ? st.stream.durationSec : durationMs(att) / 1000;
  const icon = st?.playing && !st.paused ? "pause" : "play";
  if (symbol.dataset.icon !== icon) {
    symbol.innerHTML = icon === "pause" ? ICON_PAUSE : ICON_PLAY;
    symbol.dataset.icon = icon;
  }
  if (st?.playing) {
    time.textContent = `${formatTime(st.stream.elapsedSec)} / ${formatTime(duration)}`;
    el.setAttribute(
      "aria-label",
      `${st.paused ? "Resume" : "Pause"} speech, ${formatTime(st.stream.elapsedSec)} of ${formatTime(duration)}`,
    );
  } else {
    time.textContent = duration > 0 ? formatTime(duration) : "";
    el.setAttribute("aria-label", `Play speech${duration > 0 ? `, ${formatTime(duration)}` : ""}`);
  }
}

function armChipRaf() {
  if (chipRaf == null) chipRaf = requestAnimationFrame(tickChipRaf);
}

function cancelChipRaf() {
  if (chipRaf != null) cancelAnimationFrame(chipRaf);
  chipRaf = null;
}

function tickChipRaf() {
  chipRaf = null;
  applyPlayingMark();
  const st = channelState(CHANNEL);
  if (st?.playing && !st.paused) armChipRaf();
}

function attById(attId) {
  for (const m of getMessages()) {
    for (const a of m.workflow_attachments || []) {
      if (a.id === attId && a.workflow_id === WORKFLOW_ID) return a;
    }
  }
  return null;
}

function msgIdForAtt(attId) {
  for (const m of getMessages()) {
    for (const a of m.workflow_attachments || []) {
      if (a.id === attId && a.workflow_id === WORKFLOW_ID) return m.id;
    }
  }
  return null;
}

function sliceClip(att, i) {
  const blk = att.consumption_metadata.blocks[i];
  const raw = atob(att.b64 || att.data_b64 || "");
  return { b64: btoa(raw.slice(blk.byte_start, blk.byte_end)) };
}

function buildSegPlan(blocks) {
  const plan = [];
  for (let i = 0; i < blocks.length; i++) {
    if (blocks[i].byte_end > blocks[i].byte_start) plan.push({ block: i, gap: false });
    if (blocks[i].pause_after_ms > 0) plan.push({ block: i, gap: true });
  }
  return plan;
}

function wholeSegments(att) {
  const blocks = att.consumption_metadata?.blocks;
  if (!Array.isArray(blocks) || !blocks.length) return [{ row: att.id }];
  return buildSegPlan(blocks).map((step) =>
    step.gap ? { silence: blocks[step.block].pause_after_ms / 1000 } : sliceClip(att, step.block),
  );
}

function playWhole(att) {
  const msgId = msgIdForAtt(att.id);
  return playAudio({
    channel: CHANNEL,
    segments: wholeSegments(att),
    volume: cfg.volume,
    source: { label: messageLabel(msgId), msgId },
  });
}

function playBlock(att, i, msgId) {
  return playAudio({
    channel: CHANNEL,
    segments: [sliceClip(att, i)],
    volume: cfg.volume,
    source: { label: messageLabel(msgId), msgId },
  });
}

function blocksOf(att) {
  const blocks = att.consumption_metadata?.blocks;
  return Array.isArray(blocks) ? blocks : [];
}

function startPlay(attId) {
  bindChannel();
  const att = attById(attId);
  if (!att) return;
  playingAttId = attId;
  const msgId = msgIdForAtt(attId);
  const blocks = blocksOf(att);
  const play = playWhole(att);
  startKaraoke({
    msgId,
    segPlan: buildSegPlan(blocks),
    blocks,
    getWordIndices: () => (msgId != null ? blockWordIndicesFor(msgId) : {}),
    play,
  });
}

function startBlockPlay(att, i, msgId) {
  bindChannel();
  playingAttId = att.id;
  const play = playBlock(att, i, msgId);
  startKaraoke({
    msgId,
    segPlan: [{ block: i, gap: false }],
    blocks: blocksOf(att),
    getWordIndices: () => (msgId != null ? blockWordIndicesFor(msgId) : {}),
    play,
  });
}

function toggle(attId) {
  bindChannel();
  const st = channelState(CHANNEL);
  if (playingAttId === attId && st && st.playing) {
    if (st.paused) resumeChannel(CHANNEL);
    else pauseChannel(CHANNEL);
    return;
  }
  startPlay(attId);
}

function ttsAttachmentForMessage(msgId) {
  const msg = getMessages().find((m) => m.id === msgId);
  if (!msg) return null;
  const atts = (msg.workflow_attachments || []).filter((a) => a.workflow_id === WORKFLOW_ID);
  if (!atts.length) return null;
  const att = activeSibling(atts);
  const b64 = att.b64 || att.data_b64 || "";
  if (!b64 || b64 === EVICTED) return null;
  return att;
}

function messageLabel(msgId) {
  if (msgId == null) return "Speech";
  const msg = getMessages().find((item) => item.id === msgId);
  return msg?.speaker_name || msg?.name || "Speech";
}

let _blockMap = { msgId: null, content: null, attachment: null, map: null, wordIndices: null };

function _alignmentFor(msgId) {
  const msg = getMessages().find((m) => m.id === msgId);
  const content = msg?.content || "";
  const attachment = ttsAttachmentForMessage(msgId);
  if (_blockMap.msgId === msgId && _blockMap.content === content && _blockMap.attachment === attachment)
    return _blockMap;
  const built = msg ? computeBlockMap(msg) : { map: {}, wordIndices: {}, ready: true };
  if (built.ready) _blockMap = { msgId, content, attachment, map: built.map, wordIndices: built.wordIndices };
  return built.ready ? _blockMap : { map: built.map, wordIndices: built.wordIndices };
}

function blockMapFor(msgId) {
  return _alignmentFor(msgId).map;
}

function blockWordIndicesFor(msgId) {
  return _alignmentFor(msgId).wordIndices;
}

function computeBlockMap(msg) {
  const map = {};
  const wordIndices = {};
  const att = ttsAttachmentForMessage(msg.id);
  const cm = att?.consumption_metadata;
  const clipCount = cm && Array.isArray(cm.blocks) ? cm.blocks.length : 0;
  if (!clipCount) return { map, wordIndices, ready: true };
  const segs = messageSegments(msg.id);
  if (!segs.length) return { map, wordIndices, ready: false };
  const blocks = attachmentBlocks(msg.content || "", cm.blocks);
  const words = segs.map((s) => ({ wordIndex: s.wordIndex, t: alignmentKey(s.word) }));
  const limit = Math.min(blocks.length, clipCount);
  let cursor = 0;
  for (let bi = 0; bi < limit; bi++) {
    const tokens = blocks[bi].split(/\s+/).map(alignmentKey).filter(Boolean);
    if (!tokens.length) continue;
    const at = _findRun(words, tokens, cursor);
    if (at < 0) continue;
    const idxs = [];
    for (let k = 0; k < tokens.length; k++) {
      const wi = words[at + k].wordIndex;
      map[wi] = bi;
      idxs.push(wi);
    }
    wordIndices[bi] = idxs;
    cursor = at + tokens.length;
  }
  return { map, wordIndices, ready: true };
}

function _findRun(words, tokens, from) {
  for (let i = from; i + tokens.length <= words.length; i++) {
    let ok = true;
    for (let k = 0; k < tokens.length; k++) {
      if (words[i + k].t !== tokens[k]) {
        ok = false;
        break;
      }
    }
    if (ok) return i;
  }
  return -1;
}

function speakClaims(seg) {
  if (seg.role !== "assistant") return false;
  if (cfg.click_granularity === "none") return false;
  if (ttsAttachmentForMessage(seg.msgId) == null) return false;
  if (cfg.click_granularity === "message") return true;
  return blockMapFor(seg.msgId)[seg.wordIndex] != null;
}

function speakOnClick(seg, msgId) {
  const att = ttsAttachmentForMessage(msgId);
  if (!att) return;
  if (cfg.click_play_scope === "whole" || cfg.click_granularity === "message") {
    startPlay(att.id);
    return;
  }
  const bi = blockMapFor(msgId)[seg.wordIndex];
  if (bi == null) return;
  startBlockPlay(att, bi, msgId);
}

async function create(msgId, btn) {
  if (!getActiveConvId() || !canMutate()) return;
  if (btn) btn.disabled = true;
  const ch = `workflow:tts:create:${msgId}`;
  try {
    setWorkflowPhase(ch, "Synthesizing speech...");
    const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
      action: "create",
      message_id: msgId,
    });
    if (res?.error) {
      console.warn("tts create:", res.error);
      if (btn) btn.disabled = false;
      return;
    }
    await refreshConversationMessages(msgId);
  } catch (e) {
    console.error("tts create failed", e);
    if (btn) btn.disabled = false;
  } finally {
    clearWorkflowPhase(ch);
  }
}

function hasOwnAttachment(msg) {
  const atts = Array.isArray(msg.workflow_attachments) ? msg.workflow_attachments : [];
  return atts.some((a) => a.workflow_id === WORKFLOW_ID);
}

export function createButtonRenderer(msg) {
  if (msg?.role !== "assistant" || !msg.id) return "";
  if (hasOwnAttachment(msg)) {
    const atts = (msg.workflow_attachments || []).filter((a) => a.workflow_id === WORKFLOW_ID);
    return attachmentRenderer({ msg, att: activeSibling(atts) });
  }
  if (!canMutate()) {
    return `<button class="tts-create-btn" disabled title="Close other tabs to generate speech">${ICON_SPEAK}</button>`;
  }
  return `<button class="tts-create-btn" title="Generate speech" data-wf-action="tts:create" data-msg-id="${msg.id}">${ICON_SPEAK}</button>`;
}

export function attachmentRenderer(ctx) {
  const att = ctx.att;
  const msg = ctx.msg;
  const atts = (msg?.workflow_attachments || []).filter((a) => a.workflow_id === WORKFLOW_ID);
  const root = atts.find((a) => a.parent_attachment_id == null) || att;
  const index = atts.indexOf(att);
  const total = atts.length;
  const instanceId = msg?.id ? `ws-${msg.id}-${root.id}` : "";
  const state = att.id === playingAttId ? channelState(CHANNEL) : null;
  const duration = durationMs(att) / 1000;
  const shownDuration = state?.stream?.durationSec > 0 ? state.stream.durationSec : duration;
  const shownTime = state?.playing
    ? `${formatTime(state.stream.elapsedSec)} / ${formatTime(shownDuration)}`
    : shownDuration > 0
      ? formatTime(shownDuration)
      : "";
  const canEdit = canMutate();
  const mutationDisabled = canEdit ? "" : " disabled";
  const item = `type="button" role="menuitem" class="wf-claim-item tts-menu-item"`;
  const stepButtons =
    total > 1
      ? `<button ${item} data-wf-action="tts:step" data-instance-id="${instanceId}" data-delta="-1"${index <= 0 || !canEdit ? " disabled" : ""}>Previous take</button>
       <button ${item} data-wf-action="tts:step" data-instance-id="${instanceId}" data-delta="1"${index < 0 || index >= total - 1 || !canEdit ? " disabled" : ""}>Next take</button>`
      : "";
  const evicted = (att.b64 || att.data_b64) === EVICTED;
  const restore = evicted
    ? `<button ${item} data-wf-action="tts:rehydrate" data-msg-id="${msg?.id || ""}" data-att="${att.id}"${mutationDisabled}>Restore speech</button>`
    : "";
  const icon = state?.playing && !state.paused ? "pause" : "play";
  return `<span class="tts-speech-chip${state?.playing ? (state.paused ? " is-paused" : " is-playing") : ""}" id="${instanceId}" data-msg-id="${msg?.id || ""}" data-root-id="${root.id}" data-att="${att.id}">
    <button type="button" class="tts-chip-play" title="Play speech" data-wf-action="tts:toggle" data-att="${att.id}"${evicted ? " disabled" : ""}>
      <span class="tts-chip-symbol" data-icon="${icon}" aria-hidden="true">${icon === "pause" ? ICON_PAUSE : ICON_PLAY}</span><span class="tts-chip-time">${shownTime}</span>
    </button>
    <button type="button" class="tts-chip-caret" title="Speech options" aria-label="Speech options" aria-haspopup="menu" aria-expanded="false" data-wf-action="tts:menu" data-att="${att.id}">${ICON_CARET}</button>
    <template class="tts-menu-items">
      ${restore}
      <button ${item} data-wf-action="tts:regenerate" data-msg-id="${msg?.id || ""}" data-att="${att.id}"${mutationDisabled}>Regenerate speech</button>
      <button ${item} data-wf-action="tts:reroll" data-msg-id="${msg?.id || ""}" data-att="${att.id}"${mutationDisabled}>New take</button>
      ${stepButtons}
      <button type="button" role="menuitem" class="wf-claim-item tts-menu-item danger" data-wf-action="tts:delete" data-instance-id="${instanceId}"${mutationDisabled}>Delete speech</button>
    </template>
  </span>`;
}

function activeSibling(atts) {
  if (atts.length === 1) return atts[0];
  const root = atts.find((a) => a.parent_attachment_id == null) || atts[0];
  if (root.active_sibling_id != null) {
    const chosen = atts.find((a) => a.id === root.active_sibling_id);
    if (chosen) return chosen;
  }
  return atts[atts.length - 1];
}

function freshAttachmentId(seen) {
  const msgs = getMessages();
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m.role !== "assistant") continue;
    const atts = (m.workflow_attachments || []).filter((a) => a.workflow_id === WORKFLOW_ID);
    if (!atts.length) continue;
    const att = activeSibling(atts);
    if (seen.has(att.id)) continue;
    const b64 = att.b64 || att.data_b64 || "";
    if (!b64 || b64 === EVICTED) continue;
    return att.id;
  }
  return null;
}

export function autoplayHandler() {
  if (autoplayTimer) {
    clearInterval(autoplayTimer);
    autoplayTimer = null;
  }
  const seen = new Set();
  for (const m of getMessages()) {
    for (const a of m.workflow_attachments || []) {
      if (a.workflow_id === WORKFLOW_ID) seen.add(a.id);
    }
  }
  let tries = 0;
  autoplayTimer = setInterval(() => {
    tries += 1;
    const id = freshAttachmentId(seen);
    if (id != null) {
      clearInterval(autoplayTimer);
      autoplayTimer = null;
      startPlay(id);
    } else if (tries >= AUTOPLAY_MAX_TRIES) {
      clearInterval(autoplayTimer);
      autoplayTimer = null;
    }
  }, AUTOPLAY_POLL_MS);
}
