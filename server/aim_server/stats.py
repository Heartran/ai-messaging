"""The Overview's one read (design §10.11): is the network OK right now,
and what is waiting on someone."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .db import TS_FORMAT

# Presence (§7.2) is shared with the per-chat listing.
DORMANT_AFTER_HOURS = 24
# A mention nobody answered for this long needs someone.
UNANSWERED_AFTER_HOURS = 2


def compute_stats(conn: sqlite3.Connection) -> dict:  # pylint: disable=too-many-locals
    """Everything the Overview draws, in one read.

    Read-only and anonymous like the other listings: it reveals nothing
    /chats, /chats/{id}/messages and /chats/{id}/participants do not
    already. Memory text is not among them: the memory endpoints give it
    to participants only (§4.8), so disputed memories come as id, type and
    time, and the page fetches their text with its own token. Counts are over
    rolling windows (the last 24 h against the 24 h before), so no time
    zone is assumed and a partial "today" never faces a full yesterday.
    Bucket 0 of every hourly series is the hour that began 23 h ago;
    bucket 23 is the current, unfinished hour.
    """
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    first_bucket = hour_start - timedelta(hours=23)
    cut_24 = (now - timedelta(hours=24)).strftime(TS_FORMAT)
    cut_48 = (now - timedelta(hours=48)).strftime(TS_FORMAT)
    cut_first = first_bucket.strftime(TS_FORMAT)
    dormant_cutoff = (
        now - timedelta(hours=DORMANT_AFTER_HOURS)
    ).strftime(TS_FORMAT)
    unanswered_after = (now - timedelta(hours=UNANSWERED_AFTER_HOURS)).strftime(TS_FORMAT)
    unanswered_window = (now - timedelta(days=7)).strftime(TS_FORMAT)

    def bucket(ts: str) -> int | None:
        moment = datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
        index = int((moment - first_bucket).total_seconds() // 3600)
        return index if 0 <= index < 24 else None

    last_24 = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE created_at > ?", (cut_24,)
    ).fetchone()[0]
    prev_24 = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE created_at > ? AND created_at <= ?",
        (cut_48, cut_24),
    ).fetchone()[0]

    hourly = [0] * 24
    chat_hourly: dict[int, list[int]] = {}
    sender_24: dict[int, int] = {}
    for row in conn.execute(
        "SELECT chat_id, sender_id, created_at FROM messages WHERE created_at >= ?",
        (cut_first,),
    ):
        index = bucket(row["created_at"])
        if index is None:
            continue
        hourly[index] += 1
        chat_hourly.setdefault(row["chat_id"], [0] * 24)[index] += 1
        sender_24[row["sender_id"]] = sender_24.get(row["sender_id"], 0) + 1

    chats = []
    for row in conn.execute(
        """
        SELECT c.id, c.name,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS message_count,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.chat_id = c.id)
                   AS last_message_at,
               (SELECT COUNT(*) FROM messages m
                 WHERE m.chat_id = c.id AND m.created_at > ?) AS messages_24h,
               (SELECT COUNT(*) FROM chat_members cm
                 WHERE cm.chat_id = c.id AND cm.left_at IS NULL) AS member_count,
               (SELECT COUNT(*) FROM chat_members cm
                  JOIN participants p ON p.id = cm.participant_id
                 WHERE cm.chat_id = c.id AND cm.left_at IS NULL
                   AND (p.last_seen_at IS NULL OR p.last_seen_at < ?))
                   AS dormant_members
        FROM chats c
        ORDER BY COALESCE(last_message_at, c.created_at) DESC, c.id DESC
        """,
        (cut_24, dormant_cutoff),
    ):
        entry = dict(row)
        entry["hourly"] = chat_hourly.get(row["id"], [0] * 24)
        chats.append(entry)

    participants = []
    for row in conn.execute(
        """
        SELECT p.id, p.name, p.machine, p.client_type, p.agent_type,
               p.last_seen_at,
               (SELECT COUNT(*) FROM chat_members cm
                 WHERE cm.participant_id = p.id AND cm.left_at IS NULL) AS chat_count
        FROM participants p
        ORDER BY p.last_seen_at IS NULL, p.last_seen_at DESC, p.id
        """
    ):
        entry = dict(row)
        entry["presence"] = (
            "active"
            if row["last_seen_at"] is not None and row["last_seen_at"] >= dormant_cutoff
            else "dormant"
        )
        entry["messages_24h"] = sender_24.get(row["id"], 0)
        participants.append(entry)

    # A mention is waiting while the mentioned participant, still a
    # member, has written nothing in that chat since. Older than a week
    # it is history, not a queue.
    waiting = [
        dict(row)
        for row in conn.execute(
            """
            SELECT mn.message_id, msg.chat_id, c.name AS chat_name,
                   msg.sender_id, ps.name AS sender_name,
                   mn.participant_id AS mentioned_id, pm.name AS mentioned_name,
                   msg.created_at
            FROM mentions mn
            JOIN messages msg ON msg.id = mn.message_id
            JOIN chats c ON c.id = msg.chat_id
            JOIN participants ps ON ps.id = msg.sender_id
            JOIN participants pm ON pm.id = mn.participant_id
            JOIN chat_members cm ON cm.chat_id = msg.chat_id
                 AND cm.participant_id = mn.participant_id AND cm.left_at IS NULL
            WHERE msg.created_at > ? AND msg.created_at <= ?
              AND mn.participant_id != msg.sender_id
              AND NOT EXISTS (
                  SELECT 1 FROM messages r
                  WHERE r.chat_id = msg.chat_id AND r.sender_id = mn.participant_id
                    AND r.id > msg.id)
            ORDER BY msg.created_at
            LIMIT 50
            """,
            (unanswered_window, unanswered_after),
        )
    ]

    memory_counts = {
        kind: {status: 0 for status in ("ACTIVE", "SUPERSEDED", "ARCHIVED", "DISPUTED")}
        for kind in ("DECISION", "FACT", "CONTEXT", "KNOWLEDGE")
    }
    for row in conn.execute(
        "SELECT memory_type, status, COUNT(*) AS n FROM memories "
        "GROUP BY memory_type, status"
    ):
        memory_counts[row["memory_type"]][row["status"]] = row["n"]
    disputed = [
        dict(row)
        for row in conn.execute(
            "SELECT id, memory_type, updated_at FROM memories "
            "WHERE status = 'DISPUTED' ORDER BY updated_at DESC LIMIT 8"
        )
    ]

    return {
        "generated_at": now.strftime(TS_FORMAT),
        "hour_start": first_bucket.strftime(TS_FORMAT),
        "dormant_after_hours": DORMANT_AFTER_HOURS,
        "unanswered_after_hours": UNANSWERED_AFTER_HOURS,
        "messages": {"last_24h": last_24, "previous_24h": prev_24, "hourly": hourly},
        "chats": chats,
        "participants": participants,
        "waiting_mentions": waiting,
        "memories": {"counts": memory_counts, "disputed": disputed},
    }
