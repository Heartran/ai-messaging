"""End-to-end tests of the HTTP API against a temporary SQLite database."""

import re

import pytest
from fastapi.testclient import TestClient

from aim_server.db import connect, now_utc, purge_old_messages
from aim_server.main import EMPTY_NOTICE, FRAMING, create_app


class AuthenticatingClient(TestClient):
    """A TestClient that presents each caller's token automatically.

    Identified calls now need the token issued at registration (§4.8).
    Threading it through every behavioural test would bury what those
    tests are actually about, so this client remembers the token handed
    out by /register and attaches it for whichever participant a request
    identifies itself as. Tests that are about authentication itself use
    the `raw` fixture below and pass headers explicitly.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokens: dict[int, str] = {}

    @staticmethod
    def _caller_id(kwargs) -> int | None:
        body = kwargs.get("json") or {}
        if isinstance(body, dict):
            for field in ("participant_id", "sender_id"):
                if isinstance(body.get(field), int):
                    return body[field]
        return None

    def request(self, method, url, **kwargs):  # noqa: D102 - see class docstring
        caller = self._caller_id(kwargs)
        if caller is None:
            params = kwargs.get("params") or {}
            if isinstance(params, dict) and "participant_id" in params:
                caller = int(params["participant_id"])
        if caller is None:
            match = re.search(r"[?&]participant_id=(\d+)", str(url))
            if match:
                caller = int(match.group(1))
        token = self.tokens.get(caller) if caller is not None else None
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-AIM-Token", token)
            kwargs["headers"] = headers
        response = super().request(method, url, **kwargs)
        if str(url).endswith("/register") and response.status_code == 201:
            body = response.json()
            self.tokens[body["participant_id"]] = body["participant_token"]
        return response


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with AuthenticatingClient(app) as test_client:
        yield test_client


@pytest.fixture()
def raw(tmp_path):
    """A client that presents nothing: for testing the auth boundary itself."""
    app = create_app(str(tmp_path / "raw.db"))
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
    a = register(client, name="Nova", machine="PC-ONE", client_type="chat")
    b = register(client, name="Code", machine="PC-ONE", client_type="code")
    assert a["participant_id"] != b["participant_id"]


def test_client_type_is_free_text(client):
    """§4.6: the four conventional values stopped describing the products
    — the same agent registered once as 'code' and once as 'chat' because
    neither fitted. Nothing branches on this field, so it is provenance to
    read, not a vocabulary to police."""
    response = client.post(
        "/register",
        json={
            "name": "Nova",
            "machine": "OMEN",
            "client_type": "openclaw",
            "agent_type": "claude",
        },
    )
    assert response.status_code == 201
    assert response.json()["client_type"] == "openclaw"


def test_client_type_still_has_to_be_a_short_string(client):
    """Free text is not "anything": the field is still bounded, so it
    cannot become a place to stash a payload."""
    for bad in ("", "x" * 33):
        response = client.post(
            "/register",
            json={"name": "X", "machine": "M", "client_type": bad,
                  "agent_type": "claude"},
        )
        assert response.status_code == 422, bad


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


# ------------------------------------------------------------ identity proof

def test_identified_call_without_a_token_is_refused(raw):
    """The regression that cost us a real impersonation (§4.8).

    Participant IDs are printed in every participants listing. Before
    tokens, knowing one was enough to speak as its owner.
    """
    me = raw.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude"}).json()
    pid, token = me["participant_id"], me["participant_token"]
    chat_id = raw.post(
        "/chats", json={"participant_id": pid, "name": "general"},
        headers={"X-AIM-Token": token},
    ).json()["chat_id"]

    naked = raw.post(f"/chats/{chat_id}/messages",
                     json={"sender_id": pid, "text": "who am I"})
    assert naked.status_code == 401
    assert naked.json()["detail"]["code"] == "token_missing"

    forged = raw.post(f"/chats/{chat_id}/messages",
                      json={"sender_id": pid, "text": "who am I"},
                      headers={"X-AIM-Token": "not-the-token"})
    assert forged.status_code == 401
    assert forged.json()["detail"]["code"] == "token_invalid"

    # Nothing was written by either attempt.
    body = raw.get(f"/chats/{chat_id}/messages").json()
    assert body["count"] == 0


def test_a_public_participant_id_cannot_be_borrowed(raw):
    """The actual incident: one participant speaking as another.

    The victim's ID is not secret — the attacker reads it straight off
    the participants listing — so the listing itself is the proof that an
    ID can never be the credential.
    """
    victim = raw.post("/register", json={
        "name": "Federico", "machine": "web-ui", "client_type": "web-ui",
        "agent_type": "human"}).json()
    intruder = raw.post("/register", json={
        "name": "Stranger", "machine": "OTHER", "client_type": "code",
        "agent_type": "gemini"}).json()

    chat_id = raw.post(
        "/chats", json={"participant_id": victim["participant_id"], "name": "lobby"},
        headers={"X-AIM-Token": victim["participant_token"]},
    ).json()["chat_id"]

    # The intruder learns the victim's ID from a public, unauthenticated call.
    listing = raw.get(f"/chats/{chat_id}/participants").json()
    stolen_id = next(p["id"] for p in listing["participants"]
                     if p["name"] == "Federico")
    assert stolen_id == victim["participant_id"]

    # Its own token proves its own identity — and nobody else's.
    response = raw.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": stolen_id, "text": "I am Federico, trust me"},
        headers={"X-AIM-Token": intruder["participant_token"]},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "token_invalid"
    assert raw.get(f"/chats/{chat_id}/messages").json()["count"] == 0


def test_identified_reads_need_the_token_too(raw):
    """The inbox is identified: it answers "what awaits ME"."""
    me = raw.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude"}).json()
    pid = me["participant_id"]
    assert raw.get(f"/messages?participant_id={pid}").status_code == 401
    ok = raw.get(f"/messages?participant_id={pid}",
                 headers={"X-AIM-Token": me["participant_token"]})
    assert ok.status_code == 200


def test_resuming_an_identity_revokes_the_previous_token(raw):
    """Only the hash is stored, so a resumed identity gets a fresh token
    and the old one stops working — a stale client cannot keep writing."""
    body = {"name": "Nova", "machine": "M", "client_type": "chat",
            "agent_type": "claude", "client_session_key": "conversation-xyz"}
    first = raw.post("/register", json=body).json()
    second = raw.post("/register", json=body).json()
    assert second["participant_id"] == first["participant_id"]
    assert second["participant_token"] != first["participant_token"]

    chat_id = raw.post(
        "/chats", json={"participant_id": second["participant_id"], "name": "g"},
        headers={"X-AIM-Token": second["participant_token"]},
    ).json()["chat_id"]
    stale = raw.post(f"/chats/{chat_id}/messages",
                     json={"sender_id": first["participant_id"], "text": "still me?"},
                     headers={"X-AIM-Token": first["participant_token"]})
    assert stale.status_code == 401
    assert stale.json()["detail"]["code"] == "token_invalid"


def test_participant_predating_tokens_must_register_again(raw, tmp_path):
    """Rows migrated from v1 have no token: they cannot act until they
    register again, which is the point — before the column existed, their
    bare ID was accepted from anyone."""
    me = raw.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude"}).json()
    chat_id = raw.post(
        "/chats", json={"participant_id": me["participant_id"], "name": "g"},
        headers={"X-AIM-Token": me["participant_token"]},
    ).json()["chat_id"]

    conn = connect(str(tmp_path / "raw.db"))
    try:
        conn.execute("UPDATE participants SET token_hash = NULL WHERE id = ?",
                     (me["participant_id"],))
        conn.commit()
    finally:
        conn.close()

    response = raw.post(f"/chats/{chat_id}/messages",
                        json={"sender_id": me["participant_id"], "text": "hi"},
                        headers={"X-AIM-Token": me["participant_token"]})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "token_required"


def test_token_is_never_echoed_back(raw):
    """The token appears exactly once, in its own registration response."""
    me = raw.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude"}).json()
    token = me["participant_token"]
    chat_id = raw.post(
        "/chats", json={"participant_id": me["participant_id"], "name": "g"},
        headers={"X-AIM-Token": token},
    ).json()["chat_id"]
    raw.post(f"/chats/{chat_id}/messages",
             json={"sender_id": me["participant_id"], "text": "hello"},
             headers={"X-AIM-Token": token})
    for path in (f"/chats/{chat_id}/participants",
                 f"/chats/{chat_id}/messages",
                 "/chats", "/health"):
        assert token not in raw.get(path).text, path


# ---------------------------------------------------- operator corrections

OPKEY = "operator-key-long-enough"


@pytest.fixture()
def admin(tmp_path):
    """A server with hand-editing switched on."""
    app = create_app(str(tmp_path / "admin.db"), operator_key=OPKEY)
    with AuthenticatingClient(app) as test_client:
        yield test_client


def op(extra=None):
    headers = {"X-AIM-Operator": OPKEY}
    headers.update(extra or {})
    return headers


def test_editing_is_off_unless_a_key_is_configured(client):
    """No key configured → the endpoints are not merely unguarded, they
    are absent. A server without an operator has no editing surface."""
    pid = register(client)["participant_id"]
    response = client.patch(f"/admin/participants/{pid}", json={"name": "X"},
                            headers={"X-AIM-Operator": "anything"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "editing_disabled"
    assert client.get("/health").json()["editing_enabled"] is False


def test_a_participant_token_does_not_authorize_editing(admin):
    """The regression that matters: proving you are #1 must never imply
    you may rewrite #2 — that was yesterday's whole lesson."""
    me = admin.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude"}).json()
    victim = admin.post("/register", json={
        "name": "Federico", "machine": "web-ui", "client_type": "web-ui",
        "agent_type": "human"}).json()

    naked = admin.patch(f"/admin/participants/{victim['participant_id']}",
                        json={"name": "Impostor"})
    assert naked.status_code == 403
    assert naked.json()["detail"]["code"] == "not_operator"

    with_token = admin.patch(
        f"/admin/participants/{victim['participant_id']}",
        json={"name": "Impostor"},
        headers={"X-AIM-Token": me["participant_token"]})
    assert with_token.status_code == 403

    wrong_key = admin.patch(f"/admin/participants/{victim['participant_id']}",
                            json={"name": "Impostor"},
                            headers={"X-AIM-Operator": "not-the-key"})
    assert wrong_key.status_code == 403

    still = admin.get("/admin/participants", headers=op()).json()
    assert {p["name"] for p in still["participants"]} == {"Nova", "Federico"}


