#!/usr/bin/env python3
"""
Diagnostic dump script for Orb.

Collects all application configuration, settings, and database metadata
into a single human-readable JSON file. Sensitive fields (API keys,
user names, message content) are redacted.

Usage:
    python3 scripts/dump_diagnostic.py              # outputs to diagnostic_dump.json
    python3 scripts/dump_diagnostic.py output.json   # custom output path
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR) if os.path.basename(SCRIPT_DIR) == "scripts" else SCRIPT_DIR
DB_PATH = os.path.join(PROJECT_ROOT, "backend", "data", "app.db")

# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------
SENSITIVE_PLACEHOLDER = "***redacted***"


def redact_api_key(value: str) -> str:
    """Mask an API key, showing only first/last 4 chars if long enough."""
    if not value or len(value) < 8:
        return SENSITIVE_PLACEHOLDER if value else ""
    return f"{value[:4]}...{value[-4:]}"


def redact_prompt(text: str, max_len: int = 200) -> str:
    """Truncate long prompt texts for readability."""
    if not text:
        return text
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# Collection functions
# ---------------------------------------------------------------------------

def collect_environment() -> dict:
    """System and Python environment info."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "executable": sys.executable,
        "project_root": PROJECT_ROOT,
    }


def collect_dependencies() -> dict:
    """Parsed dependency lists from requirements files."""
    result: dict = {}
    for fname in ("requirements.txt", "requirements-dev.txt"):
        path = os.path.join(PROJECT_ROOT, fname)
        if os.path.exists(path):
            with open(path) as f:
                result[fname] = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return result


def _connect() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Has the application been started at least once?")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def collect_settings(conn: sqlite3.Connection) -> dict:
    """Application-wide settings with sensitive fields redacted."""
    rows = conn.execute("SELECT * FROM settings WHERE id = 1").fetchall()
    if not rows:
        return {}
    s = dict(rows[0])
    s["api_key"] = redact_api_key(s.get("api_key", ""))
    s["user_name"] = SENSITIVE_PLACEHOLDER
    s["user_description"] = redact_prompt(s.get("user_description", ""))
    s["shared_system_prompt"] = redact_prompt(s.get("shared_system_prompt", ""))
    s["system_prompt"] = redact_prompt(s.get("system_prompt", ""))
    # Parse JSON columns
    for key in ("enabled_tools", "reasoning_enabled_passes"):
        val = s.get(key)
        if isinstance(val, str):
            try:
                s[key] = json.loads(val)
            except json.JSONDecodeError:
                pass
    return s


def collect_endpoints(conn: sqlite3.Connection) -> list[dict]:
    """Endpoint configurations with API keys redacted."""
    rows = conn.execute("SELECT * FROM endpoints ORDER BY id").fetchall()
    result = []
    for r in rows:
        ep = dict(r)
        ep["api_key"] = redact_api_key(ep.get("api_key", ""))
        result.append(ep)
    return result


def collect_model_configs(conn: sqlite3.Connection) -> list[dict]:
    """Per-model sampling and generation configs."""
    rows = conn.execute(
        "SELECT * FROM model_configs ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def collect_conversation_summary(conn: sqlite3.Connection) -> dict:
    """Conversation counts without identifying details."""
    total = conn.execute("SELECT COUNT(*) as c FROM conversations").fetchone()["c"]
    active = conn.execute(
        "SELECT COUNT(*) as c FROM conversations WHERE active_leaf_id IS NOT NULL"
    ).fetchone()["c"]
    with_character = conn.execute(
        "SELECT COUNT(*) as c FROM conversations WHERE character_card_id IS NOT NULL"
    ).fetchone()["c"]

    # Latest 5 conversations by update time (id and timestamps only)
    recent = conn.execute(
        "SELECT id, character_card_id, created_at, updated_at "
        "FROM conversations ORDER BY updated_at DESC LIMIT 5"
    ).fetchall()
    recent_list = [dict(r) for r in recent]

    return {
        "total_conversations": total,
        "active_conversations": active,
        "with_character_card": with_character,
        "recent_conversations": recent_list,
    }


def collect_schema_migrations(conn: sqlite3.Connection) -> list[str]:
    """List of applied migration IDs."""
    try:
        rows = conn.execute(
            "SELECT id FROM schema_migrations ORDER BY id"
        ).fetchall()
        return [r["id"] for r in rows]
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else "diagnostic_dump.json"

    print(f"Orb Diagnostic Dump")
    print(f"Database: {DB_PATH}")
    print(f"Output:   {output_path}")
    print()

    conn = _connect()
    try:
        dump = {
            "environment": collect_environment(),
            "dependencies": collect_dependencies(),
            "settings": collect_settings(conn),
            "endpoints": collect_endpoints(conn),
            "model_configs": collect_model_configs(conn),
            "conversations": collect_conversation_summary(conn),
            "schema_migrations": collect_schema_migrations(conn),
        }
    finally:
        conn.close()

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, ensure_ascii=False)

    print(f"Diagnostic dump written to {output_path}")
    print()
    print("Summary:")
    print(f"  Conversations : {dump['conversations']['total_conversations']}")
    print(f"  Endpoints     : {len(dump['endpoints'])}")
    print(f"  Model Configs : {len(dump['model_configs'])}")
    print()
    print("NOTE: API keys and personal identifiers have been redacted.")


if __name__ == "__main__":
    main()
