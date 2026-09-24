// Recent-character sidebar data and rendering, shared by chat and the library.
import { api } from "./api.js";
import { CLOSE_ICON, EDIT_ICON } from "./icons.js";
import { charactersView, S } from "./state.js";
import { $, avatarCell, avatarUrl, convActivity, esc, escAttr } from "./utils.js";

export const _avatarBust = new Map();

/** The query that busts a card's cached avatar once it has changed this session, else "". */
export function avatarBustQuery(cardId) {
  return _avatarBust.has(cardId) ? `?v=${_avatarBust.get(cardId)}` : "";
}

function filterRecentCharacters(characters, conversations, limit = 5) {
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
  return tagged.slice(0, limit).map((t) => t.char);
}

export async function loadCharacters() {
  const [characters, conversations] = await Promise.all([
    api.get("/characters"),
    S.conversations || api.get("/conversations"),
  ]);
  S.allCharacters = characters;
  S.characters = filterRecentCharacters(characters, conversations || []);
  renderCharacters();
}

export function refreshCharacters() {
  const source = charactersView();
  if (!source.length) return;
  S.characters = filterRecentCharacters(source, S.conversations || []);
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
      const bust = _avatarBust.has(c.id) ? `?v=${_avatarBust.get(c.id)}` : "";
      const av = avatarCell(c.has_avatar ? avatarUrl(c.id) + bust : "");
      const meta = esc(c.creator_notes || (c.tags || []).slice(0, 2).join(", ") || c.source_format || "");
      const isActive = S.activeCharId === c.id;
      return `<div class="char-item${isActive ? " active" : ""}" onclick="selectChar('${c.id}', 'recent')">
      <div class="char-avatar-sm${c.has_expressions ? " avatar-halo" : ""}">${av}</div>
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
}
