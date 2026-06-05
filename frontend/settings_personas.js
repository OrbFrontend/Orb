// User profile + persona management: the user button, the personas list modal,
// and persona create / edit / delete / activate. Split out of settings.js; the
// public surface is re-exported from settings.js.
import { api } from "./api.js";
import { closeModal, showConfirmModal, showModal } from "./modal.js";
import { S } from "./state.js";
import { $, esc, toast } from "./utils.js";
import { validate } from "./validate.js";

export async function loadPersonas() {
  try {
    S.personas = await api.get("/user-personas");
  } catch (e) {
    console.error("Failed to load personas:", e);
    S.personas = [];
  }
}

// ── User Profile
export function updateUserBtn() {
  let displayName = "User";
  if (S.activePersonaId && S.personas.length) {
    const activePersona = S.personas.find((p) => p.id === S.activePersonaId);
    if (activePersona) displayName = activePersona.name;
  }
  $("user-profile-btn").textContent = "👤 " + displayName;
}

export function showUserModal() {
  const personaItems = S.personas
    .map((p) => {
      const isActive = p.id === S.activePersonaId;
      const avatarColor = p.avatar_color || "#E1F5EE";
      const avatarTextColor = isActive ? "var(--accent)" : "#085041";
      const avatarBg = isActive ? "var(--accent-glow)" : avatarColor;
      const initials = p.name.charAt(0).toUpperCase();
      return `
      <div class="persona-item${isActive ? " persona-item-active" : ""}" onclick="activatePersona(${p.id})">
        <div class="persona-avatar" style="background:${avatarBg};color:${avatarTextColor}">${initials}</div>
        <div class="persona-info">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="persona-name">${esc(p.name)}</span>
            ${isActive ? '<span class="persona-active-badge">Active</span>' : ""}
          </div>
          <span class="persona-desc">${esc(p.description || "")}</span>
        </div>
        <button class="btn btn-sm" onclick="event.stopPropagation();editPersona(${p.id})">Edit</button>
      </div>
    `;
    })
    .join("");

  showModal(`
    <div class="modal-title-row">
      <div>
        <h2>User personas</h2>
        <p class="modal-subtitle">Click a persona to activate it.</p>
      </div>
      <div class="modal-title-actions">
        <button class="btn" onclick="showPersonaEditModal(null)">+ New persona</button>
      </div>
    </div>
    <div class="persona-list">
      ${personaItems.length ? personaItems : '<p class="modal-subtitle" style="text-align:center;padding:1rem 0">No personas yet. Create one to get started.</p>'}
    </div>
  `);
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
