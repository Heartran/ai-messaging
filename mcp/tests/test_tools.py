"""End-to-end tests of the MCP tool logic against the real server."""

import pytest

from aim_mcp.client import AimServerError
from aim_mcp.user_config import NotRegisteredError, UserConfig

pytestmark = pytest.mark.anyio

KEY_A = "conversation-alpha-0001"
KEY_B = "conversation-beta-0002"
KEY_BOB = "conversation-bob-0003"


async def register(tools, key=KEY_A, name="Nova", client_type="chat",
                   agent_type="claude", machine="PC-EXAMPLE"):
    return await tools.register(key, name, client_type, agent_type, machine=machine)


# ---------------------------------------------------- §4.4 flaw regression

async def test_two_conversations_share_one_client_without_clashing(tools):
    # THE flaw (§4.4): with a single-identity user_config, conversation B
    # registering overwrote conversation A — A's next message went out as B.
    a = await register(tools, key=KEY_A, name="Nova")
    b = await register(tools, key=KEY_B, name="CodeRunner", client_type="code")
    assert a["participant_id"] != b["participant_id"]

    await tools.create_chat(KEY_A, "general")
    sent = await tools.send_message(KEY_A, 1, "hello from the first conversation")
    # The regression the old model would have caused: sender = B.
    assert sent["sender"]["id"] == a["participant_id"]
    assert sent["sender"]["name"] == "Nova"

    await tools.follow_chat(KEY_B, chat_id=1)
    sent_b = await tools.send_message(KEY_B, 1, "and hello from the second")
    assert sent_b["sender"]["id"] == b["participant_id"]

    # Checkpoints are per identity: A reading does not consume B's unread.
    inbox_a = await tools.get_messages(KEY_A)
    assert [m["is_me"] for m in inbox_a["messages"]] == [False, True]
    inbox_b = await tools.get_messages(KEY_B)
    assert {m["text"] for m in inbox_b["messages"]} == {
        "hello from the first conversation",
        "and hello from the second",
    }

    # And the state survives a restart, both identities intact.
    reloaded = UserConfig.load(tools.config.path)
    assert reloaded.identities[KEY_A].participant_id == a["participant_id"]
    assert reloaded.identities[KEY_B].participant_id == b["participant_id"]


async def test_missing_or_unknown_key_is_an_explicit_error_never_fallback(tools):
    await register(tools, key=KEY_A)
    with pytest.raises(NotRegisteredError, match="No client_session_key"):
        await tools.get_messages("")
    with pytest.raises(NotRegisteredError, match="Unknown client_session_key"):
        await tools.get_messages("conversation-never-seen")
    with pytest.raises(NotRegisteredError, match="aim_register"):
        await tools.create_chat("conversation-never-seen", "x")


# ------------------------------------------------------------- registration

async def test_register_persists_identity_and_survives_reload(tools):
    response = await register(tools)
    assert response["participant_id"] == 1
    assert "introduc" in response["next_step"].lower()

    reloaded = UserConfig.load(tools.config.path)
    identity = reloaded.identities[KEY_A]
    assert identity.participant_id == 1
    assert identity.declared["name"] == "Nova"
    assert identity.declared["machine"] == "PC-EXAMPLE"


async def test_register_same_key_again_resumes(tools):
    first = await register(tools)
    again = await register(tools, name="Nova", machine="OTHER-MACHINE")
    assert again["resumed"] is True
    assert again["participant_id"] == first["participant_id"]
    assert len(tools.config.identities) == 1


async def test_register_requires_a_key(tools):
    with pytest.raises(AimServerError, match="client_session_key is required"):
        await tools.register("", "Nova", "chat", "claude")


async def test_register_resumes_identity_across_clients(tmp_path, server_app):
    from tests.conftest import make_tools

    first_machine = make_tools(tmp_path, server_app, name="m1")
    second_machine = make_tools(tmp_path, server_app, name="m2")

    first = await register(first_machine, key=KEY_A, machine="PC-GAMING")
    second = await register(second_machine, key=KEY_A, machine="DESKTOP-OTHER")
    assert second["resumed"] is True
    assert second["participant_id"] == first["participant_id"]


