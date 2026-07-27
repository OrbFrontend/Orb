// extension_policy.js — the frontend's trust decisions about workflow manifest
// entries. Pure predicates over plain data: no DOM, no imports, no state.
//
// This is deliberately its own leaf module rather than three lines inside
// workflow_loader.js. The predicates decide which manifest entries may become a
// dynamic import() and which are inert data, which makes them the most
// security-relevant code in the frontend — and code that cannot be imported
// under `node --test` (workflow_loader.js reaches settings.js, which needs a
// DOM) is code whose tests would have to assert on its source text instead of
// its behavior.

/** Orb-shipped, code-reviewed ES modules under /static/workflows/. */
export const TRUSTED_MODULE = "trusted_module";

/** Community packages: a validated component/command tree, rendered as data. */
export const DECLARATIVE = "declarative";

/**
 * True when a manifest entry may be loaded with a dynamic import().
 *
 * Positive matching, on purpose. `kind !== DECLARATIVE` would put an entry from
 * a newer Orb — or one whose field is missing because a response was truncated,
 * proxied, or hand-edited — into the *trusted* band by default. An unrecognised
 * entry has to fail closed: not loading a workflow degrades a feature, loading
 * package-chosen code does not degrade, it escalates.
 *
 * @param {unknown} entry a `/api/workflows` manifest record
 * @returns {boolean}
 */
export function isTrustedModuleEntry(entry) {
  if (!entry || typeof entry !== "object") return false;
  if (typeof entry.id !== "string" || entry.id === "") return false;
  return entry.frontend_kind === TRUSTED_MODULE;
}

/**
 * True when a manifest entry is a community package the host renderer owns.
 *
 * Not the negation of isTrustedModuleEntry: an entry that is neither (a
 * malformed record, or a kind this build does not know) belongs to neither
 * path and is dropped by both.
 *
 * @param {unknown} entry a `/api/workflows` manifest record
 * @returns {boolean}
 */
export function isDeclarativeEntry(entry) {
  if (!entry || typeof entry !== "object") return false;
  if (typeof entry.id !== "string" || entry.id === "") return false;
  return entry.frontend_kind === DECLARATIVE;
}

/**
 * True when a community entry's active revision can publish entry points.
 *
 * `installed`, `enabled`, and `available` are three independent axes. This
 * answers only the third: an `incompatible` or `invalid` package still renders
 * in the extension manager with its diagnostic, it just contributes no
 * commands, views, or placements.
 *
 * @param {unknown} entry a `/api/workflows` manifest record
 * @returns {boolean}
 */
export function isAvailable(entry) {
  return Boolean(entry) && typeof entry === "object" && entry.load_status === "available";
}
