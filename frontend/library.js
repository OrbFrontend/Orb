import { api } from "./api.js";
import { loadConversations, refreshSceneCardFragments, resetChatUI, stashCardFragments } from "./chat.js";
import { createChipInput } from "./chips.js";
import {
  initCardFragments,
  readCardFragments,
  renderCardFragmentsTab,
  showCardInteractiveFragmentModal,
  showCardMoodFragmentModal,
} from "./library_fragments.js";
import { loadWorlds } from "./lorebooks.js";
import { closeModal, showConfirmModal, showCropModal, showModal, switchTab } from "./modal.js";
import { SIDEBAR_CLOSE_ICON, SIDEBAR_EDIT_ICON } from "./sidebar_icons.js";
import { charactersView, S } from "./state.js";
import {
  $,
  avatarCell,
  avatarUrl,
  CHAT_AVATAR_ICON,
  convActivity,
  downloadBlob,
  esc,
  escAttr,
  NO_AVATAR_ICON,
  toast,
} from "./utils.js";
import { validate } from "./validate.js";

export {
  importInternetChar,
  loadMoreInternet,
  onCharBrowserSearch,
  randomizeInternet,
  searchInternet,
  setCharBrowserSort,
  setCharBrowserView,
  setInternetSource,
  showCharacterBrowserModal,
  toggleTagSelection,
} from "./library_browser.js";
export {
  deleteInteractiveFragment,
  deleteMoodFragment,
  loadInteractiveFragments,
  loadMoodFragments,
  renderInteractiveFragments,
  renderMoodFragments,
  saveInteractiveFragment,
  saveMoodFragment,
  showInteractiveFragmentModal,
  showMoodFragmentModal,
  toggleInteractiveFragmentEnabled,
  toggleMoodFragmentEnabled,
  updateInteractiveFragmentExample,
} from "./library_fragments.js";

let _pendingAvatar = null;
let _pendingImportId = null;
let _pendingImportSourceFormat = null;
let _pendingTags = null;
let _pendingCharacterBook = null;
let _pendingExtensions = null;
export const _avatarBust = new Map();

function filterRecentCharacters(characters, conversations, limit = 5) {
  const recentMap = new Map();
  for (const conv of conversations) {
    const ts = convActivity(conv);
    const cardIds = conv.character_card_id ? [conv.character_card_id] : conv.group_card_ids || [];
    for (const cardId of cardIds) {
      const existing = recentMap.get(cardId);
      if (!existing || ts > existing) recentMap.set(cardId, ts);
    }
  }

  const tagged = characters.map((char) => {
    const convTime = recentMap.get(char.id);
    const activityTime = convTime || char.updated_at || char.created_at || "";
    return { char, activityTime, hasConversation: !!convTime };
  });

  tagged.sort((a, b) => b.activityTime.localeCompare(a.activityTime));

  return tagged.slice(0, limit).map((t) => t.char);
}

export async function loadCharacters() {
  const [characters, conversations] = await Promise.all([
    api.get("/characters"),
    S.conversations || api.get("/conversations"),
  ]);
  S.allCharacters = characters;
  S.characters = filterRecentCharacters(characters, conversations || []);
  renderCharacters();
}

export function refreshCharacters() {
  const source = charactersView();
  if (!source.length) return;
  S.characters = filterRecentCharacters(source, S.conversations || []);
  renderCharacters();
}

export function renderCharacters() {
  if (!S.characters.length) {
    $("char-list").innerHTML =
      '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">No characters yet.</div>';
    return;
  }
  $("char-list").innerHTML = S.characters
    .map((c) => {
      const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
      const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "");
      const meta = esc(c.creator_notes || (c.tags || []).slice(0, 2).join(", ") || c.source_format || "");
      const isActive = S.activeCharId === c.id;
      return `<div class="char-item${isActive ? " active" : ""}" onclick="selectChar('${c.id}', 'recent')">
      <div class="char-avatar-sm${c.has_expressions ? " avatar-halo" : ""}">${av}</div>
      <div class="char-item-info">
        <div class="char-item-name">${esc(c.name)}</div>
        <div class="char-item-meta">${meta}</div>
      </div>
      <div class="char-item-actions">
        <button class="char-action-edit" onclick="event.stopPropagation();showCharEditModal('${c.id}')" title="Edit character" aria-label="Edit ${escAttr(c.name)}">${SIDEBAR_EDIT_ICON}</button>
        <button class="char-action-delete" onclick="event.stopPropagation();deleteCharacter('${c.id}')" title="Delete character" aria-label="Delete ${escAttr(c.name)}">${SIDEBAR_CLOSE_ICON}</button>
      </div>
    </div>`;
    })
    .join("");
}

