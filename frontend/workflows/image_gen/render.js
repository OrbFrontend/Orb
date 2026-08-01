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

// How the resolved camera reads back to a user. The levers are named for the
// control the user would go change, not for the backend symbol that chose them.
const POV_LABELS = { first_person: "First-person", third_person: "Third-person" };
const POV_SOURCE_LABELS = {
  manual: "POV picker",
  classifier: "classifier",
  no_classifier: "default (no POV classifier)",
  default: "default (the narration read as ambiguous)",
};

// Where a reference image came from, phrased as the thing a user would go look at.
const REFERENCE_SOURCE_LABELS = {
  previous: "previous image",
  character: "character reference",
  previous_or_character: "previous image, else character reference",
};
// The origin is a machine key ("attachment:41", "upload:12:3", "character:<id>"),
// and its useful half is which *kind* of thing was fed in — a wrong reference is
// usually the wrong kind, not the wrong row.
const REFERENCE_ORIGIN_LABELS = {
  attachment: "a generated image in this chat",
  upload: "an image you uploaded",
  character: "the character",
};

// One row per mapped slot, so a workflow with no reference slots gets none.
function referenceRows(cm, esc) {
  return (Array.isArray(cm.references) ? cm.references : [])
    .map((ref) => {
      const node = Array.isArray(ref?.slot) ? `#${ref.slot[0]}` : "";
      const from = REFERENCE_ORIGIN_LABELS[String(ref?.origin || "").split(":")[0]];
      const detail = [REFERENCE_SOURCE_LABELS[ref?.source] || ref?.source || "", from && `from ${from}`]
        .filter(Boolean)
        .join(" — ");
      return `<dt>Reference${node ? esc(` ${node}`) : ""}</dt><dd>${esc(detail)}</dd>`;
    })
    .join("");
}

export function hasAttachment(msg) {
  return (msg?.workflow_attachments || []).some((a) => a.workflow_id === WORKFLOW_ID);
}

// Returns "" when the message can't take an image at all: only assistant
// messages are visualizable, and one that already carries an image_gen
// attachment offers regenerate/reroll on the attachment instead.
//
// The click generates straight away with the style selected in the tools-panel
// card — no modal. Clicking again while a render is in flight cancels it.
export function messageButtonHtml(msg, { mutable, icon, escAttr }) {
  if (!msg?.id || msg.role !== "assistant" || hasAttachment(msg)) return "";
  if (!mutable)
    return `<button class="image-gen-create" disabled title="Close other tabs to generate an image">${icon}</button>`;
  return `<button class="image-gen-create" title="Visualize reply" data-wf-action="image_gen:generate" data-msg-id="${escAttr(msg.id)}">${icon}</button>`;
}

// `pending` is an unrendered prompt edit ({prompt, negative_prompt}) held by the
// widget, or undefined when what is shown is what the attachment stores.
export function attachmentDetailsHtml(att, defaultHtml, { esc, escAttr, pending }) {
  const cm = att?.consumption_metadata || {};
  // Edited in place rather than through a modal: `change` fires once on blur-after-edit
  // and the facade dispatcher already carries it, so committing costs no extra plumbing.
  // Rows are guessed from length because a DOM-free builder cannot measure the column;
  // `resize:vertical` is the escape hatch.
  const field = (name, label, value) =>
    `<textarea class="image-gen-edit" readonly aria-label="${label}" rows="${Math.min(10, Math.max(2, Math.ceil(value.length / 48)))}" data-wf-action="image_gen:savePrompt" data-wf-on="change" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">${esc(value)}</textarea>`;
  const pencil = (name, label) =>
    `<button type="button" class="image-gen-edit-btn" title="Edit ${label.toLowerCase()}" aria-label="Edit ${label.toLowerCase()}" data-wf-action="image_gen:editPrompt" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">✎</button>`;
  const marker = pending ? `<span class="image-gen-pending">edited — click 🎲 to render</span>` : "";
  // The style label opens that entry in the style editor: judging an image and
  // then tuning the style that produced it is the loop this feature lives in,
  // and without the link it costs a hunt through settings every time.
  const styleText = esc(cm.style_label || cm.style_id || "");
  const style = cm.style_id
    ? `<button type="button" class="image-gen-style-link" data-wf-action="image_gen:editStyle" data-style-id="${escAttr(cm.style_id)}">${styleText}</button>`
    : styleText;
  // Populated only when a replay could not be honoured exactly (a deleted user
  // graph, say) — the disclosure belongs where the odd-looking image is.
  const notes = (Array.isArray(cm.notes) ? cm.notes : []).map((note) => `<dt>Note</dt><dd>${esc(note)}</dd>`).join("");
  // Absent on images generated before the camera was recorded, so the row is
  // omitted rather than shown empty. The lever is what makes this actionable: a
  // wrong camera is fixed in a different place depending on which one chose it.
  const source = POV_SOURCE_LABELS[cm.pov_source] || cm.pov_source;
  const camera = cm.pov
    ? `<dt>Camera</dt><dd>${esc(POV_LABELS[cm.pov] || cm.pov)}${source ? esc(` — from the ${source}`) : ""}</dd>`
    : "";
  return `${defaultHtml}<details class="image-gen-details" open><summary>Render details</summary>
    <dl><dt>Style</dt><dd>${style}</dd>
      <dt>Source</dt><dd>${esc(cm.source || "External ComfyUI")}</dd>${camera}${referenceRows(cm, esc)}
      <dt>Seed</dt><dd><code>${esc(att?.seed || "")}</code></dd>
      <dt>Prompt ${pencil("prompt", "Prompt")}</dt><dd>${field("prompt", "Prompt", pending?.prompt ?? cm.prompt ?? "")}${marker}</dd>
      <dt>Negative ${pencil("negative_prompt", "Negative prompt")}</dt><dd>${field("negative_prompt", "Negative prompt", pending?.negative_prompt ?? cm.negative_prompt ?? "")}</dd>${notes}</dl>
  </details>`;
}
