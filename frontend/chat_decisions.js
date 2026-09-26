// The Inspector's Decisions section, in the panel and under each reply. Skips have no outcome chip.
import { outcomeLabel, skipReasonText } from "./decisions.js";
import { CHEVRON_RIGHT_ICON } from "./icons.js";
import { S } from "./state.js";
import { esc, escAttr } from "./utils.js";

const EVALUATIONS_VERSION = 2;

const ANSWER_SOURCES = { live: "live", cache: "cached", replay: "replayed" };

// [field, label, decimals] for numeric answer details.
const ANSWER_NUMBERS = [
  ["probability", "odds", 2],
  ["score", "average", 3],
  ["confidence", "confidence", 2],
  ["draw", "roll", 2],
];

function _chip(text, kind) {
  return `<span class="decision-chip decision-chip-${kind}">${esc(text)}</span>`;
}

function _metaText(record) {
  const bits = ANSWER_NUMBERS.filter(([field]) => record[field] != null).map(
    ([field, label, digits]) => `${label} ${Number(record[field]).toFixed(digits)}`,
  );
  if (record.answer_source) bits.push(ANSWER_SOURCES[record.answer_source] || record.answer_source);
  if (record.replayed_from) bits.push(`was ${ANSWER_SOURCES[record.replayed_from] || record.replayed_from}`);
  if (record.elapsed_ms) bits.push(`${record.elapsed_ms}ms`);
  return bits.join(" · ");
}

/** Render the distribution in authored order, marking the resolved outcome. */
function _distributionHtml(record, type) {
  const distribution = record.distribution;
  if (!distribution) return "";
  const criteria = record.rendered_criteria;
  const keys = Array.isArray(criteria) ? criteria.map((_level, i) => String(i)) : Object.keys(criteria || distribution);
  const rows = keys
    .filter((key) => key in distribution)
    .map((key) => {
      const value = Number(distribution[key]) || 0;
      return `<div class="decision-dist-row${key === record.outcome ? " decision-dist-selected" : ""}">
        <span class="decision-dist-key" title="${escAttr(criteria?.[key] ?? "")}">${esc(outcomeLabel(type, key))}</span>
        <span class="decision-dist-bar"><span style="width:${(Math.min(1, Math.max(0, value)) * 100).toFixed(1)}%"></span></span>
        <span class="decision-dist-value">${value.toFixed(2)}</span>
      </div>`;
    });
  return `<div class="decision-dist">${rows.join("")}</div>`;
}

// The stored answer shape identifies its question type.
const _answerType = (record) => (record.probability != null ? "noul" : record.score != null ? "score" : "choice");

const _evaluationName = (record) => record.injection_label || record.fragment_label || record.fragment_id;

function _evaluationHtml(record) {
  const type = _answerType(record);
  const guidance = String(record.guidance || "").trim();
  return `<details class="decision-eval">
    <summary>
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="decision-eval-name">${esc(_evaluationName(record))}</span>
      ${_chip(outcomeLabel(type, String(record.outcome)), "resolved")}
      <span class="decision-meta">${esc(_metaText(record))}</span>
    </summary>
    <div class="decision-eval-body">
      ${
        record.replay_invalidated
          ? `<div class="decision-reason">The message this was anchored to is gone, so the stored answer was not replayed.</div>`
          : ""
      }
      ${_distributionHtml(record, type)}
      <div class="decision-guidance${guidance ? "" : " decision-guidance-empty"}">${guidance ? esc(guidance) : "Nothing injected for this outcome."}</div>
      ${
        record.rendered_state
          ? `<details class="decision-state"><summary><span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>Situation sent</summary><div class="injection-box">${esc(record.rendered_state)}</div></details>`
          : ""
      }
    </div>
  </details>`;
}

function _skippedHtml(entry) {
  const kind = entry.failed ? "failed" : "skipped";
  // Include byte counts so oversized inputs can be corrected.
  return `<div class="decision-skipped-row${entry.failed ? " decision-skipped-failed" : ""}">
    <span class="decision-skipped-name">${esc(entry.fragment_label || entry.fragment_id)}</span>
    ${_chip(kind, kind)}
    <span class="decision-meta">${esc(skipReasonText(entry.reason))}</span>
    ${
      entry.state_limit
        ? `<div class="decision-skipped-sizes">state ${entry.oversize_state_bytes}/${entry.state_limit} B · question ${entry.oversize_question_bytes}/${entry.question_limit} B</div>`
        : ""
    }
  </div>`;
}

// *source* is a stored `decision_evaluations` envelope (*stored*; newer versions
// are skipped) or the live `decisions` event.
function _records(source, stored) {
  if (stored && (!Number.isInteger(source?.version) || source.version > EVALUATIONS_VERSION)) return null;
  const evaluations = source?.evaluations || [];
  const skipped = source?.skipped || [];
  return evaluations.length || skipped.length ? { evaluations, skipped } : null;
}

/** One ``{label, outcome}`` per resolved evaluation, for compact summaries. */
export function decisionOutcomes(source, { stored }) {
  return (_records(source, stored)?.evaluations || []).map((record) => ({
    label: _evaluationName(record),
    outcome: outcomeLabel(_answerType(record), String(record.outcome)),
  }));
}

/** The Decisions section for *source*, or "" when it has nothing to show. */
export function decisionsHtml(source, { stored }) {
  const records = _records(source, stored);
  if (!records) return "";
  const { evaluations, skipped } = records;
  return `<details class="inspector-block decision-block" data-inspect-section="decisions"${S.decisionsOpen ? " open" : ""}>
    <summary class="reasoning-summary">
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <h4>Decisions</h4>
    </summary>
    <div class="decision-block-body">
      ${evaluations.map(_evaluationHtml).join("")}
      ${skipped.length ? `<div class="decision-skipped">${skipped.map(_skippedHtml).join("")}</div>` : ""}
    </div>
  </details>`;
}

/** Render stored or in-flight decisions for the Inspector panel. */
export function currentDecisionsHtml() {
  const inspecting = Boolean(S.inspectedMsgId);
  const source = inspecting ? S.inspectedDirectorData?.decision_evaluations : S.lastDecisions;
  return decisionsHtml(source, { stored: inspecting });
}
