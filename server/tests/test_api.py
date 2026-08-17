"""End-to-end tests of the HTTP API against a temporary SQLite database."""

import pytest
from fastapi.testclient import TestClient

from aim_server.db import connect, now_utc, purge_old_messages
from aim_server.main import EMPTY_NOTICE, FRAMING, create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def register(client, name="Nova", machine="PC-EXAMPLE", client_type="chat",
             agent_type="claude"):
    response = client.post(
        "/register",
        json={
            "name": name,
            "machine": machine,
            "client_type": client_type,
            "agent_type": agent_type,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------ registration

def test_register_assigns_progressive_ids_and_instructs_introduction(client):
    first = register(client, name="Nova")
    second = register(client, name="Claude Code", client_type="code")
    assert first["participant_id"] == 1
    assert second["participant_id"] == 2
    # Handshake (§5.5): the answer instructs the agent to present itself.
    assert "introduc" in first["next_step"].lower()


def test_same_machine_two_registrations_two_ids(client):
    a = register(client, name="Nova", machine="PC-FEDERICO", client_type="chat")
    b = register(client, name="Code", machine="PC-FEDERICO", client_type="code")
    assert a["participant_id"] != b["participant_id"]


def test_register_rejects_unknown_client_type(client):
    response = client.post(
        "/register",
        json={
            "name": "X",
            "machine": "M",
            "client_type": "browser",
            "agent_type": "claude",
        },
    )
    assert response.status_code == 422


# ------------------------------------------------------- identity continuity

def test_register_with_session_key_is_idempotent_across_machines(client):
    key = "https://claude.ai/chat/0000-aaaa-bbbb"
    first = client.post(
        "/register",
        json={
            "name": "Nova",
            "machine": "PC-GAMING",
            "client_type": "chat",
            "agent_type": "claude",
            "client_session_key": key,
        },
    ).json()
    assert first["resumed"] is False

    # Same conversation, resumed from another machine (§4.3).
    second = client.post(
        "/register",
        json={
            "name": "Nova",
            "machine": "DESKTOP-OTHER",
            "client_type": "chat",
            "agent_type": "claude",
            "client_session_key": key,
        },
    ).json()
    assert second["resumed"] is True
    assert second["participant_id"] == first["participant_id"]
    assert second["machine"] == "DESKTOP-OTHER"  # descriptive, follows along
    assert "resumed" in second["next_step"].lower()

    # No ghost was created.
    chat_id = client.post(
        "/chats",
        json={"participant_id": first["participant_id"], "name": "x"},
    ).json()["chat_id"]
    body = client.get(f"/chats/{chat_id}/participants").json()
    assert body["count"] == 1


def test_different_session_keys_are_different_identities(client):
    ids = set()
    for key in ("conversation-aaaa", "conversation-bbbb"):
        response = client.post(
            "/register",
            json={
                "name": "Nova",
                "machine": "M",
                "client_type": "chat",
                "agent_type": "claude",
                "client_session_key": key,
            },
        ).json()
        ids.add(response["participant_id"])
    assert len(ids) == 2


def test_register_without_key_keeps_legacy_behavior(client):
    a = register(client)["participant_id"]
    b = register(client)["participant_id"]
    assert a != b


def test_session_key_is_never_exposed(client):
    key = "conversation-secret-key"
    pid = client.post(
        "/register",
        json={
            "name": "Nova",
            "machine": "M",
            "client_type": "chat",
            "agent_type": "claude",
            "client_session_key": key,
        },
    ).json()
    assert "client_session_key" not in pid
    chat_id = client.post(
        "/chats", json={"participant_id": pid["participant_id"], "name": "g"}
    ).json()["chat_id"]
    participants = client.get(f"/chats/{chat_id}/participants")
    assert key not in participants.text
    assert "client_session_key" not in participants.text


def test_web_ui_client_type_accepted(client):
    response = register(client, name="Fede", client_type="web-ui",
                        agent_type="human")
    assert response["participant_id"] == 1


# ---------------------------------------------------------------- presence

def test_presence_tracks_activity_and_marks_dormant(client, tmp_path):
    chat_id, a, b = make_chat_with_two(client)
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "hi"})

    body = client.get(f"/chats/{chat_id}/participants").json()
    by_id = {p["id"]: p for p in body["participants"]}
    assert by_id[a]["last_seen_at"] is not None
    assert by_id[a]["presence"] == "active"
    assert body["dormant_after_hours"] == 24

    # Backdate b's last call: it becomes dormant, visibly — not deleted.
    conn = connect(str(tmp_path / "test.db"))
    try:
        conn.execute(
            "UPDATE participants SET last_seen_at = '2000-01-01T00:00:00.000000Z' "
            "WHERE id = ?",
            (b,),
        )
        conn.commit()
    finally:
        conn.close()
    body = client.get(f"/chats/{chat_id}/participants").json()
    by_id = {p["id"]: p for p in body["participants"]}
    assert by_id[b]["presence"] == "dormant"
    assert by_id[b]["active"] is True  # never left; dormant, not gone