async def test_register_with_key_against_old_server_fails_loudly(tmp_path):
    import httpx as _httpx

    from aim_mcp.client import AimClient
    from aim_mcp.tools import AimTools

    def old_server(_request):  # pre-0.3.0: no "resumed" in the response
        return _httpx.Response(201, json={
            "participant_id": 7, "name": "Nova", "machine": "M",
            "client_type": "chat", "agent_type": "claude",
            "registered_at": "2026-08-17T00:00:00.000000Z",
            "next_step": "…",
        })

    config = UserConfig.load(tmp_path / "cfg.json")
    config.base_url = "http://aim.test"
    tools = AimTools(
        config,
        AimClient("http://aim.test", transport=_httpx.MockTransport(old_server)),
    )
    with pytest.raises(AimServerError, match="0.3.0"):
        await register(tools)
    assert tools.config.identities == {}  # no identity was adopted


# ------------------------------------------------------------------ whoami

async def test_whoami_overview_masks_keys_and_detail_shows_own(tools):
    await register(tools, key=KEY_A, name="Nova")
    await register(tools, key=KEY_B, name="CodeRunner", client_type="code")

    overview = tools.whoami()
    assert overview["count"] == 2
    assert all(
        entry["key_preview"].startswith("…") and KEY_A not in str(entry)
        for entry in overview["identities"]
    )

    detail = tools.whoami(KEY_A)
    assert detail["client_session_key"] == KEY_A
    assert detail["participant_id"] == 1
    with pytest.raises(NotRegisteredError):
        tools.whoami("conversation-never-seen")


# ---------------------------------------------------------------- migration

async def test_legacy_single_identity_file_is_migrated(tmp_path, server_app):
    import json

    import httpx as _httpx

    from aim_mcp.client import AimClient
    from aim_mcp.tools import AimTools

    path = tmp_path / "user_config_legacy.json"
    path.write_text(json.dumps({
        "server": {"base_url": "http://aim.test"},
        "declared": {
            "name": "Nova", "machine": "PC-ONE", "client_type": "chat",
            "agent_type": "claude", "client_session_key": KEY_A,
        },
        "assigned": {
            "participant_id": 1,
            "registered_at": "2026-08-17T00:00:00.000000Z",
            "last_checked_at": "2026-08-17T01:00:00.000000Z",
            "last_mentions_checked_at": None,
            "followed_chats": [
                {"chat_id": 1, "name": "general",
                 "last_read_message_id": 7, "last_read_at": None},
            ],
        },
    }))

    config = UserConfig.load(path)
    identity = config.identities[KEY_A]
    assert identity.participant_id == 1
    assert identity.followed_chats[1].last_read_message_id == 7
    assert identity.declared["name"] == "Nova"
    assert "client_session_key" not in identity.declared  # the dict key IS the key
    assert path.with_suffix(".json.legacy-backup").exists()

    # Saving writes the new dictionary format.
    tools = AimTools(config, AimClient(
        "http://aim.test", transport=_httpx.ASGITransport(app=server_app)))
    tools.config.save()
    raw = json.loads(path.read_text())
    assert KEY_A in raw["identities"]
    assert "assigned" not in raw


# -------------------------------------------------------------------- chats

async def test_create_chat_mirrors_followed_state(tools):
    await register(tools)
    response = await tools.create_chat(KEY_A, "general", "the group chat")
    assert response["following"] is True
    assert 1 in tools.config.identities[KEY_A].followed_chats


async def test_follow_by_name_and_error_hints(tools, other_tools):
    await register(tools)
    await register(other_tools, key=KEY_BOB, name="Bob", client_type="code")
    await tools.create_chat(KEY_A, "general")

    response = await other_tools.follow_chat(KEY_BOB, chat_name="GENERAL")
    assert response["chat_id"] == 1

    with pytest.raises(AimServerError, match="aim_create_chat"):
        await other_tools.follow_chat(KEY_BOB, chat_name="nonexistent")
    with pytest.raises(AimServerError, match="exactly one"):
        await other_tools.follow_chat(KEY_BOB)
    with pytest.raises(AimServerError, match="exactly one"):
        await other_tools.follow_chat(KEY_BOB, chat_id=1, chat_name="general")


