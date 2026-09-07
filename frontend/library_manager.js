// The Character Library's Manager tab: library-wide maintenance tools.
//
// Today it holds one, the auto-tagger. The panel is a stack of tool cards under
// a "Library tools" rule, so the next tool (bulk delete, dedupe, avatar
// backfill) lands as a sibling card rather than as another modal — and so one
// card already reads as one of several.
//
// It imports no chat module and not the browser it is mounted into: the browser
// hands it a container and a callback, so there is no cycle.

import { api } from "./api.js";
import { createChipInput } from "./chips.js";
import { sseEvents, streamPost } from "./sse.js";
import { $, esc, toast } from "./utils.js";

const MAX_VOCABULARY = 64; // mirrors features/library_tags/vocabulary.py

let _vocabulary = []; // the saved vocabulary, as the server last told us
let _draft = []; // what the chip editor currently holds
let _total = 0;
let _pending = 0;
let _controller = null; // the in-flight run's AbortController, if any
let _callbacks = {};

/** Mount the Manager panel into *container*.
 *
 * *onRunComplete* fires once a run terminates, so the card cache the run just
 * rewrote gets reloaded. The vocabulary needs no callback of its own: it is the
 * tagger's input, and the browser's chip row is counted off the cards a run
 * writes, so saving one changes nothing outside this panel until a run happens.
 */
export function renderLibraryManager(container, callbacks = {}) {
  _callbacks = callbacks;
  container.innerHTML = `
    <div class="lib-manager">
      <div class="lib-manager-eyebrow">Library tools</div>

      <section class="lib-tool" data-tool="auto-tag">
        <header class="lib-tool-head">
          <span class="lib-tool-icon" aria-hidden="true">🏷</span>
          <div class="lib-tool-heading">
            <h3 class="lib-tool-name">Auto-tagging</h3>
            <p class="lib-manager-note">
              The Agent model reads every character and replaces its tags with the
              ones that fit. Tags a card was imported with are overwritten.
            </p>
          </div>
        </header>

        <div class="lib-tool-body">
          <div class="lib-manager-field">
            <label for="lib-vocab-input">Tag vocabulary <span id="lib-vocab-count" class="lib-manager-count"></span></label>
            <div class="lb-chip-wrap lib-vocab-chips" id="lib-vocab-chips"></div>
            <div class="lib-manager-actions">
              <button class="btn btn-accent btn-sm" data-action="save-vocab">Save vocabulary</button>
            </div>
          </div>

          <div class="lib-manager-run">
            <div class="lib-manager-status" id="lib-run-status"></div>
            <label class="lib-manager-toggle">
              <input type="checkbox" id="lib-run-reasoning">
              <span class="lib-manager-toggle-text">
                <span class="lib-manager-toggle-label">Enable tagger thinking</span>
                <span class="lib-manager-note">Slower and more expensive, but more nuanced results.</span>
              </span>
            </label>
            <div class="lib-manager-actions">
              <button class="btn btn-accent" data-action="run"></button>
              <button class="btn" data-action="cancel" hidden>Cancel</button>
            </div>
            <div class="lib-manager-progress" id="lib-run-progress" hidden>
              <div class="lib-progress-track"><div class="lib-progress-fill" id="lib-progress-fill"></div></div>
              <div class="lib-progress-line" id="lib-progress-line"></div>
            </div>
          </div>
        </div>
      </section>
    </div>`;

  container.addEventListener("click", onPanelClick);
  chipInput().render();
  refresh();
}

function onPanelClick(e) {
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "save-vocab") saveVocabulary();
  else if (action === "run") startRun();
  else if (action === "cancel") _controller?.abort();
}

function chipInput() {
  return createChipInput({
    wrapId: "lib-vocab-chips",
    inputId: "lib-vocab-input",
    placeholder: "Add a tag…",
    getItems: () => _draft,
    setItems: (items) => {
      // The server caps it too (and owns the canonical rules); stopping here as
      // well is what keeps the chip row from growing past what the panel shows.
      _draft = items.slice(0, MAX_VOCABULARY);
    },
    onChange: paint,
  });
}

async function refresh() {
  let state;
  try {
    state = await api.get("/library/tags");
  } catch (e) {
    toast(`Failed to load library tags: ${e.message}`, true);
    return;
  }
  adopt(state);
  _draft = [..._vocabulary];
  chipInput().render();
  paint();
}

