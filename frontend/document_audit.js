// Document-mode Output Auditor — the doc-tailored Workflow pane (config +
// findings) and the /audit + /patch API calls. Panel UI and HTTP only: this
// module never touches #doc-page. All editor-DOM mutation stays in document.js,
// which injects its callbacks at init (initDocAudit(ctx), mirroring
// initDocProbs) so no import cycle forms.
//
// Results are session-only state (like the probs run store): S.docAuditResults
// holds the latest generated run {docId, runStart, draft, …} plus its report;
// a new generation replaces it. Patching revalidates that the run text still
// tiles the document before splicing — an edited run disables the buttons.

import { api } from "./api.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { AUDIT_TYPE_DEFS, showPhraseBankModal } from "./settings.js";
import { S } from "./state.js";
import { $, esc, escAttr, toast } from "./utils.js";

// Doc-applicable scanner subset — keys mirror backend DOC_AUDIT_TYPES. The
// three chat-context scanners (anti_echo, phrase_repetition,
// structural_repetition) are hidden in doc mode; the server never runs them.
const DOC_AUDIT_KEYS = ["banned_phrases", "repetitive_openers", "repetitive_templates", "contrastive_negation"];
const DOC_AUDIT_DEFS = AUDIT_TYPE_DEFS.filter((d) => DOC_AUDIT_KEYS.includes(d.key));

let _ctx = null; // { getContent, applyPatchedRun } injected by document.js

// Wire the header button once (from initDocumentMode). *ctx* = { getContent,
// applyPatchedRun }: getContent() returns the current serialized document
// string; applyPatchedRun(runStart, oldText, newText) splices the patched run
// into the editor and returns whether it applied.
export function initDocAudit(ctx) {
  _ctx = ctx;
  $("doc-workflow-btn")?.addEventListener("click", toggleDocWorkflowPanel);
}

function docAuditEnabled() {
  return Boolean(S.settings?.document_audit_enabled);
}

async function persistDocAuditSettings(payload) {
  try {
    S.settings = await api.put("/settings", payload);
  } catch (_e) {
    toast("Failed to save setting", true);
  }
}

export function toggleDocWorkflowPanel() {
  if (isUtilityPanelOpen("tools-panel")) {
    // Clear both possible opener buttons: the panel may have been opened from
    // the chat header before a mode switch, leaving that button active.
    closeUtilityPanel("tools-panel", "tools-panel-btn");
    closeUtilityPanel("tools-panel", "doc-workflow-btn");
  } else {
    openUtilityPanel("tools-panel", "doc-workflow-btn", renderDocAuditPane);
  }
}

// ── Pane render ───────────────────────────────────────────────────────────────

