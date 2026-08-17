import { api } from "./api.js";
import { castAvatars, speakingPlanHtml } from "./group_cast.js";
import { closeModal, showModal } from "./modal.js";
import { charactersView, notify, S } from "./state.js";
import { $, convUrl, esc, escAttr, toast } from "./utils.js";

function showGroupCreate() {
  const cards = charactersView();
  showModal(`<h2>New group chat</h2>
    <label>Group title<input id="group-create-title" value="New Group"></label>
    <label>Turn mode<select id="group-create-mode"><option value="director">Director</option><option value="round_robin">Round robin</option><option value="manual">Manual</option></select></label>
    <label>Maximum speakers<input id="group-create-max" type="number" min="1" max="8" value="3"></label>
    <label>Group scenario<textarea id="group-create-scenario" rows="3" placeholder="Shared scene setup"></textarea></label>
    <label>Group instructions<textarea id="group-create-instructions" rows="2" placeholder="Shared post-history instructions"></textarea></label>
    <div class="group-create-members">${cards.map((card) => `<label><input type="checkbox" data-group-card-id="${escAttr(card.id)}"> ${esc(card.name)}</label>`).join("")}</div>
    <p class="modal-hint">Public profiles are shared in the cast prompt. A speaker's raw card fields stay private to their turn. Card-linked Worlds and embedded fragments are shared scene inputs; per-member private lore is not supported in v1. Card system-prompt overrides are ignored.</p>
    <div class="modal-actions"><button type="button" class="btn" id="group-create-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-create-save">Create group</button></div>`);
  $("group-create-cancel")?.addEventListener("click", closeModal);
  $("group-create-save")?.addEventListener("click", async () => {
    const members = [...document.querySelectorAll("[data-group-card-id]:checked")].map((input) => ({
      character_card_id: input.dataset.groupCardId,
    }));
    if (!members.length) {
      toast("Choose at least one character", true);
      return;
    }
    S.castSetupBusy = true;
    try {
      const conv = await api.post("/conversations", {
        kind: "group",
        title: $("group-create-title").value.trim() || "New Group",
        group_turn_mode: $("group-create-mode").value,
        group_max_speakers: Number($("group-create-max").value) || 3,
        character_scenario: $("group-create-scenario").value.trim(),
        post_history_instructions: $("group-create-instructions").value.trim(),
        members,
      });
      closeModal();
      document.dispatchEvent(new CustomEvent("group-created", { detail: conv.id }));
    } catch (error) {
      toast(error.message, true);
    } finally {
      S.castSetupBusy = false;
    }
  });
}

function rosterRow(member) {
  return `<div class="group-roster-row" data-roster-member-id="${escAttr(member.id || "")}" data-roster-card-id="${escAttr(member.character_card_id || "")}" data-roster-kind="${escAttr(member.member_kind || "character")}">
    <input data-roster-name value="${escAttr(member.display_name || "Narrator")}" aria-label="Display name">
    <label><input type="checkbox" data-roster-muted ${member.muted ? "checked" : ""}> Muted</label>
    <div class="group-roster-actions"><button type="button" class="btn btn-sm" data-roster-move="up">↑</button><button type="button" class="btn btn-sm" data-roster-move="down">↓</button><button type="button" class="btn btn-sm" data-roster-remove>Remove</button></div>
    <textarea data-roster-profile placeholder="Optional public profile override">${esc(member.public_profile_override || "")}</textarea>
  </div>`;
}

function showRosterEditor() {
  if (!S.groupCast) return;
  const activeCards = new Set(S.groupCast.members.map((member) => member.character_card_id).filter(Boolean));
  const choices = charactersView()
    .filter((card) => !activeCards.has(card.id))
    .map((card) => `<option value="${escAttr(card.id)}">${esc(card.name)}</option>`)
    .join("");
  showModal(`<h2>Edit cast</h2>
    <div id="group-roster-list" class="group-roster-list">${S.groupCast.members.map(rosterRow).join("")}</div>
    <div class="group-roster-add"><select id="group-roster-card"><option value="">Choose a character…</option>${choices}</select><button type="button" class="btn" id="group-roster-add-card">Add</button><button type="button" class="btn" id="group-roster-add-narrator">Add narrator</button></div>
    <label>Maximum speakers<input id="group-roster-max" type="number" min="1" max="8" value="${Number(S.groupCast.max_speakers) || 3}"></label>
    <p class="modal-hint">Reordering changes cast and fragment collision priority. Public profiles are shared; raw card fields are exposed only on that member's speaking turn. Linked Worlds and embedded fragments remain scene-wide.</p>
    <div class="modal-actions"><button type="button" class="btn" id="group-roster-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-roster-save">Save cast</button></div>`);
  const list = $("group-roster-list");
  list?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-roster-member-id]");
    if (!row) return;
    if (event.target.closest("[data-roster-remove]")) row.remove();
    const direction = event.target.closest("[data-roster-move]")?.dataset.rosterMove;
    if (direction === "up" && row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
    if (direction === "down" && row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row);
  });
  $("group-roster-add-card")?.addEventListener("click", () => {
    const select = $("group-roster-card");
    const card = charactersView().find((item) => item.id === select.value);
    if (!card) return;
    list.insertAdjacentHTML("beforeend", rosterRow({ character_card_id: card.id, display_name: card.name }));
    select.querySelector(`option[value="${CSS.escape(card.id)}"]`)?.remove();
    select.value = "";
  });
  $("group-roster-add-narrator")?.addEventListener("click", () =>
    list.insertAdjacentHTML("beforeend", rosterRow({ member_kind: "narrator", display_name: "Narrator" })),
  );
  $("group-roster-cancel")?.addEventListener("click", closeModal);
  $("group-roster-save")?.addEventListener("click", async () => {
    const members = [...list.querySelectorAll("[data-roster-member-id]")].map((row) => ({
      id: row.dataset.rosterMemberId || null,
      character_card_id: row.dataset.rosterCardId || null,
      display_name: row.querySelector("[data-roster-name]").value.trim() || "Narrator",
      public_profile_override: row.querySelector("[data-roster-profile]").value.trim() || null,
      member_kind: row.dataset.rosterKind || "character",
      muted: row.querySelector("[data-roster-muted]").checked,
    }));
    if (!members.length) {
      toast("A group needs at least one member", true);
      return;
    }
    S.castSetupBusy = true;
    try {
      const updated = await api.put(convUrl(S.activeConvId, "members"), { members });
      const maxSpeakers = Math.max(1, Math.min(8, Number($("group-roster-max").value) || 3));
      const conv = await api.put(`/conversations/${S.activeConvId}`, { group_max_speakers: maxSpeakers });
      S.groupCast = { members: updated, turn_mode: conv.group_turn_mode, max_speakers: conv.group_max_speakers };
      if (!updated.some((member) => member.id === S.pinnedSpeakerId && !member.muted)) S.pinnedSpeakerId = null;
      closeModal();
      renderGroupCast();
      notify("cast", S.groupCast);
    } catch (error) {
      toast(error.message, true);
    } finally {
      S.castSetupBusy = false;
    }
  });
}

