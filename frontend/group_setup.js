import { api } from "./api.js";
import { showAvatarPopup } from "./chat_inspector.js";
import { initDragReorder } from "./drag_reorder.js";
import {
  CONTEXT_MODES,
  castClickSpeaksNow,
  castRailHtml,
  contextMode,
  eligibleMembers,
  GROUP_LIMIT,
  groupFamilies,
  groupRootId,
  memberAvatar,
  overrideIsOneShot,
  recommendContextMode,
  speakingPlanHtml,
  TURN_MODES,
  visibleGroups,
} from "./group_cast.js";
import { closeModal, setModalCloseGuard, showModal, switchTab } from "./modal.js";
import { SIDEBAR_CLOSE_ICON } from "./sidebar_icons.js";
import { charactersView, notify, S } from "./state.js";
import { $, avatarCell, avatarUrl, convUrl, esc, escAttr, toast } from "./utils.js";

// Both group setup views use this shared context explanation.

const CONTEXT_COMMON =
  "In every mode, the scene premise, user persona, cast names, and the cast's linked Worlds are shared with the whole scene. A card's system-prompt override is always ignored, and its post-history instructions are sent only on that member's turn. Per-member private lore isn't supported yet.";

function contextLine(mode) {
  return `${contextMode(mode).label}: ${contextMode(mode).detail}`;
}

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

function contextModeOptions(selected) {
  return Object.entries(CONTEXT_MODES)
    .map(
      ([value, mode]) => `<option value="${value}"${value === selected ? " selected" : ""}>${esc(mode.label)}</option>`,
    )
    .join("");
}

const CONTEXT_LABEL = "Character context";

function recommendHtml(rec, current) {
  if (!rec) return "";
  const label = contextMode(rec.mode).label;
  const action =
    rec.mode === current
      ? `<span class="ctx-rec-state">Selected</span>`
      : `<button type="button" class="btn btn-sm" data-ctx-apply="${escAttr(rec.mode)}">Use this</button>`;
  return `<p class="ctx-rec-head"><span class="ctx-rec-tag">Recommended</span><b>${esc(label)}</b>${action}</p>
    <p class="modal-hint">${esc(rec.why)}</p>
    <p class="modal-hint ctx-rec-trade">${esc(rec.cost)}</p>`;
}

function syncContextRecommendation() {
  const host = $("group-create-recommend");
  if (!host) return;
  const cards = charactersView();
  const chosen = [...document.querySelectorAll("#group-create-picker .cast-pick.selected")].map((pick) =>
    cards.find((card) => card.id === pick.dataset.groupCardId),
  );
  const rec = recommendContextMode(chosen);
  host.hidden = !rec;
  host.innerHTML = recommendHtml(rec, $("group-create-context")?.value);
}

function syncMaxRepliesRow(selectId, rowId) {
  const row = $(rowId);
  if (row) row.hidden = $(selectId)?.value !== "director";
}

function syncSheetUpdatesRow(selectId, rowId) {
  const row = $(rowId);
  if (row) row.hidden = $(selectId)?.value !== "private";
}

function titleFromNames(names) {
  if (!names.length) return "New Group";
  if (names.length <= 2) return names.join(" & ");
  return `${names[0]}, ${names[1]} & ${names.length - 2} more`;
}

function pickCardHtml(card) {
  return `<button type="button" class="cast-pick" data-group-card-id="${escAttr(card.id)}" aria-pressed="false">
    <span class="cast-pick-avatar">${avatarCell(escAttr(avatarUrl(card.id)), {
      icon: "👤",
      attrs: 'loading="lazy" decoding="async"',
    })}</span>
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
    <div class="ctx-recommend" id="group-create-recommend" aria-live="polite" hidden></div>
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
    syncContextRecommendation();
  });
  $("group-create-context")?.addEventListener("change", syncContextRecommendation);
  $("group-create-recommend")?.addEventListener("click", (event) => {
    const apply = event.target.closest("[data-ctx-apply]");
    if (!apply) return;
    const select = $("group-create-context");
    if (!select) return;
    select.value = apply.dataset.ctxApply;
    const advanced = select.closest("details");
    if (advanced) advanced.open = true;
    syncContextRecommendation();
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

function lineageHint(conv, rootId) {
  const root = rootId === conv?.id ? null : S.conversations.find((item) => item.id === rootId);
  return root
    ? `<p class="modal-hint">This scene is part of <b>${esc(root.title)}</b>, which keeps that name in the sidebar — renaming here changes only this scene's title.</p>`
    : "";
}