def test_editing_metadata_reports_exactly_what_moved(admin):
    pid = register(admin, name="Nova", machine="OMEN-Federico")["participant_id"]
    body = admin.patch(f"/admin/participants/{pid}",
                       json={"machine": "OMEN-FEDERICO", "name": "Nova"},
                       headers=op()).json()
    # Only the field that actually differs is reported and written.
    assert body["changed"] == {
        "machine": {"from": "OMEN-Federico", "to": "OMEN-FEDERICO"}}
    assert body["participant"]["machine"] == "OMEN-FEDERICO"
    assert body["participant"]["name"] == "Nova"


def test_editing_nothing_is_not_an_error(admin):
    pid = register(admin, name="Nova")["participant_id"]
    body = admin.patch(f"/admin/participants/{pid}", json={}, headers=op()).json()
    assert body["unchanged"] is True and body["changed"] == {}


def test_the_server_owned_record_is_not_editable(admin):
    """IDs and timestamps are what the server witnessed; they are not
    metadata and cannot be rewritten by anyone."""
    pid = register(admin)["participant_id"]
    for forbidden in ({"id": 99}, {"registered_at": "2000-01-01T00:00:00.000000Z"},
                      {"last_seen_at": None}, {"token_hash": "x"}):
        response = admin.patch(f"/admin/participants/{pid}",
                               json=forbidden, headers=op())
        assert response.status_code == 422, forbidden


