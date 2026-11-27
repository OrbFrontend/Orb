// The sidebar header's profile picker; its dropdown also holds New, Copy, Rename and Delete. A profile's setup is saved
// when it is left, so there is no Save: a switch, create or delete answers the settings row now live, and the settings,
// Prompt Layout, fragment and World lists reload without a page reload. Each profile owns its fragments, phrase bank and
// global Worlds, so the picker is off while a chat turn runs: a switch mid-turn would mix two profiles in one reply.
import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { reloadDecisionConfig } from "./decisions.js";
import { loadInteractiveFragments, loadMoodFragments } from "./library_fragments.js";
import { loadWorlds, refreshChatWorlds } from "./lorebooks.js";
import { closeModal, confirmDelete, showModal } from "./modal.js";
import { loadPromptLayout } from "./prompt_layout.js";
import { loadSettings } from "./settings.js";
import { acceptSettingsRow } from "./settings_store.js";
import { anyTurnActive, S, subscribe } from "./state.js";
import { $, esc, escAttr, toast } from "./utils.js";

let _profiles = [];
let _activeId = null;

export async function loadProfiles() {
  const { profiles, active_id } = await api.get("/profiles");
  _profiles = profiles;
  _activeId = active_id;
  render();
}

const _active = () => _profiles.find((p) => p.id === _activeId);

// The picker lists the profiles, then a separator and the actions on the active profile. An action entry opens its
// dialog and the picker stays on the active profile.
const _ACTIONS = [
  ["new", "New…"],
  ["duplicate", "Copy…"],
  ["rename", "Rename…"],
  ["delete", "Delete…"],
];

function render() {
  const el = $("profile-select");
  if (!el) return;
  const options = _profiles
    .map((p) => `<option value="${escAttr(p.id)}" ${p.id === _activeId ? "selected" : ""}>${esc(p.name)}</option>`)
    .join("");
  const actions = _ACTIONS
    .map(
      ([action, label]) =>
        `<option data-action="${action}"${action === "delete" && _profiles.length < 2 ? " disabled" : ""}>${label}</option>`,
    )
    .join("");
  el.innerHTML = `${options}<hr>${actions}`;
  el.disabled = anyTurnActive();
}

subscribe("turns", () => {
  const el = $("profile-select");
  if (el) el.disabled = anyTurnActive();
});

// Runs *fn*, showing a refused request as a toast and putting the picker back on the active profile.
async function _attempt(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
    render();
  }
}

// After the live state changed under every panel that shows it. The Judge config first: the settings' Judge lane and the
// fragment lists paint from it. The Worlds follow the profile's global marks, in the sidebar and in the chat in view.
async function _reloadLive() {
  await reloadDecisionConfig();
  await loadSettings();
  await Promise.all([
    loadProfiles(),
    loadPromptLayout(),
    loadMoodFragments(),
    loadInteractiveFragments(),
    loadWorlds(),
    refreshChatWorlds(S.activeConvId),
  ]);
}

async function switchTo(id) {
  await acceptSettingsRow(() => api.put("/profiles/active", { id }));
  await _reloadLive();
}

function nameModal(title, value, action) {
  showModal(
    `<h2>${esc(title)}</h2>
    <div class="field"><label for="profile-name">Name</label><input id="profile-name" value="${escAttr(value)}"></div>
    <div class="modal-actions">
      <button class="btn" data-wf-action="modal:close">Cancel</button>
      <button class="btn btn-accent" data-wf-action="profile:${action}">Save</button>
    </div>`,
    { size: "narrow" },
  );
}

// Saves the name modal through *save*.
async function saveName(save) {
  const name = $("profile-name").value.trim();
  if (!name) return toast("A profile needs a name", true);
  await _attempt(async () => {
    await save(name);
    closeModal();
  });
}

// New starts from fresh defaults; Copy takes the active profile and everything it owns.
const createProfile = (copyFrom) =>
  saveName(async (name) => switchTo((await api.post("/profiles", { name, copy_from: copyFrom })).id));

function deleteProfile() {
  const id = _activeId;
  confirmDelete("Profile", `Delete "${esc(_active()?.name || "")}"?`, () =>
    _attempt(async () => {
      await acceptSettingsRow(() => api.del(`/profiles/${encodeURIComponent(id)}`));
      await _reloadLive();
    }),
  );
}

const _actions = {
  new: () => nameModal("New Profile", "", "createNew"),
  duplicate: () => nameModal("Copy Profile", `${_active()?.name || "Profile"} copy`, "createCopy"),
  rename: () => nameModal("Rename Profile", _active()?.name || "", "saveName"),
  delete: () => deleteProfile(),
};

function _select(el) {
  const action = el.selectedOptions[0]?.dataset.action;
  if (!action) return _attempt(() => switchTo(el.value));
  el.value = _activeId;
  _actions[action]();
}

registerActions("profile", {
  select: _select,
  createNew: () => createProfile(null),
  createCopy: () => createProfile(_activeId),
  saveName: () =>
    saveName(async (name) => {
      await api.patch(`/profiles/${encodeURIComponent(_activeId)}`, { name });
      await loadProfiles();
    }),
});