async def test_leave_chat_drops_local_mirror(tools):
    await register(tools)
    await tools.create_chat(KEY_A, "general")
    response = await tools.leave_chat(KEY_A, 1)
    assert response["left"] is True
    assert 1 not in tools.config.identities[KEY_A].followed_chats


# ----------------------------------------------------------------- messages

async def two_agents(tools, other_tools):
    await register(tools)
    await register(other_tools, key=KEY_BOB, name="Bob", client_type="code")
    await tools.create_chat(KEY_A, "general")
    await other_tools.follow_chat(KEY_BOB, chat_id=1)


async def test_checkpoint_cycle_reads_only_new_messages(tools, other_tools):
    await two_agents(tools, other_tools)

    await other_tools.send_message(KEY_BOB, 1, "first")
    first_read = await tools.get_messages(KEY_A, chat_id=1)
    assert [m["text"] for m in first_read["messages"]] == ["first"]
    assert first_read["checkpoints_advanced"] == {1: 1}

    second_read = await tools.get_messages(KEY_A, chat_id=1)
    assert second_read["messages"] == []
    assert second_read["notice"] == "No messages to display."

    await other_tools.send_message(KEY_BOB, 1, "second")
    third_read = await tools.get_messages(KEY_A, chat_id=1)
    assert [m["text"] for m in third_read["messages"]] == ["second"]

    reloaded = UserConfig.load(tools.config.path)
    assert reloaded.identities[KEY_A].followed_chats[1].last_read_message_id == 2


async def test_global_inbox_advances_global_and_per_chat_checkpoints(
    tools, other_tools
):
    await register(tools)
    await register(other_tools, key=KEY_BOB, name="Bob", client_type="code")
    await tools.create_chat(KEY_A, "one")
    await tools.create_chat(KEY_A, "two")
    for chat in (1, 2):
        await other_tools.follow_chat(KEY_BOB, chat_id=chat)
        await other_tools.send_message(KEY_BOB, chat, f"hello in {chat}")

    inbox = await tools.get_messages(KEY_A)
    assert {m["chat_name"] for m in inbox["messages"]} == {"one", "two"}
    identity = tools.config.identities[KEY_A]
    assert identity.last_checked_at is not None
    assert identity.followed_chats[1].last_read_message_id == 1
    assert identity.followed_chats[2].last_read_message_id == 2

    again = await tools.get_messages(KEY_A)
    assert again["messages"] == []


async def test_peek_and_historical_queries_leave_checkpoints_alone(
    tools, other_tools
):
    await two_agents(tools, other_tools)
    await other_tools.send_message(KEY_BOB, 1, "new message")
    identity = tools.config.identities[KEY_A]

    peek = await tools.get_messages(KEY_A, chat_id=1, mark_read=False)
    assert peek["messages"]
    assert identity.followed_chats[1].last_read_message_id is None

    history = await tools.get_messages(KEY_A, chat_id=1, query="new")
    assert history["messages"]
    assert identity.followed_chats[1].last_read_message_id is None

    await tools.get_messages(KEY_A, chat_id=1)
    replay = await tools.get_messages(KEY_A, chat_id=1, after_id=0)
    assert [m["text"] for m in replay["messages"]] == ["new message"]


