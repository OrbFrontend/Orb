// Shared decision-fragment vocabulary: the cached classifier config, the
// outcome-space rule, and the human wording for every machine reason the stage
// can report.
//
// The type list, resolution policies, macros and bounds are NOT constants here.
// They come from `GET /api/decisions/config`, so a backend that gains a type or
// widens a bound does not need a matching frontend edit -- and, more to the
// point, an editor can never offer a policy the stage would reject.
import { api } from "./api.js";

// The envelope version this client understands. A newer envelope is left alone
// rather than half-rendered: an Inspector that guesses at a shape it does not
// know is worse than one that says it cannot read it.
export const DECISION_EVALUATIONS_VERSION = 2;

const NOUL_OUTCOME_KEYS = ["true", "false"];

let _config = null;
let _configRequest = null;

/** The last loaded classifier config, or null before the first load. */
export function decisionConfig() {
  return _config;
}

/** Load (and cache) `GET /api/decisions/config`. Concurrent callers share one request. */
export async function loadDecisionConfig({ force = false } = {}) {
  if (_config && !force) return _config;
  if (!_configRequest) {
    _configRequest = api
      .get("/decisions/config")
      .then((payload) => {
        _config = payload;
        return payload;
      })
      .finally(() => {
        _configRequest = null;
      });
  }
  return _configRequest;
}

/** Record a config the caller already has in hand (a PUT response). */
export function setDecisionConfig(payload) {
  if (payload && typeof payload === "object") _config = payload;
  return _config;
}

/** The label an outcome key shows in the editor and the Inspector. */
export function outcomeLabel(type, key) {
  if (type === "noul") return key === NOUL_OUTCOME_KEYS[0] ? "True" : "False";
  if (type === "score") return `Level ${key}`;
  return key;
}

// Every fallback reason `FallbackReason` in the decision stage can attach to a
// record. A reason with no entry here is shown verbatim rather than swallowed.
const FALLBACK_REASONS = {
  not_configured: "No Judge endpoint is configured",
  invalid_definition: "The definition is not valid",
  empty_input: "The situation rendered empty",
  unavailable_context: "A macro had no value on this turn",
  oversized_input: "The rendered request was over the size limit",
  budget_exhausted: "The stage ran out of its request budget",
  transport_failure: "The provider could not be reached",
  timeout: "The provider did not answer in time",
  invalid_answer: "The answer was missing or unusable",
  invalid_facet_answer: "The facet's answer was missing or unusable",
  low_confidence: "Gated below the confidence floor",
  missing_anchor: "The message this was anchored to is gone",
};

// `SkipReason`. A skip has no outcome at all: the callers of skipReasonText
// must never paint one of these as a resolved `false`.
const SKIP_REASONS = {
  not_approved: "Not approved for this character",
  resting: "Resting on cooldown",
  invalid_definition: "The definition is not valid",
};

export function fallbackReasonText(reason) {
  return FALLBACK_REASONS[reason] || String(reason || "");
}

export function skipReasonText(reason) {
  return SKIP_REASONS[reason] || String(reason || "");
}

/** Is this fallback the confidence gate rather than a failure? */
export function isGatedReason(reason) {
  return reason === "low_confidence";
}

const ANSWER_SOURCES = {
  live: "live",
  cache: "cached",
  replay: "replayed",
  fallback: "fallback",
};

export function answerSourceText(source) {
  return ANSWER_SOURCES[source] || String(source || "");
}

/** `"global"` or `"card:<id>"`, kept traceable back to the card that carried it. */
export function decisionSourceText(source) {
  const text = String(source || "");
  return text.startsWith("card:") ? `Character card ${text.slice(5)}` : text === "global" ? "Global" : text;
}

/** A probability or confidence as a fixed 2-decimal string; "—" when absent. */
export function formatProbability(value) {
  return Number.isFinite(value) ? Number(value).toFixed(2) : "—";
}

/** A score's probability-weighted mean. Three decimals: the mean and the argmax genuinely disagree. */
export function formatScore(value) {
  return Number.isFinite(value) ? Number(value).toFixed(3) : "—";
}

/**
 * A distribution as `key p` pairs in *keys* order.
 *
 * Authored order, not descending probability: an author reading the row is
 * checking their own option list against what came back, and a re-sorted list
 * makes that comparison by hand.
 */
export function distributionPairs(distribution, keys) {
  if (!distribution || typeof distribution !== "object") return [];
  const ordered = keys?.length ? keys : Object.keys(distribution);
  return ordered.filter((key) => key in distribution).map((key) => ({ key, value: Number(distribution[key]) || 0 }));
}

/** Can this client render *envelope*? A newer version is left to a newer client. */
export function readableEnvelope(envelope) {
  const version = envelope?.version;
  return Number.isInteger(version) && version <= DECISION_EVALUATIONS_VERSION;
}
