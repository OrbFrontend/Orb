// Backup & Presets: selective export, merge-import, and a library of .db
// snapshots that can be applied (merged) or restored (full replace).

import { api } from "./api.js";
import { showModal, closeModal, switchTab, showConfirmModal } from "./modal.js";
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
  const exportRows = DOMAINS.map(
    (d) => `
    <label class="modal-checkbox-label">
      <input type="checkbox" id="exp-${d.id}" data-domain="${d.id}" ${d.requires ? `data-requires="${d.requires}"` : ""}
             ${d.id === "configs" ? "" : "checked"} onchange="onPresetDomainChange(this)">
      ${esc(d.label)}${d.note ? ` <span class="preset-hint">(${esc(d.note)})</span>` : ""}
    </label>`,
  ).join("");

  showModal(`
    <h2>Backup &amp; Presets</h2>
    <p class="modal-subtitle">Export a portable preset, import one (merged into your data), or restore a full backup.</p>
    <div class="tabs">
      <div class="tab active" onclick="switchTab(this,'preset-tab-export')">Export</div>
      <div class="tab" onclick="switchTab(this,'preset-tab-library')" onmousedown="refreshPresetLibrary()">Library</div>
    </div>

    <div id="preset-tab-export" class="tab-content active">
      <div class="field">
        <label class="preset-section-label">Include in preset:</label>
        ${exportRows}
      </div>
      <div id="preset-key-warning" class="preset-warning hidden">
        ⚠️ This preset includes your endpoints. API keys are sensitive.
        <label class="modal-checkbox-label" style="margin-top:6px">
          <input type="checkbox" id="exp-strip-keys" checked> Strip API keys (recommended for sharing)
        </label>
      </div>
      <div class="field">
        <label class="preset-section-label" for="exp-label">Label (optional)</label>
        <input type="text" id="exp-label" placeholder="e.g. my-cast" maxlength="60">
      </div>
      <div class="modal-actions">
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn btn-accent" onclick="doPresetExport()">Create &amp; download</button>
      </div>
    </div>

    <div id="preset-tab-library" class="tab-content">
      <div class="modal-title-actions" style="margin-bottom:10px;display:flex;gap:8px">
        <button class="btn btn-sm" onclick="doPresetSnapshot()">📸 Snapshot current</button>
        <button class="btn btn-sm" onclick="triggerPresetImport()">⬆ Import file…</button>
        <input type="file" id="preset-import-input" accept=".db" style="display:none" onchange="handlePresetImportFile(this)">
      </div>
      <div id="preset-library-list" class="phrase-bank-list">Loading…</div>
    </div>
  `);
  refreshPresetLibrary();
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

export async function doPresetExport() {
  const domains = selectedDomains();
  if (!domains.length) {
    toast("Select at least one thing to export", true);
    return;
  }
  const strip = !domains.includes("configs") || $("exp-strip-keys")?.checked;
  try {
    toast("Building preset…");
    const { name } = await api.post("/presets/export", {
      domains,
      strip_keys: strip,
      label: $("exp-label")?.value.trim() || "",
    });
    downloadPreset(name);
    toast("Preset created");
    refreshPresetLibrary();
  } catch (e) {
    toast("Export failed: " + e.message, true);
  }
}

export async function doPresetSnapshot() {
  try {
    toast("Snapshotting…");
    await api.post("/presets/snapshot", { label: "" });
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
  showConfirmModal(
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
  showConfirmModal(
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
  showConfirmModal(
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
  showConfirmModal(
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
    el.innerHTML = items.map(presetRow).join("");
  } catch (e) {
    el.innerHTML = `<div class="phrase-bank-empty">Failed to load: ${esc(e.message)}</div>`;
  }
}

function presetRow(it) {
  const chips = (it.included_domains || []).map((d) => `<span class="preset-chip">${esc(d)}</span>`).join("");
  const title = it.label || it.name;
  const restore =
    it.kind === "auto" || it.kind === "manual" || (it.included_domains || []).length === DOMAINS.length;
  return `
    <div class="preset-item">
      <div class="preset-item-main">
        <div class="preset-item-title">
          <span class="preset-kind preset-kind-${esc(it.kind)}">${esc(it.kind)}</span>
          ${esc(title)}
        </div>
        <div class="preset-item-meta">${fmtDate(it.created_at)} · ${fmtSize(it.size)}</div>
        <div class="preset-chips">${chips}</div>
      </div>
      <div class="preset-item-actions">
        <button class="btn btn-sm" onclick="downloadPreset('${esc(it.name)}')" title="Download">⬇</button>
        <button class="btn btn-sm" onclick="applyPreset('${esc(it.name)}')" title="Merge into current data">Apply</button>
        ${restore ? `<button class="btn btn-sm" onclick="restorePreset('${esc(it.name)}')" title="Replace everything">Restore</button>` : ""}
        <button class="btn btn-sm btn-danger" onclick="deletePreset('${esc(it.name)}')" title="Delete">✕</button>
      </div>
    </div>`;
}
