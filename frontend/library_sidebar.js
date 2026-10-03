// Recent-character sidebar data and rendering, shared by chat and the library.
import { api } from "./api.js";
import { CLOSE_ICON, EDIT_ICON } from "./icons.js";
import { syncActivity } from "./operations.js";
import { charactersView, S, subscribe } from "./state.js";
import { $, avatarCell, avatarUrl, convActivity, esc, escAttr } from "./utils.js";

export const avatarBust = new Map();

/** The query that busts a card's cached avatar once it has changed this session, else "". */
export function avatarBustQuery(cardId) {
  return avatarBust.has(cardId) ? `?v=${avatarBust.get(cardId)}` : "";
}

const PANEL_LIMIT = 5;
// The panel's ids in display order, ranked once per page load.
let _panelOrder = null;

function rankRecentCharacters(characters, conversations) {
  const recentMap = new Map();
  for (const conv of conversations) {
    const ts = convActivity(conv);
    const cardIds = conv.character_card_id ? [conv.character_card_id] : conv.group_card_ids || [];
    for (const cardId of cardIds) {
      const existing = recentMap.get(cardId);
      if (!existing || ts > existing) recentMap.set(cardId, ts);
    }
  }

  const tagged = characters.map((char) => {
    const convTime = recentMap.get(char.id);
    const activityTime = convTime || char.updated_at || char.created_at || "";
    return { char, activityTime, hasConversation: !!convTime };
  });

  tagged.sort((a, b) => b.activityTime.localeCompare(a.activityTime));
  return tagged.map((t) => t.char);
}

/** The panel never reorders before a page refresh: reloads refresh each card in
 * place, a deleted card leaves, and only an empty slot at the bottom takes the
 * next most recent character. The open character always has a slot; opened from
 * outside the panel, it joins at the top and the least recently active one leaves. */
function panelCharacters(characters, conversations) {
  const byId = new Map(characters.map((c) => [c.id, c]));
  const order = (_panelOrder || []).filter((id) => byId.has(id));
  const ranked = rankRecentCharacters(characters, conversations);
  const active = S.activeCharId;
  if (byId.has(active) && !order.includes(active)) {
    if (order.length >= PANEL_LIMIT) {
      const rank = new Map(ranked.map((c, i) => [c.id, i]));
      const stalest = order.reduce((a, b) => (rank.get(b) > rank.get(a) ? b : a));
      order.splice(order.indexOf(stalest), 1);
    }
    order.unshift(active);
  }
  for (const c of ranked) {
    if (order.length >= PANEL_LIMIT) break;
    if (!order.includes(c.id)) order.push(c.id);
  }
  _panelOrder = order;
  return order.map((id) => byId.get(id));
}

export async function loadCharacters() {
  const [characters, conversations] = await Promise.all([
    api.get("/characters"),
    S.conversations || api.get("/conversations"),
  ]);
  S.allCharacters = characters;
  S.characters = panelCharacters(characters, conversations || []);
  renderCharacters();
}

export function refreshCharacters() {
  const source = charactersView();
  if (!source.length) return;
  S.characters = panelCharacters(source, S.conversations || []);
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
      const bust = avatarBust.has(c.id) ? `?v=${avatarBust.get(c.id)}` : "";
      const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "");
      const meta = esc(c.creator_notes || (c.tags || []).slice(0, 2).join(", ") || c.source_format || "");
      const isActive = S.activeCharId === c.id;
      return `<div class="char-item${isActive ? " active" : ""}" onclick="selectChar('${c.id}', 'recent')">
      <div class="chat-activity" data-activity="${escAttr(c.id)}"><div class="char-avatar-sm${c.has_expressions ? " avatar-halo" : ""}">${av}</div></div>
      <div class="char-item-info">
        <div class="char-item-name">${esc(c.name)}</div>
        <div class="char-item-meta">${meta}</div>
      </div>
      <div class="char-item-actions">
        <button class="char-action-edit" onclick="event.stopPropagation();showCharEditModal('${c.id}')" title="Edit character" aria-label="Edit ${escAttr(c.name)}">${EDIT_ICON}</button>
        <button class="char-action-delete" onclick="event.stopPropagation();deleteCharacter('${c.id}')" title="Delete character" aria-label="Delete ${escAttr(c.name)}">${CLOSE_ICON}</button>
      </div>
    </div>`;
    })
    .join("");
  syncCharacterActivity();
}

// A character whose one-on-one chat is generating wears the busy dot.
function syncCharacterActivity() {
  syncActivity("#char-list [data-activity]", (conv) => conv.character_card_id);
}
subscribe("operations", syncCharacterActivity);
