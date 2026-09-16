import {
  api,
  closeModal,
  esc,
  escAttr,
  getManifestEntry,
  refreshLocalMlStatus,
  registerAction,
  registerWorkflowToolsPanelCard,
  showModal,
  toast,
} from "/static/workflow_api.js";

const WORKFLOW_ID = "prose_rewriter";
const FEATURE = "prose_rewriter";
const BODY_ID = "pr-card-config";
const STATE_ID = "pr-card-state";
// The llama.cpp runtime a first model download fetches along with it.
const RUNTIME_MB = 150;
const POLL_MS = 1500;
const BATCH_SIZES = [
  [1, "1 · lowest VRAM"],
  [2, "2"],
  [3, "3"],
  [4, "4 · default"],
  [8, "8 · fastest"],
];
const DELETE_ICON = `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18"/></svg>`;

let status = null; // last /local-ml/status
let automatic = Boolean(getManifestEntry(WORKFLOW_ID)?.config_defaults?.automatic ?? true);
let busy = ""; // the variant id, or "runtime", while a download runs
let pollTimer = null;
let pendingDelete = "";

const info = () => status?.features?.[FEATURE];
const stateText = (f) => `${f.state || "idle"}${f.error ? `: ${f.error}` : ""}`;

async function refresh() {
  try {
    status = await refreshLocalMlStatus();
  } catch (e) {
    console.warn("prose_rewriter: local-ml status failed", e);
  }
  paint();
}

function paint() {
  const el = document.getElementById(BODY_ID);
  if (el) el.innerHTML = cardBody();
}

/** Poll the model state while it loads; `expectLoad` covers a load the last write started. */
function watch(expectLoad = false) {
  clearTimeout(pollTimer);
  pollTimer = null;
  if (expectLoad || info()?.state === "loading") pollTimer = setTimeout(poll, POLL_MS);
}

async function poll() {
  pollTimer = null;
  if (!document.getElementById(BODY_ID)) return; // card hidden; it refreshes when it appears again
  try {
    status = await refreshLocalMlStatus();
  } catch (_e) {
    return;
  }
  const el = document.getElementById(STATE_ID);
  const f = info();
  if (el && f) {
    // Only the state line: a full repaint would close an open select.
    el.textContent = stateText(f);
    el.classList.toggle("ml-foot-error", Boolean(f.error));
    el.classList.toggle("ml-foot-loading", f.state === "loading");
  }
  watch();
}

function cardBody() {
  const desc = `<div class="tool-card-desc">Rewrites Writer drafts with a local model.</div>`;
  const f = info();
  if (!f) return desc;
  if (!f.deps_ok) {
    const cmd = status.install_cmd || "pip install -r requirements-ml.txt";
    return `${desc}<div class="tool-card-desc">Needs the optional ML extras: <code>${esc(cmd)}</code></div>`;
  }
  const anyPresent = f.variants.some((v) => v.present);
  const rows = f.variants.map((v) => variantRow(f, v)).join("");
  return `${desc}
    ${anyPresent ? `<div class="lg-config">${automaticCheck()}</div>` : ""}
    <div class="ml-variants">${rows}</div>
    ${anyPresent ? foot(f) : ""}`;
}

const automaticCheck = () =>
  `<label class="lg-enforce-label" title="Off: rewrite only the replies you pick with the rewrite button.">
    <input type="checkbox"${automatic ? " checked" : ""} data-wf-action="${WORKFLOW_ID}:automatic" data-wf-on="change">
    Rewrite every reply automatically
  </label>`;

function variantRow(f, v) {
  const attrs = `data-variant="${escAttr(v.id)}"`;
  const rid = escAttr(`pr-var-${v.id}`);
  const on = v.present && v.id === f.selected;
  const label = escAttr(v.label);
  const disabled = busy ? " disabled" : "";
  const pick = v.present
    ? `<input type="radio" id="${rid}" name="pr-variant" ${on ? "checked" : ""} aria-label="Use ${label}"
              data-wf-action="${WORKFLOW_ID}:select" data-wf-on="change" ${attrs}>
       <label class="ml-variant-name" for="${rid}">${label}</label>`
    : `<span class="ml-variant-name">${label}</span>`;
  const act = v.present
    ? `<button class="btn btn-xs btn-danger btn-square ml-variant-act" title="Delete" aria-label="Delete ${label}"
               data-wf-action="${WORKFLOW_ID}:delete" ${attrs}${disabled}>${DELETE_ICON}</button>`
    : `<button class="btn btn-xs ml-variant-act" data-wf-action="${WORKFLOW_ID}:download" ${attrs}${disabled}>Download</button>`;
  const sizeMb = v.present || f.runtime_ok ? v.size_mb : v.size_mb + RUNTIME_MB;
  return `<div class="ml-variant${on ? " ml-variant-on" : ""}${busy === v.id ? " ml-busy" : ""}">
    ${pick}
    <span class="ml-variant-size">${(sizeMb / 1024).toFixed(1)} GB</span>
    ${act}
    <div class="ml-variant-detail">${esc(v.detail)}</div>
  </div>`;
}

