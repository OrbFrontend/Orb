#!/usr/bin/env python3
"""Migrate a SillyTavern install into Orb's database.

Reads an unmodified SillyTavern data directory and writes characters (with their
expression sprites and embedded lorebooks), standalone lorebooks, chat history,
personas, and group chats into Orb's SQLite file.

This is the one job Orb's HTTP API cannot do: no route inserts a message without
running the LLM, so chat history has to be written to the tables directly. The
script therefore owns its own connection and its own INSERTs, and reproduces the
invariants the query layer would otherwise enforce (see ``check_schema`` and the
per-dataset comments). It imports pure helpers from ``backend/`` -- card parsing,
lorebook field mapping, expression labelling -- rather than restating them, so
the two cannot drift apart.

Deliberately NOT migrated, because Orb has nothing to map them onto:
  * prompts and generation settings (context/, instruct/, sysprompt/, */Settings/,
    reasoning/, QuickReplies/) -- Orb assembles prompts through the
    Director/Writer/Editor pipeline with a cache-stable prefix
  * endpoints and API keys (secrets.json) -- configure those in Orb
  * UI chrome (themes/, backgrounds/, movingUI/, assets/, thumbnails/, backups/)
  * per-message generation metadata (reasoning traces, token counts, gen ids)
  * author's notes, and the ST tag taxonomy
  * ST-only lorebook knobs: recursion, probability, sticky/cooldown/delay,
    inclusion groups, roles, per-entry scan depth (docs/features/lorebooks.md)

Usage:
    python scripts/migrate_sillytavern.py --st-dir /path/to/SillyTavern --dry-run
    python scripts/migrate_sillytavern.py --st-dir /path/to/SillyTavern

Stop Orb before running: it is single-user, single-tab by design.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
import uuid
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Pure helpers, imported rather than copied. `_normalise_lorebook_entry` is
# private to the API layer, but pinning the one real ST field mapping beats
# duplicating it into scripts/ where it would silently drift from the routes.
from backend.api.deps import _normalise_lorebook_entry  # noqa: E402
from backend.database.queries.group_members import allocate_speaker_key  # noqa: E402
from backend.features.cards.expressions import extract_expressions_zip  # noqa: E402
from backend.features.cards.parsing import card_to_dict, read_orb_id  # noqa: E402
from backend.features.cards.parsing import parse as parse_card  # noqa: E402

DATASETS = ("worlds", "characters", "personas", "chats", "groups")

# Columns this script writes by hand. If the target database predates any of
# them the INSERTs would fail halfway through, so they are checked up front.
REQUIRED_COLUMNS = {
    "conversations": ("kind", "group_turn_mode", "group_context_mode", "group_sheet_updates", "macro_seed"),
    "messages": ("turn_index", "parent_id", "progressive_fields", "speaker_member_id", "exchange_id"),
    "character_cards": ("extensions", "world_id", "avatar_b64", "avatar_mime", "source_format"),
    "lorebook_entries": ("entry_layer", "overlay_action", "use_regex", "selective", "secondary_keys"),
    "group_members": ("speaker_key", "card_sheet_override", "public_profile_override", "member_kind"),
    "user_personas": ("name", "description", "avatar_color", "avatar_b64", "avatar_mime"),
    "worlds": ("content_revision", "dynamic_enabled"),
    "character_expressions": ("character_card_id", "label", "data_b64", "mime"),
    "director_state": ("conversation_id", "active_moods", "keywords"),
}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


@dataclass
class Report:
    """Per-dataset tally, plus the reason behind every skip."""

    counts: Counter = field(default_factory=Counter)
    notes: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)

    def add(self, dataset: str, outcome: str, note: str = "", problem: str = "") -> None:
        self.counts[(dataset, outcome)] += 1
        if note:
            self.notes[(dataset, outcome, note)] += 1
        if problem:
            self.problems.append(problem)

    def tally(self, dataset: str, outcome: str) -> int:
        return self.counts[(dataset, outcome)]

    def render(self) -> str:
        head = "dataset".ljust(16) + "created".rjust(9) + "reused".rjust(9) + "skipped".rjust(9) + "failed".rjust(9)
        lines = ["", "=" * 52, head, "-" * 52]
        for dataset in DATASETS:
            row = [self.tally(dataset, outcome) for outcome in ("created", "reused", "skipped", "failed")]
            if any(row):
                lines.append(dataset.ljust(16) + "".join(str(n).rjust(9) for n in row))
        for (dataset, outcome), count in sorted(k for k in self.counts.items() if k[0][0] not in DATASETS):
            lines.append(f"{dataset} {outcome}: {count}")
        lines.append("=" * 52)
        if self.notes:
            lines.append("")
            for (dataset, outcome, note), count in sorted(self.notes.items()):
                lines.append(f"  {dataset}/{outcome}: {note} ({count})")
        if self.problems:
            lines.append("")
            for problem in self.problems[:40]:
                lines.append(f"  ! {problem}")
            if len(self.problems) > 40:
                lines.append(f"  ! ...and {len(self.problems) - 40} more")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Identity -- every id is derived, so a re-run is a no-op and an interrupted run
# resumes where it stopped.
# --------------------------------------------------------------------------- #


def st_uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"orb-st:{kind}:{key}"))


def card_id_from_png(png_bytes: bytes) -> str:
    """The id ``POST /api/characters/import`` derives from the same bytes.

    Matching it means a card the user already imported by hand is recognised
    here rather than duplicated.
    """
    return str(uuid.UUID(bytes=hashlib.sha256(png_bytes).digest()[:16], version=5))


def persona_color(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return f"#{digest[0]:02x}{digest[1]:02x}{digest[2]:02x}"


# The image formats ST will have written into User Avatars/, mapped to the mime
# Orb stores alongside the blob. Anything else is left behind for the colour.
_PERSONA_AVATAR_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# Same ceiling the API enforces on an uploaded persona avatar
# (``schemas.MAX_PERSONA_AVATAR_BYTES``). Kept in step by hand rather than
# imported: this is a decoded-size cap on a file, not on a request body, and a
# script that quietly wrote rows the API would reject is worse than one that
# skips an oversized picture and says so.
MAX_PERSONA_AVATAR_BYTES = 2 * 1024 * 1024


def read_persona_avatar(paths: STPaths, avatar: str) -> tuple[str, str] | None:
    """(base64 image, mime) for an ST persona, or None when there is no usable file.

    ``power_user.personas`` is keyed by the avatar's filename under
    ``User Avatars/``, so the key *is* the lookup. Resolved under that directory
    and rejected if it escapes, since the key comes out of a user-supplied JSON
    file rather than from a directory listing.
    """
    name = str(avatar or "").strip()
    if not name:
        return None
    mime = _PERSONA_AVATAR_MIMES.get(Path(name).suffix.lower())
    if mime is None:
        return None
    try:
        root = paths.user_avatars.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        if path.stat().st_size > MAX_PERSONA_AVATAR_BYTES:
            return None
        return base64.b64encode(path.read_bytes()).decode("ascii"), mime
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Timestamps -- SillyTavern has written five different date shapes over its life
# --------------------------------------------------------------------------- #

_LEGACY_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s*@\s*(\d{1,2})h\s*(\d{1,2})m\s*(\d{1,2})s(?:\s*(\d+)ms)?")
_HUMAN_DATE = "%B %d, %Y %I:%M%p"


def parse_st_date(value: Any) -> datetime | None:
    """Best-effort parse of any ST timestamp shape into an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value) / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # ISO-8601: gen_started / gen_finished, and newer send_dates.
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    # "March 15, 2025 2:25pm" -- day and hour are both unpadded in the wild.
    try:
        return datetime.strptime(re.sub(r"\s+", " ", raw), _HUMAN_DATE).astimezone(UTC)
    except ValueError:
        pass
    # Legacy "2023-5-29 @21h 54m 44s 567ms".
    match = _LEGACY_DATE.match(raw)
    if match:
        year, month, day, hour, minute, second, millis = (int(group or 0) for group in match.groups())
        try:
            return datetime(year, month, day, hour, minute, second, millis * 1000).astimezone(UTC)
        except ValueError:
            return None
    return None


