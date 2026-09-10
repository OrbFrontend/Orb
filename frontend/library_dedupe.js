// Character Library duplicate finder controller.

import { api } from "./api.js";
import { combinations, compareHtml, duplicateResultsHtml } from "./library_dedupe_view.js";
import { showSubConfirmModal } from "./modal.js";
import { sseEvents, streamPost } from "./sse.js";
import { esc, toast } from "./utils.js";

let _root = null;
let _callbacks = {};
let _controller = null;
let _report = null;
let _comparison = null;
let _characterCount = 0;
let _lastDismissed = null;

export function dedupeToolHtml() {
  return `
    <section class="lib-tool" data-tool="duplicates">
      <header class="lib-tool-head">
        <span class="lib-tool-icon" aria-hidden="true">◌</span>
        <div class="lib-tool-heading">
          <h3 class="lib-tool-name">Duplicate finder</h3>
          <p class="lib-manager-note">Find identical cards, lightly edited copies, and versions with the same avatar. Review the evidence before deleting anything.</p>
        </div>
      </header>
      <div class="lib-tool-body">
        <div class="lib-dupe-status" data-dupe-status></div>
        <div class="lib-manager-actions">
          <button class="btn btn-accent" data-dupe-action="scan"></button>
          <button class="btn" data-dupe-action="cancel" hidden>Cancel</button>
        </div>
        <div class="lib-manager-progress" data-dupe-progress hidden>
          <div class="lib-progress-track"><div class="lib-progress-fill" data-dupe-progress-fill></div></div>
          <div class="lib-progress-line" data-dupe-progress-line></div>
        </div>
        <div class="lib-dupe-results" data-dupe-results></div>
      </div>
    </section>`;
}

/** Mount a fresh Manager-panel instance. Closing the panel intentionally drops results. */
export function mountLibraryDedupe(root, callbacks = {}) {
  _root = root;
  _callbacks = callbacks;
  _controller = null;
  _report = null;
  _comparison = null;
  _characterCount = Number(callbacks.characterCount) || 0;
  _lastDismissed = null;
  root.addEventListener("click", onClick);
  paint();
}

/** Let the sibling auto-tag state supply the current library count once it loads. */
export function setDedupeCharacterCount(total) {
  _characterCount = Number(total) || 0;
  paint();
}

function onClick(event) {
  const button = event.target.closest("[data-dupe-action]");
  if (!button || !_root?.contains(button)) return;
  const action = button.dataset.dupeAction;
  if (action === "scan") startScan();
  else if (action === "cancel") _controller?.abort();
  else if (action === "compare") loadCompare(button.dataset.dupeA, button.dataset.dupeB);
  else if (action === "back-results") {
    _comparison = null;
    paint();
  } else if (action === "dismiss-pair") {
    confirmDismiss([[button.dataset.dupeA, button.dataset.dupeB]]);
  } else if (action === "dismiss-group") {
    confirmDismiss(combinations((button.dataset.dupeCards || "").split(",").filter(Boolean)));
  } else if (action === "undo-dismiss") {
    undoDismissal();
  } else if (action === "resolve") {
    confirmResolve(button.dataset.dupeKeep, button.dataset.dupeRemove);
  }
}

function resultCount() {
  if (!_report) return 0;
  return (_report.groups || []).length + (_report.pairs || []).length;
}

function paint() {
  if (!_root) return;
  const running = !!_controller;
  const status = _root.querySelector("[data-dupe-status]");
  if (status) {
    status.textContent = running
      ? "Scanning character content and checking cached avatars…"
      : !_report
        ? _characterCount
          ? "Scan the library when you are ready; nothing runs automatically."
          : "No characters to scan yet."
        : resultCount()
          ? `${(_report.groups || []).length} strong group${(_report.groups || []).length === 1 ? "" : "s"} and ${(_report.pairs || []).length} possible pair${(_report.pairs || []).length === 1 ? "" : "s"}.`
          : "No duplicates found.";
  }
  const scan = _root.querySelector('[data-dupe-action="scan"]');
  if (scan) {
    scan.textContent = running
      ? "Scanning…"
      : _characterCount
        ? `Scan ${_characterCount} character${_characterCount === 1 ? "" : "s"}`
        : "Scan library";
    scan.disabled = running || !_characterCount;
  }
  const cancel = _root.querySelector('[data-dupe-action="cancel"]');
  if (cancel) cancel.hidden = !running;
  const results = _root.querySelector("[data-dupe-results]");
  if (results) {
    const undo = _lastDismissed
      ? `<div class="lib-dupe-undo">Intentional variant hidden. <button class="btn btn-sm" data-dupe-action="undo-dismiss">Undo dismissal</button></div>`
      : "";
    results.innerHTML = _comparison
      ? compareHtml(_comparison)
      : `${undo}${_report ? duplicateResultsHtml(_report) : ""}`;
  }
}

