import { registerActions } from "./actions.js";
import { api, reloadPage } from "./api.js";
import { CLOSE_ICON, DOWNLOAD_ICON } from "./icons.js";
import { closeSubModal, showModal, showSubConfirmModal, showSubModal } from "./modal.js";
import { $, downloadBlob, esc, escAttr, toast } from "./utils.js";

const DOMAINS = [
  { id: "characters", label: "Characters" },
  { id: "chats", label: "Chats", requires: "characters", note: "needs Characters" },
  { id: "lorebooks", label: "Lorebooks" },
  { id: "documents", label: "Documents" },
  { id: "configs", label: "Settings & endpoints" },
];

// What a backup holds of each profile. Global lorebooks are marks on Worlds, so they need the Lorebooks domain.
const PARTS = [
  { id: "setup", label: "Setup" },
  { id: "fragments", label: "Fragments" },
  { id: "phrase_bank", label: "Phrase bank" },
  { id: "global_lorebooks", label: "Global lorebooks", requires: "lorebooks" },
];
const partLabel = (id) => PARTS.find((p) => p.id === id)?.label || id;

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

function showPresetsModal() {
  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>Backups</h2>
        <p class="modal-subtitle">Snapshot your data, import a backup (merged into your data), or restore a full backup.</p>
      </div>
      <div id="preset-top-actions" class="modal-title-actions">
        <button class="btn btn-sm" data-wf-action="backups:snapshot">📸 Snapshot current</button>
        <button class="btn btn-sm" data-wf-action="backups:import">⬆ Import file…</button>
        <input type="file" id="preset-import-input" accept=".db" style="display:none" data-wf-action="backups:importFile" data-wf-on="change">
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

async function liveProfiles() {
  return (await api.get("/profiles")).profiles;
}

/** One profile's row: its name and a checkbox per part in *parts*, each checked when *checked* says so. */
function profileRow(profile, parts, checked, extra = "") {
  const boxes = parts
    .map(
      (part) => `
      <label class="modal-checkbox-label">
        <input type="checkbox" data-part="${part}" ${checked(part) ? "checked" : ""}
               data-wf-action="backups:part" data-wf-on="change"> ${esc(partLabel(part))}
      </label>`,
    )
    .join("");
  return `
    <div class="backup-profile" data-profile="${escAttr(profile.id)}">
      <div class="backup-profile-name">${esc(profile.name)}</div>
      <div class="backup-profile-parts">${boxes}</div>
      ${extra}
    </div>`;
}

/** The parts checked in each profile row under *root*, by profile id; a profile with none is left out. */
function checkedParts(root) {
  const out = {};
  for (const row of root.querySelectorAll(".backup-profile")) {
    const parts = [...row.querySelectorAll("input[data-part]:checked")].map((input) => input.dataset.part);
    if (parts.length) out[row.dataset.profile] = parts;
  }
  return out;
}

async function showSnapshotModal() {
  let profiles;
  try {
    profiles = await liveProfiles();
  } catch (e) {
    toast(e.message, true);
    return;
  }
  const rows = DOMAINS.map(
    (d) => `
    <label class="modal-checkbox-label">
      <input type="checkbox" id="exp-${d.id}" data-domain="${d.id}" ${d.requires ? `data-requires="${d.requires}"` : ""}
             ${d.id === "configs" ? "" : "checked"} data-wf-action="backups:domain" data-wf-on="change">
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
    <div class="field" id="exp-profiles">
      <label>Profiles</label>
      ${profiles
        .map((p) =>
          profileRow(
            p,
            PARTS.map((part) => part.id),
            (part) => part !== "setup",
          ),
        )
        .join("")}
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
      <button class="btn" data-wf-action="modal:closeSub">Cancel</button>
      <button class="btn btn-accent" data-wf-action="backups:createSnapshot">Create</button>
    </div>
    </div>
  `);
}

function onPresetDomainChange(cb) {
  const domain = cb.dataset.domain;
  if (cb.dataset.requires && cb.checked) {
    const req = $(`exp-${cb.dataset.requires}`);
    if (req) req.checked = true;
  }
  if (!cb.checked) {
    const dependent = DOMAINS.find((d) => d.requires === domain);
    const part = PARTS.find((p) => p.requires === domain);
    if (dependent && $(`exp-${dependent.id}`)?.checked) {
      cb.checked = true;
      toast(`${DOMAINS.find((d) => d.id === domain).label} is required by ${dependent.label}`, true);
    } else if (part && $("exp-profiles")?.querySelector(`input[data-part="${part.id}"]:checked`)) {
      cb.checked = true;
      toast(`${DOMAINS.find((d) => d.id === domain).label} is required by ${part.label}`, true);
    }
  }
  if (domain === "configs") {
    $("preset-key-warning").classList.toggle("hidden", !cb.checked);
  }
}

/** A part checkbox in the Snapshot dialog checks the domain it needs. */
function onPartChange(cb) {
  const requires = PARTS.find((p) => p.id === cb.dataset.part)?.requires;
  const domain = requires && $(`exp-${requires}`);
  if (cb.checked && domain) domain.checked = true;
}

function selectedDomains() {
  return DOMAINS.filter((d) => $(`exp-${d.id}`)?.checked).map((d) => d.id);
}

async function doCreateSnapshot() {
  const domains = selectedDomains();
  const profiles = checkedParts($("exp-profiles"));
  if (!domains.length && !Object.keys(profiles).length) {
    toast("Select at least one thing to save", true);
    return;
  }
  const strip = !domains.includes("configs") || $("exp-strip-keys")?.checked;
  const label = $("exp-label")?.value.trim() || "";
  closeSubModal();
  await runPresetOp("Creating snapshot…", null, "Snapshot failed", async () => {
    await api.post("/backups/export", { domains, profiles, strip_keys: strip, label });
    setStatus();
    if (!$("preset-status")) toast("Snapshot saved");
    await refreshPresetLibrary();
  });
}

function triggerPresetImport() {
  $("preset-import-input").click();
}

async function handlePresetImportFile(inp) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = "";
  await runPresetOp(`Importing “${f.name}”…`, null, "Import failed", async () => {
    await api.upload("/backups/import", f);
    setStatus();
    if (!$("preset-status")) toast("Added to library");
    await refreshPresetLibrary();
  });
}

