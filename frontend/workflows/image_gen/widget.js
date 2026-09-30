import {
  activateWorkflowVariant,
  api,
  canMutate,
  clearWorkflowPhase,
  convUrl,
  esc,
  escAttr,
  getActiveConvId,
  getMessages,
  refreshConversationMessages,
  registerAction,
  registerRegenerateSettled,
  registerRerollParams,
  registerRerollSuccess,
  registerWorkflowEventHandler,
  requestRepaint,
  setWorkflowPhase,
  sseEvents,
  startWorkflowJob,
  stopButtonState,
  streamPost,
  toast,
} from "/static/workflow_api.js";
import {
  attachmentDetailsHtml,
  downloadButtonHtml,
  hasAttachment,
  messageButtonHtml,
  refineRun,
  refineTimelineHtml,
  viewToggleHtml,
} from "./render.js";

const WORKFLOW_ID = "image_gen";
const FOCUS_VIEW_KEY = "orb.imageGen.focusView";
const REFINE_OPEN_KEY = "orb.imageGen.refineOpen";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="15" height="15"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 17 5-5 4 4 2-2 5 5"/></svg>`;
let cfg;

const inFlight = new Map(); // msgId -> job

const pendingEdits = new Map(); // attId -> edited fields
const rerollEditSnapshots = new Map(); // attId -> edit object submitted by the current reroll

let focusView = loadFlag(FOCUS_VIEW_KEY); // image fills the card, details hidden

// ── refinement timeline state ──
// Stages and reviews arrive between attachment refreshes, so they are merged
// over the current saved rows when the strip is drawn.
const liveRuns = new Map(); // run -> { msgId, rootId, source: "fresh" | "regen", run, stage, render, turns }
const liveReviews = new Map(); // attId -> { review, ended }
const openRows = new Set(); // attIds whose critique is unclamped
let timelineConv = null;
let refineOpen = loadFlag(REFINE_OPEN_KEY);

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "generate", (el) => generate(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "savePrompt", savePrompt);
  registerAction(WORKFLOW_ID, "editPrompt", editPrompt);
  registerAction(WORKFLOW_ID, "toggleDetails", toggleDetails);
  registerAction(WORKFLOW_ID, "download", download);
  registerAction(WORKFLOW_ID, "refineToggle", refineToggle);
  registerAction(WORKFLOW_ID, "refineMore", refineMore);
  registerAction(WORKFLOW_ID, "refineShow", refineShow);
  registerRerollParams(WORKFLOW_ID, rerollParams);
  registerRerollSuccess(WORKFLOW_ID, clearPendingEdit);
  // A run started from the card's regenerate button reports on that stream.
  registerWorkflowEventHandler(WORKFLOW_ID, "image_gen_refine_stage", (data) => onRefineStage(data, "regen"));
  registerWorkflowEventHandler(WORKFLOW_ID, "image_gen_review", onReview);
  registerRegenerateSettled(WORKFLOW_ID, (msgId, rootId) => endLiveRuns(msgId, "regen", rootId));
}

function loadFlag(key) {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function saveFlag(key, value) {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch (e) {
    console.warn(`persist ${key} failed`, e);
  }
}

// One view for every card, flipped in place: requestRepaint is skipped while a
// reply streams, which would leave the button dead until the stream ended.
function toggleDetails(el) {
  focusView = !focusView;
  saveFlag(FOCUS_VIEW_KEY, focusView);
  for (const card of document.querySelectorAll(".image-gen-attachment")) {
    card.classList.toggle("image-gen-focus", focusView);
    card.querySelector(".image-gen-view-btn").setAttribute("aria-pressed", String(!focusView));
  }
  // Cards around it resized too; a focus card is pane-sized, so the reveal alone places it.
  el.closest(".image-gen-attachment").scrollIntoView({ block: "nearest", behavior: "instant" });
}

// Fetched rather than linked so the export's note -- this PNG was converted from
// the stored copy, because the original is gone -- reaches the user.
async function download(el) {
  const attId = Number(el.dataset.attId);
  if (!Number.isInteger(attId) || attId <= 0 || el.disabled) return;
  el.disabled = true;
  try {
    const response = await fetch(`/api/workflow-attachments/${attId}/export`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(
        typeof body?.detail === "string" && body.detail ? body.detail : `Download failed (${response.status})`,
      );
    }
    const href = URL.createObjectURL(await response.blob());
    const a = document.createElement("a");
    a.href = href;
    a.download = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "")?.[1] || "image.png";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 0);
    const note = response.headers.get("X-Orb-Export-Note");
    if (note) toast(note);
  } catch (e) {
    toast(e?.message || "Download failed", "error");
  } finally {
    el.disabled = false;
  }
}