export function renderGroupCast() {
  const rail = $("group-cast-rail");
  const plan = $("group-speaking-plan");
  const controls = $("group-controls");
  const convert = $("convert-to-group-btn");
  if (!rail || !plan || !controls) return;
  const grouped = Boolean(S.groupCast);
  rail.hidden = !grouped;
  plan.hidden = !grouped;
  controls.hidden = !grouped;
  if (convert) convert.hidden = grouped || !S.activeConvId;
  rail.innerHTML = castAvatars();
  plan.innerHTML = speakingPlanHtml();
  if (grouped) {
    $("group-turn-mode").value = S.groupCast.turn_mode;
    $("group-speak-btn").disabled = !S.pinnedSpeakerId || S.isStreaming;
  }
}

export function renderGroupList() {
  const list = $("group-chat-list");
  if (!list) return;
  list.innerHTML = S.conversations
    .filter((conv) => conv.kind === "group")
    .map(
      (conv) =>
        `<button type="button" class="btn btn-block btn-sm" data-group-conversation-id="${escAttr(conv.id)}">👥 ${esc(conv.title)}</button>`,
    )
    .join("");
}

export async function loadGroupCast(conv) {
  if (!conv || conv.kind !== "group") {
    S.groupCast = null;
    S.pinnedSpeakerId = null;
    renderGroupCast();
    notify("cast", null);
    return;
  }
  const members = await api.get(convUrl(conv.id, "members"));
  S.groupCast = { members, turn_mode: conv.group_turn_mode, max_speakers: conv.group_max_speakers };
  if (!members.some((m) => m.id === S.pinnedSpeakerId && !m.muted)) S.pinnedSpeakerId = null;
  renderGroupCast();
  notify("cast", S.groupCast);
}

export function initGroupSetup() {
  $("groups-section-toggle")?.addEventListener("click", (event) => {
    event.currentTarget.querySelector(".arrow")?.classList.toggle("collapsed");
    event.currentTarget.nextElementSibling?.classList.toggle("collapsed");
  });
  $("new-group-btn")?.addEventListener("click", showGroupCreate);
  $("group-chat-list")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-group-conversation-id]");
    if (button)
      document.dispatchEvent(new CustomEvent("group-selected", { detail: button.dataset.groupConversationId }));
  });
  $("group-cast-rail")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cast-member-id]");
    if (!button || button.disabled || S.isStreaming) return;
    S.pinnedSpeakerId = S.pinnedSpeakerId === button.dataset.castMemberId ? null : button.dataset.castMemberId;
    renderGroupCast();
  });
  $("group-turn-mode")?.addEventListener("change", async (event) => {
    if (!S.activeConvId || !S.groupCast) return;
    try {
      const conv = await api.put(`/conversations/${S.activeConvId}`, { group_turn_mode: event.target.value });
      S.groupCast.turn_mode = conv.group_turn_mode;
      const local = S.conversations.find((c) => c.id === S.activeConvId);
      if (local) local.group_turn_mode = conv.group_turn_mode;
      renderGroupCast();
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("group-speak-btn")?.addEventListener("click", () => document.dispatchEvent(new CustomEvent("group-speak-request")));
  $("group-edit-btn")?.addEventListener("click", showRosterEditor);
  $("convert-to-group-btn")?.addEventListener("click", async () => {
    if (!S.activeConvId || S.castSetupBusy) return;
    S.castSetupBusy = true;
    try {
      const result = await api.post(`/conversations/${S.activeConvId}/convert-to-group`);
      const index = S.conversations.findIndex((conv) => conv.id === S.activeConvId);
      if (index >= 0) S.conversations[index] = result.conversation;
      await loadGroupCast(result.conversation);
      renderGroupList();
      document.dispatchEvent(new CustomEvent("group-selected", { detail: S.activeConvId }));
    } catch (error) {
      toast(error.message, true);
    } finally {
      S.castSetupBusy = false;
    }
  });
}