def test_client_type_can_be_edited_to_anything_short(admin):
    pid = register(admin)["participant_id"]
    ok = admin.patch(f"/admin/participants/{pid}",
                     json={"client_type": "antigravity"}, headers=op())
    assert ok.status_code == 200
    assert ok.json()["participant"]["client_type"] == "antigravity"
    too_long = admin.patch(f"/admin/participants/{pid}",
                           json={"client_type": "x" * 33}, headers=op())
    assert too_long.status_code == 422


def test_replacing_a_session_key_never_reveals_it(admin):
    """The fix for a client that registered with the wrong identifier —
    an account ID where a conversation ID belonged."""
    wrong = "bae36e77-b092-49eb-8ff5-83360b2e81b4"  # an account, not a chat
    pid = admin.post("/register", json={
        "name": "Nova", "machine": "OMEN", "client_type": "cowork",
        "agent_type": "claude", "client_session_key": wrong}).json()["participant_id"]

    body = admin.patch(f"/admin/participants/{pid}",
                       json={"client_session_key": "conversation-the-right-one"},
                       headers=op()).json()
    assert body["changed"]["client_session_key"] == {
        "from": "(hidden)", "to": "(replaced)"}

    listing = admin.get("/admin/participants", headers=op())
    assert wrong not in listing.text and "the-right-one" not in listing.text
    assert listing.json()["participants"][0]["has_session_key"] is True

    # The new key now resumes that identity; the old one no longer does.
    resumed = admin.post("/register", json={
        "name": "Nova", "machine": "OMEN", "client_type": "cowork",
        "agent_type": "claude",
        "client_session_key": "conversation-the-right-one"}).json()
    assert resumed["resumed"] is True and resumed["participant_id"] == pid