function foot(f) {
  if (!f.runtime_ok) {
    // A model on disk without the runtime: an install from before the GPU/CPU split.
    return `<div class="ml-gate${busy === "runtime" ? " ml-busy" : ""}">
      <div class="ml-gate-title">llama.cpp runtime required</div>
      <div class="ml-gate-act">
        <button class="btn btn-sm" data-wf-action="${WORKFLOW_ID}:runtime"${busy ? " disabled" : ""}>Download · ${RUNTIME_MB} MB</button>
      </div>
    </div>`;
  }
  const cls = f.state === "loading" ? " ml-foot-loading" : f.error ? " ml-foot-error" : "";
  const options = BATCH_SIZES.map(
    ([value, label]) => `<option value="${value}" ${f.batch_size === value ? "selected" : ""}>${label}</option>`,
  ).join("");
  return `<div class="ml-batch setting-row">
      <label for="pr-batch-size">Parallel</label>
      <select class="tool-card-select" id="pr-batch-size" data-wf-action="${WORKFLOW_ID}:batch" data-wf-on="change">${options}</select>
      <div>~140–190 MB VRAM per parallel slot.</div>
    </div>
    <div class="ml-foot">
      <label class="lg-enforce-label ml-check" title="Offload the model to the GPU. Switches the running model over.">
        <input type="checkbox" ${f.gpu ? "checked" : ""} data-wf-action="${WORKFLOW_ID}:gpu" data-wf-on="change">
        Run on GPU
      </label>
      <span class="ml-foot-state${cls}" id="${STATE_ID}">${esc(stateText(f))}</span>
    </div>`;
}

async function saveAutomatic(el) {
  const wanted = el.checked;
  try {
    const res = await api.put(`/workflows/${WORKFLOW_ID}/config`, { config: { automatic: wanted } });
    automatic = Boolean(res?.config?.automatic);
  } catch (e) {
    toast(e.message || "Failed to save", true);
  }
  el.checked = automatic;
}

async function saveModelConfig(patch) {
  const f = info();
  const body = { variant: f?.selected ?? null, gpu: Boolean(f?.gpu), batch_size: f?.batch_size ?? 4, ...patch };
  try {
    await api.post(`/local-ml/${FEATURE}/config`, body);
  } catch (e) {
    toast(e.message || "Failed to save", true);
  }
  await refresh();
  watch(true); // the write pre-warms in the background
}

/** Run one download with its row marked busy; the first model brings the runtime along. */
async function withBusy(key, run) {
  if (busy) return;
  busy = key;
  paint();
  try {
    await run();
  } catch (e) {
    toast(e.message || "Download failed", true);
  } finally {
    busy = "";
    await refresh();
    watch(true);
  }
}

function download(el) {
  const variant = el.dataset.variant;
  return withBusy(variant, async () => {
    if (!info()?.runtime_ok) await api.post("/local-ml/runtime", {});
    await api.post(`/local-ml/${FEATURE}/download`, { variant });
  });
}

function confirmDeleteModel(el) {
  pendingDelete = el.dataset.variant;
  showModal(`
    <h2>Delete Model</h2>
    <p>Delete this downloaded model file? It can be downloaded again.</p>
    <div class="modal-actions">
      <button class="btn" data-wf-action="${WORKFLOW_ID}:cancelDelete">Cancel</button>
      <button class="btn btn-danger" data-wf-action="${WORKFLOW_ID}:confirmDelete">Delete</button>
    </div>`);
}

async function deleteModel() {
  const variant = pendingDelete;
  pendingDelete = "";
  closeModal();
  if (!variant) return;
  try {
    await api.del(`/local-ml/${FEATURE}/model?variant=${encodeURIComponent(variant)}`);
  } catch (e) {
    toast(e.message || "Delete failed", true);
  }
  await refresh();
}

registerAction(WORKFLOW_ID, "automatic", saveAutomatic);
registerAction(WORKFLOW_ID, "select", (el) => saveModelConfig({ variant: el.dataset.variant }));
registerAction(WORKFLOW_ID, "gpu", (el) => saveModelConfig({ gpu: el.checked }));
registerAction(WORKFLOW_ID, "batch", (el) => saveModelConfig({ batch_size: Number(el.value) }));
registerAction(WORKFLOW_ID, "download", download);
registerAction(WORKFLOW_ID, "runtime", () => withBusy("runtime", () => api.post("/local-ml/runtime", {})));
registerAction(WORKFLOW_ID, "delete", confirmDeleteModel);
registerAction(WORKFLOW_ID, "confirmDelete", deleteModel);
registerAction(WORKFLOW_ID, "cancelDelete", () => {
  pendingDelete = "";
  closeModal();
});

registerWorkflowToolsPanelCard(WORKFLOW_ID, () => {
  // Both tool panes stay in the DOM, so a missing body means the card is just
  // appearing: at boot, or because the toggle turned on and started a load.
  if (!document.getElementById(BODY_ID)) setTimeout(() => refresh().then(() => watch(true)));
  return `<div id="${BODY_ID}">${cardBody()}</div>`;
});

async function loadAutomatic() {
  try {
    const res = await api.get(`/workflows/${WORKFLOW_ID}/config`);
    automatic = Boolean(res?.config?.automatic);
  } catch (e) {
    console.warn("prose_rewriter: config load failed", e);
  }
}

// Do not await config at module scope; one slow workflow must not block others.
loadAutomatic().then(paint);