function adopt(state) {
  _vocabulary = Array.isArray(state?.vocabulary) ? state.vocabulary : [];
  _total = Number(state?.total) || 0;
  _pending = Number(state?.pending) || 0;
}

/** Repaint the counts and the run button. The button's label *is* the
 * idempotency contract made visible: "Everything is up to date" is how the user
 * learns that pressing again costs nothing. */
function paint() {
  const running = !!_controller;
  const count = $("lib-vocab-count");
  if (count) count.textContent = `${_draft.length} / ${MAX_VOCABULARY}`;

  const unsaved = _draft.join("\n") !== _vocabulary.join("\n");
  const status = $("lib-run-status");
  if (status) {
    status.textContent = !_vocabulary.length
      ? "Add a few tags and save them to get started."
      : `${_total} character${_total === 1 ? "" : "s"} · ${_pending ? `${_pending} need tagging` : "all tagged"}`;
  }

  const runBtn = document.querySelector('[data-action="run"]');
  if (runBtn) {
    runBtn.textContent = running
      ? "Tagging…"
      : unsaved
        ? "Save the vocabulary first"
        : _pending
          ? `Tag ${_pending} character${_pending === 1 ? "" : "s"}`
          : "Everything is up to date";
    runBtn.disabled = running || unsaved || !_pending;
  }
  const cancelBtn = document.querySelector('[data-action="cancel"]');
  if (cancelBtn) cancelBtn.hidden = !running;
  const saveBtn = document.querySelector('[data-action="save-vocab"]');
  if (saveBtn) saveBtn.disabled = running;
  const reasoningBox = $("lib-run-reasoning");
  if (reasoningBox) reasoningBox.disabled = running;
}

async function saveVocabulary() {
  try {
    // The response is the authoritative normalized form — trimmed, deduped,
    // capped — so the chips are rebuilt from it rather than from what was typed.
    adopt(await api.put("/library/tags", { vocabulary: _draft }));
  } catch (e) {
    toast(`Failed to save vocabulary: ${e.message}`, true);
    return;
  }
  _draft = [..._vocabulary];
  chipInput().render();
  paint();
  toast(_pending ? `Saved — ${_pending} characters need tagging` : "Saved — nothing to re-tag");
}

async function startRun() {
  if (_controller) return;
  _controller = new AbortController();
  const total = _pending;
  // Read once, at the press: the run pins one value for every card, which is
  // what keeps its shared prefix shared.
  const reasoning = !!$("lib-run-reasoning")?.checked;
  showProgress(0, total, "");
  paint();

  let failed = 0;
  try {
    const response = await streamPost("/library/auto-tag/run", { reasoning }, _controller.signal);
    if (!response.ok) throw new Error(`run returned ${response.status}`);
    for await (const event of sseEvents(response.body, { signal: _controller.signal })) {
      let data = {};
      try {
        data = event.data ? JSON.parse(event.data) : {};
      } catch {
        data = event.data || {};
      }
      if (event.event === "progress") {
        showProgress(data.done, data.total, `${data.name} → ${data.tags?.length ? data.tags.join(", ") : "no tags"}`);
      } else if (event.event === "card_error") {
        failed += 1;
        showProgress(data.done, data.total, `${data.name} → failed`);
      } else if (event.event === "error") {
        // A terminal error: a run already in progress, an empty vocabulary, or
        // the circuit breaker tripping on a dead endpoint.
        toast(typeof data === "string" ? data : data.message || "Tagging failed", true);
      }
    }
  } catch (e) {
    if (e?.name !== "AbortError") toast(`Tagging failed: ${e.message}`, true);
  } finally {
    _controller = null;
    hideProgress();
    // Every completed card is already committed, so this is true of a cancelled
    // run as much as a finished one: re-read the counts and repaint the library.
    await refresh();
    await _callbacks.onRunComplete?.();
    if (failed) toast(`${failed} character${failed === 1 ? "" : "s"} could not be tagged; press again to retry`, true);
  }
}

function showProgress(done, total, line) {
  const wrap = $("lib-run-progress");
  if (wrap) wrap.hidden = false;
  const fill = $("lib-progress-fill");
  if (fill) fill.style.width = total ? `${Math.round((100 * (done || 0)) / total)}%` : "0%";
  const text = $("lib-progress-line");
  if (text) text.innerHTML = `${done || 0} / ${total || 0}${line ? ` &nbsp; ${esc(line)}` : ""}`;
}

function hideProgress() {
  const wrap = $("lib-run-progress");
  if (wrap) wrap.hidden = true;
}