export function triggerImport() {
  $("import-file-input").click();
}

export async function handleImportFile(inp) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = "";
  try {
    toast("Importing...");
    const r = await api.upload("/characters/import", f);
    showCharEditModal(r);
  } catch (e) {
    toast(`Import failed: ${e.message}`, true);
  }
}

export async function deleteCharacter(id) {
  const charName = charactersView().find((c) => c.id === id)?.name;
  const usage = await api.get(`/characters/${id}/usage`).catch(() => null);
  const usageText = usage
    ? `<p class="modal-hint">Used by ${usage.solo} solo conversation(s), ${usage.active_groups} active group(s), and ${usage.historical_groups} group history roster(s).</p>`
    : "";
  showConfirmModal(
    {
      title: "Delete Character",
      message: `Are you sure you want to delete ${charName ? `"${charName}"` : "this character card"}?`,
      confirmText: "Delete",
      extraHtml: `${usageText}
      <div class="field">
        <label class="modal-checkbox-label">
          <input type="checkbox" id="delete-conversations-checkbox">
          Also delete all conversations associated with this character
        </label>
      </div>`,
    },
    () => performDeleteCharacter(id),
  );
}

async function performDeleteCharacter(id) {
  const deleteConversations = document.getElementById("delete-conversations-checkbox")?.checked || false;
  const url = `/characters/${id}${deleteConversations ? "?delete_conversations=true" : ""}`;
  try {
    await api.del(url);
    if (S.activeCharId === id) resetChatUI();
    await loadCharacters();
    await loadConversations();
    closeModal();
    toast("Deleted");
  } catch (e) {
    toast(e.message, true);
  }
}

export function addAltGreeting(prefix) {
  const container = $(`${prefix}-ag-list`);
  if (!container) return;
  const row = document.createElement("div");
  row.className = "alt-greeting-row";
  row.innerHTML = `<textarea rows="3"></textarea><button class="btn btn-sm" onclick="this.parentElement.remove()" title="Remove">✕</button>`;
  container.appendChild(row);
}

function _readAltGreetings(prefix) {
  const container = $(`${prefix}-ag-list`);
  if (!container) return [];
  return [...container.querySelectorAll("textarea")].map((t) => t.value.trim()).filter(Boolean);
}

export function triggerAvatarCrop(prefix, _cardId) {
  showCropModal(({ b64, mime }) => {
    _pendingAvatar = { b64, mime };
    const el = $(`${prefix}-avatar-preview`);
    if (el) el.innerHTML = `<img src="data:${mime};base64,${b64}">`;
  });
}

export function exportCharacter(id, name) {
  downloadBlob(`${name || "character"}.png`, `/api/characters/${id}/export`);
}

export async function handleExpressionsZip(inp, id) {
  const f = inp.files[0];
  if (!f) return;
  inp.value = "";
  const status = inp.parentElement.parentElement.querySelector('[id$="-expr-status"]');
  if (status) status.textContent = "Uploading…";
  try {
    const r = await api.upload(`/characters/${id}/expressions`, f);
    if (status) status.textContent = `${r.labels.length} expressions loaded`;
  } catch (e) {
    if (status) status.textContent = `Error: ${e.message}`;
  }
}

export async function clearExpressions(id) {
  try {
    await api.del(`/characters/${id}/expressions`);
    const status = document.querySelector('[id$="-expr-status"]');
    if (status) status.textContent = "Cleared";
  } catch (e) {
    toast(e.message, true);
  }
}

