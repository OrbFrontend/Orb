import {
  api,
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

// One render per message, tab-wide. The Visualize button hides itself once the
// message carries an image, but that check reads local state: closing the modal
// mid-render leaves the button live, and a second click would append a second
// independent attachment root to the same message. Keyed by message id so
// distinct messages still render in parallel.
const inFlight = new Map(); // msgId -> AbortController

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "open", (el) => openGenerate(Number(el.dataset.msgId)));
  registerAction(WORKFLOW_ID, "generate", (el) => generate(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "retry", (el) => probeReadiness(Number(el.dataset.msgId)));
  registerAction(WORKFLOW_ID, "close", (el) => dismiss(Number(el.dataset.msgId)));
}

export function createButtonRenderer(msg) {
  return messageButtonHtml(msg, { mutable: canMutate(), icon: ICON, escAttr });
}

// Cancel means cancel: aborting the fetch closes the SSE stream, which closes
// the hook's generator and cancels the render task server-side. Without this the
// modal's Cancel button only hid a render that kept running.
function dismiss(msgId) {
  inFlight.get(msgId)?.abort();
  closeModal();
}

async function openGenerate(msgId) {
  if (inFlight.has(msgId)) {
    toast("An image is already being generated for this message");
    return;
  }
  let styles = [];
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/styles`);
    styles = Array.isArray(res?.styles) ? res.styles : [];
  } catch {
    toast("Could not load image styles", "error");
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
      <button class="btn" data-wf-action="image_gen:close" data-msg-id="${msgId}">Cancel</button>
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
    // Sends no config, so the backend probes the *saved* one and may answer from
    // its cached node catalogue — the modal must not pay for a multi-megabyte
    // /object_info fetch on every open.
    await api.post(`/workflows/${WORKFLOW_ID}/connections/test`, {});
    status.textContent = "ComfyUI is ready.";
    submit.disabled = false;
  } catch (e) {
    status.textContent = readinessMessage(e);
    recovery.innerHTML = `<button class="btn" data-wf-action="image_gen:settings">Open settings</button>
      <button class="btn" data-wf-action="image_gen:retry" data-msg-id="${msgId}">Retry</button>`;
  }
}

// `api` rejects with the raw response body. The route sends `{"detail": "..."}`
// naming the exact unmet prerequisite (an unreachable host, a checkpoint the
// server no longer has), which is the whole point of showing it.
function readinessMessage(error) {
  try {
    const detail = JSON.parse(error?.message)?.detail;
    if (typeof detail === "string" && detail) return detail;
  } catch {
    // Not JSON — fall through to the bounded generic message.
  }
  return `Can't reach ComfyUI at ${cfg.external_comfy?.api_url || "the configured server"}.`;
}

async function generate(msgId, button) {
  if (!getActiveConvId() || !canMutate() || inFlight.has(msgId)) return;
  const styleId = document.getElementById("image-gen-style")?.value || cfg.default_style;
  const controller = new AbortController();
  inFlight.set(msgId, controller);
  button.disabled = true;
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
    // Finishing without the terminal event means the body was never an image_gen
    // stream at all (a proxy error page, a framework-level JSON error). Silence
    // there is what left the button re-enabling with nothing shown.
    if (!terminated && !failure) failure = "Image generation did not complete";
    if (failure) toast(failure, "error");
    if (attachmentId) {
      closeModal();
      await refreshConversationMessages(msgId);
    } else {
      button.disabled = false;
    }
  } catch (e) {
    if (e?.name !== "AbortError") {
      console.error("image generation failed", e);
      toast("Image generation failed", "error");
    }
    button.disabled = false;
  } finally {
    inFlight.delete(msgId);
    clearWorkflowPhase(channel);
  }
}

export function attachmentRenderer(ctx) {
  return attachmentDetailsHtml(ctx.att, ctx.defaultHtml, { esc, escAttr });
}
