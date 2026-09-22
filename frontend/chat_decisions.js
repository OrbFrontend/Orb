// The Inspector's Decisions panel.
//
// Three things this panel must never do, each of which makes it lie about what
// actually reached the story:
//
//  1. Render a skipped decision as a `false` outcome. A skip carries no outcome
//     at all -- it never ran -- so skips live in their own list with a reason
//     and no outcome chip.
//  2. Show a `low_confidence` fallback as an answer. The gate means a returned
//     answer was deliberately discarded; showing the discarded value as the
//     outcome inverts the gate's meaning.
//  3. Half-render an envelope it does not know. The version is read first and a
//     newer one is left alone.
import {
  answerSourceText,
  decisionSourceText,
  distributionPairs,
  fallbackReasonText,
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
 * answers a weighted mean, choice answers a selected key. A fallback with no
 * answer at all falls back to the authored outcome space, which is the only
 * other thing on the record that distinguishes the three.
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
 * A fallback outcome -- gated, timed out, unparseable -- did not come out of
 * this distribution: it is the authored default. Highlighting its row would
 * dress the author's own fallback up as the Judge's answer, which is the exact
 * confusion the gate exists to prevent.
 */
function _selectedKey(record) {
  return record.answer_source === "fallback" ? null : record.outcome;
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
 * A gated record gets "gated", never its discarded answer; a fallback gets
 * "fallback" beside the outcome it actually used, so the authored fallback is
 * never mistaken for something the Judge said.
 */
function _outcomeChip(record, type) {
  const gated = isGatedReason(record.fallback_reason);
  if (record.outcome === undefined || record.outcome === null) {
    return _chip(gated ? "gated" : "no outcome", gated ? "gated" : "fallback");
  }
  const label = outcomeLabel(type, String(record.outcome));
  if (gated) return `${_chip(label, "fallback")}${_chip("gated", "gated")}`;
  if (record.answer_source === "fallback") return `${_chip(label, "fallback")}${_chip("fallback", "fallback")}`;
  return _chip(label, "resolved");
}

function _guidanceHtml(record) {
  const guidance = String(record.guidance || "").trim();
  if (!guidance) {
    return `<div class="decision-guidance decision-guidance-empty">Nothing injected for this outcome.</div>`;
  }
  return `<div class="decision-guidance">${esc(guidance)}</div>`;
}

function _facetHtml(facet) {
  const type = facet.type || _typeOf(facet);
  const gated = isGatedReason(facet.fallback_reason);
  const meta = _answerMeta(facet);
  if (facet.branch) meta.unshift(`branch ${facet.branch}`);
  if (facet.answer_source) meta.push(answerSourceText(facet.answer_source));
  return `<div class="decision-facet-row${gated ? " decision-facet-gated" : ""}">
    <div class="decision-facet-line">
      <span class="decision-facet-name">${esc(facet.label || facet.key || "")}</span>
      ${_outcomeChip(facet, type)}
      <span class="decision-meta">${esc(meta.join(" · "))}</span>
    </div>
    ${
      facet.fallback_reason
        ? `<div class="decision-reason${gated ? " decision-reason-gated" : ""}">${esc(
            gated
              ? "Gated below the confidence floor — the returned answer below was not used and nothing was injected."
              : fallbackReasonText(facet.fallback_reason),
          )}</div>`
        : ""
    }
    ${_distributionHtml(facet, _selectedKey(facet), facet.fallback_reason ? "Returned, not used" : "")}
    ${facet.fallback_reason ? "" : _guidanceHtml(facet)}
  </div>`;
}

function _discardedHtml(record) {
  const discarded = Array.isArray(record.discarded_branches) ? record.discarded_branches : [];
  if (!discarded.length) return "";
  const rows = discarded
    .map((branch) => {
      const meta = _answerMeta(branch);
      if (branch.fallback_reason) meta.push(fallbackReasonText(branch.fallback_reason));
      return `<div class="decision-discarded-row">
        <span class="decision-facet-name">${esc(branch.label || branch.key || "")}</span>
        <span class="decision-branch-tag">${esc(branch.branch || "")}</span>
        <span class="decision-meta">${esc(meta.join(" · "))}</span>
        ${_distributionHtml(branch, null)}
      </div>`;
    })
    .join("");
  return `<details class="decision-discarded">
    <summary><span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>Discarded branches (${discarded.length})</summary>
    <div class="decision-discarded-body">${rows}</div>
  </details>`;
}

/**
 * The shared request's usage, shown once on the record that owns it.
 *
 * A fan-out is one request for the whole batch, so repeating its cost per
 * decision would multiply a number that was only paid once. A gateway that
 * reports no cost is labelled unavailable rather than rendered as zero.
 */
function _usageHtml(record) {
  if (!record.usage_owner) return "";
  const usage = record.usage || {};
  const bits = [];
  const tokens = usage.total_tokens ?? usage.prompt_tokens;
  if (Number.isFinite(tokens)) bits.push(`${tokens} tokens`);
  bits.push(Number.isFinite(usage.cost) ? `$${Number(usage.cost).toFixed(6)}` : "cost unavailable");
  if (record.request_id) bits.push(esc(record.request_id));
  return `<div class="decision-usage">Shared request: ${bits.join(" · ")}</div>`;
}

function _evaluationHtml(record) {
  const type = _typeOf(record);
  const gated = isGatedReason(record.fallback_reason);
  const meta = _answerMeta(record);
  if (record.answer_source) meta.push(answerSourceText(record.answer_source));
  if (record.replayed_from) meta.push(`was ${answerSourceText(record.replayed_from)}`);
  if (Number.isFinite(record.elapsed_ms) && record.elapsed_ms) meta.push(`${record.elapsed_ms}ms`);
  const label = record.injection_label || record.fragment_label || record.fragment_id;
  const facets = Array.isArray(record.facets) ? record.facets : [];
  const oversize = record.oversize_state_bytes || record.oversize_question_bytes;
  return `<details class="decision-eval">
    <summary>
      <span class="reasoning-summary-arrow">${CHEVRON_RIGHT_ICON}</span>
      <span class="decision-eval-name">${esc(label)}</span>
      ${_outcomeChip(record, type)}
      <span class="decision-meta">${esc(meta.join(" · "))}</span>
    </summary>
    <div class="decision-eval-body">
      <div class="decision-meta decision-eval-source">${esc(decisionSourceText(record.source))}${
        record.returned_model ? ` · ${esc(record.returned_model)}` : ""
      }</div>
      ${
        record.fallback_reason
          ? `<div class="decision-reason${gated ? " decision-reason-gated" : ""}">${esc(
              gated
                ? "Gated below the confidence floor — the returned answer below was not used; the fallback outcome was injected instead."
                : `Fell back: ${fallbackReasonText(record.fallback_reason)}`,
            )}</div>`
          : ""
      }
      ${
        record.replay_invalidated
          ? `<div class="decision-reason">The message this was anchored to is gone, so the stored answer was not replayed.</div>`
          : ""
      }
      ${
        oversize
          ? `<div class="decision-reason">Rendered input was over the limit (state ${record.oversize_state_bytes || 0}/${record.state_limit || 0} bytes, question ${record.oversize_question_bytes || 0}/${record.question_limit || 0} bytes).</div>`
          : ""
      }
      ${_distributionHtml(record, _selectedKey(record), record.fallback_reason ? "Returned, not used" : "")}
      ${_guidanceHtml(record)}
      ${facets.length ? `<div class="decision-facets">${facets.map(_facetHtml).join("")}</div>` : ""}
      ${_discardedHtml(record)}
      ${_usageHtml(record)}
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
  return `<div class="decision-skipped-row">
    <span class="decision-eval-name">${esc(entry.fragment_label || entry.fragment_id || "")}</span>
    ${_chip("skipped", "skipped")}
    <span class="decision-meta">${esc(skipReasonText(entry.reason))} · ${esc(decisionSourceText(entry.source))}</span>
  </div>`;
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
      return `<div class="inspector-block"><h4>Decisions</h4>
        <div class="decision-unreadable">This turn's decisions were recorded by a newer version of Orb and are not shown here.</div>
      </div>`;
    }
    return "";
  }
  const evaluations = Array.isArray(source.evaluations) ? source.evaluations : [];
  const skipped = Array.isArray(source.skipped) ? source.skipped : [];
  if (!evaluations.length && !skipped.length) return "";
  return `<div class="inspector-block decision-block">
    <h4>Decisions</h4>
    ${evaluations.map(_evaluationHtml).join("")}
    ${skipped.length ? `<div class="decision-skipped">${skipped.map(_skippedHtml).join("")}</div>` : ""}
  </div>`;
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