function settingsPaneHtml(conv, rootId) {
  return `<div class="field"><label for="group-settings-title">Title</label>
      <input id="group-settings-title" value="${escAttr(conv?.title || "")}">${lineageHint(conv, rootId)}</div>
    <h3 class="modal-section">Character context</h3>
    <div class="field"><label for="group-settings-context">Mode</label>
      <select id="group-settings-context">${contextModeOptions(S.groupCast.context_mode)}</select></div>
    <div class="field" id="group-settings-sheet-row"><label class="modal-checkbox-label"><input type="checkbox" id="group-settings-sheet-updates"${S.groupCast.sheet_updates ? " checked" : ""}> Propose sheet updates after each reply</label>
      <p class="modal-hint">A character card describes turn one forever, so a long scene drifts away from it, e.g. change of appearance. After each exchange, each member who spoke is offered a rewritten sheet, which you apply or dismiss on the Cast tab.</p>
      <p class="modal-hint">Costs one extra model call per member who spoke, per exchange.</p></div>
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
    <div class="group-danger">
      <button type="button" class="btn btn-danger" id="group-settings-delete">Delete group</button>
      <p class="modal-hint">Deletes this group and every conversation in it. Unsaved changes on either tab are discarded.</p>
    </div>`;
}

const OVERRIDE_PLACEHOLDER = "Public profile override — how the rest of the cast sees them";
const OVERRIDE_COPY = {
  private: { placeholder: OVERRIDE_PLACEHOLDER, disabled: false },
  shared: { placeholder: "Not sent under Shared dossier — every member already reads every card", disabled: true },
  swap: { placeholder: OVERRIDE_PLACEHOLDER, disabled: false },
};

function overrideCopy(mode) {
  return OVERRIDE_COPY[mode] || OVERRIDE_COPY.private;
}

const SHEET_COPY = {
  private: {
    label: "What they read about themselves",
    placeholder: "Scene sheet override — replaces the card's description and personality for this scene only",
  },
  shared: {
    label: "What they read about themselves — and, under Shared dossier, what the whole cast reads",
    placeholder: "Scene sheet override — under Shared dossier this is the dossier every other member reads",
  },
  swap: {
    label: "What they read about themselves",
    placeholder: "Scene sheet override — sent on their turn only; other members read the public profile above",
  },
};

function sheetCopy(mode) {
  return SHEET_COPY[mode] || SHEET_COPY.private;
}

function proposalsFor(memberId) {
  return (S.groupCast?.sheet_proposals || []).filter((item) => item.member_id === memberId);
}

function proposalRow(proposal) {
  const stale = proposal.status === "stale";
  return `<div class="cast-row-proposal${stale ? " is-stale" : ""}" data-sheet-proposal="${proposal.id}">
    <div class="cast-row-proposal-head">${esc(proposal.summary || "Proposed sheet update")}</div>
    <div class="cast-row-proposal-body">${esc(proposal.proposed_sheet)}</div>
    ${stale ? `<p class="modal-hint">This sheet changed after the update was proposed, so it can no longer be applied.</p>` : ""}
    <div class="cast-row-proposal-actions">
      ${stale ? "" : `<button type="button" class="btn btn-sm" data-sheet-apply>Apply</button>`}
      <button type="button" class="btn btn-sm" data-sheet-reject>Dismiss</button>
    </div>
  </div>`;
}

