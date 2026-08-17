"""SQLite storage — the single source of truth.

Identity and ordering rules live here (docs/design.md §3.1, §4):
- participant and message IDs are progressive, assigned by the server,
  and never reused (AUTOINCREMENT prevents rowid recycling);
- every timestamp comes from the server clock, UTC, in one fixed
  ISO 8601 format so lexicographic comparison equals chronological order.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Fixed-width UTC format: string comparison == time comparison.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _canonical(dt: datetime) -> str:
    """Format an aware datetime in the canonical fixed-width UTC form.

    isoformat() zero-pads the year to 4 digits (strftime's %Y does not on
    every platform), which keeps lexicographic order == chronological order
    for any input year.
    """
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return utc.isoformat(timespec="microseconds") + "Z"

# Bump when the schema changes; migrate() upgrades live databases in place.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    machine            TEXT NOT NULL,
    client_type        TEXT NOT NULL
        CHECK (client_type IN ('chat', 'cowork', 'code', 'web-ui')),
    agent_type         TEXT NOT NULL,
    registered_at      TEXT NOT NULL,
    client_session_key TEXT,
    last_seen_at       TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_session_key
    ON participants(client_session_key)
    WHERE client_session_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    created_by  INTEGER NOT NULL REFERENCES participants(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id        INTEGER NOT NULL REFERENCES chats(id),
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    followed_at    TEXT NOT NULL,
    left_at        TEXT,
    PRIMARY KEY (chat_id, participant_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL REFERENCES chats(id),
    sender_id       INTEGER NOT NULL REFERENCES participants(id),
    text            TEXT NOT NULL,
    is_introduction INTEGER NOT NULL DEFAULT 0,
    intro_payload   TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_created
    ON messages(chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mentions (
    message_id     INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    PRIMARY KEY (message_id, participant_id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_participant
    ON mentions(participant_id, message_id);
"""


def now_utc() -> str:
    """Current server time as the canonical timestamp string."""
    return _canonical(datetime.now(timezone.utc))


def parse_client_timestamp(raw: str) -> str:
    """Normalize a client-supplied ISO 8601 instant to the canonical format.

    Raises ValueError if unparseable. Naive timestamps are taken as UTC.
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _canonical(dt)


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas this schema relies on."""
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    # One connection per request, used sequentially — but FastAPI may run a
    # sync dependency and its endpoint on different threadpool threads, so
    # the same-thread check must be off.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        _migrate(conn)
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a live database in place. Identity data is never dropped.

    v0 → v1: participants gains client_session_key (identity continuity,
    design §4.3) and last_seen_at (presence, §7.2), and the client_type
    CHECK admits 'web-ui' (§4.5). SQLite cannot alter a CHECK, so the
    table is rebuilt — preserving rows, IDs and the AUTOINCREMENT
    sequence so IDs are never reused.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'participants'"
    ).fetchone()
    if not exists:
        return  # fresh database: SCHEMA creates everything at the new shape
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(participants)").fetchall()
    }
    if "client_session_key" in columns:
        return  # already the new shape, only the version stamp was missing

    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'participants'"
    ).fetchone()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE participants_v1 (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                name               TEXT NOT NULL,
                machine            TEXT NOT NULL,
                client_type        TEXT NOT NULL
                    CHECK (client_type IN ('chat', 'cowork', 'code', 'web-ui')),
                agent_type         TEXT NOT NULL,
                registered_at      TEXT NOT NULL,
                client_session_key TEXT,
                last_seen_at       TEXT
            );
            INSERT INTO participants_v1
                (id, name, machine, client_type, agent_type, registered_at)
                SELECT id, name, machine, client_type, agent_type, registered_at
                FROM participants;
            DROP TABLE participants;
            ALTER TABLE participants_v1 RENAME TO participants;
            """
        )
        if sequence is not None:
            # DROP TABLE removed the AUTOINCREMENT high-water mark; restore
            # it so participant IDs are never reused (design §4.2).
            # sqlite_sequence has no unique constraint: update-then-insert.
            updated = conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'participants'",
                (sequence["seq"],),
            )
            if updated.rowcount == 0:
                conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) "
                    "VALUES ('participants', ?)",
                    (sequence["seq"],),
                )
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def purge_old_messages(conn: sqlite3.Connection, cutoff: str) -> int:
    """Permanently delete messages older than `cutoff` (canonical format).

    Returns the number of deleted messages. Mentions cascade. This runs
    only when a retention policy is explicitly configured — and the policy
    is declared by the API, never a silent disappearance (design §8.2).
    """
    cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount
