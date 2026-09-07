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
  requestRepaint,
  setWorkflowPhase,
  sseEvents,
  streamPost,
  toast,
} from "/static/workflow_api.js";
import { attachmentDetailsHtml, hasAttachment, messageButtonHtml } from "./render.js";

const WORKFLOW_ID = "image_gen";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="15" height="15"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 17 5-5 4 4 2-2 5 5"/></svg>`;
let cfg;

const inFlight = new Map(); // msgId -> AbortController

const pendingEdits = new Map(); // attId -> edited fields

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "generate", (el) => generate(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "savePrompt", savePrompt);
  registerAction(WORKFLOW_ID, "editPrompt", editPrompt);
  registerRerollParams(WORKFLOW_ID, rerollParams);
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
  const params = { ...(pendingEdits.get(attId) || {}) };
  if (cfg?.default_style) params.style_id = cfg.default_style; // the tools-panel picker
  return Object.keys(params).length ? params : null;
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
  const actions =
    buttons.reroll || buttons.regen ? `<div class="image-gen-actions">${buttons.reroll}${buttons.regen}</div>` : "";
  const pend = pendingEdits.get(att.id);
  const cm = att.consumption_metadata || {};
  const edited = (key) => pend && key in pend && pend[key] !== (cm[key] ?? "");
  const pending = edited("prompt") || edited("negative_prompt") ? pend : undefined;
  const details = attachmentDetailsHtml(att, { esc, escAttr, pending });
  return `<div class="image-gen-attachment"><div class="image-gen-media">${media}${actions}</div>${details}</div>`;
}
