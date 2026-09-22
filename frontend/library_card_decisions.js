// Per-card consent for the decisions a character card carries.
//
// An imported card's decisions arrive disabled and unapproved, and approving
// them is what lets their question text leave this machine. So the panel shows
// the text that would actually be sent -- every facet branch included, because
// those are questions too -- and approves against the fingerprint it just read.
// A 409 means the definitions moved under the reader, and their consent was for
// something they are no longer looking at.
import { api } from "./api.js";
import { esc } from "./utils.js";

let _state = null;

function _mount() {
  return document.getElementById("ce-card-decisions");
}

/** Load and paint the approval panel for *cardId*; a card with no decisions paints nothing. */
export async function renderCardDecisionApproval(cardId) {
  const el = _mount();
  if (!el || !cardId) return;
  try {
    _state = await api.get(`/decisions/card-approval/${cardId}`);
  } catch (_e) {
    _state = null;
    el.innerHTML = "";
    return;
  }
  _paint();
}

function _paint() {
  const el = _mount();
  if (!el) return;
  if (!_state?.has_decisions) {
    el.innerHTML = "";
    return;
  }
  const { approved, stale, questions } = _state;
  el.innerHTML = `
    <div class="frag-divider">Decisions on this character</div>
    ${
      stale
        ? `<div class="card-decision-stale">These questions changed since you approved them, so they are <strong>not running</strong>. Review the text below and approve again.</div>`
        : ""
    }
    <div class="card-decision-note">
      Approving sends the text below to the configured Judge provider on every turn these decisions run.
      ${approved && !stale ? "Approved." : "Not approved — these decisions are skipped until you approve them."}
    </div>
    <div class="card-decision-list">${(questions || []).map(_questionHtml).join("")}</div>
    <div class="card-decision-actions">
      <button type="button" class="btn btn-sm${approved && !stale ? "" : " btn-accent"}" data-card-decision="${approved && !stale ? "revoke" : "approve"}">
        ${approved && !stale ? "Revoke approval" : "Approve these questions"}
      </button>
      <span class="card-decision-status" id="ce-card-decision-status"></span>
    </div>`;
}

function _criteriaHtml(criteria) {
  const entries = Array.isArray(criteria)
    ? criteria.map((text, index) => [String(index), text])
    : criteria && typeof criteria === "object"
      ? Object.entries(criteria)
      : [];
  if (!entries.length) return "";
  return `<ul class="card-decision-criteria">${entries
    .map(([key, text]) => `<li><span class="card-decision-key">${esc(key)}</span>${esc(String(text ?? ""))}</li>`)
    .join("")}</ul>`;
}

// A facet's instructions are either one string (asked once) or one per primary
// outcome. Both are shown in full: a branch that is never selected is still a
// question that was sent.
function _instructionsHtml(instructions) {
  if (typeof instructions === "string") return `<div class="card-decision-question">${esc(instructions)}</div>`;
  if (!instructions || typeof instructions !== "object") return "";
  return Object.entries(instructions)
    .map(
      ([branch, text]) =>
        `<div class="card-decision-question"><span class="card-decision-key">${esc(branch)}</span>${esc(String(text ?? ""))}</div>`,
    )
    .join("");
}

function _questionHtml(question) {
  const facets = Array.isArray(question.facets) ? question.facets : [];
  return `<div class="card-decision-item">
    <div class="card-decision-head">
      <span class="card-decision-label">${esc(question.label || question.id || "")}</span>
      <span class="card-decision-type">${esc(question.type || "")}</span>
    </div>
    <div class="card-decision-question">${esc(String(question.instructions ?? ""))}</div>
    ${_criteriaHtml(question.criteria)}
    ${facets
      .map(
        (facet) => `<div class="card-decision-facet">
        <div class="card-decision-head">
          <span class="card-decision-label">${esc(facet.label || facet.key || "")}</span>
          <span class="card-decision-type">${esc(facet.type || "")}</span>
        </div>
        ${_instructionsHtml(facet.instructions)}
        ${_criteriaHtml(facet.criteria)}
      </div>`,
      )
      .join("")}
  </div>`;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-card-decision]");
  if (!button || !_state) return;
  const approving = button.dataset.cardDecision === "approve";
  const status = document.getElementById("ce-card-decision-status");
  button.disabled = true;
  if (status) status.textContent = "";
  try {
    // The fingerprint goes back exactly as read, so consent cannot be granted
    // against definitions the reader never saw.
    _state = await api.put(`/decisions/card-approval/${_state.card_id}`, {
      fingerprint: approving ? _state.fingerprint : null,
    });
    _paint();
  } catch (e) {
    button.disabled = false;
    if (!status) return;
    status.textContent =
      e.status === 409
        ? "These questions changed while you were reading them. Reopen this card and review the new text."
        : e.message;
    status.classList.add("card-decision-status-error");
  }
});
