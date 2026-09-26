// Decision fragment editor. Server config supplies its options and limits.
import { decisionConfig, outcomeLabel } from "./decisions.js";
import { CLOSE_ICON } from "./icons.js";
import { esc, escAttr } from "./utils.js";

// Draft fields for the open fragment. Guidance mirrors the criterion until edited.
let _draft = null;
// Validation problems keyed by their display field.
let _problems = {};

const PLACEMENT = "before_director";

function _policiesFor(type) {
  return decisionConfig()?.resolution_policies?.[type] || [];
}

// Yes/no and score keys are positional; choice keys are authored names.
function _fixedKey(type, index) {
  if (type === "noul") return index ? "false" : "true";
  return type === "score" ? String(index) : "";
}

function _option(key, text = "", output = "", touched = false) {
  return { key, text: String(text ?? ""), output: String(output ?? ""), touched };
}

/**
 * Start a draft for the fragment being edited, including defaults for type changes.
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
    // Preserve intentionally empty guidance when reopening the draft.
    options: entries.map(([key, text]) => _option(key, text, outputs[key], Object.hasOwn(outputs, key))),
    resolution: fragment.decision_resolution || _policiesFor(type)[0] || "",
    threshold: fragment.decision_threshold ?? null,
    confidence_floor: fragment.decision_confidence_floor ?? null,
  };
}

// ── Reading the form ─────────────────────────────────────────────────────────

/** Serialize all decision fields, using null to clear unused values. */
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
    // Match the displayed default when the threshold field is blank.
    decision_threshold: type === "noul" && resolution === "threshold" ? (_draft.threshold ?? 0.5) : null,
    decision_confidence_floor: type === "noul" ? null : _draft.confidence_floor,
  };
}

/** Catch choice names that would collapse when serialized as object keys. */
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

// Map backend validation messages to the relevant editor fields.
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

const COPY = {
  noul: {
    question: "a statement the Judge scores",
    questionPlaceholder: "The action described in the current request succeeds.",
    outcome: "Outcome",
    criterionPlaceholder: "What this outcome means",
  },
  choice: {
    question: "a question the Judge answers by picking one option below",
    questionPlaceholder: "Which of these best describes how {{char}} takes the current request?",
    outcome: "Option",
    criterionPlaceholder: "What this option means",
    note: 'Tip: for a "none of these" case, put that option first with empty guidance and resolve with "First option gates, else random". When the Judge finds it likeliest, nothing is rolled or injected.',
  },
  score: {
    question: "what the Judge places on the scale below",
    questionPlaceholder: "How far the current request pushes {{char}} past their patience.",
    outcome: "Level",
    criterionPlaceholder: "What this level means",
  },
};

// Display labels; option values remain the API strings.
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
  // Each type/policy pair has at most one numeric setting.
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
    ${_templateFieldHtml("state_template", "Situation", config.default_state_template || "", [
      ...(config.state_macros || []),
      ...(config.inline_macros || []),
    ])}
    ${_templateFieldHtml("instructions", `Question ${_hint(copy.question)}`, copy.questionPlaceholder, [
      ...(config.text_macros || []),
      ...(config.inline_macros || []),
    ])}
    ${_optionsHtml(config, type, copy)}
    ${_problemHtml("criteria")}`;
}

/** The outcome table: one row per outcome, criterion and guidance side by side. */
function _optionsHtml(config, type, copy) {
  const options = _draft.options;
  const editable = type !== "noul";
  const removable = editable && options.length > 2;
  const max = type === "choice" ? config.choice?.max_options : config.score?.max_levels;
  const criterionHead = `What it means ${_hint("- to the Judge")}`;
  const outputHead = `What the story does ${_hint("- injected")}`;
  const rows = options
    .map(
      (option, index) => `
      <div class="decision-option-row">
        <div class="decision-key-cell">
          ${
            type === "choice"
              ? `<input class="decision-key-input" data-opt="${index}" data-field="key" value="${escAttr(option.key)}" placeholder="name" aria-label="Option name">`
              : `<span class="decision-key-fixed">${esc(outcomeLabel(type, option.key))}</span>`
          }
          ${
            removable
              ? `<button type="button" class="btn-icon btn-square decision-row-remove" data-dec-act="del-option" data-index="${index}" title="Remove" aria-label="Remove this outcome">${CLOSE_ICON}</button>`
              : ""
          }
        </div>
        <span class="decision-cell-label" aria-hidden="true">${criterionHead}</span>
        <textarea rows="2" data-opt="${index}" data-field="text" aria-label="What it means" placeholder="${escAttr(copy.criterionPlaceholder)}">${esc(option.text)}</textarea>
        <span class="decision-cell-label" aria-hidden="true">${outputHead}</span>
        <textarea rows="2" data-opt="${index}" data-field="output" aria-label="What the story does" placeholder="What happens on a hit">${esc(option.output)}</textarea>
      </div>`,
    )
    .join("");
  const addBtn =
    editable && !(options.length >= max)
      ? `<button type="button" class="btn btn-sm" data-dec-act="add-option">+ Add ${type === "score" ? "level" : "option"}</button>`
      : "";
  const note = copy.note ? _hint(copy.note) : "";
  return `
    <div class="decision-options">
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
  // Keep criteria and guidance, but rebuild keys for the new type.
  _draft.type = type;
  _draft.options = (type === "noul" ? _draft.options.slice(0, 2) : _draft.options).map((option, index) => ({
    ...option,
    key: _fixedKey(type, index),
  }));
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

// Type changes rebuild the table; resolution changes swap the numeric control.
document.addEventListener("change", (event) => {
  const select = _inSection(event.target.closest("select[data-dec]"));
  if (select) _mutate(() => select.dataset.dec === "type" && _retype(select.value));
});

// Mirror the criterion into guidance until the author edits guidance directly.
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
