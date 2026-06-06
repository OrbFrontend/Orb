// Backup & Presets: selective export, merge-import, and a library of .db
// snapshots that can be applied (merged) or restored (full replace).

import { api } from "./api.js";
import { closeSubModal, showModal, showSubConfirmModal, showSubModal } from "./modal.js";
import { $, esc, toast } from "./utils.js";

const DOMAINS = [
  { id: "characters", label: "Characters" },
  { id: "chats", label: "Chats", requires: "characters", note: "needs Characters" },
  { id: "lorebooks", label: "Lorebooks" },
  { id: "fragments", label: "Fragments (mood & director)" },
  { id: "phrase_bank", label: "Phrase bank" },
  { id: "configs", label: "Settings & endpoints" },
];

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

export function showPresetsModal() {
  showModal(`
    <h2>Backup &amp; Presets</h2>
    <p class="modal-subtitle">Snapshot your data, import a preset (merged into your data), or restore a full backup.</p>
    <div id="preset-top-actions" class="modal-title-actions" style="margin-bottom:10px;display:flex;gap:8px">
      <button class="btn btn-sm" onclick="showSnapshotModal()">📸 Snapshot current</button>
      <button class="btn btn-sm" onclick="triggerPresetImport()">⬆ Import file…</button>
      <input type="file" id="preset-import-input" accept=".db" style="display:none" onchange="handlePresetImportFile(this)">
    </div>
    <div id="preset-library-list" class="phrase-bank-list">Loading…</div>
  `);
  refreshPresetLibrary();
}

// Sub-modal: choose what the snapshot carries. Everything checked makes a full
// backup that can be restored; a subset makes a portable preset.
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
    <h2>Snapshot current</h2>
    <p class="modal-subtitle">Pick what to include. Everything checked makes a full backup you can restore from.</p>
    <div class="field">
      <label class="preset-section-label">Include:</label>
      ${rows}
    </div>
    <div id="preset-key-warning" class="preset-warning hidden">
      ⚠️ This snapshot includes your endpoints. API keys are sensitive.
      <label class="modal-checkbox-label" style="margin-top:6px">
        <input type="checkbox" id="exp-strip-keys" checked> Strip API keys (recommended for sharing)
      </label>
    </div>
    <div class="field">
      <label class="preset-section-label" for="exp-label">Label (optional)</label>
      <input type="text" id="exp-label" placeholder="e.g. my-cast" maxlength="60">
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeSubModal()">Cancel</button>
      <button class="btn btn-accent" onclick="doCreateSnapshot()">Create</button>
    </div>
  `);
}

export function onPresetDomainChange(cb) {
  const domain = cb.dataset.domain;
  // A domain that requires another forces it on; unchecking the required one is blocked.
  if (cb.dataset.requires && cb.checked) {
    const req = $(`exp-${cb.dataset.requires}`);
    if (req) req.checked = true;
  }
  // If something requires this domain and is checked, keep this checked.
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
  try {
    toast("Creating snapshot…");
    await api.post("/presets/export", {
      domains,
      strip_keys: strip,
      label: $("exp-label")?.value.trim() || "",
    });
    closeSubModal();
    toast("Snapshot saved");
    refreshPresetLibrary();
  } catch (e) {
    toast("Snapshot failed: " + e.message, true);
  }
}

export function triggerPresetImport() {
  $("preset-import-input").click();
}

export async function handlePresetImportFile(inp) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = "";
  showSubConfirmModal(
    {
      title: "Import preset",
      message: `Merge "${esc(f.name)}" into your data? Matching items are overwritten, new ones added. An automatic backup is taken first.`,
      confirmText: "Import",
      confirmClass: "btn-accent",
    },
    async () => {
      try {
        toast("Importing…");
        const r = await api.upload("/presets/import", f);
        finishApply(r);
      } catch (e) {
        toast("Import failed: " + e.message, true);
      }
    },
  );
}

export function downloadPreset(name) {
  const a = document.createElement("a");
  a.href = `/api/presets/${encodeURIComponent(name)}/download`;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

export function applyPreset(name) {
  showSubConfirmModal(
    {
      title: "Apply preset",
      message: `Merge "${esc(name)}" into your current data? Matching items are overwritten, new ones added. An automatic backup is taken first.`,
      confirmText: "Apply",
      confirmClass: "btn-accent",
    },
    async () => {
      try {
        toast("Applying…");
        const r = await api.post(`/presets/${encodeURIComponent(name)}/apply`, {});
        finishApply(r);
      } catch (e) {
        toast("Apply failed: " + e.message, true);
      }
    },
  );
}

export function restorePreset(name) {
  showSubConfirmModal(
    {
      title: "Restore backup",
      message: `Replace ALL current data with "${esc(name)}"? This is a full rollback. An automatic backup of the current state is taken first.`,
      confirmText: "Restore",
      confirmClass: "btn-danger",
    },
    async () => {
      try {
        toast("Restoring…");
        await api.post(`/presets/${encodeURIComponent(name)}/restore`, {});
        toast("Restored — reloading");
        setTimeout(() => location.reload(), 600);
      } catch (e) {
        toast("Restore failed: " + e.message, true);
      }
    },
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
  toast(`Imported${counts ? ": " + counts : ""} — reloading`);
  setTimeout(() => location.reload(), 800);
}

export async function refreshPresetLibrary() {
  const el = $("preset-library-list");
  if (!el) return;
  try {
    const items = await api.get("/presets");
    if (!items.length) {
      el.innerHTML = '<div class="phrase-bank-empty">No presets or backups yet</div>';
      return;
    }
    items.sort((a, b) => (b.mtime || 0) - (a.mtime || 0)); // newest first
    el.innerHTML = items.map(presetRow).join("");
  } catch (e) {
    el.innerHTML = `<div class="phrase-bank-empty">Failed to load: ${esc(e.message)}</div>`;
  }
}

function presetRow(it) {
  const chips = (it.included_domains || []).map((d) => `<span class="preset-chip">${esc(d)}</span>`).join("");
  const title = it.label || it.name;
  // Restore replaces the whole DB, so only offer it for full-coverage backups we
  // made ourselves: a partial snapshot would wipe the domains it lacks, and an
  // imported preset is foreign data that should only ever be merged in (Apply).
  const restore =
    it.kind !== "imported" && (it.kind === "auto" || (it.included_domains || []).length === DOMAINS.length);
  return `
    <div class="preset-item">
      <div class="preset-item-top">
        <div class="preset-item-main">
          <div class="preset-item-title">
            <span class="preset-kind preset-kind-${esc(it.kind)}">${esc(it.kind)}</span>
            ${esc(title)}
          </div>
          <div class="preset-item-meta">${fmtDate(it.created_at)} · ${fmtSize(it.size)}</div>
        </div>
        <div class="preset-item-actions">
          <button class="btn btn-sm" onclick="downloadPreset('${esc(it.name)}')" title="Download">⬇</button>
          <button class="btn btn-sm" onclick="applyPreset('${esc(it.name)}')" title="Merge into current data">Apply</button>
          ${restore ? `<button class="btn btn-sm" onclick="restorePreset('${esc(it.name)}')" title="Replace everything">Restore</button>` : ""}
          <button class="btn btn-sm btn-danger" onclick="deletePreset('${esc(it.name)}')" title="Delete">✕</button>
        </div>
      </div>
      <div class="preset-chips">${chips}</div>
    </div>`;
}