def message_time(msg: dict, fallback: datetime) -> datetime:
    for key in ("gen_finished", "gen_started", "send_date"):
        parsed = parse_st_date(msg.get(key))
        if parsed:
            return parsed
    return fallback


class Clock:
    """Keeps one imported chat strictly increasing when ST dates tie or go missing.

    ``default`` (the log file's mtime) only ever fills in for a message ST left
    undated. It must not seed ``previous``: a chat is routinely older than the
    file holding it, and clamping to the mtime would drag every real timestamp
    forward to the day the file was last touched.
    """

    def __init__(self, default: datetime) -> None:
        self.default = default
        self.previous: datetime | None = None

    def undated(self) -> datetime:
        return self.previous + timedelta(seconds=1) if self.previous else self.default

    def stamp(self, when: datetime) -> str:
        if self.previous is not None and when <= self.previous:
            when = self.previous + timedelta(seconds=1)
        self.previous = when
        return when.isoformat()


# --------------------------------------------------------------------------- #
# Database access
# --------------------------------------------------------------------------- #


class Tx:
    """One BEGIN IMMEDIATE per unit of work; the whole run rolls back on --dry-run.

    A failed card, world, or chat loses only itself -- never half of itself, and
    never the run.
    """

    def __init__(self, conn: sqlite3.Connection, dry_run: bool) -> None:
        self.conn = conn
        self.dry_run = dry_run
        self._open = False

    def begin(self) -> None:
        if not self._open:
            self.conn.execute("BEGIN IMMEDIATE")
            self._open = True

    def commit(self) -> None:
        # A dry run keeps one transaction open for the whole pass so that
        # already-imported checks still see its own writes, then discards it.
        if self.dry_run or not self._open:
            return
        self.conn.commit()
        self._open = False

    def rollback(self) -> None:
        if self._open:
            self.conn.rollback()
            self._open = False


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def check_schema(conn: sqlite3.Connection) -> list[str]:
    """Refuse a database this script would not write correctly."""
    problems: list[str] = []
    expected = sorted(p.stem for p in (ROOT / "backend" / "database" / "migrations").glob("[0-9]*.py"))
    try:
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
    except sqlite3.Error:
        # A database built straight from schema.py has no stamps yet. That is
        # not a fault on its own -- the column checks below decide.
        applied = None
    if applied is not None:
        missing = [name for name in expected if name not in applied]
        if missing:
            problems.append(f"database is behind {len(missing)} migration(s); newest missing: {missing[-1]}")
    for table, columns in REQUIRED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            problems.append(f"table {table} is missing")
            continue
        absent = [column for column in columns if column not in present]
        if absent:
            problems.append(f"table {table} is missing column(s): {', '.join(absent)}")
    return problems