function downloadPreset(name) {
  downloadBlob(name, `/api/backups/${encodeURIComponent(name)}/download`);
}

/** The file's profiles, each with the parts it holds and where it lands: the profile with its id, or a new one. */
function targetRows(entry, live) {
  const rows = (entry?.profiles || []).map((p) => {
    const same = live.find((l) => l.id === p.id);
    const options = [
      ...(same ? [`<option value="same">${esc(same.name)}</option>`] : []),
      '<option value="new">New profile</option>',
    ].join("");
    const target = `<select class="backup-profile-target" aria-label="Apply to">${options}</select>`;
    return profileRow(p, p.parts, () => true, target);
  });
  return rows.length ? `<div class="backup-profiles" id="backup-targets">${rows.join("")}</div>` : "";
}

/** The target and checked parts of each file profile in the open Apply or Restore dialog. */
function chosenTargets() {
  const root = $("backup-targets");
  if (!root) return {};
  const out = {};
  for (const row of root.querySelectorAll(".backup-profile")) {
    out[row.dataset.profile] = {
      target: row.querySelector(".backup-profile-target").value,
      parts: [...row.querySelectorAll("input[data-part]:checked")].map((input) => input.dataset.part),
    };
  }
  return out;
}

async function confirmWithTargets(name, opts, run) {
  let live;
  try {
    live = await liveProfiles();
  } catch (e) {
    toast(e.message, true);
    return;
  }
  const extraHtml = opts.targets === false ? "" : targetRows(libraryByName[name], live);
  showSubConfirmModal({ ...opts, extraHtml }, () => {
    const profiles = chosenTargets();
    run(profiles);
  });
}

