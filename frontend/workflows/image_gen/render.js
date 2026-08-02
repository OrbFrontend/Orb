// DOM-free HTML builders for the message button and attachment details.
//
// Outside widget.js so they load under `node --test`: widget.js imports the plugin
// facade, which pulls in the chat spine and touches the DOM at module load.
//
// `esc`/`escAttr` are injected rather than imported because the plugin boundary
// (scripts/check_frontend_layers.py) allows only the facade and relative files, and
// a local copy of the escapers would be free to drift from the framework's. Every
// dynamic value below goes through one; the tests assert exactly that.

const WORKFLOW_ID = "image_gen";

// One word for the lever that chose the camera, named for the control the user
// would go change. `no_classifier` folds into `default` because whether the
// classifier is installed is the user's own setting, not news from the image.
const POV_LABELS = { first_person: "First-person", third_person: "Third-person" };
const POV_SOURCE_LABELS = {
  manual: "picker",
  classifier: "classifier",
  no_classifier: "default",
  default: "default",
};

// The origin is a machine key ("attachment:41", "upload:12:3", "character:<id>")
// whose useful half is which *kind* of thing was fed in — a wrong reference is
// usually the wrong kind, not the wrong row. The configured source policy is not
// repeated beside it: that one is a setting the user picked, while this is what
// the render actually got.
const REFERENCE_ORIGIN_LABELS = {
  attachment: "previous image",
  upload: "uploaded image",
  character: "character card",
};

// The seed is still minted and stored — rehydrate refuses a null one — so the
// honest row is "recorded but unused", not a blank and not the hex.
const UNUSED_SEED = "not used";

// Rendered only in the unit the payload names. Nothing converts: xAI reports
// `usd_ticks` and nowhere documents what a tick is worth, so calling it dollars
// would print a wrong billing figure. An unrecognised unit still shows the value
// beside its own name, which is more use than hiding it.
const COST_UNITS = {
  usd: (value) => `$${Number(value).toFixed(4)}`,
  usd_ticks: (value) => `${value} usd ticks`,
};

// `cost.provider` is not appended: the Backend row above already names who
// charged it, and the two only ever disagree if something is wrong upstream.
function costRow(cm, esc) {
  const cost = cm.cost;
  if (!cost || cost.value === undefined || cost.value === null) return "";
  const format = COST_UNITS[cost.unit];
  const text = format ? format(cost.value) : `${cost.value} ${cost.unit || ""}`.trim();
  return `<dt>Cost</dt><dd>${esc(text)}</dd>`;
}

// One row per filled slot, so a workflow with no reference slots gets none. The
// slot id is left out: it is a ComfyUI node number on one backend and the
// synthetic "cloud" on the other, and the rows already read in slot order.
function referenceRows(cm, esc) {
  return (Array.isArray(cm.references) ? cm.references : [])
    .map((ref) => {
      const kind = String(ref?.origin || "").split(":")[0];
      return `<dt>Reference</dt><dd>${esc(REFERENCE_ORIGIN_LABELS[kind] || kind)}</dd>`;
    })
    .join("");
}

export function hasAttachment(msg) {
  return (msg?.workflow_attachments || []).some((a) => a.workflow_id === WORKFLOW_ID);
}

// "" when the message cannot take an image: only assistant messages are
// visualizable, and one already carrying an image_gen attachment offers
// regenerate/reroll on the attachment instead. The click generates straight away
// with the style selected in the tools-panel card — no modal.
export function messageButtonHtml(msg, { mutable, icon, escAttr }) {
  if (!msg?.id || msg.role !== "assistant" || hasAttachment(msg)) return "";
  if (!mutable)
    return `<button class="image-gen-create" disabled title="Close other tabs to generate an image">${icon}</button>`;
  return `<button class="image-gen-create" title="Visualize reply" data-wf-action="image_gen:generate" data-msg-id="${escAttr(msg.id)}">${icon}</button>`;
}

// The <details> block alone — the caller places the image and the action strip
// itself, so nothing is prepended here.
//
// `pending` is an unrendered prompt edit ({prompt, negative_prompt}) held by the
// widget, or undefined when what is shown is what the attachment stores.
export function attachmentDetailsHtml(att, { esc, escAttr, pending }) {
  const cm = att?.consumption_metadata || {};
  // Edited in place, not through a modal: `change` fires once on blur-after-edit and
  // the facade dispatcher already carries it. Rows are guessed from length because a
  // DOM-free builder cannot measure the column; `resize:vertical` is the escape hatch.
  const field = (name, label, value) =>
    `<textarea class="image-gen-edit" readonly aria-label="${label}" rows="${Math.min(10, Math.max(2, Math.ceil(value.length / 48)))}" data-wf-action="image_gen:savePrompt" data-wf-on="change" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">${esc(value)}</textarea>`;
  const pencil = (name, label) =>
    `<button type="button" class="image-gen-edit-btn" title="Edit ${label.toLowerCase()}" aria-label="Edit ${label.toLowerCase()}" data-wf-action="image_gen:editPrompt" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">✎</button>`;
  const marker = pending ? `<span class="image-gen-pending">edited — reroll to render</span>` : "";
  // The style label opens that entry in the style editor: judge the image, tune the
  // style that made it, without a hunt through settings every lap.
  const styleText = esc(cm.style_label || cm.style_id || "");
  const style = cm.style_id
    ? `<button type="button" class="image-gen-style-link" data-wf-action="image_gen:editStyle" data-style-id="${escAttr(cm.style_id)}">${styleText}</button>`
    : styleText;
  // Populated only when a replay could not be honoured exactly — the disclosure
  // belongs where the odd-looking image is.
  const notes = (Array.isArray(cm.notes) ? cm.notes : []).map((note) => `<dt>Note</dt><dd>${esc(note)}</dd>`).join("");
  // Omitted rather than shown empty on images predating the camera record. The
  // lever makes it actionable: a wrong camera is fixed in a different place
  // depending on which one chose it.
  const source = POV_SOURCE_LABELS[cm.pov_source] || cm.pov_source;
  const camera = cm.pov
    ? `<dt>Camera</dt><dd>${esc(POV_LABELS[cm.pov] || cm.pov)}${source ? esc(` — ${source}`) : ""}</dd>`
    : "";
  return `<details class="image-gen-details" open><summary>Render details</summary>
    <dl><dt>Style</dt><dd>${style}</dd>
      <dt>Backend</dt><dd>${esc(cm.source || "External ComfyUI")}</dd>${camera}${referenceRows(cm, esc)}
      <dt>Seed</dt><dd>${cm.seed_honored === false ? esc(UNUSED_SEED) : `<code>${esc(att?.seed || "")}</code>`}</dd>${costRow(cm, esc)}
      <dt>Prompt ${pencil("prompt", "Prompt")}</dt><dd>${field("prompt", "Prompt", pending?.prompt ?? cm.prompt ?? "")}${marker}</dd>
      <dt>Negative ${pencil("negative_prompt", "Negative prompt")}</dt><dd>${field("negative_prompt", "Negative prompt", pending?.negative_prompt ?? cm.negative_prompt ?? "")}</dd>${notes}</dl>
  </details>`;
}
