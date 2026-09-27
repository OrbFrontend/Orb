import { api } from "./api.js";
import { CLOSE_ICON, DOWNLOAD_ICON } from "./icons.js";
import { closeSubModal, showModal, showSubConfirmModal, showSubModal } from "./modal.js";
import { $, downloadBlob, esc, escAttr, escHandlerArg, toast } from "./utils.js";

const DOMAINS = [
  { id: "characters", label: "Characters" },
  { id: "chats", label: "Chats", requires: "characters", note: "needs Characters" },
  { id: "lorebooks", label: "Lorebooks" },
  { id: "fragments", label: "Fragments" },
  { id: "phrase_bank", label: "Phrase bank" },
  { id: "documents", label: "Documents" },
  { id: "configs", label: "Settings & endpoints" },
];

let libraryByName = {};

// The operation in flight, or the last one that failed, shown in the strip
// under the header. One runs at a time: every action waits for it to settle.
const IDLE = { text: "", busy: false, error: false, row: null };
let status = IDLE;

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function showPresetsModal() {
  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Backup &amp; Presets</h2>
        <p class="modal-subtitle">Snapshot your data, import a preset (merged into your data), or restore a full backup.</p>
      </div>
      <div id="preset-top-actions" class="modal-title-actions">
        <button class="btn btn-sm" onclick="showSnapshotModal()">📸 Snapshot current</button>
        <button class="btn btn-sm" onclick="triggerPresetImport()">⬆ Import file…</button>
        <input type="file" id="preset-import-input" accept=".db" style="display:none" onchange="handlePresetImportFile(this)">
      </div>
    </div>
    <div id="preset-status" class="preset-status hidden" role="status" aria-live="polite">
      <div class="gen-bar"></div>
      <span class="gen-dot"></span>
      <span class="preset-status-text"></span>
    </div>
    <div id="preset-library-list" class="phrase-bank-list">Loading…</div>
  `);
  if (!status.busy) status = IDLE;
  renderStatus();
  refreshPresetLibrary();
}

function setStatus(next = {}) {
  status = { ...IDLE, ...next };
  // Closed mid-operation: the outcome still has to reach someone.
  if (!renderStatus() && status.text && !status.busy) toast(status.text, status.error);
}

/** Paint `status` into the open modal. Returns false when the modal is closed. */
function renderStatus() {
  const el = $("preset-status");
  if (!el) return false;
  el.classList.toggle("hidden", !status.text);
  el.classList.toggle("busy", status.busy);
  el.classList.toggle("error", status.error);
  el.querySelector(".preset-status-text").textContent = status.text;
  const modal = el.closest(".modal");
  for (const btn of modal.querySelectorAll("#preset-top-actions .btn, .preset-item-actions .btn")) {
    btn.disabled = status.busy;
  }
  for (const row of modal.querySelectorAll(".preset-item")) {
    row.classList.toggle("busy", status.busy && row.dataset.name === status.row);
  }
  return true;
}

/** Run one library operation with the modal showing `text` until `work` settles. */
async function runPresetOp(text, row, failLabel, work) {
  if (status.busy) return;
  setStatus({ text, busy: true, row });
  try {
    await work();
  } catch (e) {
    setStatus({ text: `${failLabel}: ${e.message}`, error: true });
  }
}

function presetTitle(name) {
  return libraryByName[name]?.label || name;
}

export function showSnapshotModal() {
  const rows = DOMAINS.map(
    (d) => `
    <label class="modal-checkbox-label">
      <input type="checkbox" id="exp-${d.id}" data-domain="${d.id}" ${d.requires ? `data-requires="${d.requires}"` : ""}
             ${d.id === "configs" ? "" : "checked"} onchange="onPresetDomainChange(this)">
      ${esc(d.label)}${d.note ? ` <span class="preset-hint">(${esc(d.note)})</span>` : ""}
    </label>`,
  ).join("");

  showSubModal(`
    <div class="snapshot-modal">
    <h2>Snapshot current</h2>
    <p class="modal-subtitle">Pick what to include. Everything checked makes a full backup you can restore from.</p>
    <div class="field">
      <label>Include</label>
      ${rows}
    </div>
    <div id="preset-key-warning" class="preset-warning hidden">
      ⚠️ This snapshot includes your endpoints. API keys are sensitive.
      <label class="modal-checkbox-label">
        <input type="checkbox" id="exp-strip-keys" checked> Strip API keys (recommended for sharing)
      </label>
    </div>
    <div class="field">
      <label for="exp-label">Label (optional)</label>
      <input type="text" id="exp-label" placeholder="e.g. my-cast" maxlength="60">
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeSubModal()">Cancel</button>
      <button class="btn btn-accent" onclick="doCreateSnapshot()">Create</button>
    </div>
    </div>
  `);
}

export function onPresetDomainChange(cb) {
  const domain = cb.dataset.domain;
  if (cb.dataset.requires && cb.checked) {
    const req = $(`exp-${cb.dataset.requires}`);
    if (req) req.checked = true;
  }
  if (!cb.checked) {
    const dependent = DOMAINS.find((d) => d.requires === domain);
    if (dependent && $(`exp-${dependent.id}`)?.checked) {
      cb.checked = true;
      toast(`${DOMAINS.find((d) => d.id === domain).label} is required by ${dependent.label}`, true);
    }
  }
  if (domain === "configs") {
    $("preset-key-warning").classList.toggle("hidden", !cb.checked);
  }
}

function selectedDomains() {
  return DOMAINS.filter((d) => $(`exp-${d.id}`)?.checked).map((d) => d.id);
}

export async function doCreateSnapshot() {
  const domains = selectedDomains();
  if (!domains.length) {
    toast("Select at least one thing to save", true);
    return;
  }
  const strip = !domains.includes("configs") || $("exp-strip-keys")?.checked;
  const label = $("exp-label")?.value.trim() || "";
  closeSubModal();
  await runPresetOp("Creating snapshot…", null, "Snapshot failed", async () => {
    await api.post("/presets/export", { domains, strip_keys: strip, label });
    setStatus();
    if (!$("preset-status")) toast("Snapshot saved");
    await refreshPresetLibrary();
  });
}

export function triggerPresetImport() {
  $("preset-import-input").click();
}

export async function handlePresetImportFile(inp) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = "";
  await runPresetOp(`Importing “${f.name}”…`, null, "Import failed", async () => {
    await api.upload("/presets/import", f);
    setStatus();
    if (!$("preset-status")) toast("Added to library");
    await refreshPresetLibrary();
  });
}

export function downloadPreset(name) {
  downloadBlob(name, `/api/presets/${encodeURIComponent(name)}/download`);
}

export function applyPreset(name) {
  showSubConfirmModal(
    {
      title: "Apply preset",
      message: `Merge "${esc(name)}" into your current data? Matching items are overwritten, new ones added. An automatic backup is taken first.`,
      confirmText: "Apply",
      confirmClass: "btn-accent",
    },
    () =>
      runPresetOp(`Applying “${presetTitle(name)}”…`, name, "Apply failed", async () => {
        const r = await api.post(`/presets/${encodeURIComponent(name)}/apply`, {});
        finishApply(r);
      }),
  );
}

export function restorePreset(name) {
  const domains = libraryByName[name]?.included_domains || [];
  const full = !domains.length || domains.length >= DOMAINS.length;
  const labels = domains.map((d) => DOMAINS.find((x) => x.id === d)?.label || d).join(", ");
  const message = full
    ? `Replace ALL current data with "${esc(name)}"? This is a full rollback. An automatic backup of the current state is taken first.`
    : `Restore <b>${esc(labels)}</b> from "${esc(name)}"? This <b>replaces</b> them to exactly match the backup — anything added since is removed. Other data is left untouched. An automatic backup is taken first.`;
  showSubConfirmModal(
    {
      title: "Restore backup",
      message,
      confirmText: "Restore",
      confirmClass: "btn-danger",
    },
    () =>
      runPresetOp(`Restoring “${presetTitle(name)}”…`, name, "Restore failed", async () => {
        await api.post(`/presets/${encodeURIComponent(name)}/restore`, {});
        setStatus({ text: "Restored — reloading…", busy: true, row: name });
        setTimeout(() => location.reload(), 600);
      }),
  );
}

export function deletePreset(name) {
  showSubConfirmModal(
    { title: "Delete file", message: `Delete "${esc(name)}" from the library?`, confirmText: "Delete" },
    async () => {
      try {
        await api.del(`/presets/${encodeURIComponent(name)}`);
        refreshPresetLibrary();
      } catch (e) {
        toast(e.message, true);
      }
    },
  );
}

function finishApply(r) {
  const counts = Object.entries(r.summary || {})
    .map(([k, v]) => `${v} ${k}`)
    .join(", ");
  setStatus({ text: `Imported${counts ? `: ${counts}` : ""} — reloading…`, busy: true, row: status.row });
  setTimeout(() => location.reload(), 800);
}

export async function refreshPresetLibrary() {
  const el = $("preset-library-list");
  if (!el) return;
  try {
    const items = await api.get("/presets");
    libraryByName = Object.fromEntries(items.map((it) => [it.name, it]));
    if (!items.length) {
      el.innerHTML = '<div class="phrase-bank-empty">No presets or backups yet</div>';
      return;
    }
    items.sort((a, b) => (b.mtime || 0) - (a.mtime || 0)); // newest first
    el.innerHTML = items.map(presetRow).join("");
    renderStatus();
  } catch (e) {
    el.innerHTML = `<div class="phrase-bank-empty">Failed to load: ${esc(e.message)}</div>`;
  }
}

function presetRow(it) {
  const chips = (it.included_domains || []).map((d) => `<span class="preset-chip">${esc(d)}</span>`).join("");
  const title = it.label || it.name;
  return `
    <div class="preset-item" data-name="${escAttr(it.name)}">
      <div class="preset-item-top">
        <div class="preset-item-main">
          <div class="preset-item-title">
            <span class="preset-kind preset-kind-${esc(it.kind)}">${esc(it.kind)}</span>
            ${esc(title)}
          </div>
          <div class="preset-item-meta">${fmtDate(it.created_at)} · ${fmtSize(it.size)}</div>
        </div>
        <div class="preset-item-actions">
          <button class="btn btn-sm btn-square" onclick="downloadPreset('${escHandlerArg(it.name)}')" title="Download" aria-label="Download preset">${DOWNLOAD_ICON}</button>
          <button class="btn btn-sm" onclick="applyPreset('${escHandlerArg(it.name)}')" title="Merge into current data">Apply</button>
          <button class="btn btn-sm" onclick="restorePreset('${escHandlerArg(it.name)}')" title="Replace everything">Restore</button>
          <button class="btn btn-sm btn-danger btn-square" onclick="deletePreset('${escHandlerArg(it.name)}')" title="Delete" aria-label="Delete preset">${CLOSE_ICON}</button>
        </div>
      </div>
      <div class="preset-chips">${chips}</div>
    </div>`;
}
