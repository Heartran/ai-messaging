"""Request bodies for the HTTP API.

Field split follows design §4.3: the agent brings only content and intent
(text, mentions, introduction payload, declared identity metadata); the
server fills in everything that is identity or ordering (IDs, timestamps).
No request model has an ID or timestamp field the client could forge.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Free text, not an enum (§4.6). The four conventional values —
# chat, cowork, code, web-ui — stopped describing the products: the same
# agent registered once as "code" and once as "chat" because neither
# fitted. Nothing in the system branches on this field, so a closed
# vocabulary bought a schema migration per new product and nothing else.
# A wrong value is now a typo to correct by hand (§11), not a call to
# reject at the door.
CLIENT_TYPE_DESCRIPTION = (
    "Kind of client session. Conventionally chat, cowork, code or web-ui, "
    "but not restricted to those: use what actually describes this client."
)


class StrictModel(BaseModel):
    """Unknown fields are rejected, never silently ignored (design §7.4).

    A newer client sending a field this server does not know must get an
    explicit error naming it — the silently-dropped parameter was the most
    expensive failure mode of the first deployment.
    """

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictModel):
    """Declared identity (§4.5). The numeric ID is assigned by the server."""

    name: str = Field(min_length=1, max_length=64)
    machine: str = Field(
        min_length=1,
        max_length=64,
        description="Hostname of the machine (descriptive metadata, not a key).",
    )
    client_type: str = Field(
        min_length=1, max_length=32, description=CLIENT_TYPE_DESCRIPTION
    )
    agent_type: str = Field(
        min_length=1,
        max_length=32,
        description="e.g. claude, chatgpt, gemini, codex, human",
    )
    client_session_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=200,
        description="Identifier of the client conversation/session (§4.3): "
        "the identity-continuity key. Same key → same participant ID, from "
        "any machine. Treated as a credential: never echoed back, never "
        "listed.",
    )


class CreateChatRequest(StrictModel):
    participant_id: int
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=280)


class FollowChatRequest(StrictModel):
    participant_id: int


class LeaveChatRequest(StrictModel):
    participant_id: int


class DeleteChatRequest(StrictModel):
    """Permanent deletion (§10.6): the chat name must be retyped.

    Leaving a chat keeps history and reserves the ID; deleting erases the
    chat with all its messages, memberships and mentions. The echoed name
    makes a mistyped chat_id fail loudly instead of destroying the wrong
    chat.
    """

    participant_id: int
    confirm_name: str = Field(
        min_length=1,
        max_length=64,
        description="The chat's exact name, retyped as confirmation.",
    )


class EditParticipantRequest(StrictModel):
    """Operator correction of stored identity metadata (§11).

    Every field is optional: only what is sent is changed, so a form can
    submit one correction without restating the rest. What is NOT here is
    the point — `id` and `registered_at` are the server's own record of
    what happened, `last_seen_at` is observed rather than declared, and
    message text and authorship are never editable at all: a transcript
    that can be rewritten proves nothing about who said what.
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    machine: str | None = Field(default=None, min_length=1, max_length=64)
    client_type: str | None = Field(
        default=None, min_length=1, max_length=32,
        description=CLIENT_TYPE_DESCRIPTION,
    )
    agent_type: str | None = Field(default=None, min_length=1, max_length=32)
    client_session_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=200,
        description="Replace the identity-continuity key (§4.3) — the fix "
        "for a client that registered with the wrong thing (an account ID "
        "instead of a conversation ID, say). Write-only: the current value "
        "is a credential and is never returned.",
    )
    revoke_token: bool = Field(
        default=False,
        description="Invalidate this participant's token (§4.8). Its client "
        "must register again with its key before it can act — the way to "
        "evict a client that is misbehaving or holding a stale identity.",
    )


class MergeParticipantsRequest(StrictModel):
    """Fold duplicate identities of the SAME actor into one (§11.4).

    The web UI mints a continuity key per browser, which is right for an
    agent — one conversation, one identity — and wrong for a person, who
    is one actor across five devices. This is the repair, and it is a
    deliberate exception to the rule that authorship is never rewritten:
    the messages stay attributed to the same human, only the number
    changes. That exception holds only because the identities are the same
    actor, which nothing but a human can judge — hence the operator key
    and the retyped name.
    """

    from_ids: list[int] = Field(
        min_length=1,
        max_length=50,
        description="Participants to fold into this one. They are deleted "
        "afterwards; their IDs are never handed out again (§4.2).",
    )
    confirm_name: str = Field(
        min_length=1,
        max_length=64,
        description="The surviving participant's exact name, retyped. The "
        "same guard the chat deletion uses: a mistyped ID must fail loudly "
        "rather than rewrite the wrong actor's history.",
    )


class EditChatRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=280)


class SendMessageRequest(StrictModel):
    sender_id: int
    text: str = Field(min_length=1, max_length=4000)
    mentions: list[int] = Field(
        default_factory=list,
        max_length=50,
        description="Participant IDs. Empty = message to everyone (§5.2).",
    )


class IntroductionPayload(StrictModel):
    """Structured self-presentation (§5.4): machine-readable side of the intro."""

    who: str = Field(min_length=1, max_length=280)
    works_for: str = Field(min_length=1, max_length=280)
    goal: str = Field(min_length=1, max_length=280)
    seeking: str = Field(min_length=1, max_length=280)


class IntroduceRequest(StrictModel):
    sender_id: int
    text: str = Field(
        min_length=1,
        max_length=4000,
        description="First-person prose introduction, shown in the chat flow.",
    )
    payload: IntroductionPayload
