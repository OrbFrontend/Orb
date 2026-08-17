import { api } from "./api.js";
import {
  castRailHtml,
  eligibleMembers,
  memberAvatar,
  replyBarHtml,
  speakingPlanHtml,
  TURN_MODES,
} from "./group_cast.js";
import { closeModal, showModal } from "./modal.js";
import { SIDEBAR_CLOSE_ICON } from "./sidebar_icons.js";
import { charactersView, notify, S } from "./state.js";
import { $, avatarCell, avatarUrl, convUrl, esc, escAttr, toast } from "./utils.js";

// One sentence every group surface can afford to show permanently. The full
// privacy contract (raw card fields, per-member lore, ignored system prompts)
// lives behind the disclosure below it.
const CONTEXT_LINE = "Characters share their public profiles and linked World context in this scene.";

const CONTEXT_HELP = `<details class="group-help"><summary>How group context works</summary>
  <p class="modal-hint">Every member's public profile, the scene premise, and the Worlds their cards link to are shared with the whole scene. A member's raw card fields — description, personality, examples, post-history instructions — are sent only on that member's own speaking turn, and their card's system-prompt override is ignored. Per-member private lore is not supported yet.</p>
</details>`;

function modeOptions(selected) {
  return Object.entries(TURN_MODES)
    .map(
      ([value, mode]) =>
        `<option value="${value}"${value === selected ? " selected" : ""}>${esc(mode.label)} — ${esc(mode.hint)}</option>`,
    )
    .join("");
}

// Max replies is a Director-only bound: the other two strategies schedule exactly
// one speaker per turn, so the field is meaningless there.
function syncMaxRepliesRow(selectId, rowId) {
  const row = $(rowId);
  if (row) row.hidden = $(selectId)?.value !== "director";
}

// "Artus", "Artus & Assistant", "Artus, Assistant & 2 more" — a scene name, not
// a sentence, so it stays short enough for the header.
function titleFromNames(names) {
  if (!names.length) return "New Group";
  if (names.length <= 2) return names.join(" & ");
  return `${names[0]}, ${names[1]} & ${names.length - 2} more`;
}

// ── Creation ────────────────────────────────────────────────────────────────
// Asks for the cast, optionally the premise, and nothing else. Title, reply
// behavior and instructions all have defaults and live under Advanced.

function pickCardHtml(card) {
  return `<button type="button" class="cast-pick" data-group-card-id="${escAttr(card.id)}" aria-pressed="false">
    <span class="cast-pick-avatar">${avatarCell(escAttr(avatarUrl(card.id)), { icon: "👤" })}</span>
    <span class="cast-pick-name">${esc(card.name)}</span>
  </button>`;
}