def backup_db(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.pre-st-{stamp}")
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            shutil.copy2(side, target.with_name(target.name + suffix))
    return target


def row_exists(conn: sqlite3.Connection, table: str, column: str, value: Any) -> bool:
    return conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (value,)).fetchone() is not None


# --------------------------------------------------------------------------- #
# Reading the SillyTavern side
# --------------------------------------------------------------------------- #


@dataclass
class STPaths:
    root: Path
    characters: Path
    chats: Path
    worlds: Path
    groups: Path
    group_chats: Path
    settings: Path
    user_avatars: Path

    @classmethod
    def locate(cls, st_dir: Path, user: str) -> STPaths:
        base = st_dir / "data" / user
        if not base.is_dir() and (st_dir / "characters").is_dir():
            base = st_dir  # also accept being handed the user directory itself
        return cls(
            root=base,
            characters=base / "characters",
            chats=base / "chats",
            worlds=base / "worlds",
            groups=base / "groups",
            group_chats=base / "group chats",
            settings=base / "settings.json",
            user_avatars=base / "User Avatars",
        )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """Split an ST chat log into its header line and its messages."""
    header: dict = {}
    messages: list[dict] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if index == 0 and "mes" not in obj:
            header = obj
        elif "mes" in obj:
            messages.append(obj)
    return header, messages


def message_variants(msg: dict) -> tuple[list[str], int]:
    """ST swipes -> (every alternate in ST's order, index of the live one).

    ``mes`` is what ST displays, and it is *not* always ``swipes[swipe_id]``:
    editing a message rewrites ``mes`` and leaves the swipe array alone. When
    they disagree the edited text is appended as one more alternate and becomes
    the live branch, so the pre-edit generation survives rather than being
    overwritten.
    """
    text = str(msg.get("mes") or "")
    swipes = msg.get("swipes")
    if not isinstance(swipes, list) or not swipes:
        return [text], 0
    variants = [str(swipe) for swipe in swipes]
    index = msg.get("swipe_id")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(variants):
        index = 0
    if variants[index] != text:
        variants.append(text)
        index = len(variants) - 1
    return variants, index


def variant_time(msg: dict, position: int, default: datetime) -> datetime:
    """A swipe's own timestamp, when ST recorded one in ``swipe_info``."""
    info = msg.get("swipe_info")
    if isinstance(info, list) and position < len(info) and isinstance(info[position], dict):
        return message_time(info[position], default)
    return default


# --------------------------------------------------------------------------- #
# Shared writers
# --------------------------------------------------------------------------- #


def insert_conversation(conn: sqlite3.Connection, row: dict) -> None:
    """A conversation and its companion director_state row.

    The pipeline assumes every conversation has one; ``create_conversation``
    writes both, and hand-rolled SQL that skips it produces a chat that breaks
    on the first turn.
    """
    conn.execute(
        """INSERT INTO conversations
           (id, title, character_card_id, character_name, character_scenario, post_history_instructions,
            persona_lock_id, macro_seed, created_at, updated_at, kind, group_turn_mode, group_max_speakers,
            group_context_mode, group_sheet_updates, group_root_id)
           VALUES (:id, :title, :character_card_id, :character_name, :character_scenario,
                   :post_history_instructions, :persona_lock_id, '', :created_at, :updated_at, :kind,
                   :group_turn_mode, :group_max_speakers, :group_context_mode, 0, :group_root_id)""",
        row,
    )
    conn.execute(
        "INSERT INTO director_state (conversation_id, active_moods, keywords) VALUES (?, '[]', '[]')",
        (row["id"],),
    )


