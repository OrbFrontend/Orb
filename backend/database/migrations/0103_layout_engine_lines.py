"""Drop the Current state, Tool notice, Length nudge and Speaker cue blocks; the engine now places their text itself.

Current state follows Scene guidance and the tool notice, Length nudge and Speaker cue lead the User message, where every layout
started them. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM prompt_blocks WHERE kind IN ('current_state', 'tool_notice', 'length_nudge', 'cue')")
    conn.commit()
    print("[migrations] 0103: Current state, Tool notice, Length nudge and Speaker cue blocks dropped")
