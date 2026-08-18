import { api } from "./api.js";
import { showAvatarPopup } from "./chat_inspector.js";
import {
  CONTEXT_MODES,
  castClickSpeaksNow,
  castRailHtml,
  contextMode,
  eligibleMembers,
  groupFamilies,
  groupRootId,
  memberAvatar,
  overrideIsOneShot,
  speakingPlanHtml,
  TURN_MODES,
} from "./group_cast.js";
import { closeModal, showModal } from "./modal.js";
import { SIDEBAR_CLOSE_ICON } from "./sidebar_icons.js";
import { charactersView, notify, S } from "./state.js";
import { $, avatarCell, avatarUrl, convUrl, esc, escAttr, toast } from "./utils.js";

// What every mode shares, so no per-mode blurb has to repeat it. Kept separate
// from the mode wording in group_cast.js: that table describes the differences,
// this sentence describes the floor under all three.
const CONTEXT_COMMON =
  "In every mode, the scene premise, user persona, cast names, and the cast's linked Worlds are shared with the whole scene. A card's system-prompt override is always ignored, and its post-history instructions are sent only on that member's turn. Per-member private lore isn't supported yet.";

// One sentence a group surface can afford to show permanently, stating *that
// mode's* contract — under Shared dossier the old blanket privacy claim would
// simply be false. For Manage cast, which shows the mode without offering it;
// a surface that owns the dropdown points at `contextHelp` instead.
function contextLine(mode) {
  return `${contextMode(mode).label}: ${contextMode(mode).detail}`;
}

// The full comparison, always all three modes — this is the only place either
// modal explains them, so it carries every mode's consequence and cost.
function contextHelp() {
  const modes = Object.values(CONTEXT_MODES)
    .map((item) => `<p class="modal-hint"><b>${esc(item.label)}</b> — ${esc(item.detail)} ${esc(item.billing)}</p>`)
    .join("");
  return `<details class="group-help"><summary>How character context works</summary>
  ${modes}
  <p class="modal-hint">${esc(CONTEXT_COMMON)}</p>
</details>`;
}

function modeOptions(selected) {
  return Object.entries(TURN_MODES)
    .map(
      ([value, mode]) =>
        `<option value="${value}"${value === selected ? " selected" : ""}>${esc(mode.label)} — ${esc(mode.hint)}</option>`,
    )
    .join("");
}

// Label only — unlike TURN_MODES, whose one-line hint fits in an option. A
// context mode's consequence takes a sentence, so the option stays short and
// the disclosure below carries the explanation for all three at once.
function contextModeOptions(selected) {
  return Object.entries(CONTEXT_MODES)
    .map(
      ([value, mode]) => `<option value="${value}"${value === selected ? " selected" : ""}>${esc(mode.label)}</option>`,
    )
    .join("");
}