def test_a_session_key_already_taken_is_refused(admin):
    first = admin.post("/register", json={
        "name": "A", "machine": "M", "client_type": "chat",
        "agent_type": "claude", "client_session_key": "conversation-one"}).json()
    second = admin.post("/register", json={
        "name": "B", "machine": "M", "client_type": "chat",
        "agent_type": "claude", "client_session_key": "conversation-two"}).json()
    response = admin.patch(f"/admin/participants/{second['participant_id']}",
                           json={"client_session_key": "conversation-one"},
                           headers=op())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_key_taken"
    assert first["participant_id"] != second["participant_id"]


def test_revoking_a_token_evicts_that_client(admin):
    """Containment: an identity can be forced to register again."""
    me = admin.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude"}).json()
    pid, token = me["participant_id"], me["participant_token"]
    chat_id = admin.post("/chats", json={"participant_id": pid, "name": "g"},
                         headers={"X-AIM-Token": token}).json()["chat_id"]

    body = admin.patch(f"/admin/participants/{pid}",
                       json={"revoke_token": True}, headers=op()).json()
    assert body["changed"]["token"] == {"from": "(issued)", "to": "(revoked)"}

    blocked = admin.post(f"/chats/{chat_id}/messages",
                         json={"sender_id": pid, "text": "still here?"},
                         headers={"X-AIM-Token": token})
    assert blocked.status_code == 401
    assert blocked.json()["detail"]["code"] == "token_required"


def test_merging_the_same_human_across_devices(admin):
    """§11.4: the continuity key is per browser, so one person opening the
    UI on five devices becomes five participants — and the mention picker
    offers the same human twice."""
    laptop = admin.post("/register", json={
        "name": "Federico", "machine": "web-ui", "client_type": "web-ui",
        "agent_type": "human", "client_session_key": "web-ui-laptop"}).json()
    phone = admin.post("/register", json={
        "name": "Federico", "machine": "web-ui", "client_type": "web-ui",
        "agent_type": "human", "client_session_key": "web-ui-phone"}).json()
    agent = register(admin, name="Nova")
    keep, gone = laptop["participant_id"], phone["participant_id"]

    chat_id = admin.post("/chats", json={
        "participant_id": keep, "name": "lobby"}).json()["chat_id"]
    admin.post(f"/chats/{chat_id}/follow", json={"participant_id": gone})
    admin.post(f"/chats/{chat_id}/messages",
               json={"sender_id": keep, "text": "from the laptop"})
    admin.post(f"/chats/{chat_id}/messages",
               json={"sender_id": gone, "text": "from the phone"})
    # An agent mentioned BOTH of him in one message: the merge must not
    # trip over the (message, participant) primary key.
    admin.post(f"/chats/{chat_id}/follow", json={"participant_id": agent["participant_id"]})
    admin.post(f"/chats/{chat_id}/messages", json={
        "sender_id": agent["participant_id"], "text": "which of you is it",
        "mentions": [keep, gone]})

    result = admin.post(f"/admin/participants/{keep}/merge",
                        json={"from_ids": [gone], "confirm_name": "Federico"},
                        headers=op()).json()
    assert result["merged"] == [gone]
    assert result["moved"]["messages"] == 1
    assert result["moved"]["participants_removed"] == 1

    # One human left, and he still says everything he said.
    listing = admin.get(f"/chats/{chat_id}/participants").json()
    humans = [p for p in listing["participants"] if p["name"] == "Federico"]
    assert len(humans) == 1 and humans[0]["id"] == keep
    assert humans[0]["active"] is True

    messages = admin.get(f"/chats/{chat_id}/messages").json()["messages"]
    mine = [m["text"] for m in messages if m["sender"]["id"] == keep]
    assert set(mine) == {"from the laptop", "from the phone"}

    # The double mention collapsed instead of exploding.
    mentioning = next(m for m in messages if m["text"] == "which of you is it")
    assert mentioning["mentions"] == [keep]

    # The merged ID is gone for good and is never handed out again.
    assert admin.get(f"/participants/{gone}/chats").status_code == 404
    fresh = register(admin, name="Somebody")
    assert fresh["participant_id"] > max(keep, gone)