const PUBLIC_APPEARANCE_PLACEHOLDER = "e.g. Tall, silver-haired, moves with a soldier's stiffness";
const PUBLIC_ROLE_PLACEHOLDER = "e.g. The caravan's hired scout, and the only one who knows the pass";

function charFormTabs(prefix, d, isEdit, worlds = []) {
  const publicProfile = d.extensions?.orb?.public_profile || {};
  const agHtml = (d.alternate_greetings || [])
    .map(
      (g) => `
    <div class="alt-greeting-row">
      <textarea rows="3">${esc(g)}</textarea>
      <button class="btn btn-sm" onclick="this.parentElement.remove()" title="Remove">✕</button>
    </div>`,
    )
    .join("");

  const noneLabel = !d.world_id && d.character_book ? `(Import from embedded lorebook)` : `(None)`;
  const worldOptions = worlds
    .map((w) => `<option value="${esc(w.id)}" ${d.world_id === w.id ? "selected" : ""}>${esc(w.name)}</option>`)
    .join("");

  return `
    <div class="tabs">
      <div class="tab active" onclick="switchTab(this,'${prefix}-tp')">Persona</div>
      <div class="tab" onclick="switchTab(this,'${prefix}-tm')">Messages</div>
      ${isEdit ? `<div class="tab" onclick="switchTab(this,'${prefix}-ta')">Advanced</div>` : ""}
      ${isEdit ? `<div class="tab" id="${prefix}-tab-frag">Fragments</div>` : ""}
    </div>
    <div id="${prefix}-tp" class="tab-content active">
      <div class="field"><label>Description</label><textarea id="${prefix}-desc" rows="8">${esc(d.description || "")}</textarea></div>
      <div class="field"><label>Personality</label><textarea id="${prefix}-personality" rows="3">${esc(d.personality || "")}</textarea></div>
      <div class="field"><label>Scenario</label><textarea id="${prefix}-scenario" rows="3">${esc(d.scenario || "")}</textarea></div>
    </div>
    <div id="${prefix}-tm" class="tab-content">
      <div class="field"><label>First Message</label><textarea id="${prefix}-first-mes" rows="5">${esc(d.first_mes || "")}</textarea></div>
      <div class="field"><label>Example Messages</label><textarea id="${prefix}-mes-example" rows="4">${esc(d.mes_example || "")}</textarea></div>
      <div class="field">
        <label>Alternate Greetings</label>
        <div id="${prefix}-ag-list">${agHtml}</div>
        <button class="btn btn-sm" style="margin-top:4px" onclick="addAltGreeting('${prefix}')">+ Add</button>
      </div>
    </div>
    ${
      isEdit
        ? `
    <div id="${prefix}-tf" class="tab-content">
      <div class="card-frag-hint">
        These fragments travel with the card (and its exported PNG). Merged into the Interactive and Mood fragment lists; 
        global fragments win on ID collision.
      </div>
      <div id="ce-card-frag-list"></div>
      <div class="card-frag-actions">
        <button class="btn btn-sm" id="ce-card-frag-add-interactive">+ Interactive</button>
        <button class="btn btn-sm" id="ce-card-frag-add-mood">+ Mood</button>
      </div>
    </div>
    <div id="${prefix}-ta" class="tab-content">
      <div class="field">
        <label>Tags</label>
        <div class="lb-chip-wrap" id="${prefix}-tag-wrap" onclick="document.getElementById('${prefix}-tag-text')?.focus()"></div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">, or Enter to add · Backspace to remove</div>
      </div>
      <div class="field"><label>Linked Lorebook</label>
        <select id="${prefix}-world-id">
          <option value="">${noneLabel}</option>
          ${worldOptions}
        </select>
      </div>
      <div class="field"><label>Creator's Note</label><textarea id="${prefix}-creator-notes" rows="1">${esc(d.creator_notes || "")}</textarea></div>
      <div class="field"><label>System Prompt Override</label><textarea id="${prefix}-sysprompt" rows="1">${esc(d.system_prompt || "")}</textarea></div>
      <div class="field"><label>Post-History Instructions</label><textarea id="${prefix}-posthist" rows="1">${esc(d.post_history_instructions || "")}</textarea></div>
      <div class="form-divider">Group chat</div>
      <div class="field"><label>Public cast appearance</label><textarea id="${prefix}-public-appearance" rows="2" placeholder="${escAttr(PUBLIC_APPEARANCE_PLACEHOLDER)}">${esc(publicProfile.appearance || "")}</textarea></div>
      <div class="field"><label>Public cast role</label><textarea id="${prefix}-public-role" rows="2" placeholder="${escAttr(PUBLIC_ROLE_PLACEHOLDER)}">${esc(publicProfile.role || "")}</textarea></div>
      ${d.id ? `<button type="button" class="btn btn-sm" id="${prefix}-generate-public-profile">Generate editable draft</button><div class="modal-hint">Only these confirmed public fields enter a group's shared cast prompt.</div>` : ""}
      ${
        d.id
          ? `<div class="form-divider">Expression images</div>
      <div class="field">
        <input type="file" id="${prefix}-expr-zip" accept=".zip" style="display:none" onchange="handleExpressionsZip(this, '${d.id}')">
        <div>
          <button class="btn btn-sm" onclick="document.getElementById('${prefix}-expr-zip').click()">Upload .zip</button>
          <button class="btn btn-sm" onclick="clearExpressions('${d.id}')">Clear</button>
        </div>
        <div id="${prefix}-expr-status" style="font-size:11px;color:var(--text-muted);margin-top:4px"></div>
      </div>`
          : ""
      }
      ${
        d.character_book
          ? `<div style="font-size:11px;color:var(--text-muted);margin-top:8px">Imported card contains an embedded lorebook (${(d.character_book.entries || []).length} entries). It will be imported as a new lorebook unless you select one above.</div>`
          : ""
      }
    </div>`
        : ""
    }
    `;
}