function showGroupCreate() {
  const cards = charactersView();
  const picker = cards.length
    ? `<div class="cast-picker" id="group-create-picker">${cards.map(pickCardHtml).join("")}</div>`
    : `<p class="modal-hint cast-picker-empty">No characters yet — create or import one first.</p>`;
  showModal(`<h2>New group chat</h2>
    <p class="modal-subtitle">Choose who is in the scene.</p>
    ${picker}
    <div class="field"><label for="group-create-scenario">Scene premise</label>
      <textarea id="group-create-scenario" rows="3" placeholder="Where and when does this open? (optional)"></textarea></div>
    <details class="group-advanced"><summary>Advanced</summary>
      <div class="field"><label for="group-create-title">Group title</label>
        <input id="group-create-title" placeholder="Named after the cast"></div>
      <div class="field"><label for="group-create-mode">Reply behavior</label>
        <select id="group-create-mode">${modeOptions("director")}</select></div>
      <div class="field" id="group-create-max-row"><label for="group-create-max">Maximum character replies per turn</label>
        <input id="group-create-max" type="number" min="1" max="8" value="3"></div>
      <div class="field"><label for="group-create-instructions">Style &amp; behavior instructions</label>
        <textarea id="group-create-instructions" rows="2" placeholder="How should this scene be written?"></textarea></div>
    </details>
    <p class="modal-hint">${CONTEXT_LINE}</p>
    ${CONTEXT_HELP}
    <div class="modal-actions"><button type="button" class="btn" id="group-create-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-create-save">Start scene</button></div>`);
  syncMaxRepliesRow("group-create-mode", "group-create-max-row");
  $("group-create-mode")?.addEventListener("change", () =>
    syncMaxRepliesRow("group-create-mode", "group-create-max-row"),
  );
  $("group-create-picker")?.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-group-card-id]");
    if (!pick) return;
    const selected = pick.classList.toggle("selected");
    pick.setAttribute("aria-pressed", String(selected));
  });
  $("group-create-cancel")?.addEventListener("click", closeModal);
  $("group-create-save")?.addEventListener("click", async () => {
    if (S.castSetupBusy) return;
    const picks = [...document.querySelectorAll("#group-create-picker .cast-pick.selected")];
    if (!picks.length) {
      toast("Choose at least one character", true);
      return;
    }
    const chosenIds = picks.map((pick) => pick.dataset.groupCardId);
    const names = chosenIds.map((id) => charactersView().find((card) => card.id === id)?.name).filter(Boolean);
    S.castSetupBusy = true;
    try {
      const conv = await api.post("/conversations", {
        kind: "group",
        title: $("group-create-title").value.trim() || titleFromNames(names),
        group_turn_mode: $("group-create-mode").value,
        group_max_speakers: Number($("group-create-max").value) || 3,
        character_scenario: $("group-create-scenario").value.trim(),
        post_history_instructions: $("group-create-instructions").value.trim(),
        members: chosenIds.map((id) => ({ character_card_id: id })),
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

// ── Group settings ──────────────────────────────────────────────────────────
// Durable scene configuration: what the composer used to carry inline.

function showGroupSettings() {
  if (!S.groupCast || !S.activeConvId) return;
  const conv = S.conversations.find((item) => item.id === S.activeConvId);
  showModal(`<h2>Group settings</h2>
    <div class="field"><label for="group-settings-title">Scene title</label>
      <input id="group-settings-title" value="${escAttr(conv?.title || "")}"></div>
    <h3 class="modal-section">Reply behavior</h3>
    <div class="field"><label for="group-settings-mode">How replies are chosen</label>
      <select id="group-settings-mode">${modeOptions(S.groupCast.turn_mode)}</select></div>
    <div class="field" id="group-settings-max-row"><label for="group-settings-max">Maximum character replies per turn</label>
      <input id="group-settings-max" type="number" min="1" max="8" value="${Number(S.groupCast.max_speakers) || 3}"></div>
    <div class="field"><label for="group-settings-scenario">Scene premise</label>
      <textarea id="group-settings-scenario" rows="3" placeholder="Where and when does this open?">${esc(conv?.character_scenario || "")}</textarea></div>
    <div class="field"><label for="group-settings-instructions">Style &amp; behavior instructions</label>
      <textarea id="group-settings-instructions" rows="2" placeholder="How should this scene be written?">${esc(conv?.post_history_instructions || "")}</textarea></div>
    <p class="modal-hint">${CONTEXT_LINE}</p>
    ${CONTEXT_HELP}
    <div class="modal-actions"><button type="button" class="btn btn-danger" id="group-settings-delete">Delete group</button><div style="flex:1"></div><button type="button" class="btn" id="group-settings-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-settings-save">Save</button></div>`);
  syncMaxRepliesRow("group-settings-mode", "group-settings-max-row");
  $("group-settings-mode")?.addEventListener("change", () =>
    syncMaxRepliesRow("group-settings-mode", "group-settings-max-row"),
  );
  $("group-settings-cancel")?.addEventListener("click", closeModal);
  $("group-settings-delete")?.addEventListener("click", () => {
    closeModal();
    document.dispatchEvent(new CustomEvent("group-delete-request", { detail: S.activeConvId }));
  });
  $("group-settings-save")?.addEventListener("click", async () => {
    if (S.castSetupBusy) return;
    S.castSetupBusy = true;
    try {
      const updated = await api.put(`/conversations/${S.activeConvId}`, {
        title: $("group-settings-title").value.trim() || conv?.title || "New Group",
        group_turn_mode: $("group-settings-mode").value,
        group_max_speakers: Math.max(1, Math.min(8, Number($("group-settings-max").value) || 3)),
        character_scenario: $("group-settings-scenario").value.trim(),
        post_history_instructions: $("group-settings-instructions").value.trim(),
      });
      const local = S.conversations.find((item) => item.id === S.activeConvId);
      if (local) Object.assign(local, updated);
      S.groupCast.turn_mode = updated.group_turn_mode;
      S.groupCast.max_speakers = updated.group_max_speakers;
      // The header title is a plain div except while it is being renamed inline.
      const titleEl = $("chat-title-text");
      if (titleEl) titleEl.textContent = updated.title;
      closeModal();
      renderGroupCast();
      renderGroupList();
    } catch (error) {
      toast(error.message, true);
    } finally {
      S.castSetupBusy = false;
    }
  });
}

// ── Manage cast ─────────────────────────────────────────────────────────────
// One compact row per member; everything else is progressive disclosure.

function castRow(member) {
  const name = member.display_name || "Narrator";
  return `<div class="cast-row" data-roster-member-id="${escAttr(member.id || "")}" data-roster-card-id="${escAttr(member.character_card_id || "")}" data-roster-kind="${escAttr(member.member_kind || "character")}">
    <button type="button" class="cast-drag" data-roster-drag title="Drag, or use the arrow keys, to reorder" aria-label="Reorder ${escAttr(name)}">⠿</button>
    ${memberAvatar(member)}
    <input data-roster-name value="${escAttr(name)}" aria-label="Display name">
    <label class="cast-reply-toggle" title="A muted member stays in scene context but never takes a turn"><input type="checkbox" data-roster-reply ${member.muted ? "" : "checked"}> Can reply</label>
    <button type="button" class="cast-row-more" data-roster-more title="More actions" aria-label="More actions for ${escAttr(name)}">•••</button>
    <div class="cast-row-menu"><button type="button" class="burger-menu-item" data-roster-remove>Remove from scene</button></div>
    <details class="cast-row-custom"><summary>Customize for this scene</summary>
      <textarea data-roster-profile placeholder="Public profile override — how the rest of the cast sees them">${esc(member.public_profile_override || "")}</textarea>
    </details>
  </div>`;
}

function addOptions() {
  const active = new Set((S.groupCast?.members || []).map((member) => member.character_card_id).filter(Boolean));
  const characters = charactersView()
    .filter((card) => !active.has(card.id))
    .map((card) => `<option value="${escAttr(card.id)}">${esc(card.name)}</option>`)
    .join("");
  return `<option value="">+ Add cast member…</option><option value="__narrator">✒️ Narrator</option>${characters ? `<optgroup label="Characters">${characters}</optgroup>` : ""}`;
}

function closeRowMenus(except = null) {
  for (const menu of document.querySelectorAll(".cast-row-menu.open")) {
    if (menu !== except) menu.classList.remove("open");
  }
}

function moveRow(row, delta) {
  const sibling = delta < 0 ? row.previousElementSibling : row.nextElementSibling;
  if (!sibling) return;
  if (delta < 0) row.parentNode.insertBefore(row, sibling);
  else row.parentNode.insertBefore(sibling, row);
  row.querySelector("[data-roster-drag]")?.focus();
}

function showCastManager() {
  if (!S.groupCast) return;
  const rotating = S.groupCast.turn_mode === "round_robin";
  showModal(`<h2>Cast</h2>
    <p class="modal-subtitle">${rotating ? "Drag to set the reply order." : "Drag to reorder the cast."}</p>
    <div id="group-roster-list" class="cast-list">${S.groupCast.members.map(castRow).join("")}</div>
    <select id="group-roster-add" class="cast-add" aria-label="Add cast member">${addOptions()}</select>
    <p class="modal-hint">${CONTEXT_LINE}</p>
    <div class="modal-actions"><button type="button" class="btn" id="group-roster-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-roster-save">Save cast</button></div>`);
  const list = $("group-roster-list");
  if (!list) return;

  // A row is only draggable while its handle is held, so the name field keeps
  // normal text selection.
  let dragging = null;
  list.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest("[data-roster-drag]");
    const row = handle?.closest(".cast-row");
    if (row) row.draggable = true;
  });
  list.addEventListener("dragstart", (event) => {
    dragging = event.target.closest(".cast-row");
    if (!dragging) return;
    dragging.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    // Firefox refuses to start a drag without payload.
    event.dataTransfer.setData("text/plain", "");
  });
  list.addEventListener("dragover", (event) => {
    if (!dragging) return;
    event.preventDefault();
    const over = event.target.closest(".cast-row");
    if (!over || over === dragging) return;
    const box = over.getBoundingClientRect();
    const after = event.clientY - box.top > box.height / 2;
    list.insertBefore(dragging, after ? over.nextElementSibling : over);
  });
  const endDrag = () => {
    dragging?.classList.remove("dragging");
    for (const row of list.querySelectorAll(".cast-row")) row.draggable = false;
    dragging = null;
  };
  list.addEventListener("dragend", endDrag);
  // A press that never became a drag must not leave the row draggable, or the
  // name field loses text selection for the rest of the modal's life.
  list.addEventListener("pointerup", endDrag);
  list.addEventListener("drop", (event) => {
    event.preventDefault();
    endDrag();
  });
  // Keyboard equivalent of the handle drag — the up/down buttons this replaced
  // were the only accessible reorder path.
  list.addEventListener("keydown", (event) => {
    const handle = event.target.closest("[data-roster-drag]");
    if (!handle) return;
    const delta = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
    if (!delta) return;
    event.preventDefault();
    moveRow(handle.closest(".cast-row"), delta);
  });

  list.addEventListener("click", (event) => {
    const row = event.target.closest(".cast-row");
    if (!row) return;
    if (event.target.closest("[data-roster-remove]")) {
      row.remove();
      return;
    }
    const more = event.target.closest("[data-roster-more]");
    if (more) {
      const menu = row.querySelector(".cast-row-menu");
      closeRowMenus(menu);
      menu.classList.toggle("open");
      return;
    }
    closeRowMenus();
  });

  $("group-roster-add")?.addEventListener("change", (event) => {
    const value = event.target.value;
    if (!value) return;
    if (value === "__narrator") {
      list.insertAdjacentHTML("beforeend", castRow({ member_kind: "narrator", display_name: "Narrator" }));
    } else {
      const card = charactersView().find((item) => item.id === value);
      if (card) {
        list.insertAdjacentHTML("beforeend", castRow({ character_card_id: card.id, display_name: card.name }));
        event.target.querySelector(`option[value="${CSS.escape(card.id)}"]`)?.remove();
      }
    }
    event.target.value = "";
    list.lastElementChild?.scrollIntoView({ block: "nearest" });
  });

  $("group-roster-cancel")?.addEventListener("click", closeModal);
  $("group-roster-save")?.addEventListener("click", async () => {
    if (S.castSetupBusy) return;
    const members = [...list.querySelectorAll(".cast-row")].map((row) => ({
      id: row.dataset.rosterMemberId || null,
      character_card_id: row.dataset.rosterCardId || null,
      display_name: row.querySelector("[data-roster-name]").value.trim() || "Narrator",
      public_profile_override: row.querySelector("[data-roster-profile]").value.trim() || null,
      member_kind: row.dataset.rosterKind || "character",
      muted: !row.querySelector("[data-roster-reply]").checked,
    }));
    if (!members.length) {
      toast("A scene needs at least one cast member", true);
      return;
    }
    S.castSetupBusy = true;
    try {
      const updated = await api.put(convUrl(S.activeConvId, "members"), { members });
      S.groupCast = { ...S.groupCast, members: updated };
      const local = S.conversations.find((item) => item.id === S.activeConvId);
      if (local) {
        local.group_member_names = updated.map((member) => member.display_name);
        local.group_card_ids = updated.flatMap((member) =>
          member.character_card_id ? [member.character_card_id] : [],
        );
      }
      if (!updated.some((member) => member.id === S.pinnedSpeakerId && !member.muted)) S.pinnedSpeakerId = null;
      closeModal();
      renderGroupCast();
      renderGroupList();
      notify("cast", S.groupCast);
    } catch (error) {
      toast(error.message, true);
    } finally {
      S.castSetupBusy = false;
    }
  });
}

// ── Chat surface ────────────────────────────────────────────────────────────

// Which secondary actions the header's ••• (and its mobile twin) offer. Convert
// is a solo-only capability and must never appear inside a group, even mid-switch.
function renderChatActionMenus() {
  const grouped = Boolean(S.groupCast);
  const visible = {
    "group-settings": grouped,
    "manage-cast": grouped,
    "convert-to-group": !grouped && Boolean(S.activeConvId),
    inspector: true,
  };
  for (const item of document.querySelectorAll("[data-chat-action]")) {
    item.hidden = !visible[item.dataset.chatAction];
  }
}

export function renderGroupCast() {
  const rail = $("group-cast-rail");
  const plan = $("group-speaking-plan");
  const bar = $("group-reply-bar");
  if (!rail || !plan || !bar) return;
  const grouped = Boolean(S.groupCast);
  rail.hidden = !grouped;
  bar.hidden = !grouped;
  rail.innerHTML = castRailHtml();
  bar.innerHTML = replyBarHtml();
  const planHtml = speakingPlanHtml();
  plan.innerHTML = planHtml;
  plan.hidden = !planHtml;
  renderChatActionMenus();
  const input = $("chat-input");
  if (input) input.placeholder = grouped ? "Write what happens next…" : "Write your message...";
  // `Choose` needs a speaker before anything can be sent, so the composer must
  // reflect a mode change or a cleared override immediately — not only after the
  // next stream settles.
  if (grouped && !S.isStreaming && S.activeConvId) {
    $("send-btn").disabled = S.groupCast.turn_mode === "manual" && !S.pinnedSpeakerId;
  }
}

// A speaker override is a one-shot instruction outside `manual` mode: it names
// who replies next, then gets out of the way so the configured strategy resumes.
export function consumeSpeakerOverride() {
  if (!S.groupCast || S.groupCast.turn_mode === "manual") return;
  if (!S.pinnedSpeakerId) return;
  S.pinnedSpeakerId = null;
  renderGroupCast();
}

export function renderGroupList() {
  const list = $("group-chat-list");
  if (!list) return;
  list.innerHTML = S.conversations
    .filter((conv) => conv.kind === "group")
    .map((conv) => {
      const members = (conv.group_member_names || []).filter(Boolean);
      const memberLine = members.length ? members.join(" · ") : "No active cast members";
      const cardIds = conv.group_card_ids || [];
      const shownCardIds = cardIds.slice(0, 3);
      const avatars = shownCardIds
        .map(
          (cardId) =>
            `<span class="group-chat-avatar">${avatarCell(escAttr(avatarUrl(cardId)), {
              icon: "👤",
              attrs: 'loading="lazy" decoding="async"',
            })}</span>`,
        )
        .join("");
      const remaining = cardIds.length - shownCardIds.length;
      const avatarStack = avatars || `<span class="group-chat-avatar group-chat-narrator">✒️</span>`;
      return `<div class="group-chat-item">
          <button type="button" class="group-chat-select" data-group-conversation-id="${escAttr(conv.id)}" title="Cast: ${escAttr(memberLine)}">
            <span class="group-chat-avatar-stack" aria-hidden="true">${avatarStack}${remaining ? `<span class="group-chat-avatar group-chat-overflow">+${remaining}</span>` : ""}</span>
            <span class="group-chat-details"><span class="group-chat-title">${esc(conv.title)}</span><span class="group-chat-members">${esc(memberLine)}</span></span>
          </button>
          <button type="button" class="btn-icon group-chat-delete" data-group-delete-conversation-id="${escAttr(conv.id)}" title="Delete group" aria-label="Delete group ${escAttr(conv.title)}">${SIDEBAR_CLOSE_ICON}</button>
        </div>`;
    })
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

async function convertToGroup() {
  if (!S.activeConvId || S.groupCast || S.castSetupBusy) return;
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
}

function setSpeakerOverride(memberId) {
  S.pinnedSpeakerId = S.pinnedSpeakerId === memberId ? null : memberId;
  renderGroupCast();
}

export function initGroupSetup() {
  $("groups-section-toggle")?.addEventListener("click", (event) => {
    event.currentTarget.querySelector(".arrow")?.classList.toggle("collapsed");
    event.currentTarget.nextElementSibling?.classList.toggle("collapsed");
  });
  $("new-group-btn")?.addEventListener("click", showGroupCreate);
  $("group-chat-list")?.addEventListener("click", (event) => {
    const deleteButton = event.target.closest("[data-group-delete-conversation-id]");
    if (deleteButton) {
      document.dispatchEvent(
        new CustomEvent("group-delete-request", { detail: deleteButton.dataset.groupDeleteConversationId }),
      );
      return;
    }
    const button = event.target.closest("[data-group-conversation-id]");
    if (button)
      document.dispatchEvent(new CustomEvent("group-selected", { detail: button.dataset.groupConversationId }));
  });
  $("group-cast-rail")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-cast-manage]")) {
      showCastManager();
      return;
    }
    const button = event.target.closest("[data-cast-member-id]");
    if (!button || button.disabled || S.isStreaming) return;
    setSpeakerOverride(button.dataset.castMemberId);
  });
  $("group-reply-bar")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-group-settings]")) showGroupSettings();
    else if (event.target.closest("[data-clear-override]")) setSpeakerOverride(S.pinnedSpeakerId);
    else if (event.target.closest("[data-speak-now]")) document.dispatchEvent(new CustomEvent("group-speak-request"));
  });
  // Empty-scene starters. "Describe the opening" hands the user the composer;
  // "Let a character begin" opens the scene with the first eligible member.
  $("chat-messages")?.addEventListener("click", (event) => {
    const starter = event.target.closest("[data-scene-starter]")?.dataset.sceneStarter;
    if (!starter || !S.groupCast) return;
    if (starter === "describe") {
      $("chat-input")?.focus();
      return;
    }
    const member = eligibleMembers()[0];
    if (member) document.dispatchEvent(new CustomEvent("group-speak-request", { detail: member.id }));
  });
  document.addEventListener("group-settings-request", showGroupSettings);
  document.addEventListener("manage-cast-request", showCastManager);
  document.addEventListener("convert-to-group-request", convertToGroup);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".cast-row")) closeRowMenus();
  });
}
