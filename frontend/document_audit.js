import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { trackRange } from "./document_editor.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { AUDIT_TYPE_DEFS, showPhraseBankModal } from "./settings.js";
import { persistSettings } from "./settings_store.js";
import { S } from "./state.js";
import { $, esc, escAttr, toast } from "./utils.js";

const DOC_AUDIT_KEYS = [
  "banned_phrases",
  "repetitive_openers",
  "repetitive_templates",
  "contrastive_negation",
  "phrase_repetition",
  "negated_narration",
];
const DOC_AUDIT_TITLES = {
  phrase_repetition: "Flag phrases the new text repeats from the last few pages or from itself. Names are ignored.",
};
const DOC_AUDIT_OFF_BY_DEFAULT = ["negated_narration"];
const DOC_AUDIT_DEFS = AUDIT_TYPE_DEFS.filter((d) => DOC_AUDIT_KEYS.includes(d.key)).map((d) => ({
  ...d,
  title: DOC_AUDIT_TITLES[d.key] || d.title,
}));
const docAuditOn = (toggles, key) => toggles[key] ?? !DOC_AUDIT_OFF_BY_DEFAULT.includes(key);

let _ctx = null; // { getContent, applyPatchedRun, selectRange } injected by document.js

export function initDocAudit(ctx) {
  _ctx = ctx;
  $("doc-workflow-btn")?.addEventListener("click", toggleDocWorkflowPanel);
  $("doc-workflow-mobile-btn")?.addEventListener("click", toggleDocWorkflowPanel);
}

function docAuditEnabled() {
  return Boolean(S.settings?.document_audit_enabled);
}

function toggleDocWorkflowPanel() {
  if (isUtilityPanelOpen("tools-panel")) {
    closeUtilityPanel("tools-panel");
  } else {
    openUtilityPanel("tools-panel", renderDocAuditPane);
  }
}

export function renderDocAuditPane() {
  const pane = $("tools-pane-doc");
  if (!pane) return;
  const on = docAuditEnabled();
  const toggles = S.settings?.document_audit_toggles || {};
  const autopatch = Boolean(S.settings?.document_audit_autopatch);

  const checks = DOC_AUDIT_DEFS.map(
    (a) => `<label class="lg-enforce-label" title="${escAttr(a.title)}">
             <input type="checkbox" data-doc-audit-key="${a.key}" ${docAuditOn(toggles, a.key) ? "checked" : ""}>
             ${esc(a.label)}
           </label>`,
  ).join("");
  const config = on
    ? `<div class="lg-config">
         <div class="audit-types">${checks}</div>
         <label class="lg-enforce-label" title="After an audit with findings, automatically run one LLM patch call that rewrites the flagged spans in the generated run. Off by default: it is an extra LLM call.">
           <input type="checkbox" id="doc-audit-autopatch-chk" ${autopatch ? "checked" : ""}>
           Auto-patch after generation
         </label>
       </div>`
    : "";

  pane.innerHTML = `
    <div class="tool-card ${on ? "tool-on" : ""}">
      <div class="tool-card-header">
        <span class="tool-card-name">Output Auditor</span>
        <label class="tog">
          <input type="checkbox" id="doc-audit-enable-chk" ${on ? "checked" : ""}>
          <span class="tog-slider"></span>
        </label>
      </div>
      <div class="tool-card-desc">Scans each generation for LLM slop and repetition. Findings appear below and can be patched in place.</div>
      ${config}
    </div>
    <div class="doc-audit-phrasebank">
      <button class="btn btn-sm btn-block" id="doc-audit-phrasebank-btn">📚 Phrase Bank</button>
    </div>
    <div id="doc-audit-results"></div>`;

  $("doc-audit-enable-chk").addEventListener("change", async (e) => {
    await persistSettings({ document_audit_enabled: e.target.checked });
    renderDocAuditPane();
  });
  $("doc-audit-autopatch-chk")?.addEventListener("change", (e) =>
    persistSettings({ document_audit_autopatch: e.target.checked }),
  );
  for (const input of pane.querySelectorAll("[data-doc-audit-key]")) {
    input.addEventListener("change", (e) => {
      const cur = S.settings?.document_audit_toggles || {};
      persistSettings({ document_audit_toggles: { ...cur, [e.target.dataset.docAuditKey]: e.target.checked } });
    });
  }
  $("doc-audit-phrasebank-btn").addEventListener("click", showPhraseBankModal);

  renderDocAuditResults();
}