function applyPreset(name) {
  confirmWithTargets(
    name,
    {
      title: "Apply backup",
      message: `Merge "${esc(name)}" into your current data? Matching items are overwritten, new ones added. An automatic backup is taken first.`,
      confirmText: "Apply",
      confirmClass: "btn-accent",
    },
    (profiles) =>
      runPresetOp(`Applying “${presetTitle(name)}”…`, name, "Apply failed", async () => {
        const r = await api.post(`/backups/${encodeURIComponent(name)}/apply`, { profiles });
        finishApply(r);
      }),
  );
}

function isFull(entry) {
  const domains = entry?.included_domains || [];
  return domains.length >= DOMAINS.length && (entry?.profiles || []).every((p) => p.parts.length >= PARTS.length);
}

function restorePreset(name) {
  const entry = libraryByName[name];
  const full = !entry || isFull(entry);
  const labels = (entry?.included_domains || []).map((d) => DOMAINS.find((x) => x.id === d)?.label || d);
  const what = labels.length
    ? `<b>${esc(labels.join(", "))}</b> and the profile parts below`
    : "the profile parts below";
  const message = full
    ? `Replace ALL current data with "${esc(name)}"? This is a full rollback. An automatic backup of the current state is taken first.`
    : `Restore ${what} from "${esc(name)}"? This <b>replaces</b> them to exactly match the backup — anything added since is removed. Other data is left untouched. An automatic backup is taken first.`;
  confirmWithTargets(
    name,
    {
      title: "Restore backup",
      message,
      confirmText: "Restore",
      confirmClass: "btn-danger",
      targets: !full,
    },
    (profiles) =>
      runPresetOp(`Restoring “${presetTitle(name)}”…`, name, "Restore failed", async () => {
        await api.post(`/backups/${encodeURIComponent(name)}/restore`, { profiles });
        setStatus({ text: "Restored — reloading…", busy: true, row: name });
        setTimeout(reloadPage, 600);
      }),
  );
}

function deletePreset(name) {
  showSubConfirmModal(
    { title: "Delete file", message: `Delete "${esc(name)}" from the library?`, confirmText: "Delete" },
    async () => {
      try {
        await api.del(`/backups/${encodeURIComponent(name)}`);
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
  setTimeout(reloadPage, 800);
}

async function refreshPresetLibrary() {
  const el = $("preset-library-list");
  if (!el) return;
  try {
    const items = await api.get("/backups");
    libraryByName = Object.fromEntries(items.map((it) => [it.name, it]));
    if (!items.length) {
      el.innerHTML = '<div class="phrase-bank-empty">No backups yet</div>';
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
  const chips = [
    ...(it.included_domains || []).map((d) => `<span class="preset-chip">${esc(d)}</span>`),
    ...(it.profiles || []).map((p) => `<span class="preset-chip preset-chip-profile">${esc(p.name)}</span>`),
  ].join("");
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
          <button class="btn btn-sm btn-square" data-wf-action="backups:download" data-name="${escAttr(it.name)}" title="Download" aria-label="Download backup">${DOWNLOAD_ICON}</button>
          <button class="btn btn-sm" data-wf-action="backups:apply" data-name="${escAttr(it.name)}" title="Merge into current data">Apply</button>
          <button class="btn btn-sm" data-wf-action="backups:restore" data-name="${escAttr(it.name)}" title="Replace everything">Restore</button>
          <button class="btn btn-sm btn-danger btn-square" data-wf-action="backups:delete" data-name="${escAttr(it.name)}" title="Delete" aria-label="Delete backup">${CLOSE_ICON}</button>
        </div>
      </div>
      <div class="preset-chips">${chips}</div>
    </div>`;
}

registerActions("backups", {
  open: () => showPresetsModal(),
  snapshot: () => showSnapshotModal(),
  createSnapshot: () => doCreateSnapshot(),
  domain: (el) => onPresetDomainChange(el),
  part: (el) => onPartChange(el),
  import: () => triggerPresetImport(),
  importFile: (el) => handlePresetImportFile(el),
  download: (el) => downloadPreset(el.dataset.name),
  apply: (el) => applyPreset(el.dataset.name),
  restore: (el) => restorePreset(el.dataset.name),
  delete: (el) => deletePreset(el.dataset.name),
});