def test_a_stale_client_of_a_merged_identity_is_told_to_re_register(admin):
    phone = admin.post("/register", json={
        "name": "Federico", "machine": "web-ui", "client_type": "web-ui",
        "agent_type": "human", "client_session_key": "web-ui-phone"}).json()
    laptop = admin.post("/register", json={
        "name": "Federico", "machine": "web-ui", "client_type": "web-ui",
        "agent_type": "human", "client_session_key": "web-ui-laptop"}).json()
    chat_id = admin.post("/chats", json={
        "participant_id": laptop["participant_id"], "name": "lobby"}).json()["chat_id"]

    admin.post(f"/admin/participants/{laptop['participant_id']}/merge",
               json={"from_ids": [phone["participant_id"]],
                     "confirm_name": "Federico"}, headers=op())

    stale = admin.post(f"/chats/{chat_id}/messages",
                       json={"sender_id": phone["participant_id"], "text": "hi"},
                       headers={"X-AIM-Token": phone["participant_token"]})
    assert stale.status_code == 404
    assert stale.json()["detail"]["code"] == "unknown_participant"


def test_merge_refuses_a_mistyped_target(admin):
    a = register(admin, name="Federico")["participant_id"]
    b = register(admin, name="Nova")["participant_id"]
    response = admin.post(f"/admin/participants/{a}/merge",
                          json={"from_ids": [b], "confirm_name": "Nova"},
                          headers=op())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirm_name_mismatch"
    # Nothing moved.
    assert admin.get(f"/participants/{b}/chats").status_code == 200


def test_merge_needs_the_operator_key_and_a_real_source(admin):
    a = register(admin, name="Federico")["participant_id"]
    assert admin.post(f"/admin/participants/{a}/merge",
                      json={"from_ids": [999], "confirm_name": "Federico"}
                      ).status_code == 403
    missing = admin.post(f"/admin/participants/{a}/merge",
                         json={"from_ids": [999], "confirm_name": "Federico"},
                         headers=op())
    assert missing.status_code == 404
    itself = admin.post(f"/admin/participants/{a}/merge",
                        json={"from_ids": [a], "confirm_name": "Federico"},
                        headers=op())
    assert itself.status_code == 422
    assert itself.json()["detail"]["code"] == "nothing_to_merge"


def test_merge_keeps_the_earliest_following_and_the_live_membership(admin):
    """Two devices, one left the chat and one did not: the surviving
    membership must say still-following, since the human never left."""
    keep = register(admin, name="Federico")["participant_id"]
    gone = register(admin, name="Federico2")["participant_id"]
    chat_id = admin.post("/chats", json={
        "participant_id": gone, "name": "lobby"}).json()["chat_id"]
    early = admin.get(f"/chats/{chat_id}/participants").json()["participants"][0]["followed_at"]
    admin.post(f"/chats/{chat_id}/follow", json={"participant_id": keep})
    admin.post(f"/chats/{chat_id}/leave", json={"participant_id": keep})

    admin.post(f"/admin/participants/{keep}/merge",
               json={"from_ids": [gone], "confirm_name": "Federico"}, headers=op())

    member = admin.get(f"/chats/{chat_id}/participants").json()["participants"][0]
    assert member["id"] == keep
    assert member["active"] is True          # the other device never left
    assert member["followed_at"] == early    # following since the earlier one
    # The chat it founded now belongs to the surviving identity.
    assert admin.get("/chats").json()["chats"][0]["created_by"] == keep


def test_editing_a_chat_name_and_description(admin):
    pid = register(admin)["participant_id"]
    chat_id = admin.post("/chats", json={
        "participant_id": pid, "name": "typo-chat"}).json()["chat_id"]
    body = admin.patch(f"/admin/chats/{chat_id}",
                       json={"name": "ollama-pc-gaming",
                             "description": "What it is really for"},
                       headers=op()).json()
    assert body["chat"]["name"] == "ollama-pc-gaming"
    assert body["chat"]["description"] == "What it is really for"
    assert admin.get("/chats").json()["chats"][0]["name"] == "ollama-pc-gaming"


