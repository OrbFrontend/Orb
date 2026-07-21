// DOM-free HTML builders for the message button and attachment details.
//
// They live outside widget.js so they load under `node --test`: widget.js
// imports the plugin facade, which pulls in the chat spine and touches the DOM
// at module load, so nothing importing it is testable without a browser.
//
// `esc`/`escAttr` are injected rather than imported because a file under
// frontend/workflows/** may import only the facade and its own relative files
// (the plugin boundary in scripts/check_frontend_layers.py) — and a local copy
// of the escapers would be free to drift from the framework's. Every dynamic
// value below goes through one of them; the tests assert exactly that.

const WORKFLOW_ID = "image_gen";

export function hasAttachment(msg) {
  return (msg?.workflow_attachments || []).some((a) => a.workflow_id === WORKFLOW_ID);
}

// Returns "" when the message can't take an image at all: only assistant
// messages are visualizable, and one that already carries an image_gen
// attachment offers regenerate/reroll on the attachment instead.
export function messageButtonHtml(msg, { mutable, icon, escAttr }) {
  if (!msg?.id || msg.role !== "assistant" || hasAttachment(msg)) return "";
  if (!mutable)
    return `<button class="image-gen-create" disabled title="Close other tabs to generate an image">${icon}</button>`;
  return `<button class="image-gen-create" title="Visualize reply" data-wf-action="image_gen:open" data-msg-id="${escAttr(msg.id)}">${icon}</button>`;
}

export function attachmentDetailsHtml(att, defaultHtml, { esc }) {
  const cm = att?.consumption_metadata || {};
  return `${defaultHtml}<details class="image-gen-details"><summary>Render details</summary>
    <dl><dt>Style</dt><dd>${esc(cm.style_label || cm.style_id || "")}</dd>
      <dt>Source</dt><dd>${esc(cm.source || "External ComfyUI")}</dd>
      <dt>Seed</dt><dd><code>${esc(att?.seed || "")}</code></dd>
      <dt>Prompt</dt><dd>${esc(cm.prompt || "")}</dd>
      <dt>Negative</dt><dd>${esc(cm.negative_prompt || "")}</dd></dl>
  </details>`;
}
