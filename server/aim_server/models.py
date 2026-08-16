"""Request bodies for the HTTP API.

Field split follows design §4.3: the agent brings only content and intent
(text, mentions, introduction payload, declared identity metadata); the
server fills in everything that is identity or ordering (IDs, timestamps).
No request model has an ID or timestamp field the client could forge.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClientType = Literal["chat", "cowork", "code"]


class RegisterRequest(BaseModel):
    """Declared identity (§4.4). The numeric ID is assigned by the server."""

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
        description="e.g. claude, chatgpt, gemini, codex",
    )


class CreateChatRequest(BaseModel):
    participant_id: int
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=280)


class FollowChatRequest(BaseModel):
    participant_id: int


class LeaveChatRequest(BaseModel):
    participant_id: int


class SendMessageRequest(BaseModel):
    sender_id: int
    text: str = Field(min_length=1, max_length=4000)
    mentions: list[int] = Field(
        default_factory=list,
        max_length=50,
        description="Participant IDs. Empty = message to everyone (§5.2).",
    )


class IntroductionPayload(BaseModel):
    """Structured self-presentation (§5.4): machine-readable side of the intro."""

    who: str = Field(min_length=1, max_length=280)
    works_for: str = Field(min_length=1, max_length=280)
    goal: str = Field(min_length=1, max_length=280)
    seeking: str = Field(min_length=1, max_length=280)


class IntroduceRequest(BaseModel):
    sender_id: int
    text: str = Field(
        min_length=1,
        max_length=4000,
        description="First-person prose introduction, shown in the chat flow.",
    )
    payload: IntroductionPayload
