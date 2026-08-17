"""Client-side state: the `user_config` file (design §3.2, revised by §4.4).

One MCP process on a machine is shared by every conversation running
there, so the file holds a DICTIONARY of identities indexed by
`client_session_key` — one entry per conversation, each with its own
participant ID and its own read checkpoints. Identity is a fact of the
conversation, never shared state contended over a file: a second
conversation registering must never overwrite the first (§4.4).

Read checkpoints live HERE, per identity, never on the server: the server
stays stateless about who has read what (design §3.1).
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
    """Raised when a call cannot be mapped to a registered identity."""


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
class Identity:
    """One conversation's identity: its ID, metadata and checkpoints."""

    key: str
    participant_id: int | None = None
    registered_at: str | None = None
    declared: dict[str, Any] = field(default_factory=dict)
    last_checked_at: str | None = None
    last_mentions_checked_at: str | None = None
    followed_chats: dict[int, FollowedChat] = field(default_factory=dict)

    @property
    def registered(self) -> bool:
        return self.participant_id is not None

    def require_participant_id(self) -> int:
        if self.participant_id is None:
            raise NotRegisteredError(
                "This client_session_key has no participant ID yet. Call "
                "aim_register with it first."
            )
        return self.participant_id

    def key_preview(self) -> str:
        """Recognizable to its owner, useless to claim the identity."""
        return "…" + self.key[-6:] if len(self.key) > 6 else "…"

    # ---------------------------------------------------------- checkpoints

    def reset_checkpoints(self) -> None:
        """Discard this identity's read state and followed-chats mirror.

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
        """Move a chat's read marker forward; never backwards."""
        chat = self.followed_chats.get(chat_id)
        if chat is None:
            return False
        if chat.last_read_message_id is None or message_id > chat.last_read_message_id:
            chat.last_read_message_id = message_id
            if created_at is not None:
                chat.last_read_at = created_at
            return True
        return False

    # -------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "registered_at": self.registered_at,
            "declared": self.declared,
            "last_checked_at": self.last_checked_at,
            "last_mentions_checked_at": self.last_mentions_checked_at,
            "followed_chats": [
                chat.to_dict()
                for chat in sorted(
                    self.followed_chats.values(), key=lambda c: c.chat_id
                )
            ],
        }

    @classmethod
    def from_dict(cls, key: str, raw: dict[str, Any]) -> "Identity":
        identity = cls(
            key=key,
            participant_id=raw.get("participant_id"),
            registered_at=raw.get("registered_at"),
            declared=dict(raw.get("declared") or {}),
            last_checked_at=raw.get("last_checked_at"),
            last_mentions_checked_at=raw.get("last_mentions_checked_at"),
        )
        for entry in raw.get("followed_chats") or []:
            chat = FollowedChat(
                chat_id=int(entry["chat_id"]),
                name=str(entry.get("name", "")),
                last_read_message_id=entry.get("last_read_message_id"),
                last_read_at=entry.get("last_read_at"),
            )
            identity.followed_chats[chat.chat_id] = chat
        return identity


@dataclass
class UserConfig:
    path: Path
    base_url: str | None = None
    identities: dict[str, Identity] = field(default_factory=dict)

    # ------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str | Path) -> "UserConfig":
        """Read the file if it exists; otherwise start empty (first run).

        A legacy single-identity file (the pre-§4.4 format with top-level
        `declared`/`assigned` blocks) is migrated in place: a keyed
        identity becomes an entry of the dictionary; an unkeyed one cannot
        be addressed under the new model (every call requires the key) and
        is left behind in a `.legacy-backup` copy of the original file.
        """
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
                "silently overwrite stored identities."
            ) from exc

        if "identities" in raw or "assigned" not in raw:
            config.base_url = raw.get("server_url") or (
                (raw.get("server") or {}).get("base_url")
            )
            for key, entry in (raw.get("identities") or {}).items():
                config.identities[key] = Identity.from_dict(key, entry)
            return config

        # ---- legacy single-identity format: migrate ----
        backup = resolved.with_suffix(resolved.suffix + ".legacy-backup")
        if not backup.exists():
            backup.write_bytes(resolved.read_bytes())
        config.base_url = (raw.get("server") or {}).get("base_url")
        declared = dict(raw.get("declared") or {})
        assigned = raw.get("assigned") or {}
        key = declared.pop("client_session_key", None)
        if key:
            entry = Identity.from_dict(
                key,
                {
                    "participant_id": assigned.get("participant_id"),
                    "registered_at": assigned.get("registered_at"),
                    "declared": declared,
                    "last_checked_at": assigned.get("last_checked_at"),
                    "last_mentions_checked_at": assigned.get(
                        "last_mentions_checked_at"
                    ),
                    "followed_chats": assigned.get("followed_chats") or [],
                },
            )
            config.identities[key] = entry
        return config

    def save(self) -> None:
        """Atomic write: never leave a truncated identity file behind."""
        payload = {
            "server_url": self.base_url,
            "identities": {
                key: identity.to_dict()
                for key, identity in sorted(self.identities.items())
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

    def identity_for(self, client_session_key: str | None) -> Identity:
        """Resolve the identity a call belongs to — explicitly, or fail.

        Never a silent fallback on "whichever identity is loaded": that is
        exactly the §4.4 flaw made implicit.
        """
        if not client_session_key or not client_session_key.strip():
            raise NotRegisteredError(
                "No client_session_key provided. Every call carries the "
                "conversation's own key — the same one used with "
                "aim_register — because the MCP process is shared by all "
                "conversations on this machine and cannot guess which one "
                "is calling (§4.4)."
            )
        identity = self.identities.get(client_session_key.strip())
        if identity is None:
            raise NotRegisteredError(
                "Unknown client_session_key: no identity is registered "
                "with this key on this client. Call aim_register with it "
                "first (one-time per conversation)."
            )
        return identity

    def upsert_identity(self, client_session_key: str) -> Identity:
        key = client_session_key.strip()
        identity = self.identities.get(key)
        if identity is None:
            identity = Identity(key=key)
            self.identities[key] = identity
        return identity