function canDraftProfile(member, mode) {
  return !overrideCopy(mode).disabled && Boolean(member.character_card_id);
}

function draftBlockedReason(member, mode) {
  if (overrideCopy(mode).disabled) return overrideCopy(mode).placeholder;
  return member.character_card_id ? "" : "This member has no character card to draft from";
}

function castRow(member, mode) {
  const name = member.display_name || "Narrator";
  const override = overrideCopy(mode);
  const draftable = canDraftProfile(member, mode);
  const proposals = proposalsFor(member.id);
  return `<div class="cast-row" data-roster-member-id="${escAttr(member.id || "")}" data-roster-card-id="${escAttr(member.character_card_id || "")}" data-roster-kind="${escAttr(member.member_kind || "character")}">
    <button type="button" class="cast-drag" data-roster-drag title="Drag, or use the arrow keys, to reorder" aria-label="Reorder ${escAttr(name)}">⠿</button>
    ${memberAvatar(member)}
    <input data-roster-name value="${escAttr(name)}" aria-label="Display name">
    <label class="cast-reply-toggle" title="Muted members stay in the scene but never take a turn"><input type="checkbox" data-roster-reply ${member.muted ? "" : "checked"}> Can reply</label>
    <button type="button" class="cast-row-more" data-roster-more title="More actions" aria-label="More actions for ${escAttr(name)}">•••</button>
    <div class="cast-row-menu"><button type="button" class="burger-menu-item" data-roster-remove>Remove from scene</button></div>
    <details class="cast-row-custom"${proposals.length ? " open" : ""}><summary>Customize for this scene${
      proposals.length ? ` <span class="cast-row-summary-badge">${proposals.length}</span>` : ""
    }</summary>
      <div class="cast-row-label">What the rest of the cast sees</div>
      <div class="cast-row-profile">
        <textarea data-roster-profile${override.disabled ? " disabled" : ""} placeholder="${escAttr(override.placeholder)}">${esc(member.public_profile_override || "")}</textarea>
        <button type="button" class="btn" data-roster-draft${draftable ? "" : ` disabled title="${escAttr(draftBlockedReason(member, mode))}"`}>${member.public_profile_override ? "Redraft" : "Draft"}</button>
      </div>
      <div class="cast-row-label" data-roster-sheet-label>${esc(sheetCopy(mode).label)}</div>
      <div class="cast-row-sheet">
        <textarea data-roster-sheet placeholder="${escAttr(sheetCopy(mode).placeholder)}">${esc(member.card_sheet_override || "")}</textarea>
      </div>
      ${proposals.map(proposalRow).join("")}
    </details>
  </div>`;
}

function addOptions(takenCardIds) {
  const taken = new Set(takenCardIds);
  const characters = charactersView()
    .filter((card) => !taken.has(card.id))
    .map((card) => `<option value="${escAttr(card.id)}">${esc(card.name)}</option>`)
    .join("");
  return `<option value="">+ Add cast member…</option><option value="__narrator">✒️ Narrator</option>${characters ? `<optgroup label="Characters">${characters}</optgroup>` : ""}`;
}

function orderHint(turnMode) {
  return turnMode === "round_robin" ? "Drag to set the reply order." : "Drag to reorder the cast.";
}

function castPaneHtml(mode) {
  const blocked = overrideCopy(mode).disabled;
  return `<div class="cast-pane-head">
      <p class="modal-subtitle" id="group-roster-order-hint">${esc(orderHint(S.groupCast.turn_mode))}</p>
      <button type="button" class="btn" id="group-roster-draft-all"${blocked ? ` disabled title="${escAttr(overrideCopy(mode).placeholder)}"` : ""}>Draft scene profiles</button>
    </div>
    <div id="group-roster-list" class="cast-list">${S.groupCast.members.map((member) => castRow(member, mode)).join("")}</div>
    <select id="group-roster-add" class="cast-add" aria-label="Add cast member">${addOptions(
      S.groupCast.members.map((member) => member.character_card_id).filter(Boolean),
    )}</select>
    <p class="modal-hint" id="group-roster-context-line">${esc(contextLine(mode))}</p>`;
}

