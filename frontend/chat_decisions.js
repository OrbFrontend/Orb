// The Inspector's Decisions panel.
//
// Three things this panel must never do, each of which makes it lie about what
// actually reached the story:
//
//  1. Render a skipped decision as a `false` outcome. A skip carries no outcome
//     at all -- it never ran, or it could not answer -- so skips live in their
//     own list with a reason and no outcome chip.
//  2. Show a `low_confidence` gate as an answer. The gate means a returned
//     answer was deliberately discarded; showing the discarded value as the
//     outcome inverts the gate's meaning.
//  3. Half-render an envelope it does not know. The version is read first and a
//     newer one is left alone.
import {
  answerSourceText,
  distributionPairs,
  formatProbability,
  formatScore,
  isGatedReason,
  outcomeLabel,
  readableEnvelope,
  skipReasonText,
} from "./decisions.js";
import { CHEVRON_RIGHT_ICON } from "./icons.js";
import { S } from "./state.js";
import { esc, escAttr } from "./utils.js";

/**
 * The question type of a record, read back off the answer it carries.
 *
 * The stage records the answer's shape rather than the definition's type, and
 * the shapes do not overlap: noul answers carry a bare probability, score
 * answers a weighted mean, choice answers a selected key. A gated decision,
 * which has no answer to read, falls back to the authored outcome space -- the
 * only other thing on the row that distinguishes the three.
 */
function _typeOf(record) {
  if (record.probability !== undefined) return "noul";
  if (record.score !== undefined) return "score";
  if (record.returned_choice !== undefined) return "choice";
  if (record.type) return record.type;
  const keys = Object.keys(record.outputs || {});
  if (keys.length === 2 && keys.includes("true") && keys.includes("false")) return "noul";
  return Array.isArray(record.rendered_criteria) ? "score" : "choice";
}

/** Outcome keys in authored order; the provider's own key order is not authored order. */
function _orderedKeys(record) {
  const criteria = record.rendered_criteria;
  if (Array.isArray(criteria)) return criteria.map((_level, index) => String(index));
  if (criteria && typeof criteria === "object") return Object.keys(criteria);
  const outputs = record.outputs;
  if (outputs && typeof outputs === "object") return Object.keys(outputs);
  return Object.keys(record.distribution || {});
}

function _criterionText(record, key) {
  const criteria = record.rendered_criteria;
  if (Array.isArray(criteria)) return criteria[Number(key)] ?? "";
  if (criteria && typeof criteria === "object") return criteria[key] ?? "";
  const legend = record.legend;
  return (legend && typeof legend === "object" && legend[key]) || "";
}

/**
 * The key the distribution actually produced, or null when it produced none.
 *
 * A gated row's answer was deliberately discarded and injected nothing, so
 * highlighting its winning key would dress a discarded number up as the outcome
 * -- the exact confusion the gate exists to prevent.
 */
function _selectedKey(record) {
  return record.skip_reason ? null : record.outcome;
}

/** The distribution as rows, with the produced key marked. */
function _distributionHtml(record, selected, caption = "") {
  const pairs = distributionPairs(record.distribution, _orderedKeys(record));
  if (!pairs.length) return "";
  const type = _typeOf(record);
  const rows = pairs
    .map(({ key, value }) => {
      const text = _criterionText(record, key);
      const width = Math.max(0, Math.min(100, value * 100));
      return `<div class="decision-dist-row${key === selected ? " decision-dist-selected" : ""}">
        <span class="decision-dist-key" title="${escAttr(text)}">${esc(outcomeLabel(type, key))}</span>
        <span class="decision-dist-bar"><span style="width:${width.toFixed(1)}%"></span></span>
        <span class="decision-dist-value">${value.toFixed(2)}</span>
      </div>`;
    })
    .join("");
  return `<div class="decision-dist">${
    caption ? `<div class="decision-dist-caption">${esc(caption)}</div>` : ""
  }${rows}</div>`;
}

/** The numbers behind an outcome: probability and draw, or mean and confidence. */
function _answerMeta(record) {
  const bits = [];
  if (record.probability !== undefined) bits.push(`p ${formatProbability(record.probability)}`);
  if (record.score !== undefined) bits.push(`mean ${formatScore(record.score)}`);
  if (record.confidence !== undefined) bits.push(`conf ${formatProbability(record.confidence)}`);
  if (record.draw !== undefined) bits.push(`draw ${formatProbability(record.draw)}`);
  return bits;
}

function _chip(text, kind) {
  return `<span class="decision-chip decision-chip-${kind}">${esc(text)}</span>`;
}

/**
 * The outcome chip for one record.
 *
 * A row that contributed nothing says so and shows no outcome, gated or
 * otherwise: there is no outcome to show, because nothing was injected on its
 * behalf.
 */
function _outcomeChip(record, type) {
  if (record.skip_reason) {
    const gated = isGatedReason(record.skip_reason);
    return _chip(gated ? "gated" : "no outcome", gated ? "gated" : "skipped");
  }
  if (record.outcome === undefined || record.outcome === null) return _chip("no outcome", "skipped");
  return _chip(outcomeLabel(type, String(record.outcome)), "resolved");
}

