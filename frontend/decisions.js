// Shared config cache and display labels for decision fragments.
// Authoring options come from the server config.
import { api } from "./api.js";

let _config = null;
let _request = null;

/** The last loaded classifier config, or null before the first load. */
export function decisionConfig() {
  return _config;
}

/** Load the config once; return null on failure so the next call retries. */
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
  return String(key).replaceAll("_", " ");
}

// Unknown reasons remain visible instead of being swallowed.
const SKIP_REASONS = {
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

/** Summarize failed skips for the turn, or return an empty string. */
export function skipNoticeText(skipped = []) {
  const failed = skipped.filter((entry) => entry.failed);
  if (!failed.length) return "";
  const reasons = [...new Set(failed.map((entry) => skipReasonText(entry.reason)))];
  const subject =
    failed.length === 1 ? failed[0].fragment_label || failed[0].fragment_id : `${failed.length} decisions`;
  return `${subject} skipped: ${reasons.join("; ").toLowerCase()}`;
}
