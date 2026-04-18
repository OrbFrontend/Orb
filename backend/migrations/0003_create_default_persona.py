"""
0003_create_default_persona — create a default user persona from existing settings.user_name
and settings.user_description, and link it as active_persona_id.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def migrate(conn: sqlite3.Connection) -> None:
    # Fetch current settings
    row = conn.execute(
        "SELECT user_name, user_description, active_persona_id FROM settings WHERE id = 1"
    ).fetchone()
    if row is None:
        # No settings row (should not happen)
        return
    user_name, user_description, active_persona_id = row

    # If active_persona_id already set, nothing to do
    if active_persona_id is not None:
        print(f"[migrations] 0003: active_persona_id already set to {active_persona_id}")
        return

    # Check if any personas already exist (maybe from previous runs)
    existing = conn.execute("SELECT COUNT(*) FROM user_personas").fetchone()[0]
    if existing > 0:
        # There are personas but none is linked; we could link the first one,
        # but for safety we'll create a new default persona anyway.
        pass

    # Create a default persona
    now = datetime.now(timezone.utc).isoformat()
    # Use a pleasant default avatar color (blue)
    avatar_color = "#3b82f6"
    cursor = conn.execute(
        "INSERT INTO user_personas (name, description, avatar_color, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (user_name or "User", user_description or "", avatar_color, now, now),
    )
    new_id = cursor.lastrowid
    print(f"[migrations] 0003: created default persona id={new_id} name={user_name or 'User'}")

    # Link it as active persona
    conn.execute(
        "UPDATE settings SET active_persona_id = ? WHERE id = 1",
        (new_id,),
    )
    print(f"[migrations] 0003: set active_persona_id to {new_id}")