function statusText(r) {
  if (r.status === "auditing") return "Auditing…";
  if (r.status === "patching") return "Patching…";
  if (r.status === "error") return "Failed — try Audit again";
  const parts = [];
  if (r.patchedCount != null) parts.push(`Patched ${r.patchedCount}`);
  if (r.skipped === "no_complete_sentence") parts.push("No complete sentence to audit");
  else if (r.skipped === "no_addressable_findings") parts.push("Nothing patchable — rewrite by hand");
  else if (r.report) {
    parts.push(
      r.report.total_issues === 0 ? "Clean" : `${r.report.total_issues} issue${r.report.total_issues === 1 ? "" : "s"}`,
    );
  }
  if (r.tailExcluded) parts.push("truncated tail excluded");
  if (isStale(r)) parts.push("edited since audit");
  return parts.join(" · ");
}

const _ids = (list) => ((list || []).length ? `<span class="doc-audit-id">[${(list || []).join(", ")}]</span> ` : "");

// A finding whose text is gone was fixed by hand; it stays listed, struck through, until the next audit.
function jumpButton(r, cls, text, inner) {
  const attrs =
    isStale(r) && locate(r, text) < 0
      ? 'disabled title="No longer in the text"'
      : `data-wf-action="docAudit:reveal" data-text="${escAttr(text)}" title="Show in the document"`;
  return `<button type="button" class="${cls} doc-audit-jump" ${attrs}>${inner}</button>`;
}

function sectionItemsHtml(r, key, items) {
  if (key === "negated_narration") {
    return items.map((it) => jumpButton(r, "doc-audit-item", it.span, `${_ids(it.ids)}${esc(it.span)}`)).join("");
  }
  if (key === "banned_phrases") {
    return items
      .map((it) =>
        jumpButton(
          r,
          "doc-audit-item",
          it.sentence,
          `${_ids(it.ids)}<b>“${esc(it.phrase)}”</b><span class="doc-audit-snippet">${esc(it.sentence)}</span>`,
        ),
      )
      .join("");
  }
  if (key === "contrastive_negation") {
    return items
      .map((it) => jumpButton(r, "doc-audit-item", it.sentence, `${_ids(it.ids)}${esc(it.sentence)}`))
      .join("");
  }
  const label = { repetitive_openers: "opener", repetitive_templates: "template", phrase_repetition: "phrase" }[key];
  const snippets = (list) => (list || []).map((t) => jumpButton(r, "doc-audit-snippet", t, `• ${esc(t)}`)).join("");
  const sentences = (it) => (key === "phrase_repetition" ? [it.earlier, it.sentence].filter(Boolean) : it.sentences);
  return items
    .map(
      (it) =>
        `<div class="doc-audit-item">${_ids(it.ids)}<b>“${esc(it[label])}”</b> ×${it.count}${snippets(sentences(it))}</div>`,
    )
    .join("");
}

function renderDocAuditResults() {
  const el = $("doc-audit-results");
  if (!el) return;
  if (!docAuditEnabled()) {
    el.innerHTML = "";
    return;
  }
  const r = S.docAuditResults;
  if (!r) {
    el.innerHTML = '<div class="doc-audit-status">Generate to audit the new text.</div>';
    return;
  }

  const current = runIsCurrent(r);
  const busy = r.status === "auditing" || r.status === "patching";
  let html = `<div class="doc-audit-status${busy ? " busy" : ""}">${esc(statusText(r))}</div>`;
  const sections = r.report?.sections || {};
  for (const def of DOC_AUDIT_DEFS) {
    const items = sections[def.key];
    if (!items?.length) continue;
    html += `<div class="doc-audit-section">
      <div class="doc-audit-section-title">${esc(def.label)}</div>
      ${sectionItemsHtml(r, def.key, items)}
    </div>`;
  }

  const hasFindings = !r.skipped && r.report && r.report.total_issues > 0;
  html += `<div class="doc-audit-actions">
    <button class="btn btn-sm" id="doc-audit-again-btn" ${busy || !current ? "disabled" : ""}>Audit again</button>
    <button class="btn btn-sm" id="doc-audit-patch-btn" ${busy || !current || !hasFindings ? "disabled" : ""}>Apply patches</button>
  </div>`;
  if (!current && !busy) {
    html += '<div class="doc-audit-hint">The audited text was removed — generate again to audit new text.</div>';
  }

  el.innerHTML = html;
  $("doc-audit-again-btn")?.addEventListener("click", auditAgain);
  $("doc-audit-patch-btn")?.addEventListener("click", runPatch);
}

// The audited run follows edits, so hand fixes inside it are what Audit again and Apply patches read next.
function track(r, content) {
  if (r.gone || r.content === content) return;
  const range = trackRange({ start: r.runStart, end: r.runStart + r.draft.length }, r.content, content);
  r.content = content;
  if (!range) {
    r.gone = true;
    return;
  }
  r.runStart = range.start;
  r.draft = content.slice(range.start, range.end);
}

/** Track the run through one edit of the open document; tracking per edit keeps separate edits from merging. */
export function syncDocAudit(content) {
  const r = S.docAuditResults;
  if (r?.docId !== S.activeDocId || r.gone) return;
  const draft = r.draft;
  track(r, content);
  if (r.gone || r.draft !== draft) renderDocAuditResults();
}

