import {
  canMutate,
  clearWorkflowPhase,
  closeModal,
  convUrl,
  esc,
  escAttr,
  getActiveConvId,
  refreshConversationMessages,
  registerAction,
  setWorkflowPhase,
  showModal,
  sseEvents,
  streamPost,
  toast,
} from "/static/workflow_api.js";
import { attachmentDetailsHtml, messageButtonHtml } from "./render.js";

const WORKFLOW_ID = "image_gen";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="15" height="15"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 17 5-5 4 4 2-2 5 5"/></svg>`;
let cfg;
let styles = [];

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "open", (el) => openGenerate(Number(el.dataset.msgId)));
  registerAction(WORKFLOW_ID, "generate", (el) => generate(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "retry", (el) => probeReadiness(Number(el.dataset.msgId)));
  registerAction(WORKFLOW_ID, "close", () => closeModal());
}

export function createButtonRenderer(msg) {
  return messageButtonHtml(msg, { mutable: canMutate(), icon: ICON, escAttr });
}

async function openGenerate(msgId) {
  try {
    const res = await fetch("/api/workflows/image_gen/styles").then((r) => {
      if (!r.ok) throw new Error("Could not load image styles");
      return r.json();
    });
    styles = Array.isArray(res.styles) ? res.styles : [];
  } catch (e) {
    toast(e.message || "Could not load image styles", "error");
    return;
  }
  const options = styles
    .map(
      (s) =>
        `<option value="${escAttr(s.id)}"${s.id === cfg.default_style ? " selected" : ""}>${esc(s.label)}</option>`,
    )
    .join("");
  showModal(`<h2>Visualize reply</h2>
    <div class="image-gen-modal">
      <label>Style<select id="image-gen-style">${options}</select></label>
      <div id="image-gen-ready" class="image-gen-note">Checking ComfyUI...</div>
    </div>
    <div class="modal-actions">
      <button class="btn" data-wf-action="image_gen:close">Cancel</button>
      <span id="image-gen-recovery"></span>
      <button id="image-gen-submit" class="btn btn-accent" data-wf-action="image_gen:generate" data-msg-id="${msgId}" disabled>Generate</button>
    </div>`);
  await probeReadiness(msgId);
}

async function probeReadiness(msgId) {
  const status = document.getElementById("image-gen-ready");
  const recovery = document.getElementById("image-gen-recovery");
  const submit = document.getElementById("image-gen-submit");
  if (!status || !recovery || !submit) return;
  status.textContent = "Checking ComfyUI...";
  recovery.innerHTML = "";
  submit.disabled = true;
  try {
    const response = await fetch("/api/workflows/image_gen/connections/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok) {
      let message = "ComfyUI is not ready";
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string") message = payload.detail;
      } catch {
        // Keep the bounded generic message; never inject a raw HTML error body.
      }
      throw new Error(message);
    }
    status.textContent = "ComfyUI is ready.";
    submit.disabled = false;
  } catch (e) {
    status.textContent = e.message || "Can't reach the configured ComfyUI server.";
    recovery.innerHTML = `<button class="btn" data-wf-action="image_gen:settings">Open settings</button>
      <button class="btn" data-wf-action="image_gen:retry" data-msg-id="${msgId}">Retry</button>`;
  }
}

async function generate(msgId, button) {
  if (!getActiveConvId() || !canMutate()) return;
  const styleId = document.getElementById("image-gen-style")?.value || cfg.default_style;
  button.disabled = true;
  const channel = `workflow:image_gen:generate:${msgId}`;
  try {
    setWorkflowPhase(channel, "Composing image prompt...");
    const response = await streamPost(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
      action: "generate",
      message_id: msgId,
      style_id: styleId,
    });
    if (!response.ok) throw new Error(await response.text());
    let attachmentId = null;
    for await (const event of sseEvents(response.body)) {
      let data = {};
      try {
        data = event.data ? JSON.parse(event.data) : {};
      } catch {
        data = {};
      }
      if (event.event === "phase_status" && data.label) setWorkflowPhase(channel, data.label);
      if (event.event === "image_gen_error") toast(data.message || "Image generation failed", "error");
      if (event.event === "image_gen_done") attachmentId = data.attachment_id;
    }
    if (attachmentId) {
      closeModal();
      await refreshConversationMessages(msgId);
    } else {
      button.disabled = false;
    }
  } catch (e) {
    console.error("image generation failed", e);
    toast("Image generation failed", "error");
    button.disabled = false;
  } finally {
    clearWorkflowPhase(channel);
  }
}

export function attachmentRenderer(ctx) {
  return attachmentDetailsHtml(ctx.att, ctx.defaultHtml, { esc });
}