function closeRowMenus(except = null) {
  for (const menu of document.querySelectorAll(".cast-row-menu.open")) {
    if (menu !== except) menu.classList.remove("open");
  }
}

function showGroupConfig(initialTab = "cast") {
  if (!S.groupCast || !S.activeConvId) return;
  const conv = S.conversations.find((item) => item.id === S.activeConvId);
  const rootId = groupRootId(conv);
  showModal(`<h2 class="modal-title-flush">Scene setup</h2>
    <div class="tabs" role="tablist">
      <div class="tab active" role="tab" tabindex="0" aria-selected="true" data-scene-tab="cast">Cast</div>
      <div class="tab" role="tab" tabindex="0" aria-selected="false" data-scene-tab="settings">Group settings</div>
    </div>
    <div class="tab-content active" id="group-config-cast">${castPaneHtml(S.groupCast.context_mode)}</div>
    <div class="tab-content" id="group-config-settings">${settingsPaneHtml(conv, rootId)}</div>
    <div class="modal-actions"><button type="button" class="btn" id="group-config-cancel">Cancel</button><button type="button" class="btn btn-accent" id="group-config-save">Save</button></div>`);
  const list = $("group-roster-list");
  if (!list) return;

  const drafting = new Set();
  let draftingAll = false;
  let cancelAll = false;
  const anyDrafting = () => drafting.size > 0;

  const currentMode = () => $("group-settings-context")?.value || S.groupCast.context_mode;

  function collectMembers() {
    return [...list.querySelectorAll(".cast-row")].map((row) => ({
      id: row.dataset.rosterMemberId || null,
      character_card_id: row.dataset.rosterCardId || null,
      display_name: row.querySelector("[data-roster-name]").value.trim() || "Narrator",
      public_profile_override: row.querySelector("[data-roster-profile]").value.trim() || null,
      card_sheet_override: row.querySelector("[data-roster-sheet]").value.trim() || null,
      member_kind: row.dataset.rosterKind || "character",
      muted: !row.querySelector("[data-roster-reply]").checked,
    }));
  }

  function collectSettings() {
    const mode = currentMode();
    return {
      title: $("group-settings-title").value.trim() || conv?.title || "New Group",
      group_turn_mode: $("group-settings-mode").value,
      group_max_speakers: Math.max(1, Math.min(8, Number($("group-settings-max").value) || 3)),
      group_context_mode: mode,
      group_sheet_updates: mode === "private" && $("group-settings-sheet-updates").checked,
      character_scenario: $("group-settings-scenario").value.trim(),
      post_history_instructions: $("group-settings-instructions").value.trim(),
    };
  }

  function syncAddOptions() {
    const select = $("group-roster-add");
    if (!select) return;
    select.innerHTML = addOptions(
      [...list.querySelectorAll(".cast-row")].map((row) => row.dataset.rosterCardId).filter(Boolean),
    );
    select.value = "";
  }

  function syncCastForMode() {
    const mode = currentMode();
    const override = overrideCopy(mode);
    const sheet = sheetCopy(mode);
    for (const row of list.querySelectorAll(".cast-row")) {
      const profile = row.querySelector("[data-roster-profile]");
      if (profile) {
        profile.disabled = override.disabled;
        profile.placeholder = override.placeholder;
      }
      const draft = row.querySelector("[data-roster-draft]");
      if (draft && !drafting.has(row)) {
        const member = { character_card_id: row.dataset.rosterCardId };
        const ok = canDraftProfile(member, mode);
        draft.disabled = !ok;
        draft.title = ok ? "" : draftBlockedReason(member, mode);
      }
      const label = row.querySelector("[data-roster-sheet-label]");
      if (label) label.textContent = sheet.label;
      const box = row.querySelector("[data-roster-sheet]");
      if (box) box.placeholder = sheet.placeholder;
    }
    const all = $("group-roster-draft-all");
    if (all && !draftingAll) {
      all.disabled = override.disabled;
      all.title = override.disabled ? override.placeholder : "";
    }
    const line = $("group-roster-context-line");
    if (line) line.textContent = contextLine(mode);
  }

  function syncOrderHint() {
    const hint = $("group-roster-order-hint");
    if (hint) hint.textContent = orderHint($("group-settings-mode")?.value);
  }

  const castBaseline = collectMembers();
  let settingsBaseline = JSON.stringify(collectSettings());
  const castDirty = () => JSON.stringify(collectMembers()) !== JSON.stringify(castBaseline);
  const settingsDirty = () => JSON.stringify(collectSettings()) !== settingsBaseline;
  setModalCloseGuard(() => (!castDirty() && !settingsDirty()) || window.confirm("Discard the changes to this scene?"));

  async function decideProposal(button, row) {
    const card = button.closest("[data-sheet-proposal]");
    const id = card?.dataset.sheetProposal;
    if (!id || button.disabled) return;
    const applying = button.hasAttribute("data-sheet-apply");
    button.disabled = true;
    try {
      const updated = await api.post(convUrl(S.activeConvId, "sheet-proposals", id, applying ? "apply" : "reject"));
      if (applying) {
        const box = row.querySelector("[data-roster-sheet]");
        if (box) box.value = updated.proposed_sheet;
        const member = (S.groupCast.members || []).find((item) => item.id === updated.member_id);
        if (member) member.card_sheet_override = updated.proposed_sheet;
        const before = castBaseline.find((item) => item.id === updated.member_id);
        if (before) before.card_sheet_override = (updated.proposed_sheet || "").trim() || null;
      }
      S.groupCast.sheet_proposals = (S.groupCast.sheet_proposals || []).filter(
        (item) => String(item.id) !== String(id),
      );
      card.remove();
    } catch (error) {
      await refreshSheetProposals();
      const fresh = (S.groupCast.sheet_proposals || []).find((item) => String(item.id) === String(id));
      if (fresh) card.outerHTML = proposalRow(fresh);
      else card.remove();
      throw error;
    } finally {
      renderGroupCast();
    }
  }

  function syncSaveGate() {
    const save = $("group-config-save");
    if (!save) return;
    save.disabled = anyDrafting();
    save.title = anyDrafting() ? "Wait for the scene profiles to finish drafting" : "";
  }

  async function draftRow(row, btn) {
    const cardId = row.dataset.rosterCardId;
    const box = row.querySelector("[data-roster-profile]");
    const nameInput = row.querySelector("[data-roster-name]");
    if (!cardId || !box || !nameInput || !btn || drafting.has(row)) return;
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

  // Order is read back off the DOM in collectMembers(), so a reorder needs no
  // commit of its own; the dirty check picks it up.
  initDragReorder(list, { itemSelector: ".cast-row", handleSelector: "[data-roster-drag]" });

  list.addEventListener("click", (event) => {
    const row = event.target.closest(".cast-row");
    if (!row) return;
    if (event.target.closest("[data-roster-remove]")) {
      row.remove();
      syncAddOptions();
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
      draftRow(row, draft).catch((error) => toast(error.message, true));
      return;
    }
    const decision = event.target.closest("[data-sheet-apply], [data-sheet-reject]");
    if (decision) {
      decideProposal(decision, row).catch((error) => toast(error.message, true));
      return;
    }
    closeRowMenus();
  });

  $("group-roster-add")?.addEventListener("change", (event) => {
    const value = event.target.value;
    if (!value) return;
    const mode = currentMode();
    if (value === "__narrator") {
      list.insertAdjacentHTML("beforeend", castRow({ member_kind: "narrator", display_name: "Narrator" }, mode));
    } else {
      const card = charactersView().find((item) => item.id === value);
      if (card) {
        list.insertAdjacentHTML("beforeend", castRow({ character_card_id: card.id, display_name: card.name }, mode));
      }
    }
    syncAddOptions();
    list.lastElementChild?.scrollIntoView({ block: "nearest" });
  });

  $("group-roster-draft-all")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (draftingAll) {
      cancelAll = true;
      button.textContent = "Stopping…";
      return;
    }
    const mode = currentMode();
    const rows = [...list.querySelectorAll(".cast-row")].filter(
      (row) =>
        canDraftProfile({ character_card_id: row.dataset.rosterCardId }, mode) &&
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
        row.querySelector(".cast-row-custom")?.setAttribute("open", "");
        row.scrollIntoView({ block: "nearest" });
        await draftRow(row, row.querySelector("[data-roster-draft]"));
        done += 1;
      }
      toast(`Drafted ${done} scene profile${done === 1 ? "" : "s"} — review, then Save`);
    } catch (error) {
      toast(`Drafted ${done} of ${rows.length} before stopping — ${error.message}`, true);
    } finally {
      draftingAll = false;
      cancelAll = false;
      button.textContent = "Draft scene profiles";
      syncCastForMode();
      syncSaveGate();
    }
  });

  const tabs = [...document.querySelectorAll("[data-scene-tab]")];
  function openTab(name) {
    const tab = tabs.find((item) => item.dataset.sceneTab === name) || tabs[0];
    if (!tab) return;
    switchTab(tab, `group-config-${tab.dataset.sceneTab}`);
    for (const item of tabs) item.setAttribute("aria-selected", String(item === tab));
  }
  for (const tab of tabs) {
    tab.addEventListener("click", () => openTab(tab.dataset.sceneTab));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openTab(tab.dataset.sceneTab);
    });
  }
  if (initialTab !== "cast") openTab(initialTab);

  syncMaxRepliesRow("group-settings-mode", "group-settings-max-row");
  $("group-settings-mode")?.addEventListener("change", () => {
    syncMaxRepliesRow("group-settings-mode", "group-settings-max-row");
    syncOrderHint();
  });
  syncSheetUpdatesRow("group-settings-context", "group-settings-sheet-row");
  $("group-settings-context")?.addEventListener("change", () => {
    syncSheetUpdatesRow("group-settings-context", "group-settings-sheet-row");
    syncCastForMode();
  });
  $("group-settings-delete")?.addEventListener("click", () => {
    setModalCloseGuard(null);
    closeModal();
    document.dispatchEvent(new CustomEvent("group-delete-request", { detail: rootId }));
  });

  async function saveSettings() {
    const updated = await api.put(`/conversations/${S.activeConvId}`, collectSettings());
    const local = S.conversations.find((item) => item.id === S.activeConvId);
    if (local) Object.assign(local, updated);
    S.groupCast.turn_mode = updated.group_turn_mode;
    S.groupCast.max_speakers = updated.group_max_speakers;
    S.groupCast.context_mode = updated.group_context_mode;
    S.groupCast.sheet_updates = Boolean(updated.group_sheet_updates);
    const titleEl = $("chat-title-text");
    if (titleEl) titleEl.textContent = updated.title;
  }

  async function saveCast(members) {
    const updated = await api.put(convUrl(S.activeConvId, "members"), { members });
    const live = new Set(updated.map((member) => member.id));
    S.groupCast = {
      ...S.groupCast,
      members: updated,
      speakerNames: new Map([...(S.groupCast.speakerNames || []), ...speakerNameMap(updated)]),
      sheet_proposals: (S.groupCast.sheet_proposals || []).filter((item) => live.has(item.member_id)),
    };
    const local = S.conversations.find((item) => item.id === S.activeConvId);
    if (local) {
      local.group_member_names = updated.map((member) => member.display_name);
      local.group_card_ids = updated.flatMap((member) => (member.character_card_id ? [member.character_card_id] : []));
    }
    if (!updated.some((member) => member.id === S.pinnedSpeakerId && !member.muted)) S.pinnedSpeakerId = null;
  }

  $("group-config-cancel")?.addEventListener("click", closeModal);
  $("group-config-save")?.addEventListener("click", async () => {
    if (S.castSetupBusy) return;
    if (S.isStreaming) {
      toast("Stop generation before changing the scene", true);
      return;
    }
    if (anyDrafting()) {
      toast("Wait for the scene profiles to finish drafting", true);
      return;
    }
    const members = collectMembers();
    if (!members.length) {
      toast("A scene needs at least one cast member", true);
      return;
    }
    const castChanged = castDirty();
    S.castSetupBusy = true;
    try {
      if (settingsDirty()) {
        await saveSettings();
        settingsBaseline = JSON.stringify(collectSettings());
      }
      if (castChanged) await saveCast(members);
      setModalCloseGuard(null);
      closeModal();
      renderGroupCast();
      renderGroupList();
      if (castChanged) {
        notify("cast", S.groupCast);
        document.dispatchEvent(new CustomEvent("group-cast-updated", { detail: S.activeConvId }));
      }
    } catch (error) {
      toast(error.message, true);
    } finally {
      S.castSetupBusy = false;
    }
  });
}