function editPrompt(el) {
  const t = document.querySelector(
    `.image-gen-edit[data-att-id="${el.dataset.attId}"][data-field="${el.dataset.field}"]`,
  );
  if (!t) return;
  t.readOnly = false;
  const grow = () => (t.parentElement.dataset.value = t.value);
  t.addEventListener("input", grow);
  t.addEventListener(
    "blur",
    () => {
      t.readOnly = true;
      t.removeEventListener("input", grow);
    },
    { once: true },
  );
  t.focus();
}

function savePrompt(el) {
  const attId = Number(el.dataset.attId);
  const fields = document.querySelectorAll(`.image-gen-edit[data-att-id="${attId}"]`);
  const edit = { ...(pendingEdits.get(attId) || {}) };
  for (const t of fields) edit[t.dataset.field] = t.value;
  const blanked = typeof edit.prompt === "string" && !edit.prompt.trim();
  if (blanked) delete edit.prompt;
  if (Object.keys(edit).length) pendingEdits.set(attId, edit);
  else pendingEdits.delete(attId);
  if (!document.activeElement?.classList.contains("image-gen-edit")) requestRepaint();
  if (blanked) toast("A prompt is required — the previous one was kept", "error");
  else toast("Prompt edited — reroll to render");
}

function rerollParams(_msgId, attId) {
  const edits = pendingEdits.get(attId);
  rerollEditSnapshots.set(attId, edits);
  const params = { ...(edits || {}) };
  if (cfg?.default_style) params.style_id = cfg.default_style; // the tools-panel picker
  return Object.keys(params).length ? params : null;
}

function clearPendingEdit(_msgId, attId) {
  const submitted = rerollEditSnapshots.get(attId);
  rerollEditSnapshots.delete(attId);
  // Keep a newer edit made while the request was running; only the edit that
  // produced the successful sibling has been rendered.
  if (pendingEdits.get(attId) === submitted) pendingEdits.delete(attId);
  requestRepaint();
}

export function createButtonRenderer(msg) {
  const job = inFlight.get(msg?.id);
  const stop = stopButtonState(job, "Visualize reply");
  return messageButtonHtml(msg, { mutable: canMutate(), icon: ICON, escAttr, stop, running: !!job });
}

// Closing the stream is the stop: the server cancels the render with it.
async function generate(msgId, button) {
  const running = inFlight.get(msgId);
  if (running) return running.stop();
  if (!getActiveConvId() || !canMutate()) return;
  const styleId = cfg.default_style || "realistic";
  const controller = new AbortController();
  const job = startWorkflowJob({ title: "Stop image generation", controller });
  inFlight.set(msgId, job);
  job.show(button);
  const channel = `workflow:image_gen:generate:${msgId}`;
  try {
    setWorkflowPhase(channel, "Composing image prompt...");
    const response = await streamPost(
      convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"),
      { action: "generate", message_id: msgId, style_id: styleId },
      controller.signal,
    );
    if (!response.ok) throw new Error(`generate returned ${response.status}`);
    let attachmentId = null;
    let terminated = false;
    let failure = null;
    let landed = 0;
    for await (const event of sseEvents(response.body, { signal: controller.signal })) {
      let data = {};
      try {
        data = event.data ? JSON.parse(event.data) : {};
      } catch {
        data = {};
      }
      if (event.event === "phase_status" && data.label) setWorkflowPhase(channel, data.label);
      if (event.event === "image_gen_refine_stage") onRefineStage(data, "fresh");
      if (event.event === "image_gen_review") onReview(data);
      // Each refinement render lands as it is made; only the first is scrolled to,
      // so a user who scrolled away to read is left there.
      if (event.event === "image_gen_render") await refreshConversationMessages(landed++ ? null : msgId);
      if (event.event === "image_gen_error") failure = data.message || "Image generation failed";
      if (event.event === "image_gen_done") {
        attachmentId = data.attachment_id;
        terminated = true;
      }
    }
    // Stop cancels the reader, so a stopped stream usually ends here instead of throwing.
    if (controller.signal.aborted) {
      await refreshConversationMessages();
      return;
    }
    if (!terminated && !failure) failure = "Image generation did not complete";
    if (failure) toast(failure, "error");
    if (attachmentId) await refreshConversationMessages(landed ? null : msgId);
  } catch (e) {
    // Stopped: the renders saved so far stay, and the saved rows decide what shows.
    if (e?.name === "AbortError") await refreshConversationMessages();
    else {
      console.warn("image generation stream dropped; polling for the result", e);
      if (!(await pollForAttachment(msgId, controller.signal)) && !controller.signal.aborted)
        toast("Image generation failed", "error");
    }
  } finally {
    inFlight.delete(msgId);
    clearWorkflowPhase(channel);
    job.end();
    endLiveRuns(msgId, "fresh");
    // The button outlived the first render only as this run's Stop.
    requestRepaint();
  }
}

