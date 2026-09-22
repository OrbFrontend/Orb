// The decision half of the Interactive Fragment editor.
//
// A decision is one question about the scene. One thing about this form is
// load-bearing and easy to undo by accident: criteria and guidance are
// different fields and are the single most confusable pair in the feature, so
// they sit on one row: the outcome, what it means to the Judge, and what the
// story does when it lands.
//
// Each question type words that row -- and the question above it -- for itself,
// from `COPY`. They are not the same question: a noul scores one statement, a
// choice picks a named option, a score places the scene on a ladder.
//
// The type list, resolution policies, macros and bounds all come from
// `GET /api/decisions/config`; nothing here restates them.
import { decisionConfig, loadDecisionConfig, outcomeLabel } from "./decisions.js";
import { CLOSE_ICON } from "./icons.js";
import { esc, escAttr } from "./utils.js";

// The working copy of the decision fields for the fragment currently open in
// the modal. Null whenever the open fragment is not a decision.
let _draft = null;
// Guidance fields the author has typed into. An untouched one mirrors its
// criterion, so the common case costs no extra typing; a touched one is never
// overwritten.
let _touched = new Set();
// Problems from the last 422, already matched to the field they belong against.
let _problems = new Map();
let _generalProblems = [];

const DEFAULT_PLACEMENT = "before_director";

/** Load the config so the selects have something to render from. */
export async function ensureDecisionConfig() {
  try {
    await loadDecisionConfig();
  } catch (_e) {
    // A failed load leaves the section in its "cannot render controls" state,
    // which says so; it must not block the rest of the fragment form.
  }
}

function _cfg() {
  return decisionConfig();
}

function _policiesFor(type) {
  return _cfg()?.resolution_policies?.[type] || [];
}

function _types() {
  return _cfg()?.question_types || [];
}

// ── Draft shape ──────────────────────────────────────────────────────────────
// Criteria and guidance are held as one ordered `options` array per question so
// that every structural edit -- add, delete, rename, retype -- is one operation
// on one list. The wire shapes (object for noul/choice, array for score) are
// built only at read time, in _wireCriteria/_wireOutputs.

function _optionsFrom(type, criteria, outputs) {
  const out = outputs && typeof outputs === "object" && !Array.isArray(outputs) ? outputs : {};
  if (type === "score") {
    const levels = Array.isArray(criteria) ? criteria : [];
    return levels.map((text, index) => ({
      key: String(index),
      text: String(text ?? ""),
      output: String(out[String(index)] ?? ""),
    }));
  }
  const map = criteria && typeof criteria === "object" && !Array.isArray(criteria) ? criteria : {};
  return Object.keys(map).map((key) => ({
    key,
    text: String(map[key] ?? ""),
    output: String(out[key] ?? ""),
  }));
}

function _keysOf(type, options) {
  return type === "score" ? options.map((_option, index) => String(index)) : options.map((option) => option.key);
}

function _wireCriteria(type, options) {
  if (type === "score") return options.map((option) => option.text);
  return Object.fromEntries(options.map((option) => [option.key, option.text]));
}

function _wireOutputs(type, options) {
  return Object.fromEntries(_keysOf(type, options).map((key, index) => [key, options[index].output]));
}

function _blankOptions(type) {
  if (type === "noul") {
    return [
      { key: "true", text: "", output: "" },
      { key: "false", text: "", output: "" },
    ];
  }
  if (type === "score") {
    return [
      { key: "0", text: "", output: "" },
      { key: "1", text: "", output: "" },
    ];
  }
  // A choice key is not an id. It goes to the Judge as the option's name and
  // comes back as the answer, so a generated `option_1` is a worse prompt than
  // no prompt: it is a name that says nothing about the option it names. The
  // author writes it, and `decisionDraftProblems` refuses a save without it.
  return [
    { key: "", text: "", output: "" },
    { key: "", text: "", output: "" },
  ];
}