function renderChatActionMenus() {
  const grouped = Boolean(S.groupCast);
  const visible = {
    "group-config": grouped,
    "convert-to-group": !grouped && Boolean(S.activeConvId),
    inspector: true,
  };
  for (const item of document.querySelectorAll("[data-chat-action]")) {
    item.hidden = !visible[item.dataset.chatAction];
  }
}

function composerHasDraft() {
  return Boolean($("chat-input")?.value.trim()) || (S.attachments?.length || 0) > 0;
}

let railDrafted = false;

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
}

export function consumeSpeakerOverride() {
  if (!S.groupCast || !overrideIsOneShot()) return;
  if (!S.pinnedSpeakerId || S.pinnedSpeakerId !== S.consumedSpeakerId) return;
  S.pinnedSpeakerId = null;
  renderGroupCast();
}

let _groupSearch = "";
let _groupsExpanded = false;

function _groupItemHtml({ rootId, root, shown, open, members }) {
  const names = (shown.group_member_names || []).filter(Boolean);
  const memberLine = names.length ? names.join(" · ") : "No active cast members";
  const cardIds = shown.group_card_ids || [];
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
  const countBadge =
    members.length > 1
      ? `<span class="group-chat-count" title="${escAttr(`${members.length} conversations in this group`)}">${members.length}</span>`
      : "";
  const title = `Cast: ${memberLine}${members.length > 1 ? `\n${members.length} conversations — open the group, then ☰ › Conversations` : ""}`;
  return `<div class="group-chat-item${open ? " active" : ""}">
      <button type="button" class="group-chat-select" data-group-conversation-id="${escAttr(shown.id)}" title="${escAttr(title)}">
        <span class="group-chat-avatar-stack" aria-hidden="true">${avatarStack}${remaining ? `<span class="group-chat-avatar group-chat-overflow">+${remaining}</span>` : ""}</span>
        <span class="group-chat-details"><span class="group-chat-title">${esc(root.title)}</span><span class="group-chat-members">${esc(memberLine)}</span></span>
        ${countBadge}
      </button>
      <button type="button" class="btn-icon group-chat-delete" data-group-delete-root-id="${escAttr(rootId)}" title="Delete group" aria-label="Delete group ${escAttr(root.title)}">${SIDEBAR_CLOSE_ICON}</button>
    </div>`;
}

