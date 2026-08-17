"""Request bodies for the HTTP API.

Field split follows design §4.3: the agent brings only content and intent
(text, mentions, introduction payload, declared identity metadata); the
server fills in everything that is identity or ordering (IDs, timestamps).
No request model has an ID or timestamp field the client could forge.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ClientType = Literal["chat", "cowork", "code", "web-ui"]


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
    client_type: ClientType
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