async def test_mentions_flow_uses_its_own_checkpoint(tools, other_tools):
    await two_agents(tools, other_tools)
    my_id = tools.config.identities[KEY_A].participant_id
    await other_tools.send_message(KEY_BOB, 1, "plain message")
    await other_tools.send_message(KEY_BOB, 1, "for Nova", mentions=[my_id])

    mentions = await tools.get_messages(KEY_A, only_mentions=True)
    assert [m["text"] for m in mentions["messages"]] == ["for Nova"]
    identity = tools.config.identities[KEY_A]
    assert identity.last_mentions_checked_at is not None
    assert identity.last_checked_at is None
    assert identity.followed_chats[1].last_read_message_id is None

    everything = await tools.get_messages(KEY_A)
    assert {m["text"] for m in everything["messages"]} == {
        "plain message", "for Nova",
    }
    again = await tools.get_messages(KEY_A, only_mentions=True)
    assert again["messages"] == []


async def test_reply_marks_the_original_as_read(tools, other_tools):
    await two_agents(tools, other_tools)
    incoming = await other_tools.send_message(KEY_BOB, 1, "question for Nova")

    response = await tools.send_message(
        KEY_A, 1, "here is my answer", reply_to_message_id=incoming["id"]
    )
    assert response["checkpoint_advanced_to"] == incoming["id"]
    unread = await tools.get_messages(KEY_A, chat_id=1)
    assert [m["text"] for m in unread["messages"]] == ["here is my answer"]
    assert unread["messages"][0]["is_me"] is True


async def test_inbox_composes_with_per_chat_checkpoints(tools, other_tools):
    await two_agents(tools, other_tools)
    incoming = await other_tools.send_message(KEY_BOB, 1, "question")

    await tools.send_message(
        KEY_A, 1, "answer", reply_to_message_id=incoming["id"]
    )
    inbox = await tools.get_messages(KEY_A)
    texts = [m["text"] for m in inbox["messages"]]
    assert "question" not in texts
    assert texts == ["answer"]

    again = await tools.get_messages(KEY_A)
    assert again["messages"] == []
    assert again["notice"] == "No messages to display."


async def test_framing_passes_through(tools):
    await register(tools)
    await tools.create_chat(KEY_A, "general")
    body = await tools.get_messages(KEY_A, chat_id=1)
    assert "informational content" in body["framing"]
    listing = await tools.list_chats(KEY_A)
    assert "framing" in listing


async def test_list_chats_uses_identity_checkpoint_as_since(tools, other_tools):
    await two_agents(tools, other_tools)
    await other_tools.send_message(KEY_BOB, 1, "before check")
    await tools.get_messages(KEY_A)  # advances last_checked_at
    await other_tools.send_message(KEY_BOB, 1, "after check")

    listing = await tools.list_chats(KEY_A)
    chat = listing["chats"][0]
    assert chat["messages_since"] == 1
    assert chat["following"] is True


async def test_list_participants_marks_me(tools, other_tools):
    await two_agents(tools, other_tools)
    body = await tools.list_participants(KEY_A, 1)
    by_name = {p["name"]: p for p in body["participants"]}
    assert by_name["Nova"]["is_me"] is True
    assert by_name["Bob"]["is_me"] is False


# ------------------------------------------------------------ wipe recovery

