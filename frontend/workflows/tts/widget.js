// Per-message create affordance, the attachment playback control, and
// auto-play. Playback runs on the shared audio engine's "tts" channel (the
// framework mounts the transport bar); message bytes are read from S.messages
// by row id, never written. The widget reflects live playback state by toggling
// classes on the clip whose row the channel is playing.

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
import { extractBlocks } from "./extract.js";
import { startKaraoke } from "./karaoke.js";

const WORKFLOW_ID = "tts";
const CHANNEL = "tts";
const EVICTED = "[evicted]";
const AUTOPLAY_POLL_MS = 125;
const AUTOPLAY_MAX_TRIES = 40;

// Fixed pseudo-waveform silhouette (bar heights in px). Decorative; the bars
// ripple via CSS while the clip plays.
const WAVE = [6, 11, 16, 9, 19, 13, 22, 15, 10, 7, 14, 20, 12, 8, 17, 21, 14, 9, 6, 12, 16, 10];

const ICON_SPEAK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polygon points="3 9 3 15 7 15 12 19 12 5 7 9 3 9"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg>`;
const ICON_PLAY = `<svg class="tts-ic-play" viewBox="0 0 24 24" fill="currentColor"><polygon points="8 5 19 12 8 19 8 5"/></svg>`;
const ICON_PAUSE = `<svg class="tts-ic-pause" viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="5" width="3.5" height="14" rx="1"/><rect x="14" y="5" width="3.5" height="14" rx="1"/></svg>`;

// Same object identity as index.js and config_panel.js: a volume change saved in
// the panel is read here on the next play without re-wiring.
let cfg = { volume: 0.75, click_granularity: "block", click_play_scope: "unit" };

// Re-applied on render (see attachmentRenderer) so a re-paint mid playback does
// not drop the playing/paused indicator.
let playingAttId = null;
let playingClass = "";
let channelBound = false;
let autoplayTimer = null;

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  // Buttons wire via data-wf-action (see the button renderers) resolved by the
  // framework's delegated dispatcher — no window globals, no inline onclick.
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
      // A newer play supersedes the old plan and emits its own "play"; leave the
      // indicator for that event to move rather than clearing it here.
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

// Callers hand the widget an attachment id, but playback needs the row's bytes
// and block metadata; resolve the live row out of S.messages.
function attById(attId) {
  for (const m of getMessages()) {
    for (const a of m.workflow_attachments || []) {
      if (a.id === attId && a.workflow_id === WORKFLOW_ID) return a;
    }
  }
  return null;
}

// The id of the message owning a speech attachment, for binding karaoke to that
// message's rendered word units; null when the attachment is not loaded.
function msgIdForAtt(attId) {
  for (const m of getMessages()) {
    for (const a of m.workflow_attachments || []) {
      if (a.id === attId && a.workflow_id === WORKFLOW_ID) return m.id;
    }
  }
  return null;
}

// One block's complete clip, sliced out of the row's concatenated bytes.
function sliceClip(att, i) {
  const blk = att.consumption_metadata.blocks[i];
  const raw = atob(att.b64 || att.data_b64 || "");
  return { b64: btoa(raw.slice(blk.byte_start, blk.byte_end)) };
}

// Ordered playback plan for a row: a CLIP step per block that produced audio,
// each followed by a GAP step when the block carries trailing silence. This is
// the single source of segment ordering -- wholeSegments renders audio from it
// and the karaoke driver maps the engine's segment index against it, so the two
// cannot drift. An empty-byte block contributes no clip (the engine drops it)
// but still contributes its gap, keeping the plan length equal to the engine's
// segment count.
function buildSegPlan(blocks) {
  const plan = [];
  for (let i = 0; i < blocks.length; i++) {
    if (blocks[i].byte_end > blocks[i].byte_start) plan.push({ block: i, gap: false });
    if (blocks[i].pause_after_ms > 0) plan.push({ block: i, gap: true });
  }
  return plan;
}

// Segment list for whole-message playback, built from the segment plan: a clip
// step slices its block's bytes, a gap step becomes inter-block silence. A row
// whose metadata carries no blocks (absent or unparseable) holds a single
// complete file and plays whole by row id.
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

// Null when the active sibling is evicted ([evicted] sentinel): an evicted row
// is present but its bytes are gone, so it cannot be played.
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

// Bidirectional word/block alignment for one message, memoized on
// (msgId, content): the framework calls claims for every word on every render,
// so recomputing the whole alignment per word would be quadratic. `map` is
// {wordIndex: blockIndex} for click-target lookup; `wordIndices` is
// {blockIndex: [wordIndex,...]} for the words karaoke highlights per clip.
let _blockMap = { msgId: null, content: null, map: null, wordIndices: null };

function _alignmentFor(msgId) {
  const msg = getMessages().find((m) => m.id === msgId);
  const content = msg?.content || "";
  if (_blockMap.msgId === msgId && _blockMap.content === content) return _blockMap;
  const built = msg ? computeBlockMap(msg) : { map: {}, wordIndices: {}, ready: true };
  // A streamed reply appears in S.messages (so autoplay can target it) before its
  // body is finalized into addressable word spans. An alignment computed against
  // that not-yet-segmented body is empty; caching it would freeze the emptiness
  // until the message text changed, so karaoke would never light up. Memoize only
  // a settled result and recompute the transient one on the next call.
  if (built.ready) _blockMap = { msgId, content, map: built.map, wordIndices: built.wordIndices };
  return built.ready ? _blockMap : { map: built.map, wordIndices: built.wordIndices };
}

function blockMapFor(msgId) {
  return _alignmentFor(msgId).map;
}

function blockWordIndicesFor(msgId) {
  return _alignmentFor(msgId).wordIndices;
}

const _norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, "");

// Align extracted block substrings against the rendered word units. Each block
// is anchored independently and the cursor only advances past a block that
// fully matched, so a block that fails to align leaves its own words unclaimed
// without shifting later blocks. Block index is positional against the stored
// clips, capped at the clip count so a word never maps past the audio. Builds
// both alignment directions in one pass; returns empty maps for a row without
// block metadata, leaving its words unclickable and un-karaoke'd.
function computeBlockMap(msg) {
  const map = {};
  const wordIndices = {};
  const att = ttsAttachmentForMessage(msg.id);
  const cm = att?.consumption_metadata;
  const clipCount = cm && Array.isArray(cm.blocks) ? cm.blocks.length : 0;
  if (!clipCount) return { map, wordIndices, ready: true };
  const segs = messageSegments(msg.id);
  // No spans means the body is not segmented yet (a freshly streamed reply still
  // mid-finalize); report not-ready so the caller does not memoize the empty map.
  if (!segs.length) return { map, wordIndices, ready: false };
  const blocks = extractBlocks(msg.content || "");
  const words = segs.map((s) => ({ wordIndex: s.wordIndex, t: _norm(s.word) }));
  const limit = Math.min(blocks.length, clipCount);
  let cursor = 0;
  for (let bi = 0; bi < limit; bi++) {
    const tokens = blocks[bi].split(/\s+/).map(_norm).filter(Boolean);
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

// First index >= from where the token run matches consecutive words.
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

// A word is clickable when its reply is voiced and the live granularity admits
// it: "message" claims every word; "block" only words that map to a clip.
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

// Toolbar button: offered only on a persisted assistant message that has no
// speech attachment yet (auto-generation or a prior create removes it).
export function createButtonRenderer(msg) {
  if (!msg || msg.role !== "assistant" || !msg.id) return "";
  if (hasOwnAttachment(msg)) return "";
  if (!canMutate()) {
    return `<button class="tts-create-btn" disabled title="Close other tabs to generate speech">${ICON_SPEAK}</button>`;
  }
  return `<button class="tts-create-btn" title="Generate speech" data-wf-action="tts:create" data-msg-id="${msg.id}">${ICON_SPEAK}</button>`;
}

// Swipe-widget body for a speech attachment. The regen/reroll buttons are
// supplied by the framework via ctx.buttons, not minted here.
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

// The newest playable speech attachment not present when auto-play was armed --
// i.e. the one this turn produced, once the post-turn refetch lands.
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

// Handles the backend's post-turn auto-play signal. The signal fires before the
// framework refetches the finished reply, so the attachment is not in S yet;
// snapshot the speech rows present now and poll until a new one appears, then
// play it. Bounded so a generation that never lands stops the poll.
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