# --------------------------------------------------------------- migration

def test_v0_database_is_migrated_preserving_ids_and_sequence(tmp_path):
    import sqlite3 as sq

    from aim_server.db import init_db

    db_path = str(tmp_path / "legacy.db")
    legacy = sq.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE participants (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            machine       TEXT NOT NULL,
            client_type   TEXT NOT NULL CHECK (client_type IN ('chat', 'cowork', 'code')),
            agent_type    TEXT NOT NULL,
            registered_at TEXT NOT NULL
        );
        INSERT INTO participants (name, machine, client_type, agent_type, registered_at)
            VALUES ('Nova', 'PC', 'chat', 'claude', '2026-08-16T00:00:00.000000Z');
        DELETE FROM participants;  -- ID 1 consumed and gone: must never return
        INSERT INTO participants (name, machine, client_type, agent_type, registered_at)
            VALUES ('Code', 'PC', 'code', 'claude', '2026-08-16T00:00:00.000000Z');
        """
    )
    legacy.commit()
    legacy.close()

    init_db(db_path)  # runs the v0 → v1 migration

    conn = sq.connect(db_path)
    conn.row_factory = sq.Row
    rows = conn.execute("SELECT * FROM participants").fetchall()
    assert [row["id"] for row in rows] == [2]  # data and IDs preserved
    assert rows[0]["client_session_key"] is None  # new columns exist
    # 'web-ui' now allowed, and the AUTOINCREMENT sequence was preserved:
    # the next ID is 3, not a reuse of 1.
    conn.execute(
        "INSERT INTO participants (name, machine, client_type, agent_type, "
        "registered_at) VALUES ('Fede', 'ui', 'web-ui', 'human', 'now')"
    )
    new_id = conn.execute("SELECT MAX(id) FROM participants").fetchone()[0]
    assert new_id == 3
    conn.close()

    init_db(db_path)  # idempotent: running the migration again is a no-op


# ------------------------------------------------------------------- chats

def test_create_chat_auto_follows_creator(client):
    creator = register(client)
    response = client.post(
        "/chats", json={"participant_id": creator["participant_id"], "name": "general"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["following"] is True

    participants = client.get(f"/chats/{body['chat_id']}/participants").json()
    assert participants["count"] == 1
    assert participants["participants"][0]["id"] == creator["participant_id"]
    assert participants["participants"][0]["active"] is True


def test_create_chat_duplicate_name_conflicts_with_explicit_hint(client):
    creator = register(client)
    pid = creator["participant_id"]
    assert client.post(
        "/chats", json={"participant_id": pid, "name": "general"}
    ).status_code == 201
    duplicate = client.post(
        "/chats", json={"participant_id": pid, "name": "General"}
    )
    assert duplicate.status_code == 409
    assert "Follow it" in duplicate.json()["detail"]


def test_create_chat_requires_registration(client):
    response = client.post("/chats", json={"participant_id": 99, "name": "general"})
    assert response.status_code == 404


def test_list_chats_ordering_and_following_flag(client):
    pid = register(client)["participant_id"]
    other = register(client, name="Other")["participant_id"]
    first = client.post(
        "/chats", json={"participant_id": pid, "name": "older"}
    ).json()["chat_id"]
    second = client.post(
        "/chats", json={"participant_id": other, "name": "newer"}
    ).json()["chat_id"]

    # A new message in the older chat bumps it to the top.
    client.post(
        f"/chats/{first}/messages", json={"sender_id": pid, "text": "bump"}
    )
    body = client.get("/chats", params={"participant_id": pid}).json()
    assert [chat["id"] for chat in body["chats"]] == [first, second]
    by_id = {chat["id"]: chat for chat in body["chats"]}
    assert by_id[first]["following"] is True
    assert by_id[second]["following"] is False
    assert by_id[first]["message_count"] == 1
    assert by_id[first]["last_message_at"] is not None


def test_list_chats_empty_has_notice(client):
    body = client.get("/chats").json()
    assert body["chats"] == []
    assert body["notice"] is not None


# ------------------------------------------------------------ follow/leave

def test_follow_is_idempotent_and_rejoin_keeps_id(client):
    founder = register(client)["participant_id"]
    joiner = register(client, name="Joiner")["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": founder, "name": "general"}
    ).json()["chat_id"]

    joined = client.post(
        f"/chats/{chat_id}/follow", json={"participant_id": joiner}
    ).json()
    assert joined["already_following"] is False
    assert "next_step" in joined

    again = client.post(
        f"/chats/{chat_id}/follow", json={"participant_id": joiner}
    ).json()
    assert again["already_following"] is True

    left = client.post(
        f"/chats/{chat_id}/leave", json={"participant_id": joiner}
    ).json()
    assert left["left"] is True and left["already_left"] is False

    # The ghost is explicit, not silent (§7.2).
    participants = client.get(f"/chats/{chat_id}/participants").json()
    ghost = next(p for p in participants["participants"] if p["id"] == joiner)
    assert ghost["active"] is False and ghost["left_at"] is not None

    rejoined = client.post(
        f"/chats/{chat_id}/follow", json={"participant_id": joiner}
    ).json()
    assert rejoined["rejoined"] is True
    assert rejoined["participant_id"] == joiner  # same ID, never migrated


def test_leave_without_following_is_explicit(client):
    pid = register(client)["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": pid, "name": "general"}
    ).json()["chat_id"]
    stranger = register(client, name="Stranger")["participant_id"]
    response = client.post(
        f"/chats/{chat_id}/leave", json={"participant_id": stranger}
    )
    assert response.status_code == 404
    assert "nothing to leave" in response.json()["detail"]


def test_leave_twice_is_idempotent_with_flag(client):
    pid = register(client)["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": pid, "name": "general"}
    ).json()["chat_id"]
    client.post(f"/chats/{chat_id}/leave", json={"participant_id": pid})
    second = client.post(
        f"/chats/{chat_id}/leave", json={"participant_id": pid}
    ).json()
    assert second["already_left"] is True


# ---------------------------------------------------------------- messages

def make_chat_with_two(client):
    a = register(client, name="Alice")["participant_id"]
    b = register(client, name="Bob", client_type="code")["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": a, "name": "general"}
    ).json()["chat_id"]
    client.post(f"/chats/{chat_id}/follow", json={"participant_id": b})
    return chat_id, a, b


def test_server_fills_identity_and_ordering_fields(client):
    chat_id, a, _ = make_chat_with_two(client)
    sent = client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "Hello, I am writing in first person."},
    )
    assert sent.status_code == 201
    message = sent.json()
    # Server-assigned fields (§4.3): id, timestamp, resolved sender metadata.
    assert message["id"] == 1
    assert message["created_at"].endswith("Z")
    assert message["sender"]["id"] == a
    assert message["sender"]["machine"] == "PC-EXAMPLE"
    assert message["sender"]["agent_type"] == "claude"
    assert message["is_introduction"] is False


def test_send_requires_active_membership(client):
    chat_id, _, b = make_chat_with_two(client)
    outsider = register(client, name="Outsider")["participant_id"]
    refused = client.post(
        f"/chats/{chat_id}/messages", json={"sender_id": outsider, "text": "hi"}
    )
    assert refused.status_code == 403

    client.post(f"/chats/{chat_id}/leave", json={"participant_id": b})
    after_leaving = client.post(
        f"/chats/{chat_id}/messages", json={"sender_id": b, "text": "hi"}
    )
    assert after_leaving.status_code == 403
    assert "left" in after_leaving.json()["detail"]


def test_mentions_are_validated_and_deduplicated(client):
    chat_id, a, b = make_chat_with_two(client)
    ok = client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "for Bob", "mentions": [b, b]},
    )
    assert ok.status_code == 201
    assert ok.json()["mentions"] == [b]

    bad = client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "for nobody", "mentions": [999]},
    )
    assert bad.status_code == 422
    assert "999" in bad.json()["detail"]


def test_get_messages_desc_after_limit_and_framing(client):
    chat_id, a, _ = make_chat_with_two(client)
    for i in range(5):
        client.post(
            f"/chats/{chat_id}/messages",
            json={"sender_id": a, "text": f"message {i}"},
        )

    body = client.get(f"/chats/{chat_id}/messages").json()
    assert body["framing"] == FRAMING
    assert body["notice"] is None
    texts = [m["text"] for m in body["messages"]]
    assert texts == [f"message {i}" for i in reversed(range(5))]  # DESC

    limited = client.get(f"/chats/{chat_id}/messages", params={"limit": 2}).json()
    assert [m["text"] for m in limited["messages"]] == ["message 4", "message 3"]

    # `after` returns only strictly newer messages.
    cutoff = body["messages"][2]["created_at"]  # message 2
    newer = client.get(
        f"/chats/{chat_id}/messages", params={"after": cutoff}
    ).json()
    assert [m["text"] for m in newer["messages"]] == ["message 4", "message 3"]


def test_get_messages_accepts_client_iso_variants(client):
    chat_id, a, _ = make_chat_with_two(client)
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "hi"})
    for variant in (
        "2020-01-01T00:00:00Z",
        "2020-01-01T00:00:00+00:00",
        "2020-01-01T01:00:00+01:00",
        "2020-01-01 00:00:00",
    ):
        body = client.get(
            f"/chats/{chat_id}/messages", params={"after": variant}
        ).json()
        assert body["count"] == 1, variant
    bad = client.get(f"/chats/{chat_id}/messages", params={"after": "yesterday"})
    assert bad.status_code == 422


def test_empty_chat_returns_explicit_sentinel(client):
    chat_id, _, _ = make_chat_with_two(client)
    body = client.get(f"/chats/{chat_id}/messages").json()
    assert body["messages"] == []
    assert body["notice"] == EMPTY_NOTICE


def test_only_mentions_filters_on_metadata_not_text(client):
    chat_id, a, b = make_chat_with_two(client)
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "to everyone"},
    )
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "Bob mentioned in text only"},
    )
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "structured mention", "mentions": [b]},
    )
    body = client.get(
        f"/chats/{chat_id}/messages",
        params={"participant_id": b, "only_mentions": "true"},
    ).json()
    assert [m["text"] for m in body["messages"]] == ["structured mention"]

    missing_pid = client.get(
        f"/chats/{chat_id}/messages", params={"only_mentions": "true"}
    )
    assert missing_pid.status_code == 422


def test_id_cursors_page_through_bursts_without_ties(client):
    chat_id, a, _ = make_chat_with_two(client)
    for i in range(5):
        client.post(
            f"/chats/{chat_id}/messages",
            json={"sender_id": a, "text": f"message {i}"},
        )
    # after_id: the tie-proof read checkpoint (IDs grow with server time).
    newer = client.get(
        f"/chats/{chat_id}/messages", params={"after_id": 3}
    ).json()
    assert [m["id"] for m in newer["messages"]] == [5, 4]

    # A burst larger than the limit: page the gap with before_id.
    window = client.get(
        f"/chats/{chat_id}/messages", params={"after_id": 0, "limit": 2}
    ).json()
    assert [m["id"] for m in window["messages"]] == [5, 4]
    older = client.get(
        f"/chats/{chat_id}/messages",
        params={"after_id": 0, "before_id": 4, "limit": 2},
    ).json()
    assert [m["id"] for m in older["messages"]] == [3, 2]

    # `before` (timestamp) pages the same way.
    cutoff = window["messages"][-1]["created_at"]  # message id 4
    older_ts = client.get(
        f"/chats/{chat_id}/messages", params={"before": cutoff}
    ).json()
    assert [m["id"] for m in older_ts["messages"]] == [3, 2, 1]


def test_mentions_list_is_bounded(client):
    chat_id, a, b = make_chat_with_two(client)
    response = client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "spam", "mentions": [b] * 51},
    )
    assert response.status_code == 422


def test_timestamps_zero_pad_years(client):
    from aim_server.db import parse_client_timestamp

    assert parse_client_timestamp("0999-01-01T00:00:00Z").startswith("0999-")
    chat_id, a, _ = make_chat_with_two(client)
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "hi"})
    body = client.get(
        f"/chats/{chat_id}/messages", params={"after": "0999-01-01T00:00:00Z"}
    ).json()
    assert body["count"] == 1


def test_framing_on_all_participant_content_paths(client):
    chat_id, _, _ = make_chat_with_two(client)
    assert client.get("/chats").json()["framing"] == FRAMING
    assert (
        client.get(f"/chats/{chat_id}/participants").json()["framing"] == FRAMING
    )


def test_from_id_filters_by_sender(client):
    chat_id, a, b = make_chat_with_two(client)
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "from a"})
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": b, "text": "from b"})
    body = client.get(
        f"/chats/{chat_id}/messages", params={"from_id": b}
    ).json()
    assert [m["text"] for m in body["messages"]] == ["from b"]
    unknown = client.get(f"/chats/{chat_id}/messages", params={"from_id": 999})
    assert unknown.status_code == 404


def test_text_query_matches_literally_including_like_wildcards(client):
    chat_id, a, _ = make_chat_with_two(client)
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "progress at 100% today"},
    )
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "progress at 100 units today"},
    )
    body = client.get(
        f"/chats/{chat_id}/messages", params={"query": "100%"}
    ).json()
    assert [m["text"] for m in body["messages"]] == ["progress at 100% today"]


def test_global_inbox_spans_only_followed_chats(client):
    a = register(client, name="Alice")["participant_id"]
    b = register(client, name="Bob")["participant_id"]
    followed = client.post(
        "/chats", json={"participant_id": a, "name": "followed"}
    ).json()["chat_id"]
    other = client.post(
        "/chats", json={"participant_id": b, "name": "not-followed"}
    ).json()["chat_id"]
    abandoned = client.post(
        "/chats", json={"participant_id": a, "name": "abandoned"}
    ).json()["chat_id"]
    client.post(f"/chats/{followed}/follow", json={"participant_id": b})
    client.post(f"/chats/{followed}/messages", json={"sender_id": b, "text": "in"})
    client.post(f"/chats/{other}/messages", json={"sender_id": b, "text": "out"})
    client.post(
        f"/chats/{abandoned}/messages", json={"sender_id": a, "text": "old"}
    )
    client.post(f"/chats/{abandoned}/leave", json={"participant_id": a})

    body = client.get("/messages", params={"participant_id": a}).json()
    assert [(m["chat_name"], m["text"]) for m in body["messages"]] == [
        ("followed", "in")
    ]
    assert body["framing"] == FRAMING

    # participant_id is mandatory for the inbox.
    assert client.get("/messages").status_code == 422


def test_global_inbox_what_awaits_me_anywhere(client):
    # The most important call of the system (§8.3): chat scope omitted,
    # only_mentions=true, after_id=<checkpoint>.
    a = register(client, name="Alice")["participant_id"]
    b = register(client, name="Bob")["participant_id"]
    one = client.post(
        "/chats", json={"participant_id": a, "name": "one"}
    ).json()["chat_id"]
    two = client.post(
        "/chats", json={"participant_id": a, "name": "two"}
    ).json()["chat_id"]
    for chat in (one, two):
        client.post(f"/chats/{chat}/follow", json={"participant_id": b})
    checkpoint = client.post(
        f"/chats/{one}/messages",
        json={"sender_id": b, "text": "old mention", "mentions": [a]},
    ).json()["id"]
    client.post(
        f"/chats/{one}/messages",
        json={"sender_id": b, "text": "new mention in one", "mentions": [a]},
    )
    client.post(
        f"/chats/{two}/messages",
        json={"sender_id": b, "text": "new mention in two", "mentions": [a]},
    )
    client.post(f"/chats/{two}/messages", json={"sender_id": b, "text": "noise"})

    body = client.get(
        "/messages",
        params={"participant_id": a, "only_mentions": "true", "after_id": checkpoint},
    ).json()
    assert [m["text"] for m in body["messages"]] == [
        "new mention in two",
        "new mention in one",
    ]


def test_list_chats_since_counts_unread_statelessly(client):
    chat_id, a, _ = make_chat_with_two(client)
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "one"})
    checkpoint = client.post(
        f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "two"}
    ).json()["created_at"]
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "three"})
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "four"})

    body = client.get("/chats", params={"since": checkpoint}).json()
    assert body["chats"][0]["messages_since"] == 2

    plain = client.get("/chats").json()
    assert "messages_since" not in plain["chats"][0]


def test_list_chats_include_last_message(client):
    chat_id, a, _ = make_chat_with_two(client)
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "first"})
    client.post(f"/chats/{chat_id}/messages", json={"sender_id": a, "text": "latest"})
    empty = client.post(
        "/chats", json={"participant_id": a, "name": "empty"}
    ).json()["chat_id"]

    body = client.get("/chats", params={"include_last_message": "true"}).json()
    by_id = {chat["id"]: chat for chat in body["chats"]}
    assert by_id[chat_id]["last_message"]["text"] == "latest"
    assert by_id[empty]["last_message"] is None

    plain = client.get("/chats").json()
    assert "last_message" not in plain["chats"][0]


def test_list_chats_query_filters_by_name(client):
    pid = register(client)["participant_id"]
    client.post("/chats", json={"participant_id": pid, "name": "general"})
    client.post("/chats", json={"participant_id": pid, "name": "dev-updates"})
    body = client.get("/chats", params={"query": "gener"}).json()
    assert [chat["name"] for chat in body["chats"]] == ["general"]
    nothing = client.get("/chats", params={"query": "zzz"}).json()
    assert nothing["chats"] == []
    assert nothing["notice"] == "No chats match this query."


def test_participant_chats_endpoint(client):
    a = register(client, name="Alice")["participant_id"]
    one = client.post(
        "/chats", json={"participant_id": a, "name": "one"}
    ).json()["chat_id"]
    two = client.post(
        "/chats", json={"participant_id": a, "name": "two"}
    ).json()["chat_id"]
    client.post(f"/chats/{two}/leave", json={"participant_id": a})

    body = client.get(f"/participants/{a}/chats").json()
    assert body["participant_name"] == "Alice"
    by_id = {chat["id"]: chat for chat in body["chats"]}
    assert by_id[one]["active"] is True
    assert by_id[two]["active"] is False

    assert client.get("/participants/999/chats").status_code == 404


# ------------------------------------------------------------ introduction

def test_introduction_is_a_message_with_a_twist(client):
    chat_id, a, _ = make_chat_with_two(client)
    payload = {
        "who": "I am Nova, Fede's assistant",
        "works_for": "Fede",
        "goal": "coordinate work across machines",
        "seeking": "updates from the other agents",
    }
    response = client.post(
        f"/chats/{chat_id}/introductions",
        json={
            "sender_id": a,
            "text": "Hi everyone, I'm Nova and I run on the main PC.",
            "payload": payload,
        },
    )
    assert response.status_code == 201
    intro = response.json()
    assert intro["is_introduction"] is True
    assert intro["intro_payload"] == payload

    # It lands in the normal history (§5.4), flag and payload readable.
    body = client.get(f"/chats/{chat_id}/messages").json()
    assert body["messages"][0]["is_introduction"] is True
    assert body["messages"][0]["intro_payload"] == payload


def test_introduction_requires_membership(client):
    chat_id, _, _ = make_chat_with_two(client)
    outsider = register(client, name="Outsider")["participant_id"]
    response = client.post(
        f"/chats/{chat_id}/introductions",
        json={
            "sender_id": outsider,
            "text": "hello",
            "payload": {"who": "x", "works_for": "y", "goal": "z", "seeking": "w"},
        },
    )
    assert response.status_code == 403


# --------------------------------------------------------------- retention

def test_purge_old_messages_deletes_and_cascades(client, tmp_path):
    chat_id, a, b = make_chat_with_two(client)
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "old", "mentions": [b]},
    )
    conn = connect(str(tmp_path / "test.db"))
    try:
        # Age the message artificially, then purge with cutoff = now.
        conn.execute("UPDATE messages SET created_at = '2000-01-01T00:00:00.000000Z'")
        conn.commit()
        deleted = purge_old_messages(conn, now_utc())
        assert deleted == 1
        assert conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == 0
    finally:
        conn.close()


def test_every_json_response_declares_the_server_version(client):
    from aim_server import __version__

    pid = register(client)["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": pid, "name": "general"}
    ).json()["chat_id"]

    responses = [
        client.get("/health"),
        client.get("/chats"),
        client.get(f"/chats/{chat_id}/messages"),
        client.get(f"/chats/{chat_id}/participants"),
        client.get(f"/participants/{pid}/chats"),
        client.get("/messages", params={"participant_id": pid}),
    ]
    for response in responses:
        assert response.json()["server_version"] == __version__, response.url

    # Errors declare it too (§7.2: every response, so skew is diagnosable
    # exactly when something goes wrong).
    error = client.get("/chats/999/messages")
    assert error.status_code == 404
    assert error.json()["server_version"] == __version__


def test_unknown_query_params_are_rejected_not_ignored(client):
    pid = register(client)["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": pid, "name": "general"}
    ).json()["chat_id"]

    response = client.get(
        f"/chats/{chat_id}/messages", params={"include_context": "3"}
    )
    assert response.status_code == 422
    assert "include_context" in response.json()["detail"]
    assert "newer than the server" in response.json()["detail"]

    # Known params still pass, obviously.
    assert client.get(
        f"/chats/{chat_id}/messages", params={"limit": 5}
    ).status_code == 200


def test_unknown_body_fields_are_rejected_not_ignored(client):
    response = client.post(
        "/register",
        json={
            "name": "X",
            "machine": "M",
            "client_type": "chat",
            "agent_type": "claude",
            "some_future_field": True,
        },
    )
    assert response.status_code == 422
    assert "some_future_field" in response.text


def test_ui_is_served_from_the_same_bind(client):
    page = client.get("/ui")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "AI Messaging" in page.text

    root = client.get("/", follow_redirects=False)
    assert root.status_code in (302, 307)
    assert root.headers["location"] == "/ui"


def test_health_declares_retention_policy(client, tmp_path):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["retention"]["days"] is None
    assert "kept forever" in body["retention"]["policy"]

    app = create_app(str(tmp_path / "retention.db"), retention_days=30)
    with TestClient(app) as retention_client:
        declared = retention_client.get("/health").json()
        assert declared["retention"]["days"] == 30
        assert "30 days" in declared["retention"]["policy"]
