import { registerActions } from "./actions.js";
import { api } from "./api.js";
import { EDIT_ICON } from "./icons.js";
import { confirmDelete, setModalDismiss, showCropModal, showModal } from "./modal.js";
import { saveSettings } from "./settings_store.js";
import { charactersView, notify, S } from "./state.js";
import {
  $,
  avatarCell,
  effectivePersonaId,
  esc,
  escAttr,
  personaAvatarSrc,
  safePersonaColour,
  toast,
} from "./utils.js";
import { validate } from "./validate.js";

export async function loadPersonas() {
  try {
    S.personas = await api.get("/user-personas");
  } catch (e) {
    console.error("Failed to load personas:", e);
    S.personas = [];
  }
  notify("personas");
}

// The image chosen in the crop modal, held until savePersona() posts it.
// `null` means "leave whatever is stored alone"; `REMOVE_AVATAR` clears it.
const REMOVE_AVATAR = Symbol("remove-avatar");
let _pendingPersonaAvatar = null;

const PERSONA_ICON = "👤";
const CONV_LOCK_ICON = "💬";
const CHAR_LOCK_ICON = "💏";

export function updateUserBtn() {
  const personaId = effectivePersonaId();
  let displayName = "User";
  if (personaId && S.personas.length) {
    const persona = S.personas.find((p) => p.id === personaId);
    if (persona) displayName = persona.name;
  }
  const { conv, card } = activeLockContext();
  const glyph =
    conv?.persona_lock_id && card?.persona_lock_id
      ? CHAR_LOCK_ICON
      : conv?.persona_lock_id
        ? CONV_LOCK_ICON
        : card?.persona_lock_id
          ? CHAR_LOCK_ICON
          : PERSONA_ICON;
  const label = `${glyph} ${displayName}`;
  $("user-profile-btn").textContent = label;
  const mobileBtn = $("mobile-user-profile-btn");
  if (mobileBtn) mobileBtn.textContent = label;
}

function activeLockContext() {
  const conv = S.conversations.find((c) => c.id === S.activeConvId);
  const card = conv?.character_card_id ? charactersView().find((c) => c.id === conv.character_card_id) : null;
  const charName = conv?.character_name || card?.name || "";
  return { conv, card, charName };
}