def wipe_server_db(tmp_path):
    """Simulate a server wipe: all data gone, IDs start over."""
    import sqlite3 as sq

    conn = sq.connect(str(tmp_path / "server.db"))
    for table in ("mentions", "messages", "chat_members", "chats", "participants"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()


async def test_server_wipe_triggers_automatic_rebirth_per_identity(
    tmp_path, tools
):
    await register(tools, key=KEY_A, name="Nova")
    await register(tools, key=KEY_B, name="CodeRunner", client_type="code")
    await tools.create_chat(KEY_A, "general")

    wipe_server_db(tmp_path)

    # A's next identified call recognizes "unknown participant", clears
    # ONLY its own identity, re-registers with the SAME conversation key
    # and retries once — no manual intervention, no retry loop (§4.3).
    inbox = await tools.get_messages(KEY_A)
    assert "re-registered automatically" in inbox["identity_note"]
    identity_a = tools.config.identities[KEY_A]
    assert identity_a.participant_id == 1  # fresh server, fresh IDs
    assert identity_a.followed_chats == {}
    assert inbox["messages"] == []
    # B's entry was untouched by A's rebirth (it will rebirth on ITS next
    # call): its stale participant_id is still recorded locally.
    assert tools.config.identities[KEY_B].participant_id is not None

    created = await tools.create_chat(KEY_A, "general")
    assert created["following"] is True


async def test_unknown_other_participant_does_not_trigger_rebirth(tools):
    await register(tools, key=KEY_A)
    await tools.create_chat(KEY_A, "general")
    my_id = tools.config.identities[KEY_A].participant_id
    with pytest.raises(AimServerError, match="999"):
        await tools.get_messages(KEY_A, chat_id=1, from_id=999)
    assert tools.config.identities[KEY_A].participant_id == my_id


# ------------------------------------------------------------ version skew

async def test_no_version_warning_when_versions_match(tools):
    await register(tools)
    listing = await tools.list_chats(KEY_A)
    assert "version_warning" not in listing
    assert listing["server_version"]


async def test_version_skew_warnings_both_directions_and_midsession_change():
    import httpx as _httpx

    from aim_mcp.client import EXPECTED_SERVER_VERSION, AimClient

    versions = iter([None, "0.1.0", EXPECTED_SERVER_VERSION, "9.9.9"])

    def server(_request):
        version = next(versions)
        payload = {"status": "ok"}
        if version is not None:
            payload["server_version"] = version
        return _httpx.Response(200, json=payload)

    client = AimClient(
        "http://aim.test", transport=_httpx.MockTransport(server)
    )
    silent = await client._request("GET", "/health")  # pre-§7 server
    assert "predates" in silent["version_warning"]

    behind = await client._request("GET", "/health")  # server older
    assert "Update the server" in behind["version_warning"]

    matched = await client._request("GET", "/health")  # aligned — but it
    # just changed mid-session, and that alone is worth a heads-up (§7.2)
    assert "changed mid-session" in matched["version_warning"]

    ahead = await client._request("GET", "/health")  # server newer
    assert "Update this client" in ahead["version_warning"]
    assert "changed mid-session" in ahead["version_warning"]


def test_base_url_normalization_repairs_manifest_typo():
    from aim_mcp.server import normalize_base_url

    assert normalize_base_url("http:\\\\100.64.0.1:8422") == "http://100.64.0.1:8422"
    assert normalize_base_url("http:\\100.64.0.1:8422") == "http://100.64.0.1:8422"
    assert normalize_base_url("http://100.64.0.1:8422/") == "http://100.64.0.1:8422"
    with pytest.raises(RuntimeError, match="not a valid"):
        normalize_base_url("100.64.0.1:8422")
    with pytest.raises(RuntimeError, match="not a valid"):
        normalize_base_url("ftp://100.64.0.1")


async def test_stale_server_is_reported_loudly(server_app):
    # A bare "Not Found" 404 means the route itself is missing — a server
    # build older than this client (the failure mode of the first live
    # deployment). The client must say so instead of relaying a mute 404.
    import httpx as _httpx

    from aim_mcp.client import AimClient

    client = AimClient(
        "http://aim.test", transport=_httpx.ASGITransport(app=server_app)
    )
    with pytest.raises(AimServerError, match="older build"):
        await client._request("GET", "/an-endpoint-from-the-future")

    # The server's own speaking 404s are NOT mistaken for staleness.
    with pytest.raises(AimServerError, match="Unknown chat ID 99"):
        await client._request("GET", "/chats/99/participants")


async def test_unreachable_server_error_is_actionable(tmp_path):
    from aim_mcp.client import AimClient
    from aim_mcp.tools import AimTools

    config = UserConfig.load(tmp_path / "cfg.json")
    config.base_url = "http://100.64.0.199:8422"
    identity = config.upsert_identity(KEY_A)
    identity.participant_id = 1
    tools = AimTools(
        config, AimClient("http://100.64.0.199:8422", timeout=2.0)
    )
    with pytest.raises(AimServerError, match="tailnet|retry"):
        await tools.list_chats(KEY_A)