export function showCharCreateModal() {
  _pendingAvatar = null;
  showModal(`
    <div class="modal-char-header">
      <div id="cc-avatar-preview" class="char-avatar-lg" onclick="triggerAvatarCrop('cc')"
           title="Click to set avatar" style="cursor:pointer">${NO_AVATAR_ICON}</div>
      <div style="flex:1">
        <div class="field" style="margin-bottom:4px">
          <input id="cc-name" placeholder="Character name…" style="font-size:18px;font-weight:600;width:100%">
        </div>
        <div class="modal-char-hint">New character · click portrait to set avatar</div>
      </div>
    </div>
    ${charFormTabs("cc", {}, false)}
    <div class="modal-actions">
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="createCharacter()">Create</button>
    </div>`);
}

function _validateCharForm(prefix, { advanced = false } = {}) {
  const checks = [
    validate.validateCharacterName($(`${prefix}-name`).value),
    validate.validateCharacterField($(`${prefix}-desc`).value, "Description"),
    validate.validateCharacterField($(`${prefix}-personality`).value, "Personality"),
    validate.validateCharacterField($(`${prefix}-scenario`).value, "Scenario"),
    validate.validateCharacterField($(`${prefix}-first-mes`).value, "First message"),
    validate.validateCharacterField($(`${prefix}-mes-example`).value, "Example messages"),
  ];
  if (advanced) {
    checks.push(
      validate.validateCharacterAdvancedField($(`${prefix}-sysprompt`).value, "System prompt"),
      validate.validateCharacterAdvancedField($(`${prefix}-posthist`).value, "Post-history instructions"),
    );
  }
  checks.push(validate.validateAlternateGreetings(_readAltGreetings(prefix)));
  return checks.find((c) => !c.valid) || { valid: true };
}