async function pollForAttachment(msgId, signal, { timeoutMs = 120_000, intervalMs = 3_000 } = {}) {
  const convId = getActiveConvId();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (signal.aborted || getActiveConvId() !== convId) return false;
    await new Promise((r) => setTimeout(r, intervalMs));
    let msgs;
    try {
      msgs = await api.get(convUrl(convId, "messages"));
    } catch {
      continue; // transient; keep waiting for the render to land
    }
    const msg = msgs.find((m) => m.id === msgId);
    if (msg && hasAttachment(msg)) {
      if (!signal.aborted && getActiveConvId() === convId) await refreshConversationMessages(msgId);
      return true;
    }
  }
  return false;
}

export function attachmentRenderer(ctx) {
  rememberTimeline();
  const { att, buttons, defaultHtml, msgId, rootId } = ctx;
  const media = defaultHtml.replace(buttons.regen, "").replace(buttons.reroll, "");
  const actions = `<div class="image-gen-actions">${viewToggleHtml(focusView)}${downloadButtonHtml(att, { escAttr })}${buttons.reroll}${buttons.regen}</div>`;
  const pend = pendingEdits.get(att.id);
  const cm = att.consumption_metadata || {};
  const edited = (key) => pend && key in pend && pend[key] !== (cm[key] ?? "");
  const pending = edited("prompt") || edited("negative_prompt") ? pend : undefined;
  const details = attachmentDetailsHtml(withLiveReview(att), { esc, escAttr, pending });
  const view = focusView ? " image-gen-focus" : "";
  const ids = rootId == null ? "" : ` data-msg-id="${escAttr(msgId)}" data-root-id="${escAttr(rootId)}"`;
  return `<div class="image-gen-attachment${view}"${ids}><div class="image-gen-main"><div class="image-gen-media">${media}${actions}</div></div>${details}${timelineHtml(ctx)}</div>`;
}

// ── refinement timeline ──────────────────────────────────────────────────────

function rememberTimeline() {
  const conv = getActiveConvId();
  if (conv !== timelineConv) {
    timelineConv = conv;
    openRows.clear();
    liveReviews.clear();
  }
  scheduleMeasure();
}

function sameReview(a, b) {
  return (a ?? null) === (b ?? null) || (a?.critique === b?.critique && a?.done === b?.done);
}

// A review that arrived before its row was refetched, merged over the row. Once
// the saved row carries it, the saved row wins and the entry goes.
function withLiveReview(att) {
  const live = liveReviews.get(att?.id);
  if (!live) return att;
  const cm = att.consumption_metadata || {};
  const refine = cm.refine && typeof cm.refine === "object" ? cm.refine : null;
  if (sameReview(cm.review, live.review) && (refine?.ended ?? null) === live.ended) {
    liveReviews.delete(att.id);
    return att;
  }
  return {
    ...att,
    consumption_metadata: {
      ...cm,
      review: live.review ?? cm.review,
      refine: refine && live.ended ? { ...refine, ended: live.ended } : refine,
    },
  };
}

// Regenerates name their group even before its first render lands. A fresh
// generation starts a new group, identified by the run on its saved renders.
function liveRunFor(msgId, rootId, siblings) {
  let fresh = null;
  for (const live of liveRuns.values()) {
    if (live.msgId !== msgId) continue;
    if (live.source === "regen" && live.rootId === rootId) return live;
    if (live.source === "fresh" && siblings.some((a) => a.consumption_metadata?.refine?.run === live.run)) fresh = live;
  }
  return fresh;
}

function timelineHtml(ctx) {
  const { att, siblings, msgId, rootId } = ctx;
  if (!Array.isArray(siblings) || msgId == null || rootId == null) return "";
  const merged = siblings.map(withLiveReview);
  const shown = merged.find((a) => a.id === att.id) || withLiveReview(att);
  const live = liveRunFor(msgId, rootId, merged);
  const model = refineRun(merged, shown, live);
  if (!model) return "";
  return refineTimelineHtml(model, {
    esc,
    escAttr,
    msgId,
    rootId,
    open: refineOpen,
    openRows,
  });
}

function onRefineStage(data, source) {
  const msgId = data?.message_id;
  if (!Number.isInteger(msgId) || typeof data.run !== "string" || !data.run) return;
  if (source === "regen" && !Number.isInteger(data.root_id)) return;
  const { run, stage, render, turns } = data;
  liveRuns.set(run, { msgId, rootId: data.root_id, source, run, stage, render, turns });
  patchTimelines(msgId);
}