function showUserModal() {
  const { conv, card, charName } = activeLockContext();
  const pinned = !!(conv?.persona_lock_id || card?.persona_lock_id);
  const personaItems = S.personas
    .map((p) => {
      const isActive = p.id === S.activePersonaId;
      const avatarColor = safePersonaColour(p.avatar_color) || "#E1F5EE";
      const avatarTextColor = isActive ? "var(--accent)" : "#085041";
      const avatarBg = isActive ? "var(--accent-glow)" : avatarColor;
      const initials = p.name.charAt(0).toUpperCase();
      const avatarSrc = personaAvatarSrc(p);
      const avatarInner = avatarSrc ? avatarCell(escAttr(avatarSrc), { icon: initials }) : esc(initials);
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
      <div class="persona-item${isActive ? " persona-item-active" : ""}" data-wf-action="personas:activate" data-persona-id="${p.id}">
        <div class="persona-avatar" style="background:${avatarBg};color:${avatarTextColor}">${avatarInner}</div>
        <div class="persona-info">
          <div class="persona-name-row">
            <span class="persona-name">${esc(p.name)}</span>
            ${isActive ? '<span class="persona-active-badge">Default</span>' : ""}
          </div>
          <span class="persona-desc">${esc(p.description || "")}</span>
        </div>
        <div class="persona-actions-direct">
          <button class="persona-action-btn${convLocked ? " locked" : ""}" ${conv ? "" : "disabled"}
            title="${convTitle}" aria-label="${convTitle}" aria-pressed="${convLocked}"
            data-wf-action="personas:conversationLock" data-persona-id="${p.id}" data-locked="${!convLocked}">${CONV_LOCK_ICON}</button>
          <button class="persona-action-btn${charLocked ? " locked" : ""}" ${card ? "" : "disabled"}
            title="${charTitle}" aria-label="${charTitle}" aria-pressed="${charLocked}"
            data-wf-action="personas:characterLock" data-persona-id="${p.id}" data-locked="${!charLocked}">${CHAR_LOCK_ICON}</button>
          <button class="persona-action-btn persona-action-edit" title="Edit ${escAttr(p.name)}" aria-label="Edit ${escAttr(p.name)}"
            data-wf-action="personas:edit" data-persona-id="${p.id}">${EDIT_ICON}</button>
        </div>
      </div>
    `;
    })
    .join("");

  const note = pinned
    ? `<div class="persona-lock-warning"><span aria-hidden="true">${CONV_LOCK_ICON}</span><span>${esc(pinnedStatusText(conv, card, charName))}</span></div>`
    : "";

  showModal(`
    <div class="persona-modal">
      <div class="modal-title-row persona-modal-header">
        <div>
          <h2>User personas</h2>
          <p class="modal-subtitle">Choose the identity to use by default. Override it with a different persona for a specific chat or character.</p>
        </div>
        <div class="modal-title-actions">
          <button class="btn btn-sm" data-wf-action="personas:new">+ New persona</button>
        </div>
      </div>
      ${note}
      <div class="persona-list">
        ${personaItems.length ? personaItems : '<p class="persona-empty">No personas yet. Create one to get started.</p>'}
      </div>
    </div>
  `);
}

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
  if (!convId) return `${scope}. Choosing another persona will pin this chat instead.`;
  return `${scope}. Choosing another persona will move this chat pin.`;
}

function showPersonaEditModal(personaId) {
  const persona = personaId ? S.personas.find((p) => p.id === personaId) : null;
  const isEdit = persona !== null;
  _pendingPersonaAvatar = null;
  showModal(`
    <h2>${isEdit ? "Edit persona" : "New persona"}</h2>
    <div class="persona-edit-avatar" id="persona-avatar-controls">
      <div class="persona-avatar persona-avatar-lg" id="persona-avatar-preview">${personaPreviewHtml(persona)}</div>
      <div class="persona-avatar-actions">
        <button type="button" class="btn btn-sm" data-action="choose">Choose image</button>
        <button type="button" class="btn btn-sm" data-action="remove"${persona?.has_avatar ? "" : " disabled"}>Remove</button>
      </div>
    </div>
    <div class="field">
      <label>Name</label>
      <input id="persona-name-input" type="text" placeholder="e.g. Kit" value="${esc(persona?.name || "")}">
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
      ${isEdit ? `<button class="btn btn-danger" data-wf-action="personas:delete" data-persona-id="${personaId}">Delete</button>` : ""}
      <button class="btn" data-wf-action="personas:open">Cancel</button>
      <button class="btn btn-accent" data-wf-action="personas:save" data-persona-id="${personaId || ""}">${isEdit ? "Save" : "Create"}</button>
    </div>
  `);
  setModalDismiss(showUserModal);
  wirePersonaAvatarControls(persona);
}

function personaPreviewHtml(persona) {
  if (_pendingPersonaAvatar && _pendingPersonaAvatar !== REMOVE_AVATAR) {
    const { b64, mime } = _pendingPersonaAvatar;
    return `<img src="data:${escAttr(mime)};base64,${escAttr(b64)}">`;
  }
  if (_pendingPersonaAvatar === REMOVE_AVATAR) return PERSONA_ICON;
  const src = personaAvatarSrc(persona);
  return src ? avatarCell(escAttr(src), { icon: PERSONA_ICON }) : PERSONA_ICON;
}

/** Wire the avatar controls. */
function wirePersonaAvatarControls(persona) {
  const box = $("persona-avatar-controls");
  if (!box || box.dataset.wired) return;
  box.dataset.wired = "1";
  box.addEventListener("click", (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action === "choose") {
      showCropModal(
        ({ b64, mime }) => {
          _pendingPersonaAvatar = { b64, mime };
          const preview = $("persona-avatar-preview");
          if (preview) preview.innerHTML = personaPreviewHtml(persona);
          const removeBtn = box.querySelector('[data-action="remove"]');
          if (removeBtn) removeBtn.disabled = false;
        },
        { kind: "persona", ratio: "square" },
      );
    } else if (action === "remove") {
      _pendingPersonaAvatar = REMOVE_AVATAR;
      const preview = $("persona-avatar-preview");
      if (preview) preview.innerHTML = personaPreviewHtml(persona);
      e.target.closest("[data-action]").disabled = true;
    }
  });
}

async function savePersona(personaId) {
  const name = $("persona-name-input").value.trim();
  const description = $("persona-desc-input").value.trim();
  const setActive = $("persona-active-checkbox").checked;
  const validation = validate.validatePersona(name, description);
  if (!validation.valid) {
    toast(validation.error, true);
    return;
  }
  const payload = { name, description };
  if (_pendingPersonaAvatar === REMOVE_AVATAR) {
    payload.avatar_b64 = null;
    payload.avatar_mime = null;
  } else if (_pendingPersonaAvatar) {
    payload.avatar_b64 = _pendingPersonaAvatar.b64;
    payload.avatar_mime = _pendingPersonaAvatar.mime;
  }
  const pending = _pendingPersonaAvatar;
  const avatarChanged = pending !== null;
  _pendingPersonaAvatar = null;
  try {
    let newId;
    if (personaId && personaId !== "null") {
      await api.put(`/user-personas/${personaId}`, payload);
      newId = parseInt(personaId, 10);
    } else {
      const result = await api.post("/user-personas", payload);
      newId = result.id;
    }
    if (avatarChanged) S.personaAvatarVersion++;
    await loadPersonas();
    if (setActive) await activatePersona(newId);
    updateUserBtn();
    showUserModal();
    toast("Persona saved");
  } catch (e) {
    _pendingPersonaAvatar = pending;
    toast(`Failed: ${e.message}`, true);
  }
}

async function deletePersona(personaId) {
  const name = S.personas.find((p) => p.id === personaId)?.name;
  confirmDelete("persona", `Delete ${name ? `"${esc(name)}"` : "this persona"}? This cannot be undone.`, async () => {
    try {
      await api.del(`/user-personas/${personaId}`);
      if (S.activePersonaId === personaId) {
        await saveSettings({ active_persona_id: null });
        updateUserBtn();
      }
      await loadPersonas();
      showUserModal();
      toast("Persona deleted");
    } catch (e) {
      toast(`Failed: ${e.message}`, true);
    }
  });
}

async function activatePersona(personaId) {
  const { conv, card } = activeLockContext();
  const pinnedId = conv?.persona_lock_id || card?.persona_lock_id || null;
  const repin = !!conv && !!pinnedId && pinnedId !== personaId;
  if (S.activePersonaId === personaId && !repin) return;
  try {
    await saveSettings({ active_persona_id: personaId });
    if (repin) {
      await api.put(`/conversations/${conv.id}`, { persona_lock_id: personaId });
      conv.persona_lock_id = personaId;
      const name = S.personas.find((p) => p.id === personaId)?.name || "persona";
      toast(`Re-pinned this chat to "${name}"`);
    }
    updateUserBtn();
    repaintUserAvatars();
    showUserModal();
  } catch (e) {
    toast(`Failed: ${e.message}`, true);
  }
}

async function editPersona(personaId) {
  showPersonaEditModal(personaId);
}

async function setPersonaConversationLock(personaId, locked) {
  const { conv } = activeLockContext();
  if (!conv) return;
  const replacing = locked && !!conv.persona_lock_id && conv.persona_lock_id !== personaId;
  const val = locked ? personaId : null;
  try {
    await api.put(`/conversations/${conv.id}`, { persona_lock_id: val });
    conv.persona_lock_id = val;
    updateUserBtn();
    repaintUserAvatars();
    toast(
      locked ? (replacing ? "Re-pinned this chat" : "Pinned to this conversation") : "Unpinned from this conversation",
    );
    showUserModal();
  } catch (e) {
    toast(`Failed: ${e.message}`, true);
  }
}

export async function ensurePersonaPinned() {
  const { conv, card } = activeLockContext();
  const val = card?.persona_lock_id || S.activePersonaId;
  if (!conv || conv.persona_lock_id || !val) return;
  try {
    await api.put(`/conversations/${conv.id}`, { persona_lock_id: val });
    conv.persona_lock_id = val;
    updateUserBtn();
  } catch (e) {
    console.warn("Failed to pin persona to conversation:", e);
  }
}

async function setPersonaCharacterLock(personaId, locked) {
  const { card } = activeLockContext();
  if (!card) return;
  const replacing = locked && !!card.persona_lock_id && card.persona_lock_id !== personaId;
  const val = locked ? personaId : null;
  try {
    await api.put(`/characters/${card.id}`, { persona_lock_id: val });
    card.persona_lock_id = val;
    updateUserBtn();
    repaintUserAvatars();
    toast(
      locked ? (replacing ? "Re-pinned this character" : "Pinned to this character") : "Unpinned from this character",
    );
    showUserModal();
  } catch (e) {
    toast(`Failed: ${e.message}`, true);
  }
}

const _personaId = (el) => (el.dataset.personaId ? Number(el.dataset.personaId) : null);

registerActions("personas", {
  open: () => showUserModal(),
  new: () => showPersonaEditModal(null),
  edit: (el) => editPersona(_personaId(el)),
  save: (el) => savePersona(_personaId(el)),
  delete: (el) => deletePersona(_personaId(el)),
  activate: (el) => activatePersona(_personaId(el)),
  conversationLock: (el) => setPersonaConversationLock(_personaId(el), el.dataset.locked === "true"),
  characterLock: (el) => setPersonaCharacterLock(_personaId(el), el.dataset.locked === "true"),
});