// Creation's dropdown label — sparse on purpose; the disclosure sitting right
// below it carries the explanation, so the label itself doesn't have to. Group
// settings doesn't reuse this: a "Character context" h3 already sits above
// that select, so it labels the control itself with just "Mode" instead.
const CONTEXT_LABEL = "Character context";

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
    <div class="field"><label for="group-create-scenario">Premise</label>
      <textarea id="group-create-scenario" rows="3" placeholder="Where and when does this open? (optional)"></textarea></div>
    <details class="group-advanced"><summary>Advanced</summary>
      <div class="field"><label for="group-create-title">Title</label>
        <input id="group-create-title" placeholder="Named after the cast"></div>
      <div class="field"><label for="group-create-context">${esc(CONTEXT_LABEL)}</label>
        <select id="group-create-context">${contextModeOptions("private")}</select></div>
      <div class="field"><label for="group-create-mode">Reply behavior</label>
        <select id="group-create-mode">${modeOptions("director")}</select></div>
      <div class="field" id="group-create-max-row"><label for="group-create-max">Max replies per turn</label>
        <input id="group-create-max" type="number" min="1" max="8" value="3"></div>
      <div class="field"><label for="group-create-instructions">Style &amp; instructions</label>
        <textarea id="group-create-instructions" rows="2" placeholder="How should this scene be written?"></textarea></div>
    </details>
    ${contextHelp()}
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
        group_context_mode: $("group-create-context").value,
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
  const rootId = groupRootId(conv);
  // Every setting here is per-scene, the title included — but the sidebar names
  // the group after the scene it started as, so a fork has to say where its own
  // name will and won't show up rather than letting the rename look broken.
  const root = rootId === conv?.id ? null : S.conversations.find((item) => item.id === rootId);
  const lineage = root
    ? `<p class="modal-hint">This scene is part of <b>${esc(root.title)}</b>, which keeps that name in the sidebar — renaming here changes only this scene's title.</p>`
    : "";
  showModal(`<h2>Group settings</h2>
    <div class="field"><label for="group-settings-title">Title</label>
      <input id="group-settings-title" value="${escAttr(conv?.title || "")}">${lineage}</div>
    <h3 class="modal-section">Character context</h3>
    <div class="field"><label for="group-settings-context">Mode</label>
      <select id="group-settings-context">${contextModeOptions(S.groupCast.context_mode)}</select></div>
    <h3 class="modal-section">Reply behavior</h3>
    <div class="field"><label for="group-settings-mode">Mode</label>
      <select id="group-settings-mode">${modeOptions(S.groupCast.turn_mode)}</select></div>
    <div class="field" id="group-settings-max-row"><label for="group-settings-max">Max replies per turn</label>
      <input id="group-settings-max" type="number" min="1" max="8" value="${Number(S.groupCast.max_speakers) || 3}"></div>
    <div class="field"><label for="group-settings-scenario">Premise</label>
      <textarea id="group-settings-scenario" rows="3" placeholder="Where and when does this open?">${esc(conv?.character_scenario || "")}</textarea></div>
    <div class="field"><label for="group-settings-instructions">Style &amp; instructions</label>
      <textarea id="group-settings-instructions" rows="2" placeholder="How should this scene be written?">${esc(conv?.post_history_instructions || "")}</textarea></div>
    ${contextHelp()}
    <div class="modal-actions"><button type="button" class="btn btn-danger" id="group-settings-delete">Delete group</button><div style="flex:1"></div><button type="button" class="btn" id="group-settings-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-settings-save">Save</button></div>`);
  syncMaxRepliesRow("group-settings-mode", "group-settings-max-row");
  $("group-settings-mode")?.addEventListener("change", () =>
    syncMaxRepliesRow("group-settings-mode", "group-settings-max-row"),
  );
  $("group-settings-cancel")?.addEventListener("click", closeModal);
  $("group-settings-delete")?.addEventListener("click", () => {
    closeModal();
    // The whole group, not just the scene in front of us — so the request
    // carries the family's root, exactly as the sidebar's × does.
    document.dispatchEvent(new CustomEvent("group-delete-request", { detail: rootId }));
  });
  $("group-settings-save")?.addEventListener("click", async () => {
    if (S.castSetupBusy) return;
    S.castSetupBusy = true;
    try {
      const updated = await api.put(`/conversations/${S.activeConvId}`, {
        title: $("group-settings-title").value.trim() || conv?.title || "New Group",
        group_turn_mode: $("group-settings-mode").value,
        group_max_speakers: Math.max(1, Math.min(8, Number($("group-settings-max").value) || 3)),
        group_context_mode: $("group-settings-context").value,
        character_scenario: $("group-settings-scenario").value.trim(),
        post_history_instructions: $("group-settings-instructions").value.trim(),
      });
      const local = S.conversations.find((item) => item.id === S.activeConvId);
      if (local) Object.assign(local, updated);
      S.groupCast.turn_mode = updated.group_turn_mode;
      S.groupCast.max_speakers = updated.group_max_speakers;
      S.groupCast.context_mode = updated.group_context_mode;
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

// One string, one meaning — the override is always the same stored field, so
// the box never repoints at a different slot. Only what the scene *does* with it
// changes, and Private perspective is the one mode that sends it: it is the
// scene's only privacy boundary, so it is the only place a curated view of a
// member is doing work. The other two say so instead of silently accepting text
// that never ships. Each entry's `placeholder` doubles as the one-line reason a
// disabled control shows, so there is one sentence per mode and not two.
const OVERRIDE_COPY = {
  private: { placeholder: "Public profile override — how the rest of the cast sees them", disabled: false },
  shared: { placeholder: "Not sent under Shared dossier — every member already reads every card", disabled: true },
  swap: { placeholder: "Not sent under Classic card swap — other members see names only", disabled: true },
};

function overrideCopy() {
  return OVERRIDE_COPY[S.groupCast?.context_mode] || OVERRIDE_COPY.private;
}

// One definition of "this row can be drafted", read by the row button, the
// cast-wide filter and the per-row guard alike — three places that would
// otherwise each re-derive the rule and eventually disagree. A member with no
// card has nothing to draft *from*; a mode that never sends the override has
// nothing to draft *for*.
function canDraftProfile(member) {
  return !overrideCopy().disabled && Boolean(member.character_card_id);
}

// Why a Draft button is off, in one line — the same sentence the disabled
// textarea shows, so a row never gives two different reasons.
function draftBlockedReason(member) {
  if (overrideCopy().disabled) return overrideCopy().placeholder;
  return member.character_card_id ? "" : "This member has no character card to draft from";
}

function castRow(member) {
  const name = member.display_name || "Narrator";
  const override = overrideCopy();
  const draftable = canDraftProfile(member);
  return `<div class="cast-row" data-roster-member-id="${escAttr(member.id || "")}" data-roster-card-id="${escAttr(member.character_card_id || "")}" data-roster-kind="${escAttr(member.member_kind || "character")}">
    <button type="button" class="cast-drag" data-roster-drag title="Drag, or use the arrow keys, to reorder" aria-label="Reorder ${escAttr(name)}">⠿</button>
    ${memberAvatar(member)}
    <input data-roster-name value="${escAttr(name)}" aria-label="Display name">
    <label class="cast-reply-toggle" title="Muted members stay in the scene but never take a turn"><input type="checkbox" data-roster-reply ${member.muted ? "" : "checked"}> Can reply</label>
    <button type="button" class="cast-row-more" data-roster-more title="More actions" aria-label="More actions for ${escAttr(name)}">•••</button>
    <div class="cast-row-menu"><button type="button" class="burger-menu-item" data-roster-remove>Remove from scene</button></div>
    <details class="cast-row-custom"><summary>Customize for this scene</summary>
      <div class="cast-row-profile">
        <textarea data-roster-profile${override.disabled ? " disabled" : ""} placeholder="${escAttr(override.placeholder)}">${esc(member.public_profile_override || "")}</textarea>
        <button type="button" class="btn" data-roster-draft${draftable ? "" : ` disabled title="${escAttr(draftBlockedReason(member))}"`}>${member.public_profile_override ? "Redraft" : "Draft"}</button>
      </div>
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
  const modeBlocks = overrideCopy().disabled;
  showModal(`<div class="modal-title-row">
      <div>
        <h2>Cast</h2>
        <p class="modal-subtitle">${rotating ? "Drag to set the reply order." : "Drag to reorder the cast."}</p>
      </div>
      <div class="modal-title-actions">
        <button type="button" class="btn" id="group-roster-draft-all"${modeBlocks ? ` disabled title="${escAttr(overrideCopy().placeholder)}"` : ""}>Draft scene profiles</button>
      </div>
    </div>
    <div id="group-roster-list" class="cast-list">${S.groupCast.members.map(castRow).join("")}</div>
    <select id="group-roster-add" class="cast-add" aria-label="Add cast member">${addOptions()}</select>
    <p class="modal-hint">${esc(contextLine(S.groupCast.context_mode))}</p>
    <div class="modal-actions"><button type="button" class="btn" id="group-roster-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-roster-save">Save cast</button></div>`);
  const list = $("group-roster-list");
  if (!list) return;

  // Drafting state is closure-local to this modal — nothing here outlives it,
  // so nothing here belongs in `S`. Explicitly *not* `S.castSetupBusy`: four
  // other surfaces read that flag, and overloading it would couple drafting
  // here to saving elsewhere.
  const drafting = new Set();
  let draftingAll = false;
  let cancelAll = false;
  const anyDrafting = () => drafting.size > 0;

  // A late draft must not land after Save has already sent the roster, and a
  // save must not race a request whose result would then have nowhere to go.
  // Gated on the request count, not on the cast-wide loop: a single row draft
  // is just as losable.
  function syncSaveGate() {
    const save = $("group-roster-save");
    if (!save) return;
    save.disabled = anyDrafting();
    save.title = anyDrafting() ? "Wait for the scene profiles to finish drafting" : "";
  }

  // One row's draft. Rethrows, so the caller decides whether to toast — the
  // cast-wide loop reports once with its progress instead of once per row.
  async function draftRow(row, btn) {
    const cardId = row.dataset.rosterCardId;
    const box = row.querySelector("[data-roster-profile]");
    const nameInput = row.querySelector("[data-roster-name]");
    if (!cardId || !box || !nameInput || !btn || drafting.has(row)) return;
    // Snapshotted before the POST. A result may only land on the row it was
    // asked about, still holding the text the user had when they asked: the
    // modal is client-side until Save, so an edit made while the request was in
    // flight is the newer answer.
    const asked = { name: nameInput.value, text: box.value };
    drafting.add(row);
    btn.disabled = true;
    btn.textContent = "Drafting…";
    syncSaveGate();
    try {
      const others = [...list.querySelectorAll(".cast-row")]
        .filter((other) => other !== row)
        .map((other) => other.querySelector("[data-roster-name]")?.value.trim() || "")
        .filter(Boolean);
      const draft = await api.post(convUrl(S.activeConvId, "members", "scene-profile", "generate"), {
        character_card_id: cardId,
        display_name: asked.name.trim(),
        cast_names: others,
      });
      // `closeModal()` empties #modal-root, so a result landing after the modal
      // closed has nowhere to write. Same for a row that has since been
      // repointed at another card or edited by hand.
      if (!row.isConnected || row.dataset.rosterCardId !== cardId) return;
      if (nameInput.value !== asked.name || box.value !== asked.text) return;
      box.value = draft.profile || "";
    } finally {
      drafting.delete(row);
      if (row.isConnected && btn.isConnected) {
        btn.disabled = false;
        btn.textContent = box.value.trim() ? "Redraft" : "Draft";
      }
      syncSaveGate();
    }
  }

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
    const draft = event.target.closest("[data-roster-draft]");
    if (draft) {
      // The standalone path owns its own error reporting; the cast-wide loop
      // reports once, with a count.
      draftRow(row, draft).catch((error) => toast(error.message, true));
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

  // Fills every empty scene profile in sequence, one call per member — never a
  // batch. A batched prompt would have to carry every member's card, which is
  // exactly the leak Private perspective exists to prevent.
  $("group-roster-draft-all")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (draftingAll) {
      cancelAll = true;
      button.textContent = "Stopping…";
      return;
    }
    const rows = [...list.querySelectorAll(".cast-row")].filter(
      (row) =>
        canDraftProfile({ character_card_id: row.dataset.rosterCardId }) &&
        !drafting.has(row) &&
        !row.querySelector("[data-roster-profile]").value.trim(),
    );
    if (!rows.length) {
      toast("Every card-backed member already has a scene profile");
      return;
    }
    draftingAll = true;
    cancelAll = false;
    let done = 0;
    try {
      for (const row of rows) {
        if (cancelAll || !row.isConnected) break;
        button.textContent = `Stop (${done + 1}/${rows.length})`;
        // Each row opens and scrolls as its turn comes, so the run is visibly
        // a sequence of members rather than one opaque wait.
        row.querySelector(".cast-row-custom")?.setAttribute("open", "");
        row.scrollIntoView({ block: "nearest" });
        await draftRow(row, row.querySelector("[data-roster-draft]"));
        done += 1;
      }
      toast(`Drafted ${done} scene profile${done === 1 ? "" : "s"} — review, then Save cast`);
    } catch (error) {
      // Stop rather than press on. A drafting failure is almost always systemic
      // — a dead endpoint, a bad key — so continuing buys N-1 more sticky
      // toasts and N-1 more billed calls.
      toast(`Drafted ${done} of ${rows.length} before stopping — ${error.message}`, true);
    } finally {
      draftingAll = false;
      cancelAll = false;
      button.textContent = "Draft scene profiles";
      syncSaveGate();
    }
  });

  $("group-roster-cancel")?.addEventListener("click", closeModal);
  $("group-roster-save")?.addEventListener("click", async () => {
    if (S.castSetupBusy) return;
    if (anyDrafting()) {
      toast("Wait for the scene profiles to finish drafting", true);
      return;
    }
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
      // The cast owns the scene's card-embedded fragments; whoever holds them
      // re-reads on this rather than watching the roster.
      document.dispatchEvent(new CustomEvent("group-cast-updated", { detail: S.activeConvId }));
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
  // The header's ••• only has group actions to offer, so a solo scene hides it.
  $("chat-overflow-btn").hidden = !grouped;
}

// Whether the composer is holding something that wants an answer. A drafted
// message is the difference between "queue X" and "X, speak now", so the rail
// has to know — but group_cast.js stays DOM-free, hence the read lives here.
function composerHasDraft() {
  return Boolean($("chat-input")?.value.trim()) || (S.attachments?.length || 0) > 0;
}

// Last draft state the rail was painted for, so a keystroke only repaints when
// it flips the chips' meaning rather than on every character typed.
let railDrafted = false;

// Called by the composer on every input and whenever attachments change; cheap
// enough to run per keystroke because it exits before touching the DOM.
export function refreshCastRailIntent() {
  if (!S.groupCast) return;
  if (composerHasDraft() === railDrafted) return;
  renderGroupCast();
}

export function renderGroupCast() {
  const rail = $("group-cast-rail");
  const plan = $("group-speaking-plan");
  if (!rail || !plan) return;
  const grouped = Boolean(S.groupCast);
  railDrafted = composerHasDraft();
  rail.hidden = !grouped;
  rail.innerHTML = castRailHtml({ hasDraft: railDrafted });
  const planHtml = speakingPlanHtml();
  plan.innerHTML = planHtml;
  plan.hidden = !planHtml;
  renderChatActionMenus();
  const input = $("chat-input");
  if (input) input.placeholder = grouped ? "Write what happens next…" : "Write your message...";
  // `Choose` needs a speaker before anything can be sent, so the composer must
  // reflect a mode change or a cleared override immediately — not only after the
  // next stream settles. Nothing on screen explains a dead send button now that
  // the reply bar is gone, so the button says why itself — and the reset above
  // it matters: leaving that reason behind would mislabel the next solo chat.
  const send = $("send-btn");
  if (send) send.title = "Send";
  if (grouped && send && !S.isStreaming && S.activeConvId) {
    const blocked = S.groupCast.turn_mode === "manual" && !S.pinnedSpeakerId;
    send.disabled = blocked;
    if (blocked) send.title = "Choose mode: click a cast member to pick who replies";
  }
}

// A speaker override is a one-shot instruction outside `manual` mode: it names
// who replies next, then gets out of the way so the configured strategy resumes.
// Only the pick this beat actually consumed is cleared — a chip clicked *during*
// the beat queued someone for the next one and must survive it.
export function consumeSpeakerOverride() {
  if (!S.groupCast || !overrideIsOneShot()) return;
  if (!S.pinnedSpeakerId || S.pinnedSpeakerId !== S.consumedSpeakerId) return;
  S.pinnedSpeakerId = null;
  renderGroupCast();
}

// One row per group, not per conversation. A checkpoint is a branch of the
// scene, so it belongs *inside* its group's row — reachable through the
// conversations modal — rather than beside it as a second group.
export function renderGroupList() {
  const list = $("group-chat-list");
  if (!list) return;
  list.innerHTML = groupFamilies(S.conversations)
    .map(({ rootId, root, newest, members }) => {
      // Title from the root (the group's stable name), cast from the newest
      // conversation (the one the click opens, whose roster may have moved on).
      const names = (newest.group_member_names || []).filter(Boolean);
      const memberLine = names.length ? names.join(" · ") : "No active cast members";
      const cardIds = newest.group_card_ids || [];
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
      const open = members.some((conv) => conv.id === S.activeConvId);
      // Silent at one conversation: the count is only news once the group has
      // branched, and every group starts with exactly one.
      const countBadge =
        members.length > 1
          ? `<span class="group-chat-count" title="${escAttr(`${members.length} conversations in this group`)}">${members.length}</span>`
          : "";
      const title = `Cast: ${memberLine}${members.length > 1 ? `\n${members.length} conversations — open the group, then ••• › Conversations` : ""}`;
      return `<div class="group-chat-item${open ? " active" : ""}">
          <button type="button" class="group-chat-select" data-group-conversation-id="${escAttr(newest.id)}" title="${escAttr(title)}">
            <span class="group-chat-avatar-stack" aria-hidden="true">${avatarStack}${remaining ? `<span class="group-chat-avatar group-chat-overflow">+${remaining}</span>` : ""}</span>
            <span class="group-chat-details"><span class="group-chat-title">${esc(root.title)}</span><span class="group-chat-members">${esc(memberLine)}</span></span>
            ${countBadge}
          </button>
          <button type="button" class="btn-icon group-chat-delete" data-group-delete-root-id="${escAttr(rootId)}" title="Delete group" aria-label="Delete group ${escAttr(root.title)}">${SIDEBAR_CLOSE_ICON}</button>
        </div>`;
    })
    .join("");
}

export async function loadGroupCast(conv) {
  if (!conv || conv.kind !== "group") {
    S.groupCast = null;
    S.pinnedSpeakerId = null;
    S.consumedSpeakerId = null;
    renderGroupCast();
    notify("cast", null);
    return;
  }
  const members = await api.get(convUrl(conv.id, "members"));
  S.groupCast = {
    members,
    turn_mode: conv.group_turn_mode,
    max_speakers: conv.group_max_speakers,
    context_mode: conv.group_context_mode,
  };
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

// A cast chip is the whole reply control now that the strategy line is gone.
// On a resting scene the click hands the member the floor immediately; while a
// beat runs or a draft waits to be sent it only names them as the next speaker,
// and clicking whoever is already named there takes the pick back.
//
// The pin is set on the speak path too, not just the queue path: it is what the
// chip lights up, and afterStream's one-shot cleanup then clears it for us
// outside `Choose` mode, where the pick is the strategy and should persist.
function onCastChipClick(memberId) {
  if (castClickSpeaksNow(composerHasDraft())) {
    S.pinnedSpeakerId = memberId;
    renderGroupCast();
    document.dispatchEvent(new CustomEvent("group-speak-request", { detail: memberId }));
    return;
  }
  S.pinnedSpeakerId = S.pinnedSpeakerId === memberId ? null : memberId;
  renderGroupCast();
}

export function initGroupSetup() {
  // The header avatar. A solo chat carries its own handler on the portrait
  // inside; a group's frame holds only the scene glyph, so the listener lives
  // here — and stays inert in a solo chat, where the inner one already fired.
  $("chat-avatar")?.addEventListener("click", (event) => {
    if (S.groupCast && event.target === event.currentTarget) showAvatarPopup();
  });
  $("groups-section-toggle")?.addEventListener("click", (event) => {
    event.currentTarget.querySelector(".arrow")?.classList.toggle("collapsed");
    event.currentTarget.nextElementSibling?.classList.toggle("collapsed");
  });
  $("new-group-btn")?.addEventListener("click", showGroupCreate);
  $("group-chat-list")?.addEventListener("click", (event) => {
    const deleteButton = event.target.closest("[data-group-delete-root-id]");
    if (deleteButton) {
      document.dispatchEvent(
        new CustomEvent("group-delete-request", { detail: deleteButton.dataset.groupDeleteRootId }),
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
    // Mid-stream clicks are allowed now — they queue rather than speak, which is
    // the one thing a user watching a beat run actually wants to do.
    const button = event.target.closest("[data-cast-member-id]");
    if (!button || button.disabled) return;
    onCastChipClick(button.dataset.castMemberId);
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
