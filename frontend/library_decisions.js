// The decision half of the Interactive Fragment editor.
//
// Criteria and guidance are the single most confusable pair in the feature, so
// they sit on one row: the outcome, what it means to the Judge, and what the
// story does when it lands. Each question type words that row -- and the
// question above it -- for itself, from `COPY`.
//
// The type list, resolution policies, macros and bounds all come from
// `GET /api/decisions/config`; nothing here restates them.
import { decisionConfig, outcomeLabel } from "./decisions.js";
import { CLOSE_ICON } from "./icons.js";
import { esc, escAttr } from "./utils.js";

// The working copy of the decision fields for the fragment open in the modal.
// Criteria and guidance are one ordered `options` list, so every structural
// edit is one operation on one list; the wire shapes are built in
// readDecisionFields. An option's `touched` is set once its guidance is typed
// into: until then the guidance mirrors its criterion.
let _draft = null;
// Problems from the last 422 (or draft check), keyed by the field they render under.
let _problems = {};

// The one placement the stage accepts.
const PLACEMENT = "before_director";

function _policiesFor(type) {
  return decisionConfig()?.resolution_policies?.[type] || [];
}

// noul and score keys are positional. A choice key is the option's name as the
// Judge reads it, so a generated `option_1` would be a worse prompt than none:
// the author writes it, and decisionDraftProblems refuses a save without it.
function _fixedKey(type, index) {
  if (type === "noul") return index ? "false" : "true";
  return type === "score" ? String(index) : "";
}

function _option(key, text = "", output = "", touched = false) {
  return { key, text: String(text ?? ""), output: String(output ?? ""), touched };
}

/**
 * Start the decision draft for the fragment about to be edited. Called for
 * every interactive fragment: one switched *to* `decision` needs defaults.
 */
export function initDecisionDraft(fragment) {
  _problems = {};
  const type = fragment.decision_type || "noul";
  const criteria = fragment.decision_criteria;
  const outputs = fragment.decision_outputs || {};
  const entries = Array.isArray(criteria)
    ? criteria.map((text, index) => [String(index), text])
    : criteria
      ? Object.entries(criteria)
      : [0, 1].map((index) => [_fixedKey(type, index), ""]);
  _draft = {
    type,
    state_template: fragment.decision_state_template || decisionConfig()?.default_state_template || "",
    instructions: fragment.decision_instructions || "",
    // Saved empty guidance is intentional too: reopening must not re-enable
    // mirroring and turn a no-injection outcome into story instructions.
    options: entries.map(([key, text]) => _option(key, text, outputs[key], Object.hasOwn(outputs, key))),
    resolution: fragment.decision_resolution || _policiesFor(type)[0] || "",
    threshold: fragment.decision_threshold ?? null,
    confidence_floor: fragment.decision_confidence_floor ?? null,
  };
}

// ── Reading the form ─────────────────────────────────────────────────────────

/**
 * The nine decision columns, always all of them. An explicit `null` is the only
 * way to clear one: switching to `roll` must send `decision_threshold: null`,
 * or the stored threshold survives in the merged row and the update is refused.
 */
export function readDecisionFields() {
  _syncFromDom();
  const { type, options, resolution } = _draft;
  return {
    decision_type: type,
    decision_placement: PLACEMENT,
    decision_state_template: _draft.state_template,
    decision_instructions: _draft.instructions,
    decision_criteria:
      type === "score" ? options.map((o) => o.text) : Object.fromEntries(options.map((o) => [o.key, o.text])),
    decision_outputs: Object.fromEntries(options.map((o) => [o.key, o.output])),
    decision_resolution: resolution,
    // Blank sends the 0.5 the field shows as its placeholder, not a refused null.
    decision_threshold: type === "noul" && resolution === "threshold" ? (_draft.threshold ?? 0.5) : null,
    decision_confidence_floor: type === "noul" ? null : _draft.confidence_floor,
  };
}

/**
 * Problems the editor has to catch itself, or "" when the draft can be sent.
 *
 * Choice names go out as JSON object keys, so a blank or repeated one has
 * collapsed into its twin before the backend could complain about it. Phrased
 * like a backend problem so applyDecisionProblems routes it the same way.
 */
