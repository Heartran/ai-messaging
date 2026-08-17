"""Client-side state: the `user_config` file (design §3.2).

Two distinct blocks, mirroring who owns each piece of data:
- `declared`: what the user stated at registration — editable by hand;
- `assigned`: what the central server assigned or this client accumulated
  (participant ID, followed chats, read checkpoints) — never hand-edited.

Read checkpoints live HERE, per registration, never on the server: the
server stays stateless about who has read what (design §3.1).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "~/.aim/user_config.json"


class NotRegisteredError(Exception):
    """Raised when a tool that needs an identity runs before registration."""


@dataclass
class FollowedChat:
    chat_id: int
    name: str
    last_read_message_id: int | None = None
    last_read_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "name": self.name,
            "last_read_message_id": self.last_read_message_id,
            "last_read_at": self.last_read_at,
        }


@dataclass
class UserConfig:
    path: Path
    base_url: str | None = None
    declared: dict[str, Any] = field(default_factory=dict)
    participant_id: int | None = None
    registered_at: str | None = None
    last_checked_at: str | None = None
    last_mentions_checked_at: str | None = None
    followed_chats: dict[int, FollowedChat] = field(default_factory=dict)

    # ------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str | Path) -> "UserConfig":
        """Read the file if it exists; otherwise start empty (first run)."""
        resolved = Path(os.path.expanduser(str(path)))
        config = cls(path=resolved)
        if not resolved.exists():
            return config
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"user_config at {resolved} exists but cannot be read "
                f"({exc}). Fix or remove the file — refusing to guess and "
                "silently overwrite the stored identity."
            ) from exc
        config.base_url = (raw.get("server") or {}).get("base_url")
        config.declared = dict(raw.get("declared") or {})
        assigned = raw.get("assigned") or {}
        config.participant_id = assigned.get("participant_id")
        config.registered_at = assigned.get("registered_at")
        config.last_checked_at = assigned.get("last_checked_at")
        config.last_mentions_checked_at = assigned.get("last_mentions_checked_at")
        for entry in assigned.get("followed_chats") or []:
            chat = FollowedChat(
                chat_id=int(entry["chat_id"]),
                name=str(entry.get("name", "")),
                last_read_message_id=entry.get("last_read_message_id"),
                last_read_at=entry.get("last_read_at"),
            )
            config.followed_chats[chat.chat_id] = chat
        return config

    def save(self) -> None:
        """Atomic write: never leave a truncated identity file behind."""
        payload = {
            "server": {"base_url": self.base_url},
            "declared": self.declared,
            "assigned": {
                "participant_id": self.participant_id,
                "registered_at": self.registered_at,
                "last_checked_at": self.last_checked_at,
                "last_mentions_checked_at": self.last_mentions_checked_at,
                "followed_chats": [
                    chat.to_dict()
                    for chat in sorted(
                        self.followed_chats.values(), key=lambda c: c.chat_id
                    )
                ],
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ identity

    @property
    def registered(self) -> bool:
        return self.participant_id is not None

    def require_participant_id(self) -> int:
        if self.participant_id is None:
            raise NotRegisteredError(
                "Not registered yet: this agent has no participant ID. "
                "Call aim_register first (one-time), then follow or create "
                "a chat and introduce yourself."
            )
        return self.participant_id

    # ---------------------------------------------------------- checkpoints

    def reset_checkpoints(self) -> None:
        """Discard all read state and the followed-chats mirror.

        Checkpoints belong to the participant ID they were written with
        (design §4.3): when the identity changes, reading twice is safe,
        skipping a message is not.
        """
        self.followed_chats.clear()
        self.last_checked_at = None
        self.last_mentions_checked_at = None

    def upsert_followed(self, chat_id: int, name: str) -> FollowedChat:
        chat = self.followed_chats.get(chat_id)
        if chat is None:
            chat = FollowedChat(chat_id=chat_id, name=name)
            self.followed_chats[chat_id] = chat
        else:
            chat.name = name or chat.name
        return chat

    def advance_checkpoint(
        self, chat_id: int, message_id: int, created_at: str | None = None
    ) -> bool:
        """Move a chat's read marker forward; never backwards.

        Returns True if the marker actually moved.
        """
        chat = self.followed_chats.get(chat_id)
        if chat is None:
            return False
        if chat.last_read_message_id is None or message_id > chat.last_read_message_id:
            chat.last_read_message_id = message_id
            if created_at is not None:
                chat.last_read_at = created_at
            return True
        return False
