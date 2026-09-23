// Shared decision-fragment vocabulary: the cached classifier config and the
// human wording for every machine reason the stage can report.
//
// The type list, resolution policies, macros and bounds are NOT constants here.
// They come from `GET /api/decisions/config`, so an editor can never offer a
// policy the stage would reject.
import { api } from "./api.js";

let _config = null;
let _request = null;

/** The last loaded classifier config, or null before the first load. */
export function decisionConfig() {
  return _config;
}

/** Load (once) `GET /api/decisions/config`. Resolves null on failure, so a later call retries. */
export function loadDecisionConfig() {
  _request ??= api.get("/decisions/config").then(
    (payload) => (_config = payload),
    () => {
      _request = null;
      return null;
    },
  );
  return _request;
}

/** Record a config the caller already has in hand (a PUT response). */
export function setDecisionConfig(payload) {
  _config = payload;
}

/** The label an outcome key shows in the editor and the Inspector. */
export function outcomeLabel(type, key) {
  if (type === "noul") return key === "true" ? "True" : "False";
  if (type === "score") return `Level ${key}`;
  return key;
}

// Every `SkipReason` the decision stage can report. A reason with no entry here
// is shown verbatim rather than swallowed. A skip has no outcome at all, so it
// must never be painted as a resolved `false`.
const SKIP_REASONS = {
  not_approved: "Not approved for this character",
  resting: "Resting on cooldown",
  not_configured: "No Judge endpoint is configured",
  invalid_definition: "The definition is not valid",
  empty_input: "The situation rendered empty",
  unavailable_context: "A macro had no value on this turn",
  oversized_input: "The rendered request was over the size limit",
  budget_exhausted: "The stage ran out of its request budget",
  transport_failure: "The provider could not be reached",
  timeout: "The provider did not answer in time",
  invalid_answer: "The answer was missing or unusable",
  low_confidence: "Gated below the confidence floor",
  missing_anchor: "The message this was anchored to is gone",
};

export function skipReasonText(reason) {
  return SKIP_REASONS[reason] || String(reason || "");
}

/**
 * One line for a turn's failed skips, or "" when nothing failed. One line for
 * the whole turn: a provider that is down takes every decision with it.
 */
export function skipNoticeText(skipped = []) {
  const failed = skipped.filter((entry) => entry.failed);
  if (!failed.length) return "";
  const reasons = [...new Set(failed.map((entry) => skipReasonText(entry.reason)))];
  const subject =
    failed.length === 1 ? failed[0].fragment_label || failed[0].fragment_id : `${failed.length} decisions`;
  return `${subject} skipped: ${reasons.join("; ").toLowerCase()}`;
}
