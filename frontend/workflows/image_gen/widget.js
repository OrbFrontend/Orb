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

// One render per message, tab-wide. The Visualize button hides itself once the
// message carries an image, but that check reads local state: a second click
// mid-render would append a second independent attachment root to the same
// message. Keyed by message id so distinct messages still render in parallel.
const inFlight = new Map(); // msgId -> AbortController

// Prompt edits typed but not yet rendered. The dice picks them up on the next
// reroll, which persists them onto the new sibling -- so this only ever holds the
// gap between "saved" and "rendered".
const pendingEdits = new Map(); // attId -> {prompt, negative_prompt}

export function initWidget(sharedConfig) {
  cfg = sharedConfig;
  registerAction(WORKFLOW_ID, "generate", (el) => generate(Number(el.dataset.msgId), el));
  registerAction(WORKFLOW_ID, "savePrompt", savePrompt);
  registerAction(WORKFLOW_ID, "editPrompt", editPrompt);
  registerRerollParams(WORKFLOW_ID, rerollParams);
}

// The pencil unlocks its one field; leaving it re-locks (blur fires after
// `change`, so savePrompt has already committed by then).
function editPrompt(el) {
  const t = document.querySelector(
    `.image-gen-edit[data-att-id="${el.dataset.attId}"][data-field="${el.dataset.field}"]`,
  );
  if (!t) return;
  t.readOnly = false;
  t.addEventListener("blur", () => (t.readOnly = true), { once: true });
  t.focus();
}

// `change` fires on the one field that blurred, but pendingEdits carries the pair, so
// read both off the DOM -- either may have been edited before the other lost focus.
function savePrompt(el) {
  const attId = Number(el.dataset.attId);
  const fields = document.querySelectorAll(`.image-gen-edit[data-att-id="${attId}"]`);
  const edit = { prompt: "", negative_prompt: "" };
  for (const t of fields) edit[t.dataset.field] = t.value;
  pendingEdits.set(attId, edit);
  // Blurring straight into the sibling field is the common case, and a repaint there
  // would yank focus back out mid-edit. The marker can wait for the next one.
  if (!document.activeElement?.classList.contains("image-gen-edit")) requestRepaint();
  toast("Prompt edited — reroll to render");
}

function rerollParams(_msgId, attId) {
  // Deliberately not cleared after use: the edit stays keyed to the attachment it was
  // made on, so a reroll that dies on the network doesn't eat the typed text and a
  // second click just retries it.
  const params = { ...(pendingEdits.get(attId) || {}) };
  if (cfg?.default_style) params.style_id = cfg.default_style; // the tools-panel picker
  return Object.keys(params).length ? params : null;
}

export function createButtonRenderer(msg) {
  return messageButtonHtml(msg, { mutable: canMutate(), icon: ICON, escAttr });
}

async function generate(msgId, button) {
  // Click again while a render is in flight = cancel. Aborting the fetch closes
  // the SSE stream, which closes the hook's generator and cancels the render
  // server-side, not just the local view.
  if (inFlight.has(msgId)) {
    inFlight.get(msgId).abort();
    return;
  }
  if (!getActiveConvId() || !canMutate()) return;
  // Style comes from the tools-panel card's picker, which writes cfg.default_style
  // on change; the panel need not be open for this to hold the last choice.
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
    // Finishing without the terminal event means the body was never an image_gen
    // stream at all (a proxy error page, a framework-level JSON error). Silence
    // there is what left the button re-enabling with nothing shown.
    if (!terminated && !failure) failure = "Image generation did not complete";
    if (failure) toast(failure, "error");
    // On success the message re-renders without its Visualize button; on failure
    // the button stays and the finally block restores it to its idle state.
    if (attachmentId) await refreshConversationMessages(msgId);
  } catch (e) {
    // AbortError = the user clicked cancel; the finally restores the button.
    // Anything else is a read-side drop of the SSE stream (a browser/network
    // error on the fetch body — Firefox surfaces it as "Error in input stream").
    // The backend never sees a read-side drop, so it finishes and persists the
    // image regardless. Poll for it instead of declaring failure, which used to
    // leave the user reloading the page to find the image already there.
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

// Re-fetch the message until the backend's still-running render persists its
// image_gen attachment, so a dropped SSE stream recovers on its own. Stops on a
// second click (signal aborted), on navigating to another conversation, or after
// the ceiling — whichever comes first.
async function pollForAttachment(msgId, signal, { timeoutMs = 120_000, intervalMs = 3_000 } = {}) {
  const convId = getActiveConvId();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (signal.aborted || getActiveConvId() !== convId) return false;
    await new Promise((r) => setTimeout(r, intervalMs));
    // Peek at the raw messages instead of refreshConversationMessages, which
    // repaints the list and refetches the context counter on every call --
    // that flickered the page (and spammed /messages + /context-size) for the
    // whole poll. Only repaint once, when the image actually lands.
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
  // defaultHtml is the image plus the framework's regen/reroll strip concatenated;
  // the two button strings are documented substrings of it, so stripping them
  // leaves the bare image, which we re-place with the controls as an overlay
  // toolbar on the image corner instead of floating between image and details.
  const media = defaultHtml.replace(buttons.regen, "").replace(buttons.reroll, "");
  const actions =
    buttons.reroll || buttons.regen ? `<div class="image-gen-actions">${buttons.reroll}${buttons.regen}</div>` : "";
  // Show the pending edit only while it still differs from what the attachment stores:
  // that is what makes the marker disappear once a reroll has rendered it, and it saves
  // savePrompt an equality branch of its own.
  const pend = pendingEdits.get(att.id);
  const cm = att.consumption_metadata || {};
  const pending = pend && (pend.prompt !== cm.prompt || pend.negative_prompt !== cm.negative_prompt) ? pend : undefined;
  // Empty defaultHtml -> attachmentDetailsHtml returns just the <details> block.
  const details = attachmentDetailsHtml(att, "", { esc, escAttr, pending });
  return `<div class="image-gen-attachment"><div class="image-gen-media">${media}${actions}</div>${details}</div>`;
}