// Also catches changes no edit reported (a generation with the auditor off), as one edit.
function runIsCurrent(r) {
  if (!r || !_ctx || S.activeDocId !== r.docId) return false;
  track(r, _ctx.getContent());
  return !r.gone;
}

function fullContext(r) {
  return _ctx.getContent().slice(0, r.runStart);
}

const isStale = (r) => Boolean(r.report) && r.auditedDraft !== r.draft;

/** Where *text* sits: inside the run first, else the nearest copy before it (an opener group's anchor can live there). */
function locate(r, text) {
  if (!text) return -1;
  const inRun = r.content.indexOf(text, r.runStart);
  if (inRun >= 0 && inRun + text.length <= r.runStart + r.draft.length) return inRun;
  return r.runStart >= text.length ? r.content.lastIndexOf(text, r.runStart - text.length) : -1;
}

function revealFinding(el) {
  const r = S.docAuditResults;
  if (!runIsCurrent(r)) return;
  const at = locate(r, el.dataset.text);
  if (at < 0) toast("No longer in the text");
  else _ctx.selectRange(at, at + el.dataset.text.length);
}

registerActions("docAudit", { reveal: revealFinding });

export function onGenerationEnd({ docId, runStart, draft, truncated, assisted }) {
  if (!docId || !draft || !docAuditEnabled()) return;
  const run = {
    docId,
    runStart,
    draft,
    content: _ctx.getContent(),
    gone: false,
    truncated,
    assisted,
    status: "auditing",
    report: null,
    auditedDraft: null,
    skipped: null,
    tailExcluded: false,
    patchedCount: null,
    errors: [],
  };
  S.docAuditResults = run;
  postAudit(run, { autopatch: true });
}

async function postAudit(run, { autopatch = false } = {}) {
  S.docAuditBusy = true;
  run.status = "auditing";
  renderDocAuditResults();
  const draft = run.draft;
  try {
    const res = await api.post(`/documents/${run.docId}/audit`, {
      draft,
      context: fullContext(run),
      assisted: run.assisted,
      truncated: run.truncated,
    });
    if (S.docAuditResults !== run) return; // superseded by a newer generation
    run.report = res.report;
    run.auditedDraft = draft;
    run.skipped = res.skipped;
    run.tailExcluded = res.tail_excluded;
    run.status = "done";
  } catch (e) {
    if (S.docAuditResults !== run) return;
    run.status = "error";
    toast(`Audit failed: ${e.message}`, true);
  } finally {
    if (S.docAuditResults === run) {
      S.docAuditBusy = false;
      renderDocAuditResults();
    }
  }
  if (
    autopatch &&
    S.settings?.document_audit_autopatch &&
    run.status === "done" &&
    !run.skipped &&
    run.report?.total_issues > 0
  ) {
    runPatch();
  }
}

async function auditAgain() {
  const r = S.docAuditResults;
  if (!r || S.docAuditBusy || !runIsCurrent(r)) {
    renderDocAuditResults();
    return;
  }
  r.patchedCount = null;
  await postAudit(r);
}

export async function runPatch() {
  const r = S.docAuditResults;
  if (!r || S.docAuditBusy || r.skipped || !r.report || r.report.total_issues === 0) return;
  if (!runIsCurrent(r)) {
    renderDocAuditResults(); // repaints with the buttons disabled + hint
    return;
  }
  S.docAuditBusy = true;
  r.status = "patching";
  renderDocAuditResults();
  const draft = r.draft;
  try {
    const res = await api.post(`/documents/${r.docId}/patch`, {
      draft,
      context: fullContext(r),
      assisted: r.assisted,
      truncated: r.truncated,
    });
    if (S.docAuditResults !== r) return;
    if (!runIsCurrent(r) || r.draft !== draft) {
      r.status = "done";
      if (S.activeDocId === r.docId) toast("The text changed while patching — patch discarded", true);
      return;
    }
    if (res.patch_count > 0 && res.patched_draft !== draft && _ctx.applyPatchedRun(r, res.patched_draft)) {
      r.draft = res.patched_draft;
      r.content = _ctx.getContent();
    }
    r.report = res.report_after;
    r.auditedDraft = res.patched_draft;
    r.patchedCount = res.patch_count;
    r.errors = res.errors || [];
    r.skipped = res.skipped;
    r.status = "done";
    if (r.errors.length) toast(`${r.errors.length} patch(es) did not apply`, true);
  } catch (e) {
    if (S.docAuditResults === r) r.status = "error";
    toast(`Patch failed: ${e.message}`, true);
  } finally {
    if (S.docAuditResults === r) {
      S.docAuditBusy = false;
      renderDocAuditResults();
    }
  }
}
