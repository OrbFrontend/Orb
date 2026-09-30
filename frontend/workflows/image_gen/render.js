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

const INFO_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" width="15" height="15"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/></svg>`;
const DOWNLOAD_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M12 4v11m-4.5-4.5L12 15l4.5-4.5M5 19.5h14"/></svg>`;

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

// The prompter's verdict on this render: accepted, or the problems it revised for.
function reviewRow(cm, esc) {
  const review = cm.review;
  if (!review || typeof review.done !== "boolean") return "";
  const critique = typeof review.critique === "string" ? review.critique.trim() : "";
  const verdict = review.done
    ? "Accepted"
    : review.reseed === true
      ? "Asked for another render from a new seed"
      : "Asked for another render";
  return `<dt>Review</dt><dd>${esc(critique ? `${verdict}: ${critique}` : verdict)}</dd>`;
}

function compositionSkillsRow(cm, esc) {
  const labels = (Array.isArray(cm.composition_skills) ? cm.composition_skills : [])
    .map((skill) => skill?.label || skill?.id)
    .filter((label) => typeof label === "string" && label);
  return labels.length ? `<dt>Composition skills</dt><dd>${esc(labels.join(", "))}</dd>` : "";
}

export function hasAttachment(msg) {
  return (msg?.workflow_attachments || []).some((a) => a.workflow_id === WORKFLOW_ID);
}

// *stop* is `stopButtonState` for the reply's running render, if any.
const IDLE_CREATE = { cls: "", attrs: ' title="Visualize reply"' };

// A running render keeps its button, as its Stop, after its first image lands.
export function messageButtonHtml(msg, { mutable, icon, escAttr, stop = IDLE_CREATE, running = false }) {
  if (!msg?.id || msg.role !== "assistant" || (hasAttachment(msg) && !running)) return "";
  if (!mutable)
    return `<button class="image-gen-create" disabled title="Close other tabs to generate an image">${icon}</button>`;
  return `<button class="image-gen-create${stop.cls}"${stop.attrs} data-wf-action="image_gen:generate" data-msg-id="${escAttr(msg.id)}">${icon}</button>`;
}

// Pressed while the details are showing, like a gallery's info button.
export function viewToggleHtml(focus) {
  return `<button type="button" class="image-gen-view-btn" title="Render details" aria-pressed="${!focus}" data-wf-action="image_gen:toggleDetails">${INFO_ICON}</button>`;
}

export function downloadButtonHtml(att, { escAttr }) {
  return `<button type="button" class="image-gen-download-btn" title="Download PNG" aria-label="Download PNG" data-wf-action="image_gen:download" data-att-id="${escAttr(att?.id ?? "")}">${DOWNLOAD_ICON}</button>`;
}

