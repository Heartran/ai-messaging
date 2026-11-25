"""/stats: the one read behind the Overview (design §10.11)."""

from datetime import datetime, timedelta, timezone

import pytest

from aim_server.db import TS_FORMAT, connect
from aim_server.main import create_app

from test_api import AuthenticatingClient, register


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "stats.db")


@pytest.fixture()
def client(db_path):
    with AuthenticatingClient(create_app(db_path)) as test_client:
        yield test_client


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime(TS_FORMAT)


def backdate(db_path, message_id, when):
    conn = connect(db_path)
    with conn:
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (when, message_id))
    conn.close()


def test_empty_server_answers_with_zeros_not_errors(client):
    body = client.get("/stats").json()
    assert body["messages"] == {"last_24h": 0, "previous_24h": 0, "hourly": [0] * 24}
    assert body["chats"] == [] and body["participants"] == []
    assert body["waiting_mentions"] == []
    assert body["memories"]["counts"]["DECISION"]["ACTIVE"] == 0


def test_counts_windows_buckets_and_waiting_mentions(client, db_path):
    a = register(client, name="Nova")["participant_id"]
    b = register(client, name="Orion")["participant_id"]
    chat = client.post("/chats", json={"participant_id": a, "name": "release"}).json()["chat_id"]
    client.post(f"/chats/{chat}/follow", json={"participant_id": b})

    def send(sender, text, mentions=()):
        return client.post(
            f"/chats/{chat}/messages",
            json={"sender_id": sender, "text": text, "mentions": list(mentions)},
        ).json()["id"]

    old = send(a, "yesterday's news")
    waiting = send(a, "@Orion can you check?", [b])
    answered = send(b, "@Nova ping", [a])
    send(a, "pong")
    send(b, "fresh")

    backdate(db_path, old, ago(hours=30))         # previous window
    backdate(db_path, waiting, ago(hours=3))      # past the 2 h threshold
    backdate(db_path, answered, ago(hours=4))     # Nova answered after it

    body = client.get("/stats").json()
    assert body["messages"]["last_24h"] == 4
    assert body["messages"]["previous_24h"] == 1
    assert sum(body["messages"]["hourly"]) == 4
    assert body["messages"]["hourly"][-1] == 2    # the current, unfinished hour

    [row] = body["chats"]
    assert row["messages_24h"] == 4 and row["message_count"] == 5
    assert row["member_count"] == 2 and row["dormant_members"] == 0
    assert sum(row["hourly"]) == 4

    # Orion later wrote "fresh" in the chat, so the mention is answered too:
    # waiting means nothing written since by the one who was asked.
    assert body["waiting_mentions"] == []

    by_id = {p["id"]: p for p in body["participants"]}
    assert by_id[a]["messages_24h"] == 2 and by_id[b]["messages_24h"] == 2
    assert by_id[a]["presence"] == "active"


def test_a_mention_nobody_answered_is_waiting(client, db_path):
    a = register(client, name="Nova")["participant_id"]
    b = register(client, name="Orion")["participant_id"]
    chat = client.post("/chats", json={"participant_id": a, "name": "q"}).json()["chat_id"]
    client.post(f"/chats/{chat}/follow", json={"participant_id": b})
    mid = client.post(
        f"/chats/{chat}/messages",
        json={"sender_id": a, "text": "@Orion?", "mentions": [b]},
    ).json()["id"]

    assert client.get("/stats").json()["waiting_mentions"] == []  # too fresh
    backdate(db_path, mid, ago(hours=3))
    [entry] = client.get("/stats").json()["waiting_mentions"]
    assert entry["mentioned_id"] == b and entry["chat_name"] == "q"

    backdate(db_path, mid, ago(days=8))                             # history now
    assert client.get("/stats").json()["waiting_mentions"] == []
