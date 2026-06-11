// User profile + persona management: the user button, the personas list modal,
// and persona create / edit / delete / activate. Split out of settings.js; the
// public surface is re-exported from settings.js.
import { api } from "./api.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, esc, escAttr, toast } from "./utils.js";
import { validate } from "./validate.js";

export async function loadPersonas() {
  try {
    S.personas = await api.get("/user-personas");
  } catch (e) {
    console.error("Failed to load personas:", e);
    S.personas = [];
  }
}

// Persona-lock glyphs. The user button borrows the character-lock glyph when
// the active persona is paired with the open character; 💬/💏 also label the
// per-scope lock buttons and modal subtitle below.
const PERSONA_ICON = "👤";
const CONV_LOCK_ICON = "💬";
const CHAR_LOCK_ICON = "💏";

// ── User Profile
export function updateUserBtn() {
  let displayName = "User";
  if (S.activePersonaId && S.personas.length) {
    const activePersona = S.personas.find((p) => p.id === S.activePersonaId);
    if (activePersona) displayName = activePersona.name;
  }
  // Glyph reflects the lock that's actually in force for the active persona.
  // Conversation lock wins over character lock, matching applyPersonaLock.
  const { conv, card } = activeLockContext();
  const lockedToConv = !!S.activePersonaId && conv?.persona_lock_id === S.activePersonaId;
  const lockedToChar = !!S.activePersonaId && card?.persona_lock_id === S.activePersonaId;
  const glyph = lockedToConv ? CONV_LOCK_ICON : lockedToChar ? CHAR_LOCK_ICON : PERSONA_ICON;
  const label = glyph + " " + displayName;
  $("user-profile-btn").textContent = label;
  const mobileBtn = $("mobile-user-profile-btn");
  if (mobileBtn) mobileBtn.textContent = label;
}

// The active conversation / character card a persona lock would attach to.
// The card lookup goes through S.allCharacters: S.characters is the
// recent-filtered subset and may not contain the active card.
export function activeLockContext() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  const card = conv?.character_card_id ? (S.allCharacters || []).find((c) => c.id === conv.character_card_id) : null;
  const charName = conv?.character_name || card?.name || "";
  return { conv, card, charName };
}