export function attachmentDetailsHtml(att, { esc, escAttr, pending }) {
  const cm = att?.consumption_metadata || {};
  // The wrapper mirrors the text so the field is exactly as tall as its wrapped lines.
  const field = (name, label, value) =>
    `<div class="image-gen-edit-wrap" data-value="${escAttr(value)}"><textarea class="image-gen-edit" readonly aria-label="${label}" rows="1" data-wf-action="image_gen:savePrompt" data-wf-on="change" data-att-id="${escAttr(att?.id ?? "")}" data-field="${name}">${esc(value)}</textarea></div>`;
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
      ${compositionSkillsRow(cm, esc)}
      <dt>Seed</dt><dd>${cm.seed_honored === false ? esc(UNUSED_SEED) : `<code>${esc(att?.seed || "")}</code>`}</dd>${costRow(cm, esc)}
      <dt>Prompt ${pencil("prompt", "Prompt")}</dt><dd>${field("prompt", "Prompt", pending?.prompt ?? cm.prompt ?? "")}${marker}</dd>
      <dt>Negative ${pencil("negative_prompt", "Negative prompt")}</dt><dd>${field("negative_prompt", "Negative prompt", pending?.negative_prompt ?? cm.negative_prompt ?? "")}</dd>${reviewRow(cm, esc)}${notes}</dl>
  </details>`;
}

// ── refinement timeline ──────────────────────────────────────────────────────
//
// A refinement run is the renders one generate made while the prompter reviewed
// each and asked for another. Rows carry `consumption_metadata.refine`
// (`{run, render, turns, ended?}`) and `review` (`{critique, done, reseed?}`); `live` is
// the stage the running run last reported, or null once it ended.

const ENDING_ROW_TEXT = {
  no_review: "No usable review, so refinement stopped",
  no_prompt: "Asked for another render but wrote no revised prompt, so refinement stopped",
  render_failed: "The next render failed, so refinement stopped",
};

function refineOf(att) {
  const refine = att?.consumption_metadata?.refine;
  return refine && typeof refine.run === "string" && refine.run && Number.isInteger(refine.render) ? refine : null;
}

function reviewOf(att) {
  const review = att?.consumption_metadata?.review;
  if (!review || typeof review.done !== "boolean") return null;
  const out = { critique: typeof review.critique === "string" ? review.critique.trim() : "", done: review.done };
  if (!review.done && review.reseed === true) out.reseed = true;
  return out;
}

function liveHeader(live) {
  if (live.stage === "composing") return "Composing prompt…";
  if (live.stage === "reviewing") return `Reviewing render ${live.render}…`;
  if (live.render > 1) return `Rendering revision ${live.render - 1} of up to ${live.turns}…`;
  return "Rendering…";
}

function endedHeader(ended, render, turns) {
  if (ended === "accepted") return `Accepted at render ${render}`;
  if (ended === "turns_used") return `Used all ${turns} revisions — render ${render} not reviewed`;
  return `Refinement stopped at render ${render}`;
}

/**
 * The timeline of the run on show, or of *live* when one is running on this
 * group; null when neither is a refinement run. Rows are in render order, one
 * per render still saved, so a deleted render leaves a gap in the numbers.
 */
export function refineRun(siblings, shown, live = null) {
  const runId = live?.run || refineOf(shown)?.run;
  if (!runId) return null;
  const members = (Array.isArray(siblings) ? siblings : [])
    .filter((att) => refineOf(att)?.run === runId)
    .sort((a, b) => refineOf(a).render - refineOf(b).render || a.id - b.id);
  if (!members.length && !live) return null;
  const last = members.at(-1);
  const turns = live?.turns ?? refineOf(last)?.turns ?? 0;
  const endedAt = members.find((att) => refineOf(att).ended);
  const running = !!live && !endedAt;
  const rows = members.map((att) => {
    const refine = refineOf(att);
    const review = reviewOf(att);
    const ended = refine.ended || null;
    let verdict;
    if (review) verdict = review.done ? "accepted" : "rejected";
    else if (running && live.stage === "reviewing" && live.render === refine.render) verdict = "reviewing";
    else if (ended) verdict = "ended";
    else verdict = "unreviewed";
    return { id: att.id, render: refine.render, review, ended, verdict, current: att.id === shown?.id };
  });
  if (running && live.stage === "rendering" && !rows.some((row) => row.render === live.render))
    rows.push({
      id: null,
      render: live.render,
      review: null,
      ended: null,
      verdict: "rendering",
      current: false,
      ghost: true,
    });
  let header;
  if (endedAt) {
    const { ended, render } = refineOf(endedAt);
    header = { state: ended === "accepted" ? "accepted" : "ended", text: endedHeader(ended, render, turns) };
  } else if (running) header = { state: "live", text: liveHeader({ ...live, turns }) };
  else header = { state: "stopped", text: `Stopped at render ${refineOf(last).render}` };
  // The shown render's own review, else the newest: mid-run that one describes the
  // image on show and says what the next render fixes; after it, the final verdict.
  const reviewed = rows.filter((row) => row.review);
  const focus = reviewed.find((row) => row.current) || reviewed.at(-1) || null;
  return { run: runId, turns, rows, header, running, focusRowId: focus?.id ?? null };
}

function verdictText(row, turns) {
  if (row.verdict === "reviewing") return "Reviewing…";
  if (row.verdict === "rendering") return "Rendering…";
  if (row.review?.critique) return row.review.critique;
  if (row.verdict === "accepted") return "Accepted";
  if (row.verdict === "rejected") return "Asked for another render";
  if (row.ended === "turns_used") return `Not reviewed: all ${turns} revisions were used`;
  if (row.verdict === "ended") return ENDING_ROW_TEXT[row.ended] || "Refinement stopped here";
  return "Not reviewed";
}

// Drawn rather than typed: not every monospace font has a check mark.
const CHECK_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><path d="M4.5 12.5l5 5L19.5 7"/></svg>`;
const CROSS_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" width="12" height="12"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>`;

const VERDICT_MARKS = {
  accepted: [CHECK_ICON, "Accepted"],
  rejected: [CROSS_ICON, "Asked for changes"],
  reviewing: ["", ""],
  rendering: ["", ""],
  ended: ["–", "Stopped"],
  unreviewed: ["–", "Not reviewed"],
};

function critiqueHtml(row, turns, { esc, escAttr, openRows, domId, prefix = "" }) {
  const open = openRows?.has(row.id);
  const id = `${domId}-c${row.id}`;
  // The full text is always in the DOM; the clamp is CSS alone.
  const more = `<button type="button" class="ig-refine-more" data-wf-action="image_gen:refineMore" data-att-id="${escAttr(row.id)}" aria-expanded="${open ? "true" : "false"}" aria-controls="${escAttr(id)}">${open ? "Less" : "More"}</button>`;
  // An ending beside a review (no revised prompt, a failed next render) is its own line.
  const ending =
    row.review && ENDING_ROW_TEXT[row.ended]
      ? `<span class="ig-refine-ending">${esc(ENDING_ROW_TEXT[row.ended])}</span>`
      : "";
  // Why the next render looks unlike this one: the prompter changed the seed too.
  const reseed = row.review?.reseed
    ? `<span class="ig-refine-reseed" title="The next render was drawn from a new seed">New seed</span> `
    : "";
  return `<div class="ig-refine-text${open ? " is-open" : ""}"><span class="ig-refine-critique" id="${escAttr(id)}">${prefix}${reseed}${esc(verdictText(row, turns))}</span>${more}</div>${ending}`;
}

/**
 * The timeline strip under the image. *open* is the list's state, *openRows*
 * the attachment ids whose critique is unclamped, and *stop* `{ jobId,
 * stopping }` while a run this tab can stop is live.
 */
export function refineTimelineHtml(model, { esc, escAttr, msgId, rootId, open = false, openRows = null, stop = null }) {
  const domId = `ig-refine-${rootId}`;
  const opts = { esc, escAttr, openRows, domId };
  const { header, rows, turns } = model;
  const mark =
    header.state === "live"
      ? `<span class="ig-refine-dot" aria-hidden="true"></span>`
      : header.state === "accepted"
        ? `<span class="ig-refine-ok" aria-hidden="true">${CHECK_ICON}</span>`
        : "";
  const stopBtn = stop
    ? `<button type="button" class="ig-refine-stop" data-wf-action="image_gen:refineStop" data-msg-id="${escAttr(msgId)}" data-wf-job="${escAttr(stop.jobId)}" title="Stop refining and keep the renders made so far"${stop.stopping ? " disabled" : ""}>Stop here</button>`
    : "";
  const count = rows.filter((row) => !row.ghost).length;
  // Nothing to list until the run's first render lands.
  const toggle = !count
    ? ""
    : `<button type="button" class="ig-refine-toggle" data-wf-action="image_gen:refineToggle" aria-expanded="${open ? "true" : "false"}" aria-controls="${domId}-list" aria-label="${open ? "Hide" : "Show"} every render's review">${open ? "▴" : `▾ ${count}`}</button>`;
  const head = `<div class="ig-refine-head"><span class="ig-refine-status ig-refine-${header.state}" role="status" aria-live="polite" title="${escAttr(header.text)}">${mark}<span class="ig-refine-status-text">${esc(header.text)}</span></span>${stopBtn}${toggle}</div>`;
  let body;
  if (open) {
    const items = rows.map((row) => {
      if (row.ghost)
        return `<li class="ig-refine-row is-ghost"><span class="ig-refine-num">${esc(row.render)}</span><span class="ig-refine-dot" aria-hidden="true"></span><span class="ig-refine-critique">${esc(verdictText(row, turns))}</span></li>`;
      const [glyph, label] = VERDICT_MARKS[row.verdict];
      const verdict =
        row.verdict === "reviewing"
          ? `<span class="ig-refine-dot" aria-hidden="true"></span>`
          : `<span class="ig-refine-verdict ig-refine-v-${row.verdict}" title="${escAttr(label)}" aria-hidden="true">${glyph}</span><span class="ig-sr">${esc(label)}: </span>`;
      const num = `<button type="button" class="ig-refine-num" data-wf-action="image_gen:refineShow" data-msg-id="${escAttr(msgId)}" data-root-id="${escAttr(rootId)}" data-att-id="${escAttr(row.id)}" aria-label="Show render ${escAttr(row.render)}">${esc(row.render)}</button>`;
      return `<li class="ig-refine-row"${row.current ? ' aria-current="true"' : ""}>${num}${verdict}<div class="ig-refine-cell">${critiqueHtml(row, turns, opts)}</div></li>`;
    });
    body = `<ol class="ig-refine-list" id="${domId}-list">${items.join("")}</ol>`;
  } else {
    const focus = rows.find((row) => row.id === model.focusRowId);
    body = focus
      ? `<div class="ig-refine-body" id="${domId}-list">${critiqueHtml(focus, turns, { ...opts, prefix: `<span class="ig-refine-label">${esc(`Render ${focus.render} review:`)}</span> ` })}</div>`
      : `<div class="ig-refine-body is-empty" id="${domId}-list"></div>`;
  }
  return `<div class="ig-refine${open ? " is-open" : ""}" id="${domId}" data-msg-id="${escAttr(msgId)}" data-root-id="${escAttr(rootId)}">${head}${body}</div>`;
}