export function renderDocAuditPane() {
  const pane = $("tools-pane-doc");
  if (!pane) return;
  const on = docAuditEnabled();
  const toggles = S.settings?.document_audit_toggles || {};
  const autopatch = Boolean(S.settings?.document_audit_autopatch);

  const checks = DOC_AUDIT_DEFS.map(
    (a) => `<label class="lg-enforce-label" title="${escAttr(a.title)}">
             <input type="checkbox" data-doc-audit-key="${a.key}" ${toggles[a.key] !== false ? "checked" : ""}>
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

  // Wired here, not inline — the layer-check ratchet forbids new on*= handlers.
  $("doc-audit-enable-chk").addEventListener("change", async (e) => {
    await persistDocAuditSettings({ document_audit_enabled: e.target.checked });
    renderDocAuditPane();
  });
  $("doc-audit-autopatch-chk")?.addEventListener("change", (e) =>
    persistDocAuditSettings({ document_audit_autopatch: e.target.checked }),
  );
  for (const input of pane.querySelectorAll("[data-doc-audit-key]")) {
    input.addEventListener("change", (e) => {
      const cur = S.settings?.document_audit_toggles || {};
      persistDocAuditSettings({ document_audit_toggles: { ...cur, [e.target.dataset.docAuditKey]: e.target.checked } });
    });
  }
  $("doc-audit-phrasebank-btn").addEventListener("click", showPhraseBankModal);

  renderDocAuditResults();
}

// ── Results render ────────────────────────────────────────────────────────────

function statusText(r) {
  if (r.status === "auditing") return "Auditing…";
  if (r.status === "patching") return "Patching…";
  if (r.status === "error") return "Failed — try Audit again";
  const parts = [];
  if (r.patchedCount != null) parts.push(`Patched ${r.patchedCount}`);
  if (r.skipped === "no_complete_sentence") parts.push("No complete sentence to audit");
  else if (r.report) {
    parts.push(
      r.report.total_issues === 0 ? "Clean" : `${r.report.total_issues} issue${r.report.total_issues === 1 ? "" : "s"}`,
    );
  }
  if (r.tailExcluded) parts.push("truncated tail excluded");
  return parts.join(" · ");
}

const _snippets = (list) => (list || []).map((s) => `<span class="doc-audit-snippet">• ${esc(s)}</span>`).join("");

function sectionItemsHtml(key, items) {
  if (key === "banned_phrases") {
    return items
      .map(
        (it) =>
          `<div class="doc-audit-item"><b>“${esc(it.phrase)}”</b><span class="doc-audit-snippet">${esc(it.sentence)}</span></div>`,
      )
      .join("");
  }
  if (key === "repetitive_openers" || key === "repetitive_templates") {
    const label = key === "repetitive_openers" ? "opener" : "template";
    return items
      .map(
        (it) => `<div class="doc-audit-item"><b>“${esc(it[label])}”</b> ×${it.count}${_snippets(it.sentences)}</div>`,
      )
      .join("");
  }
  // contrastive_negation
  return items.map((it) => `<div class="doc-audit-item">${esc(it.sentence)}</div>`).join("");
}

export function renderDocAuditResults() {
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

  const busy = r.status === "auditing" || r.status === "patching";
  let html = `<div class="doc-audit-status${busy ? " busy" : ""}">${esc(statusText(r))}</div>`;

  const sections = r.report?.sections || {};
  for (const def of DOC_AUDIT_DEFS) {
    const items = sections[def.key];
    if (!items?.length) continue;
    html += `<div class="doc-audit-section">
      <div class="doc-audit-section-title">${esc(def.label)}</div>
      ${sectionItemsHtml(def.key, items)}
    </div>`;
  }

  const current = runIsCurrent(r);
  const hasFindings = !r.skipped && r.report && r.report.total_issues > 0;
  html += `<div class="doc-audit-actions">
    <button class="btn btn-sm" id="doc-audit-again-btn" ${busy || !current ? "disabled" : ""}>Audit again</button>
    <button class="btn btn-sm" id="doc-audit-patch-btn" ${busy || !current || !hasFindings ? "disabled" : ""}>Apply patches</button>
  </div>`;
  if (!current && !busy) {
    html +=
      '<div class="doc-audit-hint">The generated run was edited — results are read-only until the next generation.</div>';
  }

  el.innerHTML = html;
  $("doc-audit-again-btn")?.addEventListener("click", auditAgain);
  $("doc-audit-patch-btn")?.addEventListener("click", runPatch);
}

// ── Run tracking + API flows ─────────────────────────────────────────────────

// The stored run is trustworthy only while its text still tiles the document
// at the recorded offset (mirrors the probs store's validate-on-use).
function runIsCurrent(r) {
  if (!r || !_ctx || S.activeDocId !== r.docId) return false;
  const content = _ctx.getContent();
  return content.slice(r.runStart, r.runStart + r.draft.length) === r.draft;
}

// The FULL document text before the run — the exact generation prompt. The
// patch call byte-extends it to reuse the server's KV cache, so no client-side
// windowing here; the server owns the (cheap, CPU-side) scanner cap.
function fullContext(r) {
  return _ctx.getContent().slice(0, r.runStart);
}

// Called by document.js after finalizeGeneration commits a run. Skips are the
// caller's non-cases: stream error (never called), empty commit, audit off.
export function onGenerationEnd({ docId, runStart, draft, truncated, assisted }) {
  if (!docId || !draft || !docAuditEnabled()) return;
  const run = {
    docId,
    runStart,
    draft,
    truncated,
    assisted,
    status: "auditing",
    report: null,
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
  try {
    const res = await api.post(`/documents/${run.docId}/audit`, {
      draft: run.draft,
      context: fullContext(run),
      assisted: run.assisted,
      truncated: run.truncated,
    });
    if (S.docAuditResults !== run) return; // superseded by a newer generation
    run.report = res.report;
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
  try {
    const res = await api.post(`/documents/${r.docId}/patch`, {
      draft: r.draft,
      context: fullContext(r),
      assisted: r.assisted,
      truncated: r.truncated,
    });
    if (S.docAuditResults !== r) return;
    if (res.patch_count > 0 && res.patched_draft !== r.draft) {
      // applyPatchedRun revalidates against the live editor before splicing;
      // tracking the new text keeps the run current for further actions.
      if (_ctx.applyPatchedRun(r.runStart, r.draft, res.patched_draft)) r.draft = res.patched_draft;
    }
    r.report = res.report_after;
    r.patchedCount = res.patch_count;
    r.errors = res.errors || [];
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
