import { api } from "./api.js";
import { closeModal, confirmDelete, showModal } from "./modal.js";
import { closeUtilityPanel, isUtilityPanelOpen, openUtilityPanel } from "./panels.js";
import { S } from "./state.js";
import { requestSendPermission } from "./tabLock.js";
import { $, convUrl, esc, toast } from "./utils.js";

export const USER_NOTE_ID = "human";

let notes = [];

let regenCutMsgId = null;

export function toggleDirectionNotesPanel() {
  if (isUtilityPanelOpen("direction-notes-panel")) {
    closeUtilityPanel("direction-notes-panel", "direction-notes-panel-btn");
  } else {
    openUtilityPanel("direction-notes-panel", "direction-notes-panel-btn", renderDirectionNotesPanel);
  }
}

function renderRows() {
  const el = $("direction-notes-panel-content");
  if (!el) return;
  if (!notes.length) {
    el.innerHTML = `<div class="notes-empty">No direction notes on this branch yet. Turn on Direction Notes to record them.</div>`;
    return;
  }
  el.innerHTML = notes
    .map((n) => {
      const isUser = n.interactive_fragment_id === USER_NOTE_ID;
      const badge = isUser ? ` <span class="notes-row-user-badge">You</span>` : "";
      return `<div class="notes-row${isUser ? " user-note" : ""}">
      <div class="notes-row-meta">
        <span class="notes-row-frag">${esc(n.interactive_fragment_label || "(unnamed)")}${badge}</span>
        <span class="notes-row-turn">Turn ${n.turn_index}</span>
      </div>
      <div class="notes-row-content">${esc(n.content)}</div>
      <div class="notes-row-actions">
        <button class="btn btn-sm" onclick="editDirectionNote(${n.id})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteDirectionNote(${n.id})">Delete</button>
      </div>
    </div>`;
    })
    .join("");
}

export async function renderDirectionNotesPanel() {
  const el = $("direction-notes-panel-content");
  if (!el) return;
  if (!S.activeConvId) {
    el.innerHTML = `<div class="notes-empty">No conversation selected.</div>`;
    return;
  }
  try {
    notes = applyRegenCut(await api.get(convUrl(S.activeConvId, "direction-notes")));
  } catch (e) {
    el.innerHTML = `<div class="notes-empty">${esc(e.message)}</div>`;
    return;
  }
  renderRows();
}

function applyRegenCut(list) {
  if (regenCutMsgId == null) return list;
  const path = S.messages.map((m) => m.id);
  const cut = path.indexOf(regenCutMsgId);
  if (cut < 0) return list;
  const surviving = new Set(path.slice(0, cut));
  return list.filter((n) => surviving.has(n.message_id));
}

export function clearDirectionNotesRegenCut() {
  regenCutMsgId = null;
}

export function optimisticDropDirectionNotesFrom(msgId) {
  regenCutMsgId = msgId;
  notes = applyRegenCut(notes);
  if (isUtilityPanelOpen("direction-notes-panel")) renderRows();
}

export function addUserDirectionNote(msgId) {
  if (!isUtilityPanelOpen("direction-notes-panel")) {
    openUtilityPanel("direction-notes-panel", "direction-notes-panel-btn", renderDirectionNotesPanel);
  }
  showModal(`
    <h2>Add Direction Note</h2>
    <div class="field"><label>Label</label>
      <input id="user-note-label" type="text" value="Note" maxlength="80"></div>
    <div class="field"><label>Note</label>
      <textarea id="user-note-content" rows="5" placeholder="A lasting fact or direction to keep on this branch..."></textarea></div>
    <div class="modal-actions">
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveUserDirectionNote(${msgId})">Save</button>
    </div>`);
}

export async function saveUserDirectionNote(msgId) {
  if (!requestSendPermission()) return;
  const label = document.getElementById("user-note-label").value.trim() || "Note";
  const content = document.getElementById("user-note-content").value.trim();
  if (!content) {
    toast("Note cannot be empty", true);
    return;
  }
  try {
    await api.post(convUrl(S.activeConvId, "direction-notes"), { message_id: msgId, label, content });
    closeModal();
    await renderDirectionNotesPanel();
    toast("Note added");
  } catch (e) {
    toast(e.message, true);
  }
}

export function editDirectionNote(fid) {
  const note = notes.find((n) => n.id === fid);
  showModal(`
    <h2>Edit Direction Note</h2>
    <div class="field"><label>Note</label>
      <textarea id="direction-note-content" rows="5">${esc(note ? note.content : "")}</textarea></div>
    <div class="modal-actions">
      <div style="flex:1"></div>
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveDirectionNote(${fid})">Save</button>
    </div>`);
}

export async function saveDirectionNote(fid) {
  const content = document.getElementById("direction-note-content").value.trim();
  if (!content) {
    toast("Note cannot be empty", true);
    return;
  }
  try {
    await api.put(convUrl(S.activeConvId, "direction-notes", fid), { content });
    closeModal();
    await renderDirectionNotesPanel();
    toast("Note saved");
  } catch (e) {
    toast(e.message, true);
  }
}

export function deleteDirectionNote(fid) {
  confirmDelete("Direction Note", "Delete this direction note?", async () => {
    try {
      await api.del(convUrl(S.activeConvId, "direction-notes", fid));
      await renderDirectionNotesPanel();
      toast("Note deleted");
    } catch (e) {
      toast(e.message, true);
    }
  });
}