function onReview(data) {
  const attId = data?.attachment_id;
  if (!Number.isInteger(attId)) return;
  liveReviews.set(attId, { review: data.review ?? null, ended: data.ended ?? null });
  if (Number.isInteger(data.message_id)) patchTimelines(data.message_id);
}

// Every outcome ends here, so no strip is left saying "Reviewing…".
function endLiveRuns(msgId, source, rootId = null) {
  for (const [run, live] of liveRuns)
    if (live.msgId === msgId && live.source === source && (rootId == null || live.rootId === rootId))
      liveRuns.delete(run);
  patchTimelines(msgId);
}

// Rebuilds only the strips, from current message data: requestRepaint is skipped
// while a reply streams, and a status change must not wait for it. The shown
// attachment is looked up on each patch.
function patchTimelines(msgId = null) {
  for (const card of document.querySelectorAll(".image-gen-attachment[data-root-id]")) {
    const id = Number(card.dataset.msgId);
    const rootId = Number(card.dataset.rootId);
    if (msgId != null && id !== msgId) continue;
    const msg = getMessages().find((m) => m.id === id);
    const siblings = (msg?.workflow_attachments || []).filter((a) => (a.parent_attachment_id || a.id) === rootId);
    const attId = Number(card.closest(".workflow-widget")?.dataset.attachmentId);
    const att = siblings.find((a) => a.id === attId);
    if (!att) continue;
    swapStrip(card, timelineHtml({ att, siblings, msgId: id, rootId }));
  }
  scheduleMeasure();
}

// The status node is kept, so its live region announces the new text; focus
// stays on the control it was on.
function swapStrip(card, html) {
  const old = card.querySelector(":scope > .ig-refine");
  if (!html) return old?.remove();
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const next = tpl.content.firstElementChild;
  if (!old) return card.appendChild(next);
  const oldStatus = old.querySelector(".ig-refine-status");
  const newStatus = next.querySelector(".ig-refine-status");
  if (oldStatus && newStatus) {
    oldStatus.className = newStatus.className;
    oldStatus.title = newStatus.title;
    if (oldStatus.innerHTML !== newStatus.innerHTML) oldStatus.innerHTML = newStatus.innerHTML;
    newStatus.replaceWith(oldStatus);
  }
  const focused = old.contains(document.activeElement) ? document.activeElement : null;
  const scrollTop = old.querySelector(".ig-refine-list")?.scrollTop ?? 0;
  old.replaceWith(next);
  const list = next.querySelector(".ig-refine-list");
  if (list) list.scrollTop = scrollTop;
  if (!focused?.dataset.wfAction) return;
  const attId = focused.dataset.attId;
  next
    .querySelector(`[data-wf-action="${focused.dataset.wfAction}"]${attId ? `[data-att-id="${attId}"]` : ""}`)
    ?.focus({ preventScroll: true });
}

function refineToggle() {
  refineOpen = !refineOpen;
  saveFlag(REFINE_OPEN_KEY, refineOpen);
  patchTimelines();
}

function refineMore(el) {
  const attId = Number(el.dataset.attId);
  if (openRows.has(attId)) openRows.delete(attId);
  else openRows.add(attId);
  const msgId = Number(el.closest(".ig-refine")?.dataset.msgId);
  patchTimelines(Number.isInteger(msgId) ? msgId : null);
}

function refineShow(el) {
  const [msgId, rootId, attId] = [el.dataset.msgId, el.dataset.rootId, el.dataset.attId].map(Number);
  if ([msgId, rootId, attId].every(Number.isInteger)) activateWorkflowVariant(msgId, rootId, attId);
}

// ── critique clamping ──
// "More" shows only where the text is actually cut off, which depends on the
// column's width, so it is measured after each draw and whenever a strip resizes.
const observed = new Set();
const resizeObserver =
  typeof ResizeObserver === "undefined"
    ? null
    : new ResizeObserver((entries) => {
        for (const entry of entries) measureStrip(entry.target);
      });
let measureQueued = false;

function scheduleMeasure() {
  if (measureQueued) return;
  measureQueued = true;
  requestAnimationFrame(() => {
    measureQueued = false;
    for (const strip of observed) {
      if (strip.isConnected) continue;
      resizeObserver?.unobserve(strip);
      observed.delete(strip);
    }
    for (const strip of document.querySelectorAll(".ig-refine")) {
      if (!observed.has(strip)) {
        observed.add(strip);
        resizeObserver?.observe(strip);
      }
      measureStrip(strip);
    }
  });
}

function measureStrip(strip) {
  for (const text of strip.querySelectorAll(".ig-refine-text")) {
    const critique = text.querySelector(".ig-refine-critique");
    const clamped = !text.classList.contains("is-open") && critique.scrollHeight > critique.clientHeight + 1;
    text.classList.toggle("is-clamped", clamped);
  }
}