/**
 * Start (or clear) the decision draft for the fragment about to be edited.
 *
 * Called for every interactive fragment, decision or not: a fragment being
 * switched *to* `decision` needs a draft with the shipped defaults, and one
 * being switched away needs the stale draft gone.
 */
export function initDecisionDraft(fragment) {
  _touched = new Set();
  _problems = new Map();
  _generalProblems = [];
  const type = typeof fragment?.decision_type === "string" && fragment.decision_type ? fragment.decision_type : "noul";
  const options = fragment?.decision_criteria
    ? _optionsFrom(type, fragment.decision_criteria, fragment.decision_outputs)
    : _blankOptions(type);
  _draft = {
    type,
    placement: String(fragment?.decision_placement || DEFAULT_PLACEMENT),
    state_template: String(fragment?.decision_state_template ?? ""),
    instructions: String(fragment?.decision_instructions ?? ""),
    options,
    resolution: String(fragment?.decision_resolution || _policiesFor(type)[0] || ""),
    threshold: Number.isFinite(fragment?.decision_threshold) ? fragment.decision_threshold : null,
    confidence_floor: Number.isFinite(fragment?.decision_confidence_floor) ? fragment.decision_confidence_floor : null,
  };
  // Guidance an author already wrote is theirs: mirroring must never overwrite
  // it, so everything non-empty starts out touched.
  _draft.options.forEach((option, index) => {
    if (option.output) _touched.add(`p:${index}`);
  });
  if (!_draft.state_template) _draft.state_template = _cfg()?.default_state_template || "";
}

/** Drop the draft; called when the modal closes. */
export function clearDecisionDraft() {
  _draft = null;
  _touched = new Set();
  _problems = new Map();
  _generalProblems = [];
}

// ── Reading the form ─────────────────────────────────────────────────────────

/**
 * The nine decision columns, always all of them.
 *
 * For decision columns an explicit `null` is a write, not an omission: it is
 * the only way to clear one. Switching to `roll` has to send
 * `decision_threshold: null` in the same request, or the stored threshold
 * survives in the merged row the backend validates and the update is refused.
 */
export function readDecisionFields() {
  _syncFromDom();
  if (!_draft) return {};
  const isNoul = _draft.type === "noul";
  return {
    decision_type: _draft.type,
    decision_placement: _draft.placement || DEFAULT_PLACEMENT,
    decision_state_template: _draft.state_template,
    decision_instructions: _draft.instructions,
    decision_criteria: _wireCriteria(_draft.type, _draft.options),
    decision_outputs: _wireOutputs(_draft.type, _draft.options),
    decision_resolution: _draft.resolution,
    decision_threshold: isNoul && _draft.resolution === "threshold" ? _draft.threshold : null,
    decision_confidence_floor: isNoul ? null : _draft.confidence_floor,
  };
}

/**
 * Problems the editor has to catch itself, or "" when the draft can be sent.
 *
 * Option names are the one part of a choice that cannot survive to the backend
 * to be complained about. `decision_criteria` and `decision_outputs` go out as
 * JSON objects, so a blank or repeated name is already gone -- collapsed into
 * its twin -- by the time the row is validated. Three options with two names
 * save cleanly as two, losing one silently; two unnamed options arrive as one
 * and come back as "must contain 2 to 255 options", which is not what went
 * wrong. Both are checked here, while the rows still exist.
 *
 * Phrased and prefixed like a backend problem so `applyDecisionProblems` can
 * route it to the outcome table: one rendering path for every rule.
 */
export function decisionDraftProblems() {
  _syncFromDom();
  if (_draft?.type !== "choice") return "";
  const problems = [];
  const seen = new Set();
  const repeated = new Set();
  let unnamed = false;
  for (const option of _draft.options) {
    const key = String(option.key || "").trim();
    if (!key) unnamed = true;
    else if (seen.has(key)) repeated.add(key);
    else seen.add(key);
  }
  if (unnamed)
    problems.push("decision_criteria: every option needs a name, because the Judge reads it and answers with it");
  if (repeated.size)
    problems.push(`decision_criteria: option names must differ (repeated: ${[...repeated].join(", ")})`);
  return problems.join("; ");
}