def insert_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    role: str,
    content: str,
    turn_index: int,
    parent_id: int | None,
    created_at: str,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO messages
           (conversation_id, role, content, turn_index, parent_id, progressive_fields, created_at,
            speaker_member_id, exchange_id)
           VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)""",
        (conversation_id, role, content, turn_index, parent_id, created_at, speaker_member_id, exchange_id),
    )
    message_id = cursor.lastrowid
    assert message_id is not None
    return message_id


def import_message_log(
    conn: sqlite3.Connection,
    conversation_id: str,
    messages: list[dict],
    fallback_time: datetime,
    report: Report,
    speaker_for: Callable[[dict, bool], str | None] | None = None,
) -> tuple[int, int | None, str, str]:
    """Write one ST log as a message tree. Returns (rows, leaf id, first ts, last ts).

    Swipes become sibling rows sharing a parent and turn_index -- Orb derives
    ``branch_index`` from ``id ASC``, so inserting them in ST's array order makes
    the branch arrows line up with what ST showed.
    """
    clock = Clock(fallback_time)
    parent_id: int | None = None
    turn_index = 0
    written = 0
    first_stamp = ""
    last_stamp = ""
    exchange_id: str | None = None

    for msg in messages:
        if msg.get("is_system"):
            report.add("chats", "skipped", "system message dropped (no system role in Orb)")
            continue
        is_user = bool(msg.get("is_user"))
        role = "user" if is_user else "assistant"
        member_id = None if speaker_for is None else speaker_for(msg, is_user)
        if speaker_for is not None:
            # One exchange per user turn, shared by the replies that follow it.
            if is_user or exchange_id is None:
                exchange_id = str(uuid.uuid4())
        variants, live = message_variants(msg)
        base_time = message_time(msg, clock.undated())
        live_id: int | None = None
        for position, text in enumerate(variants):
            when = base_time if position == live else variant_time(msg, position, base_time)
            stamp = clock.stamp(when)
            message_id = insert_message(
                conn,
                conversation_id,
                role,
                text,
                turn_index,
                parent_id,
                stamp,
                speaker_member_id=member_id,
                exchange_id=exchange_id if speaker_for is not None else None,
            )
            written += 1
            first_stamp = first_stamp or stamp
            last_stamp = stamp
            if position == live:
                live_id = message_id
        parent_id = live_id
        turn_index += 1

    return written, parent_id, first_stamp, last_stamp


# --------------------------------------------------------------------------- #
# Worlds
# --------------------------------------------------------------------------- #


def world_id_for(name: str) -> str:
    return st_uuid("world", name)


def find_world(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT id FROM worlds WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row[0] if row else None


def write_world(conn: sqlite3.Connection, name: str, entries: list[dict], enabled: bool, now: str) -> str:
    """Create a World and its entries, bumping content_revision once for the book."""
    world_id = world_id_for(name)
    conn.execute(
        """INSERT INTO worlds (id, name, enabled, dynamic_enabled, content_revision, created_at, updated_at)
           VALUES (?, ?, ?, 0, 0, ?, ?)""",
        (world_id, name, 1 if enabled else 0, now, now),
    )
    for item in entries:
        # entry_layer/overlay_action pin the row to the user-authored layer; the
        # overlay is the Agent's to write, never an importer's.
        data = _normalise_lorebook_entry(item)
        conn.execute(
            """INSERT INTO lorebook_entries
               (world_id, name, content, keywords, case_insensitive, constant, at_depth, use_regex, selective,
                secondary_keys, priority, enabled, sort_order, entry_layer, entry_revision, overlay_action,
                supersedes_entry_id, archived, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'authored', 0, '', NULL, 0, ?, ?)""",
            (
                world_id,
                data["name"],
                data["content"],
                json.dumps(data["keywords"]),
                1 if data["case_insensitive"] else 0,
                1 if data["constant"] else 0,
                1 if data["at_depth"] else 0,
                1 if data["use_regex"] else 0,
                1 if data["selective"] else 0,
                json.dumps(data["secondary_keys"]),
                data["priority"],
                1 if data["enabled"] else 0,
                data["sort_order"],
                now,
                now,
            ),
        )
    if entries:
        conn.execute("UPDATE worlds SET content_revision = content_revision + 1 WHERE id = ?", (world_id,))
    return world_id


def book_entries(book: Any) -> list[dict]:
    """ST writes ``entries`` as a uid-keyed dict; V2/V3 books use a list."""
    entries = book.get("entries") if isinstance(book, dict) else None
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def import_worlds(conn: sqlite3.Connection, tx: Tx, paths: STPaths, report: Report) -> None:
    if not paths.worlds.is_dir():
        return
    globally_enabled: set[str] = set()
    if paths.settings.is_file():
        try:
            settings = read_json(paths.settings)
            selected = (settings.get("world_info_settings") or {}).get("world_info") or {}
            globally_enabled = {str(name) for name in (selected.get("globalSelect") or [])}
        except (OSError, ValueError, AttributeError):
            pass

    now = datetime.now(UTC).isoformat()
    for path in sorted(paths.worlds.glob("*.json")):
        name = path.stem
        try:
            entries = book_entries(read_json(path))
        except (OSError, ValueError) as exc:
            report.add("worlds", "failed", problem=f"{path.name}: {exc}")
            continue
        if not entries:
            # Empty here does not mean empty everywhere: a card may carry a book
            # of the same name with real content, and leaving the name free lets
            # that one land instead of colliding with a hollow World.
            report.add("worlds", "skipped", "no entries in the file")
            continue
        if find_world(conn, name):
            report.add("worlds", "reused", "a World of that name already exists")
            continue
        try:
            tx.begin()
            write_world(conn, name, entries, name in globally_enabled, now)
            tx.commit()
            report.add("worlds", "created")
        except sqlite3.Error as exc:
            tx.rollback()
            report.add("worlds", "failed", problem=f"{path.name}: {exc}")


# --------------------------------------------------------------------------- #
# Characters
# --------------------------------------------------------------------------- #


def sprite_pack(folder: Path) -> dict[str, tuple[str, str]]:
    """A folder of ST expression sprites, through Orb's own zip reader.

    Zipping in memory reuses the label matching and the size guards instead of
    restating them here.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for image in sorted(folder.iterdir()):
            if image.is_file():
                archive.write(image, image.name)
    return extract_expressions_zip(buffer.getvalue())


