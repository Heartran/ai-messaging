"""SQLite storage — the single source of truth.

Identity and ordering rules live here (docs/design.md §3.1, §4):
- participant IDs are progressive, assigned by the server, and never
  reused *within one database*: AUTOINCREMENT prevents rowid recycling,
  but it cannot survive the file being deleted. That is why every
  database carries an `instance_id` (§4.7): recreate the database and
  the IDs start from 1 again, so a client holding a cached ID from the
  previous instance would silently land on whoever now owns that number.
  The instance ID lets a client notice instead of impersonating;
- a participant proves who it is with a secret token issued at
  registration and stored only as a hash (§4.8). The numeric ID is a
  public identifier — it is printed in every participants listing — and
  therefore can never be the credential;
- every timestamp comes from the server clock, UTC, in one fixed
  ISO 8601 format so lexicographic comparison equals chronological order.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
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
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    machine            TEXT NOT NULL,
    -- Free text by design (§4.6): the four conventional values stopped
    -- describing the products. The same agent registered once as 'code'
    -- and once as 'chat' because neither fitted, which is a taxonomy
    -- failing rather than a client misbehaving. Nothing in the system
    -- branches on this field — it is provenance shown to a reader — so a
    -- wrong value is a typo to correct by hand (§11), not a call to
    -- reject.
    client_type        TEXT NOT NULL,
    agent_type         TEXT NOT NULL,
    registered_at      TEXT NOT NULL,
    client_session_key TEXT,
    last_seen_at       TEXT,
    token_hash         TEXT
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

-- Memory Layer tables (Issue #11: lightweight temporal knowledge graph)
-- Stores facts, decisions, context, and derived knowledge with full provenance

CREATE TABLE IF NOT EXISTS memories (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type        TEXT NOT NULL CHECK (memory_type IN ('FACT', 'DECISION', 'CONTEXT', 'KNOWLEDGE')),
    content            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'ARCHIVED', 'DISPUTED')),
    confidence         REAL NOT NULL DEFAULT 0.95 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_message_id  INTEGER REFERENCES messages(id),
    project_id         INTEGER,
    creator_id         INTEGER REFERENCES participants(id),
    metadata           TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_type_status
    ON memories(memory_type, status);
CREATE INDEX IF NOT EXISTS idx_memories_project
    ON memories(project_id, status);
CREATE INDEX IF NOT EXISTS idx_memories_created
    ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_source_message
    ON memories(source_message_id);

-- Track memory supersession relationships (MEM-182 → MEM-431)
CREATE TABLE IF NOT EXISTS memory_lineage (
    superseded_id      INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    superseding_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    reason             TEXT,
    recorded_at        TEXT NOT NULL,
    PRIMARY KEY (superseded_id, superseding_id)
);

CREATE INDEX IF NOT EXISTS idx_lineage_superseding
    ON memory_lineage(superseding_id);

-- Tags for efficient semantic search
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id          INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag                TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags_tag
    ON memory_tags(tag, memory_id);

-- Track contradictions and disputes between memories
CREATE TABLE IF NOT EXISTS memory_disputes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id          INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    conflicting_id     INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    reason             TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_disputes_memory
    ON memory_disputes(memory_id);
CREATE INDEX IF NOT EXISTS idx_disputes_conflicting
    ON memory_disputes(conflicting_id);
"""


def now_utc() -> str:
    """Current server time as the canonical timestamp string."""
    return _canonical(datetime.now(timezone.utc))


# ------------------------------------------------------- identity secrets

