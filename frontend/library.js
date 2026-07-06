// Character library entrypoint. Mood/interactive fragments and the character
// browser modal were split into library_fragments.js and library_browser.js;
// this file keeps character CRUD (create / import / edit / delete / export), the
// recent-characters sidebar, and the shared avatar cache-bust map, and
// re-exports the two sub-modules so "./library.js" stays the stable import path
// for app.js and chat modules.
import { api } from "./api.js";
import { loadConversations, resetChatUI } from "./chat.js";
import { loadWorlds } from "./lorebooks.js";
import { closeModal, showConfirmModal, showCropModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, avatarCell, avatarUrl, CHAT_AVATAR_ICON, convActivity, esc, NO_AVATAR_ICON, toast } from "./utils.js";
import { validate } from "./validate.js";

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

// Pending avatar for the character create modal (cleared on submit or cancel)
let _pendingAvatar = null;
// Stable ID and source format carried over from an imported card (cleared on submit)
let _pendingImportId = null;
let _pendingImportSourceFormat = null;
let _pendingTags = null;
// Embedded character_book from an imported PNG (cleared on submit)
let _pendingCharacterBook = null;
// Per-card cache-bust timestamps so the browser re-fetches updated avatars.
// Shared with library_browser.js (read-only there) for its card thumbnails.
export const _avatarBust = new Map();

// ── Characters

/**
 * Build a sorted list of characters showing only the top N most recently
 * talked-to characters (ordered by most recent conversation time).
 * Characters without conversations are sorted by their last update time
 * and included only if there are fewer than `limit` characters total.
 */
function filterRecentCharacters(characters, conversations, limit = 5) {
  // Map each character_card_id to its most recent conversation timestamp
  const recentMap = new Map();
  for (const conv of conversations) {
    const cardId = conv.character_card_id;
    if (!cardId) continue;
    const ts = convActivity(conv);
    const existing = recentMap.get(cardId);
    if (!existing || ts > existing) {
      recentMap.set(cardId, ts);
    }
  }

  // Tag each character with its "activity" timestamp for sorting
  const tagged = characters.map((char) => {
    const convTime = recentMap.get(char.id);
    const activityTime = convTime || char.updated_at || char.created_at || "";
    return { char, activityTime, hasConversation: !!convTime };
  });

  // Sort by activity time descending (conversations beat updates)
  tagged.sort((a, b) => b.activityTime.localeCompare(a.activityTime));

  // Return only the top N
  return tagged.slice(0, limit).map((t) => t.char);
}

export async function loadCharacters() {
  const [characters, conversations] = await Promise.all([
    api.get("/characters"),
    S.conversations || api.get("/conversations"),
  ]);
  // Store full list for efficient refreshes
  S.allCharacters = characters;
  S.characters = filterRecentCharacters(characters, conversations || []);
  renderCharacters();
}

/**
 * Efficiently refresh the character list by re-filtering from the full character
 * set using already-loaded conversations (no API calls). Called after sending a
 * message to promote the active character to the top of the recent list.
 */
export function refreshCharacters() {
  const source = S.allCharacters || S.characters;
  if (!source || source.length === 0) return;
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
      <div class="char-avatar-sm${c.has_expressions ? " has-expr-halo" : ""}">${av}</div>
      <div class="char-item-info">
        <div class="char-item-name">${esc(c.name)}</div>
        <div class="char-item-meta">${meta}</div>
      </div>
      <div class="char-item-actions">
        <button onclick="event.stopPropagation();showCharEditModal('${c.id}')" title="Edit character">✏</button>
        <button class="del-btn" onclick="event.stopPropagation();deleteCharacter('${c.id}')">✕</button>
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
    toast("Import failed: " + e.message, true);
  }
}