export function renderGroupList() {
  const list = $("group-chat-list");
  if (!list) return;
  const families = groupFamilies(S.conversations, S.activeConvId);

  const searchWrap = $("group-search-wrap");
  if (searchWrap) {
    searchWrap.style.display = families.length > GROUP_LIMIT || _groupSearch.trim() ? "" : "none";
  }
  const searchInp = $("group-search");
  if (searchInp && searchInp.value !== _groupSearch) searchInp.value = _groupSearch;

  const { shown, hidden } = visibleGroups(families, { query: _groupSearch, expanded: _groupsExpanded });
  if (families.length && !shown.length) {
    list.innerHTML = `<div class="worlds-empty">No groups match “${esc(_groupSearch.trim())}”</div>`;
    return;
  }

  let html = shown.map(_groupItemHtml).join("");
  if (!_groupSearch.trim()) {
    if (hidden > 0) {
      html += `<button type="button" class="worlds-more" data-group-expand>+${hidden} more — show all</button>`;
    } else if (_groupsExpanded && families.length > GROUP_LIMIT) {
      html += `<button type="button" class="worlds-more" data-group-collapse>Show less</button>`;
    }
  }
  list.innerHTML = html;
}

async function fetchSheetProposals(cid) {
  try {
    return await api.get(convUrl(cid, "sheet-proposals"));
  } catch {
    return [];
  }
}