export function decisionDraftProblems() {
  _syncFromDom();
  if (_draft.type !== "choice") return "";
  const keys = _draft.options.map((o) => o.key);
  const repeated = [...new Set(keys.filter((key, index) => key && keys.indexOf(key) !== index))];
  const problems = [];
  if (keys.includes(""))
    problems.push("decision_criteria: every option needs a name, because the Judge reads it and answers with it");
  if (repeated.length) problems.push(`decision_criteria: option names must differ (repeated: ${repeated.join(", ")})`);
  return problems.join("; ");
}

function _number(raw) {
  const text = String(raw ?? "").trim();
  return text && Number.isFinite(Number(text)) ? Number(text) : null;
}

function _syncFromDom() {
  const root = document.getElementById("decision-section");
  if (!root || !_draft) return;
  for (const el of root.querySelectorAll("[data-dec]")) {
    const field = el.dataset.dec;
    _draft[field] = field === "threshold" || field === "confidence_floor" ? _number(el.value) : el.value;
  }
  for (const el of root.querySelectorAll("[data-opt]")) {
    const field = el.dataset.field;
    _draft.options[Number(el.dataset.opt)][field] = field === "key" ? el.value.trim() : el.value;
  }
}

// ── Problems from a 422 ──────────────────────────────────────────────────────

// Validation comes back as problems joined by "; ". Each renders against the
// field it names, because "decision_outputs must have exactly the keys true,
// false" is only actionable next to the outcome table. Anything unmatched --
// decision_placement has no control -- goes to the general list, not nowhere.
const PROBLEM_ANCHORS = [
  [/^decision_(type|resolution|threshold|confidence_floor)\b/, "policy"],
  [/^(decision_state_template|Situation template)\b/, "state_template"],
  [/^(decision_instructions|Question)\b/, "instructions"],
  [/^(decision_criteria|decision_outputs|Outcome description|Guidance)\b/, "criteria"],
];

/** Route a problem string onto the fields it names; false when there was nothing to route. */
export function applyDecisionProblems(detail) {
  const problems = String(detail || "")
    .split("; ")
    .map((problem) => problem.trim())
    .filter(Boolean);
  if (!problems.length) return false;
  _problems = {};
  for (const problem of problems) {
    const anchor = PROBLEM_ANCHORS.find(([pattern]) => pattern.test(problem))?.[1] || "general";
    _problems[anchor] = [...(_problems[anchor] || []), problem];
  }
  repaintDecisionSection();
  return true;
}

function _problemHtml(anchor) {
  const list = _problems[anchor];
  return list ? `<div class="decision-problem">${list.map((problem) => esc(problem)).join("<br>")}</div>` : "";
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
  fitDecisionTextareas(root);
}

// For most authors this is the only description of the feature they will ever
// read, so each type words its question and outcome table in its own terms.
const COPY = {
  noul: {
    question: "a statement the Judge scores",
    questionPlaceholder: "The action described in the current request succeeds.",
    outcome: "Outcome",
    criterion: "What it looks like",
    criterionPlaceholder: "What this outcome looks like in the scene",
  },
  choice: {
    question: "a question the Judge answers by picking one option below",
    questionPlaceholder: "Which of these best describes how {{char}} takes the current request?",
    outcome: "Option",
    criterion: "What it means",
    criterionPlaceholder: "What picking this option means",
    note: 'Tip: for a "none of these" case, put that option first with empty guidance and resolve with "First option gates, else random". When the Judge finds it likeliest, nothing is rolled or injected.',
  },
  score: {
    question: "what the Judge places on the scale below",
    questionPlaceholder: "How far the current request pushes {{char}} past their patience.",
    outcome: "Level",
    criterion: "What it looks like",
    criterionPlaceholder: "What this level looks like in the scene",
    note: "Lowest level first.",
  },
};

// Layman labels for the resolution policies; the option value stays the wire string.
const RESOLUTION_LABELS = {
  threshold: "Cutoff",
  roll: "Random roll",
  argmax: "Most likely",
  weighted: "Random by odds",
  gated: "First option gates, else random",
  nearest: "Closest level",
};

