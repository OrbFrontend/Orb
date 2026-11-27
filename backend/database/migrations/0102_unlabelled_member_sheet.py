"""A member's baseline sheet no longer labels the personality ``Personality: ``: it is the card's description and personality, a
blank line between them, as the Group cast text renders them.

Undecided sheet proposals made from the labelled baseline name it as the sheet an apply must match, so each one whose member
still reads the card takes the new baseline. A proposal made from an override, or from a card that has changed since, keeps its
base. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3


def _labelled(description: str, personality: str) -> str:
    parts = [description] if description else []
    if personality:
        parts.append("Personality: " + personality)
    return "\n\n".join(parts)


def migrate(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"member_sheet_proposals", "group_members", "character_cards"} <= tables:
        return
    rows = conn.execute(
        "SELECT p.id, p.base_sheet, c.description, c.personality FROM member_sheet_proposals p "
        "JOIN group_members m ON m.id = p.member_id "
        "JOIN character_cards c ON c.id = m.character_card_id "
        "WHERE p.status IN ('pending', 'stale') AND m.card_sheet_override IS NULL"
    ).fetchall()
    updates = []
    for proposal_id, base, description, personality in rows:
        description, personality = (description or "").strip(), (personality or "").strip()
        if personality and base == _labelled(description, personality):
            updates.append(("\n\n".join(part for part in (description, personality) if part), proposal_id))
    conn.executemany("UPDATE member_sheet_proposals SET base_sheet = ? WHERE id = ?", updates)
    conn.commit()
    print(f"[migrations] 0102: {len(updates)} sheet proposals rebased on the unlabelled member sheet")
