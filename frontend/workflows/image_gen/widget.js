import {
  api,
  canMutate,
  clearWorkflowPhase,
  convUrl,
  esc,
  escAttr,
  getActiveConvId,
  refreshConversationMessages,
  registerAction,
  registerRerollParams,
  registerRerollSuccess,
  requestRepaint,
  setWorkflowPhase,
  sseEvents,
  streamPost,
  toast,
} from "/static/workflow_api.js";
import {
  attachmentDetailsHtml,
  downloadButtonHtml,
  hasAttachment,
  messageButtonHtml,
  viewToggleHtml,
} from "./render.js";

const WORKFLOW_ID = "image_gen";
const FOCUS_VIEW_KEY = "orb.imageGen.focusView";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="15" height="15"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 17 5-5 4 4 2-2 5 5"/></svg>`;
let cfg;

const inFlight = new Map(); // msgId -> AbortController

const pendingEdits = new Map(); // attId -> edited fields
const rerollEditSnapshots = new Map(); // attId -> edit object submitted by the current reroll

let focusView = loadFocusView(); // image fills the card, details hidden

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "generate", (el) => generate(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "savePrompt", savePrompt);
  registerAction(WORKFLOW_ID, "editPrompt", editPrompt);
  registerAction(WORKFLOW_ID, "toggleDetails", toggleDetails);
  registerAction(WORKFLOW_ID, "download", download);
  registerRerollParams(WORKFLOW_ID, rerollParams);
  registerRerollSuccess(WORKFLOW_ID, clearPendingEdit);
}

function loadFocusView() {
  try {
    return localStorage.getItem(FOCUS_VIEW_KEY) === "1";
  } catch {
    return false;
  }
}

// One view for every card, flipped in place: requestRepaint is skipped while a
// reply streams, which would leave the button dead until the stream ended.
function toggleDetails(el) {
  focusView = !focusView;
  try {
    localStorage.setItem(FOCUS_VIEW_KEY, focusView ? "1" : "0");
  } catch (e) {
    console.warn("persist image_gen focus view failed", e);
  }
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
  t.addEventListener("blur", () => (t.readOnly = true), { once: true });
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
  return messageButtonHtml(msg, { mutable: canMutate(), icon: ICON, escAttr });
}

async function generate(msgId, button) {
  if (inFlight.has(msgId)) {
    inFlight.get(msgId).abort();
    return;
  }
  if (!getActiveConvId() || !canMutate()) return;
  const styleId = cfg.default_style || "realistic";
  const controller = new AbortController();
  inFlight.set(msgId, controller);
  button.classList.add("image-gen-generating");
  button.title = "Cancel image generation";
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
    for await (const event of sseEvents(response.body, { signal: controller.signal })) {
      let data = {};
      try {
        data = event.data ? JSON.parse(event.data) : {};
      } catch {
        data = {};
      }
      if (event.event === "phase_status" && data.label) setWorkflowPhase(channel, data.label);
      if (event.event === "image_gen_error") failure = data.message || "Image generation failed";
      if (event.event === "image_gen_done") {
        attachmentId = data.attachment_id;
        terminated = true;
      }
    }
    if (!terminated && !failure) failure = "Image generation did not complete";
    if (failure) toast(failure, "error");
    if (attachmentId) await refreshConversationMessages(msgId);
  } catch (e) {
    if (e?.name !== "AbortError") {
      console.warn("image generation stream dropped; polling for the result", e);
      if (!(await pollForAttachment(msgId, controller.signal)) && !controller.signal.aborted)
        toast("Image generation failed", "error");
    }
  } finally {
    inFlight.delete(msgId);
    clearWorkflowPhase(channel);
    button.classList.remove("image-gen-generating");
    button.title = "Visualize reply";
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
  const { att, buttons, defaultHtml } = ctx;
  const media = defaultHtml.replace(buttons.regen, "").replace(buttons.reroll, "");
  const actions = `<div class="image-gen-actions">${viewToggleHtml(focusView)}${downloadButtonHtml(att, { escAttr })}${buttons.reroll}${buttons.regen}</div>`;
  const pend = pendingEdits.get(att.id);
  const cm = att.consumption_metadata || {};
  const edited = (key) => pend && key in pend && pend[key] !== (cm[key] ?? "");
  const pending = edited("prompt") || edited("negative_prompt") ? pend : undefined;
  const details = attachmentDetailsHtml(att, { esc, escAttr, pending });
  const view = focusView ? " image-gen-focus" : "";
  return `<div class="image-gen-attachment${view}"><div class="image-gen-media">${media}${actions}</div>${details}</div>`;
}