function _hint(text) {
  return `<span class="decision-hint">${esc(text)}</span>`;
}

function _selectOptions(values, selected, label = (value) => value) {
  return values
    .map(
      (value) =>
        `<option value="${escAttr(value)}"${value === selected ? " selected" : ""}>${esc(label(value))}</option>`,
    )
    .join("");
}

/** A template textarea, the macros it expands as one quiet line under it, and its problems. */
function _templateFieldHtml(field, labelHtml, placeholder, macros = []) {
  return `<div class="field">
    <label>${labelHtml}</label>
    <textarea data-dec="${field}" rows="2" placeholder="${escAttr(placeholder)}">${esc(_draft[field])}</textarea>
    ${macros.length ? `<div class="decision-macros">${macros.map((macro) => `<code>{{${esc(macro)}}}</code>`).join(" ")}</div>` : ""}
    ${_problemHtml(field)}
  </div>`;
}

function _innerHtml() {
  if (!_draft) return "";
  const config = decisionConfig();
  if (!config) {
    return `<div class="decision-problem">Could not load the decision configuration, so the question controls cannot be rendered. Reopen this fragment once the app can reach <code>/api/decisions/config</code>.</div>`;
  }
  const { type, resolution } = _draft;
  const copy = COPY[type] || COPY.noul;
  // At most one knob applies to a type/policy pair, so it takes the row's third slot.
  const knob =
    type !== "noul"
      ? [
          "confidence_floor",
          "Min confidence",
          "Discard answers the Judge is less sure of than this; blank = never",
          "off",
        ]
      : resolution === "threshold"
        ? ["threshold", "Threshold", "Resolves true at or above this probability", "0.5"]
        : null;
  return `
    ${config.configured ? "" : `<div class="decision-status">No Judge endpoint configured. Set one under <strong>Endpoints → Judge</strong>.</div>`}
    ${_problemHtml("general")}
    <div class="frag-divider">Decision</div>
    <div class="field-row">
      <div class="field">
        <label>Question type</label>
        <select data-dec="type">${_selectOptions(config.question_types || [], type)}</select>
      </div>
      <div class="field">
        <label>Resolution</label>
        <select data-dec="resolution">${_selectOptions(_policiesFor(type), resolution, (value) => RESOLUTION_LABELS[value] || value)}</select>
      </div>
      ${
        knob
          ? `<div class="field decision-knob">
        <label title="${escAttr(knob[2])}">${knob[1]}</label>
        <input type="number" min="0" max="1" step="0.01" data-dec="${knob[0]}" value="${escAttr(_draft[knob[0]] ?? "")}" placeholder="${knob[3]}" title="${escAttr(knob[2])}">
      </div>`
          : ""
      }
    </div>
    ${_problemHtml("policy")}
    ${_templateFieldHtml("state_template", "Situation", config.default_state_template || "", config.state_macros)}
    ${_templateFieldHtml("instructions", `Question ${_hint(copy.question)}`, copy.questionPlaceholder, config.text_macros)}
    ${_optionsHtml(config, type, copy)}
    ${_problemHtml("criteria")}`;
}

