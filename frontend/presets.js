// The sidebar header's global preset picker. A preset is saved when it is left, so there is no Save: a switch, create or
// delete answers the settings row now live, and the settings, Prompt Layout and fragment lists reload without a page reload.
import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { reloadDecisionConfig } from "./decisions.js";
import { CLOSE_ICON, COPY_ICON, EDIT_ICON, PLUS_ICON } from "./icons.js";
import { loadInteractiveFragments, loadMoodFragments } from "./library_fragments.js";
import { closeModal, confirmDelete, showModal } from "./modal.js";
import { loadPromptLayout } from "./prompt_layout.js";
import { loadSettings } from "./settings.js";
import { acceptSettingsRow } from "./settings_store.js";
import { $, esc, escAttr, toast } from "./utils.js";

let _presets = [];
let _activeId = null;

export async function loadPresets() {
  const { presets, active_id } = await api.get("/presets");
  _presets = presets;
  _activeId = active_id;
  render();
}

const _active = () => _presets.find((p) => p.id === _activeId);

function _button(action, icon, label, disabled = false) {
  return `<button type="button" class="preset-btn" data-wf-action="preset:${action}" title="${label}" aria-label="${label}"${disabled ? " disabled" : ""}>${icon}</button>`;
}

function render() {
  const el = $("preset-bar");
  if (!el) return;
  const options = _presets
    .map((p) => `<option value="${escAttr(p.id)}" ${p.id === _activeId ? "selected" : ""}>${esc(p.name)}</option>`)
    .join("");
  el.innerHTML = `<select class="theme-select" aria-label="Preset" data-wf-action="preset:select" data-wf-on="change">${options}</select>
    ${_button("new", PLUS_ICON, "New preset")}
    ${_button("duplicate", COPY_ICON, "Duplicate preset")}
    ${_button("rename", EDIT_ICON, "Rename preset")}
    ${_button("delete", CLOSE_ICON, "Delete preset", _presets.length < 2)}`;
}

// Runs *fn*, showing a refused request as a toast and putting the picker back on the active preset.
async function _attempt(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
    render();
  }
}

// After the live state changed under every panel that shows it. The Judge config first: the settings' Judge lane and the
// fragment lists paint from it.
async function _reloadLive() {
  await reloadDecisionConfig();
  await loadSettings();
  await Promise.all([loadPresets(), loadPromptLayout(), loadMoodFragments(), loadInteractiveFragments()]);
}

async function switchTo(id) {
  await acceptSettingsRow(() => api.put("/presets/active", { id }));
  await _reloadLive();
}

function nameModal(title, value, action) {
  showModal(
    `<h2>${esc(title)}</h2>
    <div class="field"><label for="preset-name">Name</label><input id="preset-name" value="${escAttr(value)}"></div>
    <div class="modal-actions">
      <button class="btn" data-wf-action="modal:close">Cancel</button>
      <button class="btn btn-accent" data-wf-action="preset:${action}">Save</button>
    </div>`,
    { size: "narrow" },
  );
}

// Saves the name modal through *save*.
async function saveName(save) {
  const name = $("preset-name").value.trim();
  if (!name) return toast("A preset needs a name", true);
  await _attempt(async () => {
    await save(name);
    closeModal();
  });
}

const createPreset = (copyFrom) =>
  saveName(async (name) => switchTo((await api.post("/presets", { name, copy_from: copyFrom })).id));

function deletePreset() {
  const id = _activeId;
  confirmDelete("Preset", `Delete "${esc(_active()?.name || "")}" and its prompt layout?`, () =>
    _attempt(async () => {
      await acceptSettingsRow(() => api.del(`/presets/${encodeURIComponent(id)}`));
      await _reloadLive();
    }),
  );
}

registerActions("preset", {
  select: (el) => _attempt(() => switchTo(el.value)),
  new: () => nameModal("New Preset", "", "createNew"),
  duplicate: () => nameModal("Duplicate Preset", `${_active()?.name || "Preset"} copy`, "createCopy"),
  rename: () => nameModal("Rename Preset", _active()?.name || "", "saveName"),
  createNew: () => createPreset(null),
  createCopy: () => createPreset(_activeId),
  saveName: () =>
    saveName(async (name) => {
      await api.patch(`/presets/${encodeURIComponent(_activeId)}`, { name });
      await loadPresets();
    }),
  delete: () => deletePreset(),
});
