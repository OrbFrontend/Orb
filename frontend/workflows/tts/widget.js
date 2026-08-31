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
import { alignmentKey, extractBlocks } from "./extract.js";
import { startKaraoke } from "./karaoke.js";

const WORKFLOW_ID = "tts";
const CHANNEL = "tts";
const EVICTED = "[evicted]";
const AUTOPLAY_POLL_MS = 125;
const AUTOPLAY_MAX_TRIES = 40;

const WAVE = [6, 11, 16, 9, 19, 13, 22, 15, 10, 7, 14, 20, 12, 8, 17, 21, 14, 9, 6, 12, 16, 10];

const ICON_SPEAK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polygon points="3 9 3 15 7 15 12 19 12 5 7 9 3 9"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg>`;
const ICON_PLAY = `<svg class="tts-ic-play" viewBox="0 0 24 24" fill="currentColor"><polygon points="8 5 19 12 8 19 8 5"/></svg>`;
const ICON_PAUSE = `<svg class="tts-ic-pause" viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="5" width="3.5" height="14" rx="1"/><rect x="14" y="5" width="3.5" height="14" rx="1"/></svg>`;

let cfg = { volume: 0.75, click_granularity: "block", click_play_scope: "unit" };

let playingAttId = null;
let playingClass = "";
let channelBound = false;
let autoplayTimer = null;

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "create", (el) => create(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "toggle", (el) => toggle(Number(el.dataset.att)));
  registerClickHandler({ id: WORKFLOW_ID, label: "Speak", claims: speakClaims, onClick: speakOnClick });
}

function bindChannel() {
  if (channelBound) return;
  channelBound = true;
  onChannel(CHANNEL, (ev) => {
    if (ev.type === "play") {
      playingClass = "is-playing";
    } else if (ev.type === "pause") {
      playingClass = "is-paused";
    } else if (ev.type === "close") {
      if (ev.reason === "superseded") return;
      playingAttId = null;
      playingClass = "";
    }
    applyPlayingMark();
  });
}

function applyPlayingMark() {
  for (const el of document.querySelectorAll(".tts-clip.is-playing, .tts-clip.is-paused")) {
    el.classList.remove("is-playing", "is-paused");
  }
  if (playingAttId == null || !playingClass) return;
  const el = document.querySelector(`.tts-clip[data-att="${playingAttId}"]`);
  if (el) el.classList.add(playingClass);
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
  return playAudio({ channel: CHANNEL, segments: wholeSegments(att), volume: cfg.volume });
}

function playBlock(att, i) {
  return playAudio({ channel: CHANNEL, segments: [sliceClip(att, i)], volume: cfg.volume });
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
  playingClass = "";
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
  playingClass = "";
  const play = playBlock(att, i);
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

let _blockMap = { msgId: null, content: null, map: null, wordIndices: null };

function _alignmentFor(msgId) {
  const msg = getMessages().find((m) => m.id === msgId);
  const content = msg?.content || "";
  if (_blockMap.msgId === msgId && _blockMap.content === content) return _blockMap;
  const built = msg ? computeBlockMap(msg) : { map: {}, wordIndices: {}, ready: true };
  if (built.ready) _blockMap = { msgId, content, map: built.map, wordIndices: built.wordIndices };
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
  const blocks = extractBlocks(msg.content || "");
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
  if (hasOwnAttachment(msg)) return "";
  if (!canMutate()) {
    return `<button class="tts-create-btn" disabled title="Close other tabs to generate speech">${ICON_SPEAK}</button>`;
  }
  return `<button class="tts-create-btn" title="Generate speech" data-wf-action="tts:create" data-msg-id="${msg.id}">${ICON_SPEAK}</button>`;
}

export function attachmentRenderer(ctx) {
  const att = ctx.att;
  const live = att.id === playingAttId && playingClass ? ` ${playingClass}` : "";
  const bars = WAVE.map((h, i) => `<span class="tts-bar" style="height:${h}px;--i:${i}"></span>`).join("");
  return `<div class="tts-clip${live}" data-att="${att.id}">
    <button class="tts-toggle" title="Play speech" aria-label="Play speech" data-wf-action="tts:toggle" data-att="${att.id}">${ICON_PLAY}${ICON_PAUSE}</button>
    <span class="tts-wave" aria-hidden="true">${bars}</span>
    <span class="tts-clip-actions">${ctx.buttons.regen}${ctx.buttons.reroll}</span>
  </div>`;
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