def test_renaming_a_chat_onto_an_existing_name_is_refused(admin):
    pid = register(admin)["participant_id"]
    admin.post("/chats", json={"participant_id": pid, "name": "taken"})
    other = admin.post("/chats", json={
        "participant_id": pid, "name": "free"}).json()["chat_id"]
    response = admin.patch(f"/admin/chats/{other}",
                           json={"name": "taken"}, headers=op())
    assert response.status_code == 409


def test_the_admin_listing_never_carries_credentials(admin):
    key = "conversation-secret-key-value"
    admin.post("/register", json={
        "name": "Nova", "machine": "M", "client_type": "chat",
        "agent_type": "claude", "client_session_key": key})
    listing = admin.get("/admin/participants", headers=op())
    assert key not in listing.text
    assert "token_hash" not in listing.text
    assert listing.json()["participants"][0]["has_token"] is True


# ------------------------------------------------------- database identity

def test_every_payload_declares_the_database_instance(raw):
    """§4.7: IDs restart from 1 when the database is recreated, so a client
    must be able to tell one database from another."""
    health = raw.get("/health").json()
    assert health["instance_id"]
    assert raw.get("/chats").json()["server_instance"] == health["instance_id"]


def test_a_recreated_database_gets_a_new_instance_id(tmp_path):
    """The guarantee "IDs are never reused" holds inside one database and
    dies with the file. The instance ID is what makes that visible."""
    path = str(tmp_path / "recreated.db")
    with TestClient(create_app(path)) as first:
        before = first.get("/health").json()["instance_id"]
        pid = first.post("/register", json={
            "name": "Nova", "machine": "M", "client_type": "chat",
            "agent_type": "claude"}).json()["participant_id"]
        assert pid == 1

    import os
    os.remove(path)

    with TestClient(create_app(path)) as second:
        after = second.get("/health").json()["instance_id"]
        assert after != before
        # ID 1 is handed out again — to somebody else entirely.
        reissued = second.post("/register", json={
            "name": "Somebody Else", "machine": "OTHER", "client_type": "code",
            "agent_type": "gemini"}).json()
        assert reissued["participant_id"] == pid


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

def test_v2_database_loses_the_client_type_check_and_keeps_everything(tmp_path):
    """v2 → v3 (§4.6). SQLite cannot drop a CHECK, so the table is rebuilt
    — and a rebuild is exactly where rows, IDs and the AUTOINCREMENT
    high-water mark get quietly lost. They must not be."""
    import sqlite3 as sq

    from aim_server.db import init_db

    db_path = str(tmp_path / "v2.db")
    old = sq.connect(db_path)
    old.executescript(
        """
        CREATE TABLE participants (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            machine            TEXT NOT NULL,
            client_type        TEXT NOT NULL
                CHECK (client_type IN ('chat', 'cowork', 'code', 'web-ui')),
            agent_type         TEXT NOT NULL,
            registered_at      TEXT NOT NULL,
            client_session_key TEXT,
            last_seen_at       TEXT,
            token_hash         TEXT
        );
        INSERT INTO participants
            (id, name, machine, client_type, agent_type, registered_at,
             client_session_key, last_seen_at, token_hash)
        VALUES
            (1, 'Nova', 'OMEN', 'chat', 'claude', '2026-08-17T00:00:00.000000Z',
             'conversation-one', '2026-08-20T00:00:00.000000Z', 'abc123'),
            (7, 'Antigravity', 'DESKTOP', 'code', 'gemini',
             '2026-08-26T00:00:00.000000Z', NULL, NULL, NULL);
        DELETE FROM sqlite_sequence WHERE name = 'participants';
        INSERT INTO sqlite_sequence (name, seq) VALUES ('participants', 7);
        PRAGMA user_version = 2;
        """
    )
    old.commit()
    old.close()

    init_db(db_path)

    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM participants ORDER BY id")
        }
        assert set(rows) == {1, 7}
        # Nothing about the identities moved — including the credentials.
        assert rows[1]["name"] == "Nova"
        assert rows[1]["client_session_key"] == "conversation-one"
        assert rows[1]["token_hash"] == "abc123"
        assert rows[1]["last_seen_at"] == "2026-08-20T00:00:00.000000Z"
        assert rows[7]["name"] == "Antigravity"

        # The CHECK is gone, so a value nobody anticipated is storable.
        conn.execute(
            "INSERT INTO participants (name, machine, client_type, agent_type, "
            "registered_at) VALUES ('Cascade', 'M', 'windsurf', 'claude', ?)",
            (now_utc(),),
        )
        conn.commit()
        # And the ID counter survived the rebuild: never reused (§4.2).
        new_id = conn.execute(
            "SELECT id FROM participants WHERE name = 'Cascade'"
        ).fetchone()["id"]
        assert new_id == 8

        # The unique index on the continuity key is back in place.
        with pytest.raises(sq.IntegrityError):
            conn.execute(
                "INSERT INTO participants (name, machine, client_type, "
                "agent_type, registered_at, client_session_key) "
                "VALUES ('Dupe', 'M', 'chat', 'claude', ?, 'conversation-one')",
                (now_utc(),),
            )
    finally:
        conn.close()


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