def import_characters(conn: sqlite3.Connection, tx: Tx, paths: STPaths, report: Report, create: bool = True) -> dict[str, str]:
    """Returns {avatar file stem: card id} so chats can find their character.

    With ``create=False`` nothing is written -- the pass only maps ST avatars
    onto cards Orb already holds. That is what ``--only chats`` needs: card ids
    are derived from the PNG bytes, so a card imported through the UI earlier
    resolves to the same id and the chats still attach to it.
    """
    by_stem: dict[str, str] = {}
    if not paths.characters.is_dir():
        return by_stem

    now = datetime.now(UTC).isoformat()
    for path in sorted(paths.characters.glob("*.png")):
        try:
            png = path.read_bytes()
            card = card_to_dict(parse_card(str(path)))
        except (OSError, ValueError, KeyError) as exc:
            if create:
                report.add("characters", "failed", problem=f"{path.name}: {exc}")
            continue

        card_id = read_orb_id(str(path)) or card_id_from_png(png)
        known = row_exists(conn, "character_cards", "id", card_id)
        if known or create:
            by_stem[path.stem] = card_id
        if not create:
            continue
        if known:
            report.add("characters", "reused", "already in the library")
            continue

        # ST keeps sprites under the character's *name*, while chats live under
        # the avatar file's stem -- `default_Seraphina.png` has both.
        sprites: dict[str, tuple[str, str]] = {}
        for candidate in (str(card.get("name") or ""), path.stem):
            folder = paths.characters / candidate
            if candidate and folder.is_dir():
                try:
                    sprites = sprite_pack(folder)
                except (OSError, ValueError, zipfile.BadZipFile) as exc:
                    report.add("characters", "skipped", "expression pack unreadable", f"{path.name}: {exc}")
                break

        try:
            tx.begin()
            world_id = None
            book = card.get("character_book")
            entries = book_entries(book)
            if entries:
                book_name = str((book or {}).get("name") or card.get("name") or path.stem)
                world_id = find_world(conn, book_name)
                if world_id is None:
                    world_id = write_world(conn, book_name, entries, False, now)
                    report.add("worlds", "created", "from an embedded character book")
                else:
                    report.add("worlds", "reused", "card book matched an existing World")
            conn.execute(
                """INSERT INTO character_cards
                   (id, name, description, personality, scenario, first_mes, mes_example, creator_notes,
                    system_prompt, post_history_instructions, tags, creator, character_version,
                    alternate_greetings, avatar_b64, avatar_mime, source_format, world_id, extensions,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'image/png', ?, ?, ?, ?, ?)""",
                (
                    card_id,
                    str(card.get("name") or path.stem),
                    str(card.get("description") or ""),
                    str(card.get("personality") or ""),
                    str(card.get("scenario") or ""),
                    str(card.get("first_mes") or ""),
                    str(card.get("mes_example") or ""),
                    str(card.get("creator_notes") or ""),
                    str(card.get("system_prompt") or ""),
                    str(card.get("post_history_instructions") or ""),
                    json.dumps(card.get("tags") or []),
                    str(card.get("creator") or ""),
                    str(card.get("character_version") or ""),
                    json.dumps(card.get("alternate_greetings") or []),
                    base64.b64encode(png).decode("ascii"),
                    str(card.get("source_format") or "tavern_v2"),
                    world_id,
                    json.dumps(card["extensions"]) if card.get("extensions") else None,
                    now,
                    now,
                ),
            )
            for label, (data_b64, mime) in sprites.items():
                conn.execute(
                    """INSERT INTO character_expressions (character_card_id, label, data_b64, mime)
                       VALUES (?, ?, ?, ?)""",
                    (card_id, label, data_b64, mime),
                )
            tx.commit()
            report.add("characters", "created", f"with {len(sprites)} expression sprites" if sprites else "")
        except sqlite3.Error as exc:
            tx.rollback()
            report.add("characters", "failed", problem=f"{path.name}: {exc}")

    return by_stem


def link_card_worlds(conn: sqlite3.Connection, tx: Tx, paths: STPaths, report: Report) -> None:
    """Resolve each card's ``extensions.world`` once every World exists.

    Runs last on purpose: an ST card may name a World that arrived as a
    standalone file *or* as some other card's embedded book.
    """
    rows = conn.execute("SELECT id, name, extensions FROM character_cards WHERE world_id IS NULL").fetchall()
    for row in rows:
        try:
            extensions = json.loads(row["extensions"]) if row["extensions"] else {}
        except ValueError:
            continue
        name = extensions.get("world") if isinstance(extensions, dict) else None
        if not name:
            continue
        world_id = find_world(conn, str(name))
        if world_id is None:
            report.add("worlds", "skipped", "card names a World this install no longer has", f"{row['name']} -> {name}")
            continue
        tx.begin()
        conn.execute("UPDATE character_cards SET world_id = ? WHERE id = ?", (world_id, row["id"]))
        tx.commit()
        report.add("worlds", "reused", "linked to a character card")