function _guidanceHtml(record) {
  const guidance = String(record.guidance || "").trim();
  if (!guidance) {
    return `<div class="decision-guidance decision-guidance-empty">Nothing injected for this outcome.</div>`;
  }
  return `<div class="decision-guidance">${esc(guidance)}</div>`;
}

function _evaluationHtml(record) {
  const type = _typeOf(record);
  const meta = _answerMeta(record);
  if (record.answer_source) meta.push(answerSourceText(record.answer_source));
  if (record.replayed_from) meta.push(`was ${answerSourceText(record.replayed_from)}`);
  if (Number.isFinite(record.elapsed_ms) && record.elapsed_ms) meta.push(`${record.elapsed_ms}ms`);
  const label = record.injection_label || record.fragment_label || record.fragment_id;
  return `<details class="decision-eval">
    <summary>
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="decision-eval-name">${esc(label)}</span>
      ${_outcomeChip(record, type)}
      <span class="decision-meta">${esc(meta.join(" · "))}</span>
    </summary>
    <div class="decision-eval-body">
      ${
        record.replay_invalidated
          ? `<div class="decision-reason">The message this was anchored to is gone, so the stored answer was not replayed.</div>`
          : ""
      }
      ${_distributionHtml(record, _selectedKey(record), record.skip_reason ? "answer discarded" : "")}
      ${_guidanceHtml(record)}
      ${
        record.rendered_state
          ? `<details class="decision-state"><summary><span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>Situation sent</summary><div class="injection-box">${esc(record.rendered_state)}</div></details>`
          : ""
      }
    </div>
  </details>`;
}

function _skippedHtml(entry) {
  // No outcome chip, ever: this decision resolved to nothing and injected
  // nothing. Painting a `false` here would be the panel inventing a result.
  //
  // The oversize numbers are the one skip detail worth spelling out, because
  // "too big" is not actionable without the size that was too big. Only the
  // numbers: the reason beside the chip has already said what went wrong.
  const oversize = entry.oversize_state_bytes || entry.oversize_question_bytes;
  return `<div class="decision-skipped-row${entry.failed ? " decision-skipped-failed" : ""}">
    <span class="decision-skipped-name">${esc(entry.fragment_label || entry.fragment_id || "")}</span>
    ${_chip(entry.failed ? "failed" : "skipped", entry.failed ? "failed" : "skipped")}
    <span class="decision-meta">${esc(skipReasonText(entry.reason))}</span>
    ${
      oversize
        ? `<div class="decision-skipped-sizes">state ${entry.oversize_state_bytes || 0}/${entry.state_limit || 0} B · question ${entry.oversize_question_bytes || 0}/${entry.question_limit || 0} B</div>`
        : ""
    }
  </div>`;
}

/** The id the Inspector's toggle listener watches to persist this block's open state. */
export const DECISIONS_SECTION_ID = "decisions-section";

/**
 * The block's outer shell.
 *
 * A `details`, like Reasoning, Tool Calls and Injection Block beside it: this is
 * the only Inspector section whose height grows with the turn, so it is the one
 * that most needs folding away -- and a section whose rows carry disclosure
 * arrows while its own heading carries none reads as though the first row's
 * arrow belongs to the heading.
 */
function _sectionHtml(body) {
  return `<details class="inspector-block decision-block" id="${DECISIONS_SECTION_ID}"${S.decisionsOpen ? " open" : ""}>
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4>Decisions</h4>
    </summary>
    <div class="decision-block-body">${body}</div>
  </details>`;
}

/**
 * The Decisions block for the Inspector.
 *
 * *source* is either the stored envelope from the director-log route, which
 * carries a version, or the live SSE payload, which does not -- it came from
 * this build's own stream this turn, so there is no version skew to guard.
 */
export function buildDecisionsHtml(source, { live = false } = {}) {
  if (!source) return "";
  if (!live && !readableEnvelope(source)) {
    // A newer envelope than this client knows. Say so rather than rendering the
    // fields that happen to be recognisable.
    if (source.version !== undefined) {
      return _sectionHtml(
        `<div class="decision-unreadable">This turn's decisions were recorded by a newer version of Orb and are not shown here.</div>`,
      );
    }
    return "";
  }
  const evaluations = Array.isArray(source.evaluations) ? source.evaluations : [];
  const skipped = Array.isArray(source.skipped) ? source.skipped : [];
  if (!evaluations.length && !skipped.length) return "";
  return _sectionHtml(
    evaluations.map(_evaluationHtml).join("") +
      (skipped.length ? `<div class="decision-skipped">${skipped.map(_skippedHtml).join("")}</div>` : ""),
  );
}

/**
 * The decisions to show right now.
 *
 * An inspected message reads its stored envelope; a turn in flight reads the
 * live event, which arrives before the message it belongs to exists.
 */
export function currentDecisionsHtml() {
  if (S.inspectedMsgId && S.inspectedDirectorData) {
    return buildDecisionsHtml(S.inspectedDirectorData.decision_evaluations);
  }
  return buildDecisionsHtml(S.lastDecisions, { live: true });
}
