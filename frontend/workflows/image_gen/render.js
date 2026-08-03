const WORKFLOW_ID = "image_gen";

const POV_LABELS = { first_person: "First-person", third_person: "Third-person" };
const POV_SOURCE_LABELS = {
  manual: "picker",
  classifier: "classifier",
  no_classifier: "default",
  default: "default",
};

const REFERENCE_ORIGIN_LABELS = {
  attachment: "previous image",
  upload: "uploaded image",
  character: "character card",
};

const UNUSED_SEED = "not used";

const COST_UNITS = {
  usd: (value) => `$${Number(value).toFixed(4)}`,
  usd_ticks: (value) => `${value} usd ticks`,
};

function costRow(cm, esc) {
  const cost = cm.cost;
  if (!cost || cost.value === undefined || cost.value === null) return "";
  const format = COST_UNITS[cost.unit];
  const text = format ? format(cost.value) : `${cost.value} ${cost.unit || ""}`.trim();
  return `<dt>Cost</dt><dd>${esc(text)}</dd>`;
}

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

export function messageButtonHtml(msg, { mutable, icon, escAttr }) {
  if (!msg?.id || msg.role !== "assistant" || hasAttachment(msg)) return "";
  if (!mutable)
    return `<button class="image-gen-create" disabled title="Close other tabs to generate an image">${icon}</button>`;
  return `<button class="image-gen-create" title="Visualize reply" data-wf-action="image_gen:generate" data-msg-id="${escAttr(msg.id)}">${icon}</button>`;
}

export function attachmentDetailsHtml(att, { esc, escAttr, pending }) {
  const cm = att?.consumption_metadata || {};
  const field = (name, label, value) =>
    `<textarea class="image-gen-edit" readonly aria-label="${label}" rows="${Math.min(10, Math.max(2, Math.ceil(value.length / 48)))}" data-wf-action="image_gen:savePrompt" data-wf-on="change" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">${esc(value)}</textarea>`;
  const pencil = (name, label) =>
    `<button type="button" class="image-gen-edit-btn" title="Edit ${label.toLowerCase()}" aria-label="Edit ${label.toLowerCase()}" data-wf-action="image_gen:editPrompt" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">✎</button>`;
  const marker = pending ? `<span class="image-gen-pending">edited — reroll to render</span>` : "";
  const styleText = esc(cm.style_label || cm.style_id || "");
  const style = cm.style_id
    ? `<button type="button" class="image-gen-style-link" data-wf-action="image_gen:editStyle" data-style-id="${escAttr(cm.style_id)}">${styleText}</button>`
    : styleText;
  const notes = (Array.isArray(cm.notes) ? cm.notes : []).map((note) => `<dt>Note</dt><dd>${esc(note)}</dd>`).join("");
  const source = POV_SOURCE_LABELS[cm.pov_source] || cm.pov_source;
  const camera = cm.pov
    ? `<dt>Camera</dt><dd>${esc(POV_LABELS[cm.pov] || cm.pov)}${source ? esc(` — ${source}`) : ""}</dd>`
    : "";
  const size = cm.width && cm.height ? `<dt>Size</dt><dd>${esc(`${cm.width} × ${cm.height}`)}</dd>` : "";
  return `<details class="image-gen-details" open><summary>Render details</summary>
    <dl><dt>Style</dt><dd>${style}</dd>
      <dt>Backend</dt><dd>${esc(cm.source || "External ComfyUI")}</dd>${size}${camera}${referenceRows(cm, esc)}
      <dt>Seed</dt><dd>${cm.seed_honored === false ? esc(UNUSED_SEED) : `<code>${esc(att?.seed || "")}</code>`}</dd>${costRow(cm, esc)}
      <dt>Prompt ${pencil("prompt", "Prompt")}</dt><dd>${field("prompt", "Prompt", pending?.prompt ?? cm.prompt ?? "")}${marker}</dd>
      <dt>Negative ${pencil("negative_prompt", "Negative prompt")}</dt><dd>${field("negative_prompt", "Negative prompt", pending?.negative_prompt ?? cm.negative_prompt ?? "")}</dd>${notes}</dl>
  </details>`;
}