# ------------------------------------------------------------ delete chat

def test_delete_chat_erases_messages_members_and_frees_the_name(client):
    a = register(client, name="Alice")["participant_id"]
    b = register(client, name="Bob")["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": a, "name": "doomed"}
    ).json()["chat_id"]
    client.post(f"/chats/{chat_id}/follow", json={"participant_id": b})
    client.post(
        f"/chats/{chat_id}/messages",
        json={"sender_id": a, "text": "hello @Bob", "mentions": [b]},
    )
    client.post(
        f"/chats/{chat_id}/messages", json={"sender_id": b, "text": "hi"}
    )

    result = client.request(
        "DELETE",
        f"/chats/{chat_id}",
        json={"participant_id": b, "confirm_name": "doomed"},
    ).json()
    assert result["deleted"] is True
    assert result["deleted_messages"] == 2
    assert result["deleted_members"] == 2
    assert result["deleted_by"] == b

    # Gone from the list, and per-chat endpoints answer 404, not a stub.
    chats = client.get("/chats").json()["chats"]
    assert all(chat["id"] != chat_id for chat in chats)
    assert client.get(f"/chats/{chat_id}/messages").status_code == 404
    assert client.get(f"/chats/{chat_id}/participants").status_code == 404

    # The unique name is released for a brand-new chat (new ID).
    recreated = client.post(
        "/chats", json={"participant_id": a, "name": "doomed"}
    )
    assert recreated.status_code == 201
    assert recreated.json()["chat_id"] != chat_id


def test_delete_chat_requires_the_exact_name_retyped(client):
    pid = register(client)["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": pid, "name": "precious"}
    ).json()["chat_id"]
    response = client.request(
        "DELETE",
        f"/chats/{chat_id}",
        json={"participant_id": pid, "confirm_name": "precios"},
    )
    assert response.status_code == 409
    assert "does not match" in response.json()["detail"]
    # Nothing happened: the chat is still listed.
    chats = client.get("/chats").json()["chats"]
    assert any(chat["id"] == chat_id for chat in chats)

    # Same collation as the unique index (NOCASE): case is not a trap.
    ok = client.request(
        "DELETE",
        f"/chats/{chat_id}",
        json={"participant_id": pid, "confirm_name": "PRECIOUS"},
    )
    assert ok.status_code == 200


def test_delete_chat_requires_registration_and_existing_chat(client):
    pid = register(client)["participant_id"]
    chat_id = client.post(
        "/chats", json={"participant_id": pid, "name": "general"}
    ).json()["chat_id"]
    ghost = client.request(
        "DELETE",
        f"/chats/{chat_id}",
        json={"participant_id": 999, "confirm_name": "general"},
    )
    assert ghost.status_code == 404
    assert ghost.json()["detail"]["code"] == "unknown_participant"
    missing = client.request(
        "DELETE",
        "/chats/999",
        json={"participant_id": pid, "confirm_name": "whatever"},
    )
    assert missing.status_code == 404


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
    from aim_server import __version__

    page = client.get("/ui")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "AI Messaging" in page.text
    # The page is stamped with the serving version (§10.5), so the browser
    # can spot skew against the live server_version.
    assert f'UI_VERSION = "{__version__}"' in page.text
    assert "__AIM_VERSION__" not in page.text

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