export async function deleteCharacter(id) {
  const charName = (S.allCharacters || S.characters || []).find((c) => c.id === id)?.name;
  showConfirmModal(
    {
      title: "Delete Character",
      message: `Are you sure you want to delete ${charName ? `"${charName}"` : "this character card"}?`,
      confirmText: "Delete",
      extraHtml: `
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
  const url = "/characters/" + id + (deleteConversations ? "?delete_conversations=true" : "");
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

// ── Alternate greetings helpers (used by both create and edit modals)

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

// ── Avatar crop helpers

export function triggerAvatarCrop(prefix, cardId) {
  // TODO: unused param cardId
  showCropModal(({ b64, mime }) => {
    _pendingAvatar = { b64, mime };
    const el = $(`${prefix}-avatar-preview`);
    if (el) el.innerHTML = `<img src="data:${mime};base64,${b64}">`;
  });
}

// ── Export

export function exportCharacter(id, name) {
  const a = document.createElement("a");
  a.href = `/api/characters/${id}/export`;
  a.download = (name || "character") + ".png";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ── Expression images (uploaded per character, shown in the avatar popup)

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
    if (status) status.textContent = "Error: " + e.message;
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

// ── Shared tab template for create / edit modals
function charFormTabs(prefix, d, isEdit, worlds = []) {
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
      <div class="tab" onclick="switchTab(this,'${prefix}-ts')">Scenario</div>
      <div class="tab" onclick="switchTab(this,'${prefix}-tm')">Messages</div>
      ${isEdit ? `<div class="tab" onclick="switchTab(this,'${prefix}-ta')">Advanced</div>` : ""}
    </div>
    <div id="${prefix}-tp" class="tab-content active">
      <div class="field"><label>Description</label><textarea id="${prefix}-desc" rows="5">${esc(d.description || "")}</textarea></div>
      <div class="field"><label>Personality</label><textarea id="${prefix}-personality" rows="4">${esc(d.personality || "")}</textarea></div>
    </div>
    <div id="${prefix}-ts" class="tab-content">
      <div class="field"><label>Scenario</label><textarea id="${prefix}-scenario" rows="7">${esc(d.scenario || "")}</textarea></div>
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
      ${
        d.id
          ? `<div class="field">
        <label>Expression Images</label>
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

export async function createCharacter() {
  const name = $("cc-name").value.trim();
  const validation = validate.validateCharacterName(name);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }

  const descValidation = validate.validateCharacterField($("cc-desc").value, "Description");
  if (!descValidation.valid) {
    toast(descValidation.error, true);
    return;
  }
  const personalityValidation = validate.validateCharacterField($("cc-personality").value, "Personality");
  if (!personalityValidation.valid) {
    toast(personalityValidation.error, true);
    return;
  }
  const scenarioValidation = validate.validateCharacterField($("cc-scenario").value, "Scenario");
  if (!scenarioValidation.valid) {
    toast(scenarioValidation.error, true);
    return;
  }
  const firstMesValidation = validate.validateCharacterField($("cc-first-mes").value, "First message");
  if (!firstMesValidation.valid) {
    toast(firstMesValidation.error, true);
    return;
  }
  const mesExampleValidation = validate.validateCharacterField($("cc-mes-example").value, "Example messages");
  if (!mesExampleValidation.valid) {
    toast(mesExampleValidation.error, true);
    return;
  }
  const greetingsValidation = validate.validateAlternateGreetings(_readAltGreetings("cc"));
  if (!greetingsValidation.valid) {
    toast(greetingsValidation.error, true);
    return;
  }

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
    const created = await api.post("/characters", payload);
    closeModal();
    await loadCharacters();
    toast("Created");
  } catch (e) {
    toast(e.message, true);
  }
}

// ── Character tag chip helpers
function _renderCharTagChips(prefix) {
  const wrap = document.getElementById(`${prefix}-tag-wrap`);
  if (!wrap) return;
  const tags = _pendingTags || [];
  wrap.innerHTML =
    tags
      .map(
        (t, i) =>
          `<span class="lb-chip">${esc(t)}<button class="lb-chip-remove" onclick="charTagRemoveChip(${i})">×</button></span>`,
      )
      .join("") +
    `<input id="${prefix}-tag-text" class="lb-chip-text" placeholder="${tags.length ? "" : "Add tag…"}" onkeydown="charTagKeydown(event)" oninput="charTagInput(this)">`;
}

export function charTagKeydown(e) {
  const input = e.target;
  if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
    e.preventDefault();
    const val = input.value.replace(/,$/, "").trim();
    if (val && !_pendingTags.includes(val)) {
      _pendingTags = [..._pendingTags, val];
      _renderCharTagChips("ce");
      setTimeout(() => document.getElementById("ce-tag-text")?.focus(), 0);
    }
    return;
  }
  if (e.key === "Backspace" && !input.value && _pendingTags.length) {
    _pendingTags = _pendingTags.slice(0, -1);
    _renderCharTagChips("ce");
    setTimeout(() => document.getElementById("ce-tag-text")?.focus(), 0);
  }
}

export function charTagInput(input) {
  if (input.value.endsWith(",")) {
    const val = input.value.slice(0, -1).trim();
    if (val && !_pendingTags.includes(val)) {
      _pendingTags = [..._pendingTags, val];
      _renderCharTagChips("ce");
      setTimeout(() => document.getElementById("ce-tag-text")?.focus(), 0);
    } else {
      input.value = "";
    }
  }
}

export function charTagRemoveChip(i) {
  _pendingTags = _pendingTags.filter((_, j) => j !== i);
  _renderCharTagChips("ce");
  setTimeout(() => document.getElementById("ce-tag-text")?.focus(), 0);
}