export function showUserModal() {
  const { conv, card, charName } = activeLockContext();
  // A lock in force on the open conversation or character pins one persona.
  // While it holds, selecting or locking any *other* persona is blocked so the
  // existing turns keep their author — the user must unlock first. The pinned
  // persona's own row stays interactive so it can be unlocked.
  const lockedIds = lockedPersonaIds(conv, card);
  const hasLock = lockedIds.size > 0;
  const personaItems = S.personas
    .map((p) => {
      const isActive = p.id === S.activePersonaId;
      const avatarColor = p.avatar_color || "#E1F5EE";
      const avatarTextColor = isActive ? "var(--accent)" : "#085041";
      const avatarBg = isActive ? "var(--accent-glow)" : avatarColor;
      const initials = p.name.charAt(0).toUpperCase();
      const convLocked = !!conv && conv.persona_lock_id === p.id;
      const charLocked = !!card && card.persona_lock_id === p.id;
      const blocked = hasLock && !lockedIds.has(p.id);
      const convTitle = blocked
        ? "Unlock the pinned persona first"
        : conv
          ? convLocked
            ? "Unlock from this conversation"
            : "Lock to this conversation"
          : "Open a conversation to enable";
      const charTitle = blocked
        ? "Unlock the pinned persona first"
        : card
          ? charLocked
            ? `Unlock from ${escAttr(charName)}`
            : `Lock to ${escAttr(charName)}`
          : "Only available for saved characters";
      return `
      <div class="persona-item${isActive ? " persona-item-active" : ""}${blocked ? " persona-item-locked" : ""}" onclick="activatePersona(${p.id})">
        <div class="persona-avatar" style="background:${avatarBg};color:${avatarTextColor}">${initials}</div>
        <div class="persona-info">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="persona-name">${esc(p.name)}</span>
            ${isActive ? '<span class="persona-active-badge">Active</span>' : ""}
          </div>
          <span class="persona-desc">${esc(p.description || "")}</span>
        </div>
        <div class="persona-lock-btns">
          <button class="persona-lock-btn${convLocked ? " locked" : ""}" ${conv && !blocked ? "" : "disabled"} title="${convTitle}"
            onclick="event.stopPropagation();setPersonaConversationLock(${p.id}, ${!convLocked})">${CONV_LOCK_ICON}</button>
          <button class="persona-lock-btn${charLocked ? " locked" : ""}" ${card && !blocked ? "" : "disabled"} title="${charTitle}"
            onclick="event.stopPropagation();setPersonaCharacterLock(${p.id}, ${!charLocked})">${CHAR_LOCK_ICON}</button>
        </div>
        <button class="btn btn-sm" onclick="event.stopPropagation();editPersona(${p.id})">Edit</button>
      </div>
    `;
    })
    .join("");

  const warning = hasLock ? `<p class="persona-lock-warning">⚠️ ${esc(lockWarningText(conv, card, charName))}</p>` : "";

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>User personas</h2>
        <p class="modal-subtitle">${CONV_LOCK_ICON} lock to conversation, ${CHAR_LOCK_ICON} to character — locked personas activate on chat open.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn" onclick="showPersonaEditModal(null)">+ New persona</button>
      </div>
    </div>
    ${warning}
    <div class="persona-list">
      ${personaItems.length ? personaItems : '<p class="modal-subtitle" style="text-align:center;padding:1rem 0">No personas yet. Create one to get started.</p>'}
    </div>
  `);
}

// Persona id(s) pinned in the open scope(s). A conversation and its character
// can in principle hold locks on different personas, so this is a set; usually
// it's empty or a single id.
function lockedPersonaIds(conv, card) {
  const ids = new Set();
  if (conv?.persona_lock_id) ids.add(conv.persona_lock_id);
  if (card?.persona_lock_id) ids.add(card.persona_lock_id);
  return ids;
}

// Human-readable summary of which persona is pinned where, for the modal banner
// and the blocked-switch toast. Caller escapes the result before injecting it.
function lockWarningText(conv, card, charName) {
  const named = (id) => `"${S.personas.find((p) => p.id === id)?.name || "A persona"}"`;
  const convId = conv?.persona_lock_id || null;
  const cardId = card?.persona_lock_id || null;
  const where = charName || "this character";
  let scope;
  if (convId && cardId && convId === cardId) scope = `${named(convId)} is locked to this conversation and ${where}`;
  else if (convId && cardId) scope = `${named(convId)} is locked to this conversation and ${named(cardId)} to ${where}`;
  else if (convId) scope = `${named(convId)} is locked to this conversation`;
  else scope = `${named(cardId)} is locked to ${where}`;
  return `${scope}. Unlock first to select or lock another persona.`;
}

export async function saveUserProfile() {
  const name = $("user-name-input").value.trim();
  const desc = $("user-desc-input").value.trim();
  const validation = validate.validateUserProfile(name, desc);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    S.settings = await api.put("/settings", { user_name: name || "User", user_description: desc });
    updateUserBtn();
    closeModal();
    toast("User profile saved");
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export function showPersonaEditModal(personaId) {
  const persona = personaId ? S.personas.find((p) => p.id === personaId) : null;
  const isEdit = persona !== null;
  showModal(`
    <h2>${isEdit ? "Edit persona" : "New persona"}</h2>
    <div class="field">
      <label>Name</label>
      <input id="persona-name-input" type="text" placeholder="e.g. Kai" value="${esc(persona?.name || "")}">
    </div>
    <div class="field">
      <label>Description <span style="font-weight:400;text-transform:none;letter-spacing:0">(injected into system prompt)</span></label>
      <textarea id="persona-desc-input" placeholder="Describe yourself — appearance, personality, background…" rows="4" style="resize:vertical;min-height:90px">${esc(persona?.description || "")}</textarea>
    </div>
    <label class="modal-checkbox-label">
      <input type="checkbox" id="persona-active-checkbox" ${!personaId || personaId === S.activePersonaId ? "checked" : ""} style="width:14px;height:14px;margin:0;flex-shrink:0">
      <span style="font-size:13px;text-transform:none;letter-spacing:0;font-weight:400">Set as active persona after saving</span>
    </label>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn btn-danger" onclick="deletePersona(${personaId})">Delete</button>` : ""}
      <div style="flex:1"></div>
      <button class="btn" onclick="showUserModal()">Cancel</button>
      <button class="btn btn-accent" onclick="savePersona(${personaId || "null"})">${isEdit ? "Update" : "Create"}</button>
    </div>
  `);
}

export async function savePersona(personaId) {
  const name = $("persona-name-input").value.trim();
  const description = $("persona-desc-input").value.trim();
  const setActive = $("persona-active-checkbox").checked;
  const validation = validate.validatePersona(name, description);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  try {
    let newId;
    if (personaId && personaId !== "null") {
      await api.put("/user-personas/" + personaId, { name, description });
      newId = parseInt(personaId, 10);
    } else {
      const result = await api.post("/user-personas", { name, description });
      newId = result.id;
    }
    await loadPersonas();
    if (setActive) {
      await api.put("/settings", { active_persona_id: newId });
      S.activePersonaId = newId;
      updateUserBtn();
    }
    showUserModal();
    toast("Persona saved");
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export async function deletePersona(personaId) {
  showConfirmModal(
    {
      title: "Delete Persona",
      message: "Are you sure you want to delete this persona?",
      confirmText: "Delete",
    },
    async () => {
      try {
        await api.del("/user-personas/" + personaId);
        if (S.activePersonaId === personaId) {
          await api.put("/settings", { active_persona_id: null });
          S.activePersonaId = null;
          updateUserBtn();
        }
        await loadPersonas();
        showUserModal();
        toast("Persona deleted");
      } catch (e) {
        toast("Failed: " + e.message, true);
      }
    },
  );
}

export async function activatePersona(personaId) {
  if (S.activePersonaId === personaId) return;
  // A lock pins one persona to the open conversation/character; switching to a
  // different persona is blocked until the user unlocks. The lock would
  // override the choice at generation time anyway, so refuse it explicitly.
  const { conv, card, charName } = activeLockContext();
  const lockedIds = lockedPersonaIds(conv, card);
  if (lockedIds.size && !lockedIds.has(personaId)) {
    toast(lockWarningText(conv, card, charName), true);
    return;
  }
  try {
    await api.put("/settings", { active_persona_id: personaId });
    S.activePersonaId = personaId;
    updateUserBtn();
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export async function editPersona(personaId) {
  showPersonaEditModal(personaId);
}

// ── Persona locks (override the global active persona within a scope)
// One pin at a time: while a persona is locked to the open conversation or
// character, locking a *different* persona is refused until the user unlocks.
// Unlocking is always allowed. Re-rendering keeps every row's buttons truthful.
export async function setPersonaConversationLock(personaId, locked) {
  const { conv, card, charName } = activeLockContext();
  if (!conv) return;
  const lockedIds = lockedPersonaIds(conv, card);
  if (locked && lockedIds.size && !lockedIds.has(personaId)) {
    toast(lockWarningText(conv, card, charName), true);
    return;
  }
  const val = locked ? personaId : null;
  try {
    await api.put("/conversations/" + conv.id, { persona_lock_id: val });
    conv.persona_lock_id = val; // keep S in sync so the buttons re-read correctly
    updateUserBtn(); // locking the open conversation may flip the button glyph
    toast(locked ? "Locked to this conversation" : "Conversation lock removed");
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

// Pin the active persona to a conversation the first time the user writes in
// it, so later persona switches don't silently rewrite who the existing turns
// were authored by. No-op once anything is locked, or with no active persona.
export async function lockPersonaOnFirstMessage() {
  const { conv } = activeLockContext();
  if (!conv || conv.persona_lock_id || !S.activePersonaId) return;
  const val = S.activePersonaId;
  try {
    await api.put("/conversations/" + conv.id, { persona_lock_id: val });
    conv.persona_lock_id = val; // keep S in sync so the buttons re-read correctly
    updateUserBtn();
  } catch (e) {
    console.warn("Failed to lock persona to conversation:", e);
  }
}

export async function setPersonaCharacterLock(personaId, locked) {
  const { conv, card, charName } = activeLockContext();
  if (!card) return;
  const lockedIds = lockedPersonaIds(conv, card);
  if (locked && lockedIds.size && !lockedIds.has(personaId)) {
    toast(lockWarningText(conv, card, charName), true);
    return;
  }
  const val = locked ? personaId : null;
  try {
    await api.put("/characters/" + card.id, { persona_lock_id: val });
    card.persona_lock_id = val; // keep S in sync so the buttons re-read correctly
    updateUserBtn(); // pairing with the open character may flip the button glyph
    toast(locked ? "Locked to this character" : "Character lock removed");
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}
