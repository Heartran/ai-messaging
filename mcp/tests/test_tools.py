"""End-to-end tests of the MCP tool logic against the real server."""

import pytest

from aim_mcp.client import AimServerError
from aim_mcp.user_config import NotRegisteredError, UserConfig

pytestmark = pytest.mark.anyio


async def register(tools, name="Nova", client_type="chat", agent_type="claude"):
    return await tools.register(
        name, client_type, agent_type, machine="PC-EXAMPLE"
    )


# ------------------------------------------------------------- registration

async def test_register_persists_identity_and_survives_reload(tools):
    response = await register(tools)
    assert response["participant_id"] == 1
    assert "introduc" in response["next_step"].lower()

    # The state survives a client restart (§3.2: re-read at every start).
    reloaded = UserConfig.load(tools.config.path)
    assert reloaded.participant_id == 1
    assert reloaded.declared["name"] == "Nova"
    assert reloaded.declared["machine"] == "PC-EXAMPLE"


async def test_register_twice_is_refused_client_side(tools):
    await register(tools)
    again = await tools.register("Other", "code", "claude", machine="X")
    assert again["already_registered"] is True
    assert again["participant_id"] == 1
    # No second registration reached the server: whoami still shows Nova.
    assert tools.whoami()["declared"]["name"] == "Nova"


async def test_tools_require_registration(tools):
    with pytest.raises(NotRegisteredError, match="aim_register"):
        await tools.create_chat("general")
    with pytest.raises(NotRegisteredError):
        await tools.get_messages()


async def test_whoami_before_registration(tools):
    state = tools.whoami()
    assert state["registered"] is False
    assert state["followed_chats"] == []


# -------------------------------------------------------------------- chats

async def test_create_chat_mirrors_followed_state(tools):
    await register(tools)
    response = await tools.create_chat("general", "the group chat")
    assert response["following"] is True
    assert 1 in tools.config.followed_chats
    reloaded = UserConfig.load(tools.config.path)
    assert reloaded.followed_chats[1].name == "general"


async def test_follow_by_name_and_error_hints(tools, other_tools):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")

    response = await other_tools.follow_chat(chat_name="GENERAL")
    assert response["chat_id"] == 1
    assert other_tools.config.followed_chats[1].name == "general"

    with pytest.raises(AimServerError, match="aim_create_chat"):
        await other_tools.follow_chat(chat_name="nonexistent")
    with pytest.raises(AimServerError, match="exactly one"):
        await other_tools.follow_chat()
    with pytest.raises(AimServerError, match="exactly one"):
        await other_tools.follow_chat(chat_id=1, chat_name="general")


async def test_leave_chat_drops_local_mirror(tools):
    await register(tools)
    await tools.create_chat("general")
    response = await tools.leave_chat(1)
    assert response["left"] is True
    assert 1 not in tools.config.followed_chats


# ----------------------------------------------------------------- messages

async def test_checkpoint_cycle_reads_only_new_messages(tools, other_tools):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)

    await other_tools.send_message(1, "first")
    first_read = await tools.get_messages(chat_id=1)
    assert [m["text"] for m in first_read["messages"]] == ["first"]
    assert first_read["checkpoints_advanced"] == {1: 1}

    # Nothing new → explicit sentinel, checkpoint untouched.
    second_read = await tools.get_messages(chat_id=1)
    assert second_read["messages"] == []
    assert second_read["notice"] == "No messages to display."

    await other_tools.send_message(1, "second")
    third_read = await tools.get_messages(chat_id=1)
    assert [m["text"] for m in third_read["messages"]] == ["second"]

    # The checkpoint survives a restart.
    reloaded = UserConfig.load(tools.config.path)
    assert reloaded.followed_chats[1].last_read_message_id == 2


async def test_global_inbox_advances_global_and_per_chat_checkpoints(
    tools, other_tools
):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("one")
    await tools.create_chat("two")
    for chat in (1, 2):
        await other_tools.follow_chat(chat_id=chat)
        await other_tools.send_message(chat, f"hello in {chat}")

    inbox = await tools.get_messages()
    assert {m["chat_name"] for m in inbox["messages"]} == {"one", "two"}
    assert all(m["is_me"] is False for m in inbox["messages"])
    assert tools.config.last_checked_at is not None
    assert tools.config.followed_chats[1].last_read_message_id == 1
    assert tools.config.followed_chats[2].last_read_message_id == 2

    # The follow-up inbox call sees nothing new.
    again = await tools.get_messages()
    assert again["messages"] == []