export async function showCharEditModal(idOrData) {
  _pendingAvatar = null;
  const isNew = typeof idOrData === "object";
  const c = isNew ? idOrData : await api.get("/characters/" + idOrData);

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

  if (isNew) {
    _pendingTags = c.tags || [];
    _pendingCharacterBook = c.character_book || null;
    console.log("showCharEditModal import tags:", c.tags, "pending:", _pendingTags);
  } else {
    _pendingTags = c.tags || [];
    _pendingCharacterBook = null;
    console.log("showCharEditModal edit tags:", c.tags, "pending:", _pendingTags);
  }

  const tags = (c.tags || []).map((t) => `<span class="char-tag">${esc(t)}</span>`).join("");

  // Load worlds for the lorebook selector
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
  _renderCharTagChips("ce");
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
  const name = $("ce-name").value.trim();
  const nameValidation = validate.validateCharacterName(name);
  if (!nameValidation.valid) {
    toast(nameValidation.error, true);
    return;
  }

  const descValidation = validate.validateCharacterField($("ce-desc").value, "Description");
  if (!descValidation.valid) {
    toast(descValidation.error, true);
    return;
  }
  const personalityValidation = validate.validateCharacterField($("ce-personality").value, "Personality");
  if (!personalityValidation.valid) {
    toast(personalityValidation.error, true);
    return;
  }
  const scenarioValidation = validate.validateCharacterField($("ce-scenario").value, "Scenario");
  if (!scenarioValidation.valid) {
    toast(scenarioValidation.error, true);
    return;
  }
  const firstMesValidation = validate.validateCharacterField($("ce-first-mes").value, "First message");
  if (!firstMesValidation.valid) {
    toast(firstMesValidation.error, true);
    return;
  }
  const mesExampleValidation = validate.validateCharacterField($("ce-mes-example").value, "Example messages");
  if (!mesExampleValidation.valid) {
    toast(mesExampleValidation.error, true);
    return;
  }
  const syspromptValidation = validate.validateCharacterAdvancedField($("ce-sysprompt").value, "System prompt");
  if (!syspromptValidation.valid) {
    toast(syspromptValidation.error, true);
    return;
  }
  const posthistValidation = validate.validateCharacterAdvancedField(
    $("ce-posthist").value,
    "Post-history instructions",
  );
  if (!posthistValidation.valid) {
    toast(posthistValidation.error, true);
    return;
  }
  const greetingsValidation = validate.validateAlternateGreetings(_readAltGreetings("ce"));
  if (!greetingsValidation.valid) {
    toast(greetingsValidation.error, true);
    return;
  }

  const d = {
    name,
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
  };
  console.log("saveCharEdit payload:", d);
  if (_pendingAvatar) {
    d.avatar_b64 = _pendingAvatar.b64;
    d.avatar_mime = _pendingAvatar.mime;
  }
  const avatarChanged = !!_pendingAvatar;
  _pendingAvatar = null;
  try {
    await api.put("/characters/" + id, d);
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
    // If the active conversation belongs to this character, refresh its title
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
  console.log("saveImportedChar pendingTags:", _pendingTags);
  const name = $("ce-name").value.trim();
  const nameValidation = validate.validateCharacterName(name);
  if (!nameValidation.valid) {
    toast(nameValidation.error, true);
    return;
  }

  const descValidation = validate.validateCharacterField($("ce-desc").value, "Description");
  if (!descValidation.valid) {
    toast(descValidation.error, true);
    return;
  }
  const personalityValidation = validate.validateCharacterField($("ce-personality").value, "Personality");
  if (!personalityValidation.valid) {
    toast(personalityValidation.error, true);
    return;
  }
  const scenarioValidation = validate.validateCharacterField($("ce-scenario").value, "Scenario");
  if (!scenarioValidation.valid) {
    toast(scenarioValidation.error, true);
    return;
  }
  const firstMesValidation = validate.validateCharacterField($("ce-first-mes").value, "First message");
  if (!firstMesValidation.valid) {
    toast(firstMesValidation.error, true);
    return;
  }
  const mesExampleValidation = validate.validateCharacterField($("ce-mes-example").value, "Example messages");
  if (!mesExampleValidation.valid) {
    toast(mesExampleValidation.error, true);
    return;
  }
  const syspromptValidation = validate.validateCharacterAdvancedField($("ce-sysprompt").value, "System prompt");
  if (!syspromptValidation.valid) {
    toast(syspromptValidation.error, true);
    return;
  }
  const posthistValidation = validate.validateCharacterAdvancedField(
    $("ce-posthist").value,
    "Post-history instructions",
  );
  if (!posthistValidation.valid) {
    toast(posthistValidation.error, true);
    return;
  }
  const greetingsValidation = validate.validateAlternateGreetings(_readAltGreetings("ce"));
  if (!greetingsValidation.valid) {
    toast(greetingsValidation.error, true);
    return;
  }

  const d = {
    name,
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
  };
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
  try {
    const created = await api.post("/characters", d);
    closeModal();
    await Promise.all([loadCharacters(), loadWorlds()]);
    toast(`Imported "${created.name}"`);
  } catch (e) {
    if (e.status === 409) toast("Character already in your library", true);
    else toast(e.message, true);
  }
}
