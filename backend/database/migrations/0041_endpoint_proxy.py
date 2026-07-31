"""
0041_endpoint_proxy -- add the per-endpoint LLM proxy URL.

Empty string (the default) means no proxy, so that endpoint's LLM requests keep
connecting directly (the prior behavior). A set value routes them through the
proxy; httpx accepts http/https/socks5 URLs (socks5 via the httpx[socks] extra).
The scheme is validated at the API layer (EndpointUpdate), not in the DB.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(endpoints)").fetchall()}
    if "proxy" not in cols:
        conn.execute("ALTER TABLE endpoints ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
        print("[migrations] 0041: added proxy column to endpoints")