function _readCharEditForm() {
  const ext = structuredClone(_pendingExtensions || {});
  const frags = readCardFragments();
  if (frags && (frags.mood.length || frags.interactive.length)) {
    ext.orb = { ...(ext.orb || {}), fragments: frags };
  } else if (ext.orb) {
    delete ext.orb.fragments;
    if (!Object.keys(ext.orb).length) delete ext.orb;
  }
  const appearance = $("ce-public-appearance")?.value.trim() || "";
  const role = $("ce-public-role")?.value.trim() || "";
  if (appearance || role) {
    ext.orb = { ...(ext.orb || {}), public_profile: { appearance, role } };
  } else if (ext.orb) {
    delete ext.orb.public_profile;
    if (!Object.keys(ext.orb).length) delete ext.orb;
  }
  return {
    name: $("ce-name").value.trim(),
    description: $("ce-desc").value.trim(),
    personality: $("ce-personality").value.trim(),
    scenario: $("ce-scenario").value.trim(),
    first_mes: $("ce-first-mes").value.trim(),
    mes_example: $("ce-mes-example").value.trim(),
    creator_notes: $("ce-creator-notes").value.trim(),
    system_prompt: $("ce-sysprompt").value.trim(),
    post_history_instructions: $("ce-posthist").value.trim(),
    tags: _pendingTags || [],
    alternate_greetings: _readAltGreetings("ce"),
    world_id: $("ce-world-id")?.value || null,
    extensions: ext,
  };
}

export async function createCharacter() {
  const validation = _validateCharForm("cc");
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  const name = $("cc-name").value.trim();

  try {
    const payload = {
      name: name,
      description: $("cc-desc").value.trim(),
      personality: $("cc-personality").value.trim(),
      scenario: $("cc-scenario").value.trim(),
      first_mes: $("cc-first-mes").value.trim(),
      mes_example: $("cc-mes-example").value.trim(),
      alternate_greetings: _readAltGreetings("cc"),
    };
    if (_pendingAvatar) {
      payload.avatar_b64 = _pendingAvatar.b64;
      payload.avatar_mime = _pendingAvatar.mime;
    }
    _pendingAvatar = null;
    const _created = await api.post("/characters", payload);
    closeModal();
    await loadCharacters();
    toast("Created");
  } catch (e) {
    toast(e.message, true);
  }
}

const _charTagChips = createChipInput({
  wrapId: "ce-tag-wrap",
  inputId: "ce-tag-text",
  placeholder: "Add tag…",
  getItems: () => _pendingTags || [],
  setItems: (v) => {
    _pendingTags = v;
  },
});