# --------------------------------------------------------------------------- #
# Personas
# --------------------------------------------------------------------------- #


def import_personas(conn: sqlite3.Connection, tx: Tx, paths: STPaths, report: Report, create: bool = True) -> dict[str, int]:
    """Returns {lowercased persona name: user_personas.id}.

    With ``create=False`` only the personas Orb already has are reported back,
    so ``--only chats`` can still pin a chat to a matching persona without
    quietly creating the rest.
    """
    by_name: dict[str, int] = {}
    for row in conn.execute("SELECT id, name FROM user_personas"):
        by_name.setdefault(str(row["name"]).casefold(), row["id"])
    if not create or not paths.settings.is_file():
        return by_name

    try:
        power_user = read_json(paths.settings).get("power_user") or {}
    except (OSError, ValueError, AttributeError) as exc:
        report.add("personas", "failed", problem=f"settings.json: {exc}")
        return by_name

    personas = power_user.get("personas") or {}
    descriptions = power_user.get("persona_descriptions") or {}
    now = datetime.now(UTC).isoformat()
    for avatar, name in personas.items():
        label = str(name).strip()
        if not label:
            continue
        if label.casefold() in by_name:
            report.add("personas", "reused", "a persona of that name already exists")
            continue
        description = ""
        entry = descriptions.get(avatar)
        if isinstance(entry, dict):
            description = str(entry.get("description") or "")
        # The image is the persona's portrait when ST has one on disk; the
        # generated colour stays on the row either way, since it is still the
        # fallback chip Orb draws when no image loads.
        image = read_persona_avatar(paths, str(avatar))
        why_no_image = "colour only -- no usable avatar file under User Avatars/"
        try:
            tx.begin()
            cursor = conn.execute(
                """INSERT INTO user_personas (name, description, avatar_color, avatar_b64, avatar_mime, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (label, description, persona_color(label), image[0] if image else None, image[1] if image else None, now, now),
            )
            tx.commit()
            persona_id = cursor.lastrowid
            assert persona_id is not None
            by_name[label.casefold()] = persona_id
            report.add("personas", "created", "with its avatar image" if image else why_no_image)
        except sqlite3.Error as exc:
            tx.rollback()
            report.add("personas", "failed", problem=f"persona {label}: {exc}")
    return by_name


# --------------------------------------------------------------------------- #
# Chats
# --------------------------------------------------------------------------- #


def chat_title(stem: str, character_name: str) -> str:
    if character_name and not stem.casefold().startswith(character_name.casefold()):
        return f"{character_name} - {stem}"
    return stem


def chat_persona(header: dict, messages: list[dict], personas: dict[str, int]) -> int | None:
    """Whoever the user was in this chat, matched against the migrated personas."""
    candidates: list[str] = []
    for msg in messages:
        if msg.get("is_user") and msg.get("name"):
            candidates.append(str(msg["name"]))
            break
    header_name = str(header.get("user_name") or "")
    if header_name and header_name != "unused":  # ST's placeholder in newer logs
        candidates.append(header_name)
    for candidate in candidates:
        persona_id = personas.get(candidate.casefold())
        if persona_id is not None:
            return persona_id
    return None


def import_chats(
    conn: sqlite3.Connection,
    tx: Tx,
    paths: STPaths,
    cards: dict[str, str],
    personas: dict[str, int],
    report: Report,
    include_orphans: bool,
    limit: int | None,
) -> None:
    if not paths.chats.is_dir():
        return

    card_rows: dict[str, sqlite3.Row] = {}
    imported = 0
    for folder in sorted(p for p in paths.chats.iterdir() if p.is_dir()):
        card_id = cards.get(folder.name)
        if card_id is None and not include_orphans:
            # Chats outlive their character in ST; without the card there is no
            # description, scenario, or avatar to give the conversation.
            for _ in folder.glob("*.jsonl"):
                report.add("chats", "skipped", "the character card is gone from this install")
            continue
        if card_id and card_id not in card_rows:
            row = conn.execute(
                "SELECT name, scenario, post_history_instructions FROM character_cards WHERE id = ?",
                (card_id,),
            ).fetchone()
            if row is None:
                continue
            card_rows[card_id] = row

        card = card_rows.get(card_id or "")
        character_name = str(card["name"]) if card else folder.name
        for path in sorted(folder.glob("*.jsonl")):
            if limit is not None and imported >= limit:
                return
            conversation_id = st_uuid("chat", f"{folder.name}/{path.name}")
            if row_exists(conn, "conversations", "id", conversation_id):
                report.add("chats", "reused", "already imported")
                continue
            try:
                header, messages = read_jsonl(path)
            except (OSError, ValueError) as exc:
                report.add("chats", "failed", problem=f"{folder.name}/{path.name}: {exc}")
                continue
            if not messages:
                report.add("chats", "skipped", "no messages in the log")
                continue

            fallback = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            try:
                tx.begin()
                insert_conversation(
                    conn,
                    {
                        "id": conversation_id,
                        "title": chat_title(path.stem, character_name),
                        "character_card_id": card_id,
                        "character_name": character_name,
                        "character_scenario": str(card["scenario"]) if card else "",
                        "post_history_instructions": str(card["post_history_instructions"]) if card else "",
                        "persona_lock_id": chat_persona(header, messages, personas),
                        "created_at": fallback.isoformat(),
                        "updated_at": fallback.isoformat(),
                        "kind": "solo",
                        "group_turn_mode": "director",
                        "group_max_speakers": 3,
                        "group_context_mode": "private",
                        "group_root_id": None,
                    },
                )
                written, leaf_id, first_stamp, last_stamp = import_message_log(
                    conn, conversation_id, messages, fallback, report
                )
                if leaf_id is None:
                    tx.rollback()
                    report.add("chats", "skipped", "no importable messages in the log")
                    continue
                # A conversation whose active_leaf_id is NULL renders as empty.
                conn.execute(
                    "UPDATE conversations SET active_leaf_id = ?, created_at = ?, updated_at = ? WHERE id = ?",
                    (leaf_id, first_stamp, last_stamp, conversation_id),
                )
                tx.commit()
                imported += 1
                report.add("chats", "created")
                report.counts[("messages", "created")] += written
            except (sqlite3.Error, ValueError) as exc:
                tx.rollback()
                report.add("chats", "failed", problem=f"{folder.name}/{path.name}: {exc}")


# --------------------------------------------------------------------------- #
# Group chats
# --------------------------------------------------------------------------- #


def speaker_resolver(member_ids: dict[str, str], by_display: dict[str, str]) -> Callable[[dict, bool], str | None]:
    """ST attributes a group reply by avatar file; fall back to the display name."""

    def resolve(msg: dict, is_user: bool) -> str | None:
        if is_user:
            return None
        avatar = msg.get("original_avatar")
        if isinstance(avatar, str) and avatar in member_ids:
            return member_ids[avatar]
        return by_display.get(str(msg.get("name") or ""))

    return resolve


def import_groups(
    conn: sqlite3.Connection,
    tx: Tx,
    paths: STPaths,
    cards: dict[str, str],
    personas: dict[str, int],
    report: Report,
) -> None:
    if not paths.groups.is_dir():
        return

    for path in sorted(paths.groups.glob("*.json")):
        try:
            group = read_json(path)
        except (OSError, ValueError) as exc:
            report.add("groups", "failed", problem=f"{path.name}: {exc}")
            continue

        title = str(group.get("name") or path.stem)
        disabled = {str(name) for name in (group.get("disabled_members") or [])}
        members: list[dict] = []
        used_keys: set[str] = set()
        seen_cards: set[str] = set()
        for order, avatar in enumerate(group.get("members") or []):
            stem = Path(str(avatar)).stem
            card_id = cards.get(stem)
            if card_id is None:
                report.add("groups", "skipped", "a roster member's card is missing", f"{title}: {avatar}")
                continue
            if card_id in seen_cards:
                # One active member per card, per the partial unique index.
                report.add("groups", "skipped", "the same card appears twice in the roster", f"{title}: {avatar}")
                continue
            seen_cards.add(card_id)
            row = conn.execute("SELECT name FROM character_cards WHERE id = ?", (card_id,)).fetchone()
            display_name = str(row["name"]) if row else stem
            members.append(
                {
                    "avatar": str(avatar),
                    "card_id": card_id,
                    "display_name": display_name,
                    "speaker_key": allocate_speaker_key(display_name, used_keys),
                    "sort_order": order,
                    "muted": 1 if str(avatar) in disabled else 0,
                }
            )
        if not members:
            report.add("groups", "skipped", "no roster member resolved to a card")
            continue

        chat_ids = [str(name) for name in (group.get("chats") or []) if name]
        if not chat_ids and group.get("chat_id"):
            chat_ids = [str(group["chat_id"])]
        for chat_id in chat_ids:
            log = paths.group_chats / f"{chat_id}.jsonl"
            if not log.is_file():
                report.add("groups", "skipped", "the group's chat log is missing")
                continue
            conversation_id = st_uuid("group-chat", f"{group.get('id') or path.stem}/{chat_id}")
            if row_exists(conn, "conversations", "id", conversation_id):
                report.add("groups", "reused", "already imported")
                continue
            try:
                header, messages = read_jsonl(log)
            except (OSError, ValueError) as exc:
                report.add("groups", "failed", problem=f"{log.name}: {exc}")
                continue
            if not messages:
                report.add("groups", "skipped", "no messages in the log")
                continue

            fallback = datetime.fromtimestamp(log.stat().st_mtime, UTC)
            member_ids = {
                member["avatar"]: st_uuid("member", f"{conversation_id}:{member['speaker_key']}") for member in members
            }
            by_display = {member["display_name"]: member_ids[member["avatar"]] for member in members}

            speaker_for = speaker_resolver(member_ids, by_display)

            try:
                tx.begin()
                insert_conversation(
                    conn,
                    {
                        "id": conversation_id,
                        "title": title,
                        "character_card_id": None,
                        # A group has no single card; the title stands in as the
                        # character name, matching create_group_conversation.
                        "character_name": title,
                        "character_scenario": "",
                        "post_history_instructions": "",
                        "persona_lock_id": chat_persona(header, messages, personas),
                        "created_at": fallback.isoformat(),
                        "updated_at": fallback.isoformat(),
                        "kind": "group",
                        "group_turn_mode": "director",
                        "group_max_speakers": 3,
                        "group_context_mode": "private",
                        "group_root_id": None,
                    },
                )
                for member in members:
                    conn.execute(
                        """INSERT INTO group_members
                           (id, conversation_id, speaker_key, character_card_id, display_name,
                            public_profile_override, card_sheet_override, member_kind, sort_order, muted, active)
                           VALUES (?, ?, ?, ?, ?, NULL, NULL, 'character', ?, ?, 1)""",
                        (
                            member_ids[member["avatar"]],
                            conversation_id,
                            member["speaker_key"],
                            member["card_id"],
                            member["display_name"],
                            member["sort_order"],
                            member["muted"],
                        ),
                    )
                written, leaf_id, first_stamp, last_stamp = import_message_log(
                    conn, conversation_id, messages, fallback, report, speaker_for=speaker_for
                )
                if leaf_id is None:
                    tx.rollback()
                    report.add("groups", "skipped", "no importable messages in the log")
                    continue
                conn.execute(
                    "UPDATE conversations SET active_leaf_id = ?, created_at = ?, updated_at = ? WHERE id = ?",
                    (leaf_id, first_stamp, last_stamp, conversation_id),
                )
                tx.commit()
                report.add("groups", "created")
                report.counts[("messages", "created")] += written
            except (sqlite3.Error, ValueError) as exc:
                tx.rollback()
                report.add("groups", "failed", problem=f"{log.name}: {exc}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    st_dir: str
    user: str = "default-user"
    db: str = ""
    only: str = ""
    dry_run: bool = False
    no_backup: bool = False
    include_orphans: bool = False
    limit: int | None = None


def run(options: Options) -> tuple[Report, list[str]]:
    paths = STPaths.locate(Path(options.st_dir), options.user)
    if not paths.root.is_dir():
        return Report(), [f"no SillyTavern data at {paths.root}"]

    db_path = Path(options.db) if options.db else ROOT / "backend" / "data" / "app.db"
    if not db_path.is_file():
        return Report(), [f"no Orb database at {db_path} -- start Orb once to create it"]

    conn = open_db(db_path)
    try:
        problems = check_schema(conn)
        if problems:
            return Report(), problems + ["start Orb once so its migrations run, then try again"]

        if not options.dry_run and not options.no_backup:
            print(f"backup: {backup_db(db_path)}")

        selected = set(options.only.split(",")) if options.only else set(DATASETS)
        report = Report()
        tx = Tx(conn, options.dry_run)

        if "worlds" in selected:
            import_worlds(conn, tx, paths, report)
        # Chats and groups need the avatar-to-card and name-to-persona maps even
        # when those datasets were not selected -- but they must only *read*
        # then, or --only chats would quietly import the whole library too.
        cards: dict[str, str] = {}
        if selected & {"characters", "chats", "groups"}:
            cards = import_characters(conn, tx, paths, report, create="characters" in selected)
        personas: dict[str, int] = {}
        if selected & {"personas", "chats", "groups"}:
            personas = import_personas(conn, tx, paths, report, create="personas" in selected)
        if "chats" in selected:
            import_chats(conn, tx, paths, cards, personas, report, options.include_orphans, options.limit)
        if "groups" in selected:
            import_groups(conn, tx, paths, cards, personas, report)
        if "characters" in selected and "worlds" in selected:
            link_card_worlds(conn, tx, paths, report)

        tx.rollback()  # discards the dry run; a no-op after a committed pass
        return report, []
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate SillyTavern characters, lorebooks, chats, personas and groups into Orb.",
        epilog=(
            "Not migrated: prompts and generation settings, endpoints and API keys, themes and "
            "backgrounds, per-message generation metadata, author's notes, ST tags, "
            "and the lorebook knobs Orb has no equivalent for (recursion, probability, sticky/cooldown, "
            "inclusion groups, per-entry scan depth)."
        ),
    )
    parser.add_argument("--st-dir", required=True, help="SillyTavern install directory")
    parser.add_argument("--user", default="default-user", help="ST data user directory (default: default-user)")
    parser.add_argument("--db", default="", help="target Orb database (default: backend/data/app.db)")
    parser.add_argument("--only", default="", help=f"comma-separated subset of: {','.join(DATASETS)}")
    parser.add_argument("--dry-run", action="store_true", help="do the whole run, then roll it back")
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-migration database copy")
    parser.add_argument("--include-orphans", action="store_true", help="import chats whose character card is gone")
    parser.add_argument("--limit", type=int, default=None, help="stop after N chats (for testing)")
    args = parser.parse_args()

    if args.only:
        unknown = sorted(set(args.only.split(",")) - set(DATASETS))
        if unknown:
            print(f"unknown dataset(s): {', '.join(unknown)}", file=sys.stderr)
            return 2

    report, problems = run(
        Options(
            st_dir=args.st_dir,
            user=args.user,
            db=args.db,
            only=args.only,
            dry_run=args.dry_run,
            no_backup=args.no_backup,
            include_orphans=args.include_orphans,
            limit=args.limit,
        )
    )
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    print(report.render())
    if args.dry_run:
        print("\ndry run -- nothing was written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
