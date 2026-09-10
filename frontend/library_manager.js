// Character Library manager tools.

import { api } from "./api.js";
import { createChipInput } from "./chips.js";
import { TAG_ICON } from "./icons.js";
import { dedupeToolHtml, mountLibraryDedupe, setDedupeCharacterCount } from "./library_dedupe.js";
import { showSubConfirmModal } from "./modal.js";
import { sseEvents, streamPost } from "./sse.js";
import { $, esc, toast } from "./utils.js";

const MAX_VOCABULARY = 64;
const MAX_TAG_LENGTH = 40;

let _vocabulary = [];
let _draft = [];
let _total = 0;
let _pending = 0;
let _tagged = 0;
let _controller = null;
let _saving = false;
let _revision = "";
let _callbacks = {};

/** Mount the Manager panel into a container. */
export function renderLibraryManager(container, callbacks = {}) {
  _callbacks = callbacks;
  container.innerHTML = `
    <div class="lib-manager">
      <div class="lib-manager-eyebrow">Library tools</div>

      <section class="lib-tool" data-tool="auto-tag">
        <header class="lib-tool-head">
          <span class="lib-tool-icon">${TAG_ICON}</span>
          <div class="lib-tool-heading">
            <h3 class="lib-tool-name">Auto-tagging</h3>
            <p class="lib-manager-note">
              The Agent model reads every character and replaces its tags with the
              appropriate ones in the vocabulary. Tags that came with the characters will be overwritten.
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
      ${dedupeToolHtml()}
    </div>`;

  container.addEventListener("click", onPanelClick);
  mountLibraryDedupe(container.querySelector('[data-tool="duplicates"]'), callbacks);
  chipInput().render();
  refresh();
}

function onPanelClick(e) {
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "save-vocab") saveVocabulary();
  else if (action === "run") confirmRun();
  else if (action === "cancel") _controller?.abort();
}

function chipInput() {
  return createChipInput({
    wrapId: "lib-vocab-chips",
    inputId: "lib-vocab-input",
    placeholder: "Add a tag…",
    getItems: () => _draft,
    setItems: (items) => {
      _draft = items.slice(0, MAX_VOCABULARY);
    },
    onChange: paint,
    isDisabled: () => !!_controller || _saving,
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
  _tagged = Number(state?.tagged) || 0;
  _revision = typeof state?.revision === "string" ? state.revision : "";
  setDedupeCharacterCount(_total);
}

/** Fold a draft tag for comparison with the server's normalized vocabulary. */
function fold(tag) {
  return String(tag ?? "")
    .replace(/\|/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_TAG_LENGTH)
    .trim()
    .toLowerCase();
}

function removedTags() {
  const kept = new Set(_draft.map(fold));
  return _vocabulary.filter((tag) => !kept.has(fold(tag)));
}

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
      : _saving
        ? "Saving vocabulary…"
        : unsaved
          ? "Save the vocabulary first"
          : _pending
            ? `Tag ${_pending} character${_pending === 1 ? "" : "s"}`
            : _total
              ? `Retag all ${_total} character${_total === 1 ? "" : "s"}`
              : "No characters to tag";
    runBtn.disabled = running || _saving || unsaved || !_vocabulary.length || !_total;
  }
  const cancelBtn = document.querySelector('[data-action="cancel"]');
  if (cancelBtn) cancelBtn.hidden = !running;
  const saveBtn = document.querySelector('[data-action="save-vocab"]');
  if (saveBtn) saveBtn.disabled = running || _saving || !unsaved;
  const reasoningBox = $("lib-run-reasoning");
  if (reasoningBox) reasoningBox.disabled = running;
}

/** Save the vocabulary, confirming deletions that affect tagged cards. */
function saveVocabulary() {
  const removed = removedTags();
  if (!removed.length || !_tagged) return commitVocabulary();
  const n = removed.length;
  const names = removed.map((tag) => `“${esc(tag)}”`).join(", ");
  const it = n === 1 ? "it" : "them";
  showSubConfirmModal(
    {
      title: n === 1 ? "Delete this tag?" : `Delete these ${n} tags?`,
      message:
        `${names} ${n === 1 ? "is" : "are"} removed from every tagged character carrying ${it}, ` +
        `across ${_tagged} card${_tagged === 1 ? "" : "s"} a run has written. Adding ${it} back later ` +
        `does not bring the assignments back — it makes the whole library pending and tags it again ` +
        `from scratch.`,
      confirmText: n === 1 ? "Delete tag" : `Delete ${n} tags`,
    },
    commitVocabulary,
  );
}

async function commitVocabulary() {
  if (_saving) return;
  const submitted = [..._draft];
  _saving = true;
  chipInput().render();
  paint();
  let state;
  try {
    state = await api.put("/library/tags", { vocabulary: submitted, base_revision: _revision });
  } catch (e) {
    if (e?.status === 409 && e.message.includes("vocabulary changed")) {
      const localDraft = [..._draft];
      await refresh();
      _draft = localDraft;
      toast("The vocabulary changed in another window. Your draft was kept; review it and save again.", true);
    } else {
      toast(`Failed to save vocabulary: ${e.message}`, true);
    }
    _saving = false;
    chipInput().render();
    paint();
    return;
  }
  const editedWhileSaving = _draft.join("\n") !== submitted.join("\n");
  const cardsChanged = Number(state?.cards_changed) > 0;
  adopt(state);
  if (!editedWhileSaving) _draft = [..._vocabulary];
  _saving = false;
  chipInput().render();
  paint();
  if (cardsChanged) await _callbacks.onRunComplete?.();
  toast(_pending ? `Saved — ${_pending} characters need tagging` : "Saved — nothing to re-tag");
}

/** Confirm the destructive library-wide rewrite. */
function confirmRun() {
  if (_controller) return;
  const force = !_pending;
  const n = force ? _total : _pending;
  const verb = force ? "Retag" : "Tag";
  showSubConfirmModal(
    {
      title: `${verb} ${n} character${n === 1 ? "" : "s"}?`,
      message: `The tags ${n === 1 ? "this card" : "these cards"} already carry will be replaced by your vocabulary. This cannot be undone, and exported cards will carry the new tags.`,
      confirmText: `${verb} ${n} character${n === 1 ? "" : "s"}`,
    },
    () => startRun(force),
  );
}

async function startRun(force = false) {
  if (_controller) return;
  _controller = new AbortController();
  const total = force ? _total : _pending;
  const reasoning = !!$("lib-run-reasoning")?.checked;
  showProgress(0, total, "");
  paint();

  let failed = 0;
  try {
    const response = await streamPost("/library/auto-tag/run", { reasoning, force }, _controller.signal);
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
        toast(typeof data === "string" ? data : data.message || "Tagging failed", true);
      }
    }
  } catch (e) {
    if (e?.name !== "AbortError") toast(`Tagging failed: ${e.message}`, true);
  } finally {
    _controller = null;
    hideProgress();
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