/** The outcome table: one row per outcome, criterion and guidance side by side. */
function _optionsHtml(config, type, copy) {
  const options = _draft.options;
  const editable = type !== "noul";
  const max = type === "choice" ? config.choice?.max_options : config.score?.max_levels;
  const criterionHead = `${esc(copy.criterion)} ${_hint("- to the Judge")}`;
  const outputHead = `What the story does ${_hint("- injected")}`;
  const rows = options
    .map(
      (option, index) => `
      <div class="decision-option-row">
        ${
          type === "choice"
            ? `<input class="decision-key-input" data-opt="${index}" data-field="key" value="${escAttr(option.key)}" placeholder="name" aria-label="Option name">`
            : `<span class="decision-key-fixed">${esc(outcomeLabel(type, option.key))}</span>`
        }
        <span class="decision-cell-label" aria-hidden="true">${criterionHead}</span>
        <textarea rows="2" data-opt="${index}" data-field="text" aria-label="${escAttr(copy.criterion)}" placeholder="${escAttr(copy.criterionPlaceholder)}">${esc(option.text)}</textarea>
        <span class="decision-cell-label" aria-hidden="true">${outputHead}</span>
        <textarea rows="2" data-opt="${index}" data-field="output" aria-label="What the story does" placeholder="What the story does on hit">${esc(option.output)}</textarea>
        ${
          !editable
            ? ""
            : options.length > 2
              ? `<button type="button" class="btn-icon btn-square decision-row-remove" data-dec-act="del-option" data-index="${index}" title="Remove" aria-label="Remove this outcome">${CLOSE_ICON}</button>`
              : "<span></span>"
        }
      </div>`,
    )
    .join("");
  const addBtn =
    editable && !(options.length >= max)
      ? `<button type="button" class="btn btn-sm" data-dec-act="add-option">+ Add ${type === "score" ? "level" : "option"}</button>`
      : "";
  const note = copy.note ? _hint(copy.note) : "";
  return `
    <div class="decision-options${editable ? " decision-options-editable" : ""}">
      <div class="decision-option-head">
        <span>${esc(copy.outcome)}</span>
        <span>${criterionHead}</span>
        <span>${outputHead}</span>
      </div>
      ${rows}
      ${addBtn || note ? `<div class="decision-options-foot">${addBtn}${note}</div>` : ""}
    </div>`;
}

/** Grow the section's textareas to their text. */
export function fitDecisionTextareas(root = document.getElementById("decision-section")) {
  for (const el of root?.querySelectorAll("textarea") || []) _fit(el);
}

function _fit(el) {
  // A hidden section measures 0; it is fitted again when it is shown.
  if (!el.offsetParent) return;
  // From zero, not "auto": auto is the rows attribute's height, a floor the
  // measurement would never go under.
  el.style.height = "0";
  el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
}

// ── Structural edits ─────────────────────────────────────────────────────────

/** Re-read the form, apply *change* to the draft, then repaint. */
function _mutate(change) {
  _syncFromDom();
  change();
  repaintDecisionSection();
}

function _retype(type) {
  // The old keys were the old type's own (true/false, level indices), and a
  // choice between options named "true" and "false" is a noul with its
  // threshold taken away. Criteria and guidance survive; the names do not.
  _draft.type = type;
  _draft.options = (type === "noul" ? _draft.options.slice(0, 2) : _draft.options).map((option, index) => ({
    ...option,
    key: _fixedKey(type, index),
  }));
  // Policies are per type and do not overlap between noul and the rest.
  _draft.resolution = _policiesFor(type)[0] || "";
}

function _inSection(el) {
  return el && document.getElementById("decision-section")?.contains(el) ? el : null;
}

document.addEventListener("click", (event) => {
  const button = _inSection(event.target.closest("button[data-dec-act]"));
  if (!button) return;
  _mutate(() => {
    const { options, type } = _draft;
    if (button.dataset.decAct === "add-option") {
      options.push(_option(_fixedKey(type, options.length)));
    } else if (options.length > 2) {
      options.splice(Number(button.dataset.index), 1);
      if (type === "score") _draft.options = options.map((option, index) => ({ ...option, key: String(index) }));
    }
  });
});

// Both selects repaint on change: the type rebuilds the table, the resolution swaps the knob.
document.addEventListener("change", (event) => {
  const select = _inSection(event.target.closest("select[data-dec]"));
  if (select) _mutate(() => select.dataset.dec === "type" && _retype(select.value));
});

// Guidance mirrors its criterion until the author types into it. That makes the
// common case free without inventing an injection: empty still means "nothing".
document.addEventListener("input", (event) => {
  const el = _inSection(event.target);
  if (!el) return;
  if (el.tagName === "TEXTAREA") _fit(el);
  const option = _draft?.options[Number(el.dataset.opt)];
  if (!option) return;
  if (el.dataset.field === "output") {
    option.touched = true;
  } else if (el.dataset.field === "text" && !option.touched) {
    const output = el.parentElement.querySelector('[data-field="output"]');
    output.value = el.value;
    _fit(output);
  }
});