export async function refreshSheetProposals() {
  if (!S.groupCast || !S.activeConvId) return;
  S.groupCast.sheet_proposals = await fetchSheetProposals(S.activeConvId);
}

function speakerNameMap(rows) {
  return new Map((rows || []).map((member) => [member.id, member.display_name]));
}

export async function loadGroupCast(conv) {
  if (conv?.kind !== "group") {
    S.groupCast = null;
    S.pinnedSpeakerId = null;
    S.consumedSpeakerId = null;
    renderGroupCast();
    notify("cast", null);
    return;
  }
  const [roster, proposals] = await Promise.all([
    api.get(`${convUrl(conv.id, "members")}?include_inactive=true`),
    fetchSheetProposals(conv.id),
  ]);
  if (S.activeConvId !== conv.id) return;
  const members = roster.filter((member) => member.active !== 0);
  S.groupCast = {
    members,
    speakerNames: speakerNameMap(roster),
    turn_mode: conv.group_turn_mode,
    max_speakers: conv.group_max_speakers,
    context_mode: conv.group_context_mode,
    sheet_updates: Boolean(conv.group_sheet_updates),
    sheet_proposals: proposals,
  };
  if (!members.some((m) => m.id === S.pinnedSpeakerId && !m.muted)) S.pinnedSpeakerId = null;
  renderGroupCast();
  notify("cast", S.groupCast);
}