function showProgress(done, total, name) {
  const progress = _root?.querySelector("[data-dupe-progress]");
  if (progress) progress.hidden = false;
  const fill = _root?.querySelector("[data-dupe-progress-fill]");
  if (fill) fill.style.width = total ? `${Math.round((100 * done) / total)}%` : "0%";
  const line = _root?.querySelector("[data-dupe-progress-line]");
  if (line) line.innerHTML = `${done} / ${total}${name ? ` &nbsp; ${esc(name)}` : ""}`;
}

function hideProgress() {
  const progress = _root?.querySelector("[data-dupe-progress]");
  if (progress) progress.hidden = true;
}

async function startScan() {
  if (_controller) return;
  _controller = new AbortController();
  _comparison = null;
  showProgress(0, 0, "");
  paint();
  try {
    const response = await streamPost("/library/duplicates/scan", {}, _controller.signal);
    if (!response.ok) throw new Error(`scan returned ${response.status}`);
    for await (const event of sseEvents(response.body, { signal: _controller.signal })) {
      let data = event.data;
      try {
        data = event.data ? JSON.parse(event.data) : {};
      } catch {}
      if (event.event === "start") {
        showProgress(0, Number(data?.total) || 0, "");
      } else if (event.event === "progress") {
        showProgress(Number(data?.done) || 0, Number(data?.total) || 0, data?.name || "");
      } else if (event.event === "done") {
        _report = data;
      } else if (event.event === "error") {
        toast(typeof data === "string" ? data : data?.message || "Duplicate scan failed", true);
      }
    }
  } catch (error) {
    if (error?.name !== "AbortError") toast(`Duplicate scan failed: ${error.message}`, true);
  } finally {
    _controller = null;
    hideProgress();
    paint();
  }
}

async function loadCompare(a, b) {
  if (!a || !b) return;
  try {
    _comparison = await api.get(`/library/duplicates/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    paint();
  } catch (error) {
    toast(`Could not load the comparison: ${error.message}`, true);
  }
}

function confirmDismiss(pairs) {
  if (!pairs.length) return;
  const count = pairs.length;
  showSubConfirmModal(
    {
      title: count === 1 ? "Keep these two versions?" : `Keep this ${count}-pair group?`,
      message:
        "This hides the selected duplicate evidence until either card's meaningful content changes. Tags and generated public profiles do not make it reappear.",
      confirmText: "Keep both",
    },
    () => dismissPairs(pairs),
  );
}

async function dismissPairs(pairs) {
  try {
    await api.post("/library/duplicates/dismiss", { pairs });
    _lastDismissed = pairs;
    await startScan();
  } catch (error) {
    toast(`Could not dismiss the match: ${error.message}`, true);
  }
}

async function undoDismissal() {
  if (!_lastDismissed) return;
  try {
    await api.del("/library/duplicates/dismiss", { pairs: _lastDismissed });
    _lastDismissed = null;
    await startScan();
  } catch (error) {
    toast(`Could not restore the match: ${error.message}`, true);
  }
}

function confirmResolve(keepId, removeId) {
  const remove = [keepId, removeId].find((id) => id !== keepId) || removeId;
  const compared = _comparison?.a?.card?.id === remove ? _comparison.a : _comparison?.b;
  const activity = Number(compared?.activity?.total) || 0;
  const name = compared?.card?.name || "this copy";
  const relink = activity > 0;
  showSubConfirmModal(
    {
      title: `Remove ${name}?`,
      message: relink
        ? `${activity} conversation${activity === 1 ? " is" : "s are"} linked to this copy. They will be relinked to the keeper before deletion.`
        : "This removes the redundant card. It has no conversations to move.",
      confirmText: relink ? "Relink and remove" : "Remove copy",
    },
    () => resolve(keepId, removeId, relink),
  );
}

async function resolve(keepId, removeId, relink) {
  try {
    await api.post("/library/duplicates/resolve", { keep_id: keepId, remove_id: removeId, relink });
    _comparison = null;
    await _callbacks.onRunComplete?.();
    await startScan();
    toast("Duplicate removed");
  } catch (error) {
    toast(`Could not remove the duplicate: ${error.message}`, true);
  }
}