async def test_peek_and_historical_queries_leave_checkpoints_alone(
    tools, other_tools
):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)
    await other_tools.send_message(1, "new message")

    peek = await tools.get_messages(chat_id=1, mark_read=False)
    assert peek["messages"]
    assert tools.config.followed_chats[1].last_read_message_id is None

    history = await tools.get_messages(chat_id=1, query="new")
    assert history["messages"]
    assert tools.config.followed_chats[1].last_read_message_id is None

    # After a real read the message is consumed...
    await tools.get_messages(chat_id=1)
    # ...but an explicit cursor can always re-read history.
    replay = await tools.get_messages(chat_id=1, after_id=0)
    assert [m["text"] for m in replay["messages"]] == ["new message"]


async def test_mentions_flow_uses_its_own_checkpoint(tools, other_tools):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)
    await other_tools.send_message(1, "plain message")
    await other_tools.send_message(
        1, "for Nova", mentions=[tools.config.participant_id]
    )

    mentions = await tools.get_messages(only_mentions=True)
    assert [m["text"] for m in mentions["messages"]] == ["for Nova"]
    assert tools.config.last_mentions_checked_at is not None
    # The general checkpoints are untouched: the plain message is not lost.
    assert tools.config.last_checked_at is None
    assert tools.config.followed_chats[1].last_read_message_id is None

    everything = await tools.get_messages()
    assert {m["text"] for m in everything["messages"]} == {
        "plain message",
        "for Nova",
    }

    # And the mentions flow itself does not repeat what it already served.
    again = await tools.get_messages(only_mentions=True)
    assert again["messages"] == []


async def test_reply_marks_the_original_as_read(tools, other_tools):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)
    incoming = await other_tools.send_message(1, "question for Nova")

    response = await tools.send_message(
        1, "here is my answer", reply_to_message_id=incoming["id"]
    )
    assert response["checkpoint_advanced_to"] == incoming["id"]
    # The routine check no longer returns the handled message (only the
    # reply itself, which is ours).
    unread = await tools.get_messages(chat_id=1)
    assert [m["text"] for m in unread["messages"]] == ["here is my answer"]
    assert unread["messages"][0]["is_me"] is True


async def test_inbox_composes_with_per_chat_checkpoints(tools, other_tools):
    # Found by the first live rehearsal: a reply-and-archive (or any
    # chat-scoped read) advances the per-chat marker, and the global inbox
    # must not re-serve those messages as new.
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)
    incoming = await other_tools.send_message(1, "question")

    await tools.send_message(
        1, "answer", reply_to_message_id=incoming["id"]
    )  # per-chat marker → incoming handled; global marker still unset

    inbox = await tools.get_messages()
    texts = [m["text"] for m in inbox["messages"]]
    assert "question" not in texts  # handled, not served again
    assert texts == ["answer"]  # own reply, marked is_me
    assert inbox["messages"][0]["is_me"] is True

    # And once seen, the follow-up inbox call is empty with the sentinel.
    again = await tools.get_messages()
    assert again["messages"] == []
    assert again["notice"] == "No messages to display."


async def test_framing_passes_through(tools, other_tools):
    await register(tools)
    await tools.create_chat("general")
    body = await tools.get_messages(chat_id=1)
    assert "informational content" in body["framing"]
    listing = await tools.list_chats()
    assert "framing" in listing


# -------------------------------------------------------- chats listing etc.

async def test_list_chats_uses_global_checkpoint_as_since(tools, other_tools):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)
    await other_tools.send_message(1, "before check")
    await tools.get_messages()  # advances last_checked_at
    await other_tools.send_message(1, "after check")

    listing = await tools.list_chats()
    chat = listing["chats"][0]
    assert chat["messages_since"] == 1  # only "after check"
    assert chat["following"] is True


async def test_list_participants_marks_me(tools, other_tools):
    await register(tools)
    await register(other_tools, name="Bob", client_type="code")
    await tools.create_chat("general")
    await other_tools.follow_chat(chat_id=1)
    body = await tools.list_participants(1)
    by_name = {p["name"]: p for p in body["participants"]}
    assert by_name["Nova"]["is_me"] is True
    assert by_name["Bob"]["is_me"] is False


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
    config.participant_id = 1
    tools = AimTools(
        config, AimClient("http://100.64.0.199:8422", timeout=2.0)
    )
    with pytest.raises(AimServerError, match="tailnet|retry"):
        await tools.list_chats()
