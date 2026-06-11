// User profile + persona management: the user button, the personas list modal,
// and persona create / edit / delete / activate. Split out of settings.js; the
// public surface is re-exported from settings.js.
import { api } from "./api.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, effectivePersonaId, esc, escAttr, toast } from "./utils.js";
import { validate } from "./validate.js";

export async function loadPersonas() {
  try {
    S.personas = await api.get("/user-personas");
  } catch (e) {
    console.error("Failed to load personas:", e);
    S.personas = [];
  }
}

// Persona-pin glyphs. The user button shows how the displayed persona is
// pinned (conversation pin wins over character pin); 💬/💏 also label the
// per-scope pin buttons and modal subtitle below.
const PERSONA_ICON = "👤";
const CONV_LOCK_ICON = "💬";
const CHAR_LOCK_ICON = "💏";

// ── User Profile
export function updateUserBtn() {
  // Show the persona generation will actually use: conv pin → char pin →
  // global default, matching backend resolve_persona_id.
  const personaId = effectivePersonaId();
  let displayName = "User";
  if (personaId && S.personas.length) {
    const persona = S.personas.find((p) => p.id === personaId);
    if (persona) displayName = persona.name;
  }
  const { conv, card } = activeLockContext();
  const glyph = conv?.persona_lock_id ? CONV_LOCK_ICON : card?.persona_lock_id ? CHAR_LOCK_ICON : PERSONA_ICON;
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
  // A pin on the open conversation or character decides who speaks here; the
  // global default (Default badge) only seeds new, unpinned chats. Nothing is
  // gated: selecting another persona while pinned simply re-pins the chat.
  const pinned = !!(conv?.persona_lock_id || card?.persona_lock_id);
  const effectiveId = effectivePersonaId();
  const personaItems = S.personas
    .map((p) => {
      const isActive = p.id === S.activePersonaId;
      const avatarColor = p.avatar_color || "#E1F5EE";
      const avatarTextColor = isActive ? "var(--accent)" : "#085041";
      const avatarBg = isActive ? "var(--accent-glow)" : avatarColor;
      const initials = p.name.charAt(0).toUpperCase();
      const convLocked = !!conv && conv.persona_lock_id === p.id;
      const charLocked = !!card && card.persona_lock_id === p.id;
      const convTitle = conv
        ? convLocked
          ? "Unpin from this conversation"
          : "Pin to this conversation"
        : "Open a conversation to enable";
      const charTitle = card
        ? charLocked
          ? `Unpin from ${escAttr(charName)}`
          : `Pin to ${escAttr(charName)}`
        : "Only available for saved characters";
      return `
      <div class="persona-item${isActive ? " persona-item-active" : ""}" onclick="activatePersona(${p.id})">
        <div class="persona-avatar" style="background:${avatarBg};color:${avatarTextColor}">${initials}</div>
        <div class="persona-info">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="persona-name">${esc(p.name)}</span>
            ${isActive ? '<span class="persona-active-badge">Default</span>' : ""}
            ${pinned && p.id === effectiveId ? '<span class="persona-active-badge">This chat</span>' : ""}
          </div>
          <span class="persona-desc">${esc(p.description || "")}</span>
        </div>
        <div class="persona-lock-btns">
          <button class="persona-lock-btn${convLocked ? " locked" : ""}" ${conv ? "" : "disabled"} title="${convTitle}"
            onclick="event.stopPropagation();setPersonaConversationLock(${p.id}, ${!convLocked})">${CONV_LOCK_ICON}</button>
          <button class="persona-lock-btn${charLocked ? " locked" : ""}" ${card ? "" : "disabled"} title="${charTitle}"
            onclick="event.stopPropagation();setPersonaCharacterLock(${p.id}, ${!charLocked})">${CHAR_LOCK_ICON}</button>
        </div>
        <button class="btn btn-sm" onclick="event.stopPropagation();editPersona(${p.id})">Edit</button>
      </div>
    `;
    })
    .join("");

  const note = pinned
    ? `<p class="persona-lock-warning">${CONV_LOCK_ICON} ${esc(pinnedStatusText(conv, card, charName))}</p>`
    : "";

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>User personas</h2>
        <p class="modal-subtitle">${CONV_LOCK_ICON} pin to conversation, ${CHAR_LOCK_ICON} to character — pins override the default persona.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn" onclick="showPersonaEditModal(null)">+ New persona</button>
      </div>
    </div>
    ${note}
    <div class="persona-list">
      ${personaItems.length ? personaItems : '<p class="modal-subtitle" style="text-align:center;padding:1rem 0">No personas yet. Create one to get started.</p>'}
    </div>
  `);
}

// Human-readable summary of which persona is pinned where, for the modal's
// neutral status note. Caller escapes the result before injecting it.
function pinnedStatusText(conv, card, charName) {
  const named = (id) => `"${S.personas.find((p) => p.id === id)?.name || "A persona"}"`;
  const convId = conv?.persona_lock_id || null;
  const cardId = card?.persona_lock_id || null;
  const where = charName || "this character";
  let scope;
  if (convId && cardId && convId === cardId) scope = `${named(convId)} is pinned to this chat and ${where}`;
  else if (convId && cardId) scope = `${named(convId)} is pinned to this chat and ${named(cardId)} to ${where}`;
  else if (convId) scope = `${named(convId)} is pinned to this chat`;
  else scope = `${named(cardId)} is pinned to ${where}`;
  return `${scope} — selecting another persona re-pins this chat.`;
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
      <span style="font-size:13px;text-transform:none;letter-spacing:0;font-weight:400">Set as default persona after saving</span>
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
    // Same path as clicking the row: sets the default and re-pins the open
    // chat when it was pinned to someone else.
    if (setActive) await activatePersona(newId);
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
  // Selecting a persona sets the global default (seeds new chats) and, when
  // the open chat is pinned to someone else, follows the user's intent by
  // moving the conversation pin too. A character pin is never touched here:
  // the new conversation pin wins over it (matching resolve_persona_id).
  const { conv, card } = activeLockContext();
  const pinnedId = conv?.persona_lock_id || card?.persona_lock_id || null;
  const repin = !!conv && !!pinnedId && pinnedId !== personaId;
  if (S.activePersonaId === personaId && !repin) return;
  try {
    await api.put("/settings", { active_persona_id: personaId });
    S.activePersonaId = personaId;
    if (repin) {
      await api.put("/conversations/" + conv.id, { persona_lock_id: personaId });
      conv.persona_lock_id = personaId;
      const name = S.personas.find((p) => p.id === personaId)?.name || "persona";
      toast(`Re-pinned this chat to "${name}"`);
    }
    updateUserBtn();
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

export async function editPersona(personaId) {
  showPersonaEditModal(personaId);
}

// ── Persona pins (override the global active persona within a scope)
// Pinning is never gated: pinning B while A holds the scope simply moves the
// pin to B. Re-rendering keeps every row's buttons truthful.
export async function setPersonaConversationLock(personaId, locked) {
  const { conv } = activeLockContext();
  if (!conv) return;
  const replacing = locked && !!conv.persona_lock_id && conv.persona_lock_id !== personaId;
  const val = locked ? personaId : null;
  try {
    await api.put("/conversations/" + conv.id, { persona_lock_id: val });
    conv.persona_lock_id = val; // keep S in sync so the buttons re-read correctly
    updateUserBtn(); // pinning the open conversation may flip the button glyph
    toast(
      locked ? (replacing ? "Re-pinned this chat" : "Pinned to this conversation") : "Unpinned from this conversation",
    );
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}

// Pin the effective persona (char pin → global default) to the conversation on
// send, so later persona switches don't silently rewrite who the existing
// turns were authored by. No-op while a conversation pin already holds, so an
// explicit unpin stays a transient state that re-resolves on the next send.
export async function ensurePersonaPinned() {
  const { conv, card } = activeLockContext();
  const val = card?.persona_lock_id || S.activePersonaId;
  if (!conv || conv.persona_lock_id || !val) return;
  try {
    await api.put("/conversations/" + conv.id, { persona_lock_id: val });
    conv.persona_lock_id = val; // keep S in sync so the buttons re-read correctly
    updateUserBtn();
  } catch (e) {
    console.warn("Failed to pin persona to conversation:", e);
  }
}

export async function setPersonaCharacterLock(personaId, locked) {
  const { card } = activeLockContext();
  if (!card) return;
  const replacing = locked && !!card.persona_lock_id && card.persona_lock_id !== personaId;
  const val = locked ? personaId : null;
  try {
    await api.put("/characters/" + card.id, { persona_lock_id: val });
    card.persona_lock_id = val; // keep S in sync so the buttons re-read correctly
    updateUserBtn(); // pairing with the open character may flip the button glyph
    toast(
      locked ? (replacing ? "Re-pinned this character" : "Pinned to this character") : "Unpinned from this character",
    );
    showUserModal();
  } catch (e) {
    toast("Failed: " + e.message, true);
  }
}