async function convertToGroup() {
  if (!S.activeConvId || S.groupCast || S.castSetupBusy) return;
  if (S.isStreaming) {
    toast("Stop generation before converting to a group", true);
    return;
  }
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
  $("chat-avatar")?.addEventListener("click", (event) => {
    if (S.groupCast && event.target === event.currentTarget) showAvatarPopup();
  });
  $("groups-section-toggle")?.addEventListener("click", (event) => {
    event.currentTarget.querySelector(".arrow")?.classList.toggle("collapsed");
    event.currentTarget.nextElementSibling?.classList.toggle("collapsed");
  });
  $("new-group-btn")?.addEventListener("click", showGroupCreate);
  $("group-search")?.addEventListener("input", (event) => {
    _groupSearch = event.target.value;
    renderGroupList();
  });
  $("group-chat-list")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-group-expand]")) {
      _groupsExpanded = true;
      renderGroupList();
      return;
    }
    if (event.target.closest("[data-group-collapse]")) {
      _groupsExpanded = false;
      renderGroupList();
      return;
    }
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
      showGroupConfig();
      return;
    }
    const button = event.target.closest("[data-cast-member-id]");
    if (!button || button.disabled) return;
    onCastChipClick(button.dataset.castMemberId);
  });
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
  document.addEventListener("group-config-request", () => showGroupConfig());
  document.addEventListener("convert-to-group-request", convertToGroup);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".cast-row")) closeRowMenus();
  });
}