def new_token() -> str:
    """A participant's proof of identity: 256 bits of urandom (§4.8)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Store the hash, never the token.

    A plain SHA-256 is the right tool here and a slow KDF is not: this is
    a full-entropy random secret, not a human-chosen password, so there is
    no dictionary to grind through.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str | None, stored_hash: str | None) -> bool:
    """Constant-time comparison; a missing side never matches."""
    if not token or not stored_hash:
        return False
    return secrets.compare_digest(hash_token(token), stored_hash)


def instance_id(conn: sqlite3.Connection) -> str:
    """This database's identity (§4.7), created once and never changed.

    Recreating the database mints a new one, which is exactly the signal
    clients need: every participant ID they cached belongs to a server
    that no longer exists.
    """
    row = conn.execute(
        "SELECT value FROM server_meta WHERE key = 'instance_id'"
    ).fetchone()
    if row is not None:
        return row["value"]
    value = str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO server_meta (key, value) VALUES ('instance_id', ?)",
        (value,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT value FROM server_meta WHERE key = 'instance_id'"
    ).fetchone()
    return row["value"]


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
        instance_id(conn)  # mint this database's identity on first run
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a live database in place. Identity data is never dropped."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'participants'"
    ).fetchone()
    if not exists:
        return  # fresh database: SCHEMA creates everything at the new shape
    _migrate_to_v1(conn)
    _migrate_to_v2(conn)
    _migrate_to_v3(conn)
    _migrate_to_v4(conn)


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: client_type stops being an enum (§4.6).

    SQLite cannot drop a CHECK, so the table is rebuilt — preserving rows,
    IDs and the AUTOINCREMENT high-water mark, exactly as the v1 migration
    did when 'web-ui' had to be admitted. That earlier rebuild is the
    argument for this one: a closed vocabulary needs a schema migration
    every time a new kind of client appears, and they keep appearing.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'participants'"
    ).fetchone()
    if row is None or "CHECK (client_type" not in (row["sql"] or ""):
        return  # already free, or a fresh database built from SCHEMA

    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'participants'"
    ).fetchone()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE participants_v3 (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                name               TEXT NOT NULL,
                machine            TEXT NOT NULL,
                client_type        TEXT NOT NULL,
                agent_type         TEXT NOT NULL,
                registered_at      TEXT NOT NULL,
                client_session_key TEXT,
                last_seen_at       TEXT,
                token_hash         TEXT
            );
            INSERT INTO participants_v3
                (id, name, machine, client_type, agent_type, registered_at,
                 client_session_key, last_seen_at, token_hash)
                SELECT id, name, machine, client_type, agent_type,
                       registered_at, client_session_key, last_seen_at,
                       token_hash
                FROM participants;
            DROP TABLE participants;
            ALTER TABLE participants_v3 RENAME TO participants;
            """
        )
        if sequence is not None:
            # DROP TABLE took the AUTOINCREMENT high-water mark with it;
            # restore it so participant IDs are never reused (§4.2, §4.7).
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


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: participants gains token_hash (§4.8).

    Existing identities are left with a NULL token: nobody can prove them
    yet, so every identified call they make is refused until the client
    registers again with its client_session_key and collects a token.
    That is the intended blast radius — before this column existed, the
    numeric ID alone was accepted from anyone who could read it.
    """
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(participants)").fetchall()
    }
    if "token_hash" in columns:
        return
    conn.execute("ALTER TABLE participants ADD COLUMN token_hash TEXT")
    conn.commit()


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """v0 → v1: participants gains client_session_key (identity continuity,
    design §4.3) and last_seen_at (presence, §7.2), and the client_type
    CHECK admits 'web-ui' (§4.5). SQLite cannot alter a CHECK, so the
    table is rebuilt — preserving rows, IDs and the AUTOINCREMENT
    sequence so IDs are never reused.
    """
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(participants)").fetchall()
    }
    if "client_session_key" in columns:
        return  # already at v1 or beyond

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


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: Add Memory Layer tables (Issue #11).

    Adds the lightweight temporal knowledge graph tables for storing facts,
    decisions, context, and derived knowledge with full provenance tracking.
    Existing databases are not affected — the new tables are simply added.
    """
    # Check if memories table already exists
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
    ).fetchone()
    if exists:
        return  # already migrated

    # Create all memory layer tables at once
    conn.executescript("""
        CREATE TABLE memories (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type        TEXT NOT NULL CHECK (memory_type IN ('FACT', 'DECISION', 'CONTEXT', 'KNOWLEDGE')),
            content            TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'ARCHIVED', 'DISPUTED')),
            confidence         REAL NOT NULL DEFAULT 0.95 CHECK (confidence >= 0.0 AND confidence <= 1.0),
            source_message_id  INTEGER REFERENCES messages(id),
            project_id         INTEGER,
            creator_id         INTEGER REFERENCES participants(id),
            metadata           TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        );

        CREATE INDEX idx_memories_type_status
            ON memories(memory_type, status);
        CREATE INDEX idx_memories_project
            ON memories(project_id, status);
        CREATE INDEX idx_memories_created
            ON memories(created_at DESC);
        CREATE INDEX idx_memories_source_message
            ON memories(source_message_id);

        CREATE TABLE memory_lineage (
            superseded_id      INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            superseding_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            reason             TEXT,
            recorded_at        TEXT NOT NULL,
            PRIMARY KEY (superseded_id, superseding_id)
        );

        CREATE INDEX idx_lineage_superseding
            ON memory_lineage(superseding_id);

        CREATE TABLE memory_tags (
            memory_id          INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            tag                TEXT NOT NULL,
            PRIMARY KEY (memory_id, tag)
        );

        CREATE INDEX idx_tags_tag
            ON memory_tags(tag, memory_id);

        CREATE TABLE memory_disputes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id          INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            conflicting_id     INTEGER REFERENCES memories(id) ON DELETE SET NULL,
            reason             TEXT NOT NULL,
            created_at         TEXT NOT NULL
        );

        CREATE INDEX idx_disputes_memory
            ON memory_disputes(memory_id);
        CREATE INDEX idx_disputes_conflicting
            ON memory_disputes(conflicting_id);
    """)
    conn.commit()