export async function showCharEditModal(idOrData) {
  _pendingAvatar = null;
  const isNew = typeof idOrData === "object";
  const c = isNew ? idOrData : await api.get(`/characters/${idOrData}`);

  let av;
  if (isNew && c.avatar_b64) {
    _pendingAvatar = { b64: c.avatar_b64, mime: c.avatar_mime || "image/png" };
    _pendingImportId = c.id || null;
    _pendingImportSourceFormat = c.source_format || null;
    av = `<img src="data:${_pendingAvatar.mime};base64,${_pendingAvatar.b64}">`;
  } else {
    const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
    av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "");
  }

  _pendingTags = c.tags || [];
  _pendingCharacterBook = isNew ? c.character_book || null : null;
  _pendingExtensions = structuredClone(c.extensions || {});
  initCardFragments(_pendingExtensions.orb?.fragments);

  const tags = (c.tags || []).map((t) => `<span class="char-tag">${esc(t)}</span>`).join("");

  let worlds = [];
  try {
    worlds = await api.get("/worlds");
  } catch (e) {
    console.error("Failed to load worlds:", e);
  }

  showModal(`
    <div class="modal-char-header">
      <div id="ce-avatar-preview" class="char-avatar-lg" onclick="triggerAvatarCrop('ce')"
           title="Click to change avatar" style="cursor:pointer">${av}</div>
      <div style="flex:1">
        <div class="field" style="margin-bottom:4px">
          <input id="ce-name" value="${esc(c.name)}" style="font-size:18px;font-weight:600;width:100%">
        </div>
        ${c.creator ? `<div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">by ${esc(c.creator)}</div>` : ""}
        ${tags ? `<div class="char-tags">${tags}</div>` : ""}
      </div>
    </div>
    ${charFormTabs("ce", c, true, worlds)}
    <div class="modal-actions">
      ${!isNew ? `<button class="btn btn-danger btn-sm" onclick="deleteCharacter('${c.id}')">Delete</button>` : ""}
      <div style="flex:1"></div>
      ${!isNew ? `<button class="btn btn-sm" onclick="saveCharEdit('${c.id}', true)">Export PNG</button>` : ""}
      <button class="btn" onclick="closeModal()">Cancel</button>
      ${
        isNew
          ? `<button class="btn btn-accent" onclick="saveImportedChar()">Save</button>`
          : `<button class="btn btn-accent" onclick="saveCharEdit('${c.id}')">Save</button>`
      }
    </div>`);
  _charTagChips.render();
  renderCardFragmentsTab();
  $("ce-tab-frag")?.addEventListener("click", (e) => switchTab(e.currentTarget, "ce-tf"));
  $("ce-card-frag-add-mood")?.addEventListener("click", () => showCardMoodFragmentModal());
  $("ce-card-frag-add-interactive")?.addEventListener("click", () => showCardInteractiveFragmentModal());
  $("ce-generate-public-profile")?.addEventListener("click", async () => {
    try {
      const draft = await api.post(`/characters/${c.id}/public-profile/generate`);
      $("ce-public-appearance").value = draft.appearance || "";
      $("ce-public-role").value = draft.role || "";
      toast("Draft ready — review it, then save the card");
    } catch (error) {
      toast(error.message, true);
    }
  });
  if (c.id) {
    api
      .get(`/characters/${c.id}/expressions`)
      .then((r) => {
        const status = document.getElementById("ce-expr-status");
        if (status) status.textContent = `${r.labels.length} expressions`;
      })
      .catch(() => {});
  }
}

export async function saveCharEdit(id, exportAfter = false) {
  const validation = _validateCharForm("ce", { advanced: true });
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }

  const d = _readCharEditForm();
  const name = d.name;
  if (_pendingAvatar) {
    d.avatar_b64 = _pendingAvatar.b64;
    d.avatar_mime = _pendingAvatar.mime;
  }
  const avatarChanged = !!_pendingAvatar;
  _pendingAvatar = null;
  try {
    const updated = await api.put(`/characters/${id}`, d);
    if (S.activeCharId === id) stashCardFragments(updated);
    else if ((S.groupCast?.members || []).some((member) => member.character_card_id === id)) {
      await refreshSceneCardFragments();
    }
    if (avatarChanged) {
      _avatarBust.set(id, Date.now());
      if (S.activeCharId === id) {
        const av = document.getElementById("chat-avatar");
        if (av) av.innerHTML = avatarCell(`${avatarUrl(id)}?v=${_avatarBust.get(id)}`, { icon: CHAT_AVATAR_ICON });
      }
    }
    closeModal();
    await loadCharacters();
    await loadConversations();
    const activeConv = S.conversations.find((c) => c.id === S.activeConvId);
    if (activeConv && activeConv.character_card_id === id) {
      const titleEl = document.getElementById("chat-title-text");
      if (titleEl) titleEl.textContent = activeConv.title || activeConv.character_name || "";
    }
    toast("Saved");
    if (exportAfter) exportCharacter(id, name);
  } catch (e) {
    toast(e.message, true);
  }
}

export async function saveImportedChar() {
  const validation = _validateCharForm("ce", { advanced: true });
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }

  const d = _readCharEditForm();
  if (_pendingAvatar) {
    d.avatar_b64 = _pendingAvatar.b64;
    d.avatar_mime = _pendingAvatar.mime;
  }
  if (_pendingImportId) d.id = _pendingImportId;
  if (_pendingImportSourceFormat) d.source_format = _pendingImportSourceFormat;
  if (_pendingCharacterBook && !d.world_id) d.character_book = _pendingCharacterBook;
  _pendingAvatar = null;
  _pendingImportId = null;
  _pendingImportSourceFormat = null;
  _pendingTags = null;
  _pendingCharacterBook = null;
  _pendingExtensions = null;
  try {
    const created = await api.post("/characters", d);
    await Promise.all([loadCharacters(), loadWorlds()]);
    closeModal();
    toast(`Imported "${created.name}"`);
  } catch (e) {
    if (e.status === 409) toast("Character already in your library", true);
    else toast(e.message, true);
  }
}