/** Is a decision section currently open? */
export function hasDecisionDraft() {
  return _draft !== null;
}

function _number(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function _syncFromDom() {
  const root = document.getElementById("decision-section");
  if (!root || !_draft) return;
  for (const el of root.querySelectorAll("[data-dec]")) {
    const field = el.dataset.dec;
    if (field === "threshold" || field === "confidence_floor") _draft[field] = _number(el.value);
    else _draft[field] = el.value;
  }
  for (const el of root.querySelectorAll("[data-dec-key]")) {
    const option = _draft.options[Number(el.dataset.decKey)];
    if (option) option.key = el.value.trim();
  }
  for (const el of root.querySelectorAll("[data-dec-criterion]")) {
    const option = _draft.options[Number(el.dataset.decCriterion)];
    if (option) option.text = el.value;
  }
  for (const el of root.querySelectorAll("[data-dec-output]")) {
    const option = _draft.options[Number(el.dataset.decOutput)];
    if (option) option.output = el.value;
  }
}

// ── Problems from a 422 ──────────────────────────────────────────────────────

// Validation runs on the merged row and comes back as problems joined by "; ".
// Each is rendered against the field it names rather than thrown at a toast,
// because "decision_outputs must have exactly the keys true, false" is only
// actionable next to the outcome table it is about.
const PROBLEM_ANCHORS = [
  [/^decision_type\b/, "type"],
  // No anchor for decision_placement: there is one placement and so no control
  // to hang it on, and a problem routed at a field that is not rendered is a
  // problem the author never sees. It falls through to the general list.
  [/^(decision_state_template|Situation template)\b/, "state_template"],
  [/^(decision_instructions|Question)\b/, "instructions"],
  [/^(decision_criteria|Outcome description)\b/, "criteria"],
  [/^(decision_outputs|Guidance)\b/, "criteria"],
  [/^decision_resolution\b/, "resolution"],
  [/^decision_threshold\b/, "threshold"],
  [/^decision_confidence_floor\b/, "confidence_floor"],
];

/**
 * Route a `422` detail onto the fields it names.
 *
 * Returns true when at least one problem was placed, so the caller can fall
 * back to a toast for an error that is not about the decision at all.
 */
export function applyDecisionProblems(detail) {
  _problems = new Map();
  _generalProblems = [];
  const problems = String(detail || "")
    .split("; ")
    .map((problem) => problem.trim())
    .filter(Boolean);
  if (!problems.length) return false;
  let placed = false;
  for (const problem of problems) {
    const anchor = PROBLEM_ANCHORS.find(([pattern]) => pattern.test(problem));
    if (anchor) {
      _push(anchor[1], problem);
      placed = true;
    } else {
      _generalProblems.push(problem);
    }
  }
  if (!placed && !_generalProblems.length) return false;
  repaintDecisionSection();
  return true;
}

function _push(anchor, problem) {
  const list = _problems.get(anchor) || [];
  list.push(problem);
  _problems.set(anchor, list);
}

function _problemHtml(anchor) {
  const list = _problems.get(anchor);
  if (!list?.length) return "";
  return `<div class="decision-problem">${list.map((problem) => esc(problem)).join("<br>")}</div>`;
}

// ── Rendering ────────────────────────────────────────────────────────────────

export function decisionSectionHtml(fieldType) {
  const hidden = fieldType === "decision" ? "" : ' style="display:none"';
  return `<div id="decision-section" class="decision-section"${hidden}>${_innerHtml()}</div>`;
}

export function repaintDecisionSection() {
  const root = document.getElementById("decision-section");
  if (!root) return;
  root.innerHTML = _innerHtml();
}

/** Re-read the form, then repaint: every structural edit goes through here. */
function _mutate(change) {
  _syncFromDom();
  if (!_draft) return;
  change();
  repaintDecisionSection();
}

// The words each type uses for the question and for its outcome table. Nothing
// here is a contract -- but for most authors it is the only description of the
// feature they will ever read, and the shared noul wording it replaces made a
// `choice` look like a true/false question it is not.
const COPY = {
  noul: {
    question: "a statement the Judge scores",
    questionPlaceholder: "The action described in the current request succeeds.",
    outcome: "If this outcome",
    criterion: "What it looks like",
    criterionPlaceholder: "What this outcome looks like in the scene",
  },
  choice: {
    question: "a question the Judge answers by picking one option below",
    questionPlaceholder: "Which of these best describes how {{char}} takes the current request?",
    outcome: "Option",
    outcomeHint: "",
    criterion: "What this option means",
    criterionPlaceholder: "What picking this option means; the Judge reads it as part of the question",
    note: 'If the fragment should sometimes do nothing, add a catch-all option (e.g. "none of these") and leave its guidance empty to inject nothing.',
  },
  score: {
    question: "what the Judge places on the scale below",
    questionPlaceholder: "How far the current request pushes {{char}} past their patience.",
    outcome: "Level",
    criterion: "What this level looks like",
    criterionPlaceholder: "What this level looks like in the scene",
  },
};

function _copy(type) {
  return COPY[type] || COPY.noul;
}

function _hint(text) {
  return `<span class="decision-hint">${esc(text)}</span>`;
}

function _macroHint(list) {
  return (list || []).map((macro) => `{{${macro}}}`).join(" ");
}

function _innerHtml() {
  if (!_draft) return "";
  const config = _cfg();
  if (!config) {
    return `<div class="decision-problem">Could not load the decision configuration, so the question controls cannot be rendered. Reopen this fragment once the app can reach <code>/api/decisions/config</code>.</div>`;
  }
  return [
    _statusHtml(config),
    _generalProblems.length
      ? `<div class="decision-problem">${_generalProblems.map((problem) => esc(problem)).join("<br>")}</div>`
      : "",
    _primaryHtml(config),
  ].join("");
}

function _statusHtml(config) {
  if (!config.configured) {
    return `<div class="decision-status decision-status-warn">No Judge endpoint is configured, so enabled decisions are skipped and inject nothing. Set one in the Endpoints panel under <strong>Judge</strong>.</div>`;
  }
}

// Layman labels for the resolution policies the backend serves. The select's
// `value` stays the raw wire string; only the displayed text differs.
const RESOLUTION_LABELS = {
  threshold: "Cutoff",
  roll: "Random roll",
  argmax: "Most likely",
  weighted: "Random by odds",
  nearest: "Closest level",
};

function _primaryHtml(config) {
  const type = _draft.type;
  const policies = _policiesFor(type);
  const showThreshold = type === "noul" && _draft.resolution === "threshold";
  const typeOptions = _types()
    .map((value) => `<option value="${escAttr(value)}"${value === type ? " selected" : ""}>${esc(value)}</option>`)
    .join("");
  const policyOptions = policies
    .map(
      (value) =>
        `<option value="${escAttr(value)}"${value === _draft.resolution ? " selected" : ""}>${esc(
          RESOLUTION_LABELS[value] || value,
        )}</option>`,
    )
    .join("");
  return `
    <div class="frag-divider">Question</div>
    <div class="field-row">
      <div class="field">
        <label>Question Type</label>
        <select data-dec="type" data-dec-act="retype">${typeOptions}</select>
        ${_problemHtml("type")}
      </div>
      <div class="field">
        <label>Resolution</label>
        <select data-dec="resolution" data-dec-act="repaint">${policyOptions}</select>
        ${_problemHtml("resolution")}
      </div>
    </div>
    <div class="field-row">
      <div class="field field-half"${showThreshold ? "" : ' style="display:none"'}>
        <label>Threshold ${_hint("resolves true at or above this probability")}</label>
        <input type="number" min="0" max="1" step="0.01" data-dec="threshold" value="${escAttr(_draft.threshold ?? "")}" placeholder="0.5">
        ${_problemHtml("threshold")}
      </div>
      <div class="field field-half"${type === "noul" ? ' style="display:none"' : ""}>
        <label>Confidence floor ${_hint("blank = no gating")}</label>
        <input type="number" min="0" max="1" step="0.01" data-dec="confidence_floor" value="${escAttr(_draft.confidence_floor ?? "")}" placeholder="none">
        ${_problemHtml("confidence_floor")}
      </div>
    </div>
    <div class="field">
      <label>Situation template ${_hint(`macros: ${_macroHint(config.state_macros)}`)}</label>
      <textarea data-dec="state_template" rows="5" placeholder="${escAttr(config.default_state_template || "")}">${esc(_draft.state_template)}</textarea>
      ${_problemHtml("state_template")}
    </div>
    <div class="field">
      <label>Question ${_hint(`${_copy(type).question}; macros: ${_macroHint(config.text_macros)}`)}</label>
      <textarea data-dec="instructions" rows="2" placeholder="${escAttr(_copy(type).questionPlaceholder)}">${esc(_draft.instructions)}</textarea>
      ${_problemHtml("instructions")}
    </div>
    ${_optionsHtml(config, type, _draft.options)}
    ${_problemHtml("criteria")}`;
}

function _optionLabel(type, key, option) {
  if (type === "noul") return outcomeLabel(type, key);
  if (type === "score") return `Level ${key}`;
  return option?.key || key;
}

/**
 * The criteria table: one row per outcome, criterion and guidance side by side.
 *
 * Criteria describe the world to the Judge; guidance is what gets injected into
 * the story if that outcome lands. They are adjacent because authors conflate
 * them constantly, and the header names both jobs in this type's own words.
 */
function _optionsHtml(config, type, options) {
  const copy = _copy(type);
  const bounds =
    type === "choice"
      ? `2 to ${config.choice?.max_options ?? ""} options`
      : type === "score"
        ? `${config.score?.min_levels ?? ""} to ${config.score?.max_levels ?? ""} levels, in order`
        : "";
  const canEditCount = type !== "noul";
  const rows = options
    .map((option, index) => {
      const keyCell =
        type === "choice"
          ? `<input class="decision-key-input" data-dec-key="${index}" value="${escAttr(option.key)}" placeholder="name">`
          : `<span class="decision-key-fixed">${esc(_optionLabel(type, _keysOf(type, options)[index], option))}</span>`;
      const removeBtn =
        canEditCount && options.length > 2
          ? `<button type="button" class="btn-icon btn-square decision-row-remove" data-dec-act="del-option" data-index="${index}" title="Remove this outcome" aria-label="Remove this outcome">${CLOSE_ICON}</button>`
          : "";
      return `
      <div class="decision-option-row">
        <div class="decision-option-key">${keyCell}</div>
        <textarea rows="2" data-dec-criterion="${index}" placeholder="${escAttr(copy.criterionPlaceholder)}">${esc(option.text)}</textarea>
        <textarea rows="2" data-dec-output="${index}" data-mirror="p:${index}" placeholder="What the story does if it lands">${esc(option.output)}</textarea>
        ${removeBtn}
      </div>`;
    })
    .join("");
  const addBtn = canEditCount
    ? `<button type="button" class="btn btn-sm" data-dec-act="add-option">+ Add outcome</button>`
    : "";
  return `
    <div class="decision-options">
      <div class="decision-option-head">
        <span>${esc(copy.outcome)}${copy.outcomeHint ? ` ${_hint(copy.outcomeHint)}` : ""}</span>
        <span>${esc(copy.criterion)} ${_hint("sent to the Judge")}</span>
        <span>What the story does ${_hint("injected as guidance")}</span>
        <span></span>
      </div>
      ${rows}
      <div class="decision-options-foot">${addBtn}${bounds ? _hint(bounds) : ""}</div>
      ${copy.note ? `<div class="decision-options-note">${_hint(copy.note)}</div>` : ""}
    </div>`;
}

// ── Structural edits ─────────────────────────────────────────────────────────

function _retypePrimary(nextType) {
  _draft.type = nextType;
  if (nextType === "noul") {
    const [first, second] = _draft.options;
    _draft.options = [
      { key: "true", text: first?.text ?? "", output: first?.output ?? "" },
      { key: "false", text: second?.text ?? "", output: second?.output ?? "" },
    ];
  } else if (nextType === "score") {
    if (_draft.options.length < 2) _draft.options = _blankOptions("score");
    _draft.options = _draft.options.map((option, index) => ({ ...option, key: String(index) }));
  } else {
    // Whatever the keys were, they were the other type's: `true`/`false` from a
    // noul, level indices from a score. Carrying them over asks the Judge to
    // pick between two options called "true" and "false", which is a noul with
    // its threshold taken away. The criteria and guidance are the author's and
    // survive; the names do not.
    _draft.options = _draft.options.map((option) => ({ ...option, key: "" }));
  }
  // The policies are per type and do not overlap between noul and the rest, so
  // a retype always re-picks rather than keeping a policy the stage would
  // reject on save.
  _draft.resolution = _policiesFor(nextType)[0] || "";
  if (nextType !== "noul" || _draft.resolution !== "threshold") _draft.threshold = null;
  if (nextType === "noul") _draft.confidence_floor = null;
}

function _addOption() {
  const index = _draft.options.length;
  _draft.options.push({
    key: _draft.type === "score" ? String(index) : "",
    text: "",
    output: "",
  });
}

function _deleteOption(index) {
  if (_draft.options.length <= 2) return;
  _draft.options.splice(index, 1);
  if (_draft.type === "score") _draft.options = _draft.options.map((option, i) => ({ ...option, key: String(i) }));
  _shiftTouched("p:", index);
}

// The mirror flags are positional, so a delete has to shift them or the wrong
// guidance field stops mirroring.
function _shiftTouched(prefix, removed) {
  const next = new Set();
  for (const mark of _touched) {
    if (!mark.startsWith(prefix)) {
      next.add(mark);
      continue;
    }
    const index = Number(mark.slice(prefix.length));
    if (index === removed) continue;
    next.add(`${prefix}${index > removed ? index - 1 : index}`);
  }
  _touched = next;
}

// ── Events ───────────────────────────────────────────────────────────────────

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-dec-act]");
  if (!button || !document.getElementById("decision-section")?.contains(button)) return;
  const action = button.dataset.decAct;
  // The selects carry their own actions and fire on change, not click.
  if (button.tagName === "SELECT" || button.tagName === "INPUT") return;
  const index = Number(button.dataset.index);
  if (action === "add-option") _mutate(() => _addOption());
  else if (action === "del-option") _mutate(() => _deleteOption(index));
});

document.addEventListener("change", (event) => {
  const el = event.target.closest("[data-dec-act]");
  if (!el || !document.getElementById("decision-section")?.contains(el)) return;
  const action = el.dataset.decAct;
  if (action === "retype") _mutate(() => _retypePrimary(el.value));
  else if (action === "repaint") _mutate(() => {});
});

// Guidance mirrors its criterion until the author types into the guidance
// field. That makes the common case free without ever inventing an injection:
// an empty output still means "inject nothing", never an implicit echo.
document.addEventListener("input", (event) => {
  const root = document.getElementById("decision-section");
  if (!root?.contains(event.target)) return;
  const el = event.target;
  const mirrorTarget = el.dataset.mirror;
  if (mirrorTarget) {
    _touched.add(mirrorTarget);
    return;
  }
  if (el.dataset.decCriterion === undefined) return;
  const row = el.closest(".decision-option-row");
  const output = row?.querySelector("[data-mirror]");
  if (!output || _touched.has(output.dataset.mirror)) return;
  output.value = el.value;
});
