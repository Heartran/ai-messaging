"""Tool logic, independent of the MCP wiring.

Every method returns a plain dict (the MCP layer serializes it). The split
of responsibilities follows design §4.3: the agent brings content and
intention; identity and ordering come from the server. This layer adds the
client-side state: read checkpoints, the followed-chats mirror, and the
"is_me" marking that lets an agent tell its own messages apart (§8.1).
"""

from __future__ import annotations

import socket
from typing import Any

from .client import AimClient, AimServerError
from .user_config import UserConfig

INTRO_FIELDS = ("who", "works_for", "goal", "seeking")


class AimTools:
    def __init__(self, config: UserConfig, client: AimClient):
        self.config = config
        self.client = client

    # ------------------------------------------------------------ identity

    async def register(
        self,
        name: str,
        client_type: str,
        agent_type: str,
        machine: str | None = None,
        client_session_key: str | None = None,
    ) -> dict[str, Any]:
        key = client_session_key or self.config.declared.get("client_session_key")
        if self.config.registered and not key:
            return {
                "already_registered": True,
                "participant_id": self.config.participant_id,
                "declared": self.config.declared,
                "note": "Registration is one-time and this identity is "
                "already stored in user_config. There is nothing to do; "
                "use aim_whoami to inspect it. (To make the identity "
                "portable across machines, re-register passing "
                "client_session_key — the conversation identifier.)",
            }
        machine = (machine or socket.gethostname()).strip()
        response = await self.client.register(
            name=name,
            machine=machine,
            client_type=client_type,
            agent_type=agent_type,
            client_session_key=key,
        )
        if key and "resumed" not in response:
            raise AimServerError(
                "The central server silently ignored client_session_key: it "
                "predates identity continuity and would mint a new ghost "
                "identity on every machine. Update it to aim-server >= "
                "0.3.0 (git pull && pip install --upgrade ./server) and "
                "restart it, then register again."
            )

        previous_id = self.config.participant_id
        new_id = response["participant_id"]
        if previous_id is not None and previous_id != new_id:
            # The stored state belongs to another identity (§4.3): reading
            # twice is safe, skipping a message is not.
            self.config.reset_checkpoints()
            response["note"] = (
                f"user_config held state for participant {previous_id}, but "
                f"this session is participant {new_id}: stored checkpoints "
                "were discarded and the identity switched."
            )
        # On a resume the server's stored identity wins over the declared one.
        self.config.declared = {
            "name": response.get("name", name),
            "machine": machine,
            "client_type": response.get("client_type", client_type),
            "agent_type": response.get("agent_type", agent_type),
        }
        if key:
            self.config.declared["client_session_key"] = key
        self.config.participant_id = new_id
        self.config.registered_at = response["registered_at"]
        self.config.save()
        return response

    def whoami(self) -> dict[str, Any]:
        return {
            "registered": self.config.registered,
            "participant_id": self.config.participant_id,
            "declared": self.config.declared,
            "registered_at": self.config.registered_at,
            "last_checked_at": self.config.last_checked_at,
            "last_mentions_checked_at": self.config.last_mentions_checked_at,
            "followed_chats": [
                chat.to_dict()
                for chat in sorted(
                    self.config.followed_chats.values(), key=lambda c: c.chat_id
                )
            ],
            "server": self.client.base_url,
        }

    # ------------------------------------------------------- wipe recovery

    async def _rebirth(self) -> str:
        """The cache is wrong by definition: the server does not know our ID.

        Wipe-safe recovery (§4.3): zero the assigned state and, when a
        client_session_key is stored, re-register with it automatically —
        same logical identity, new ID. Without a key, clear the cache and
        explain; never leave a stale identity that would retry forever.
        """
        declared = dict(self.config.declared)
        key = declared.get("client_session_key")
        old_id = self.config.participant_id
        self.config.participant_id = None
        self.config.registered_at = None
        self.config.reset_checkpoints()
        self.config.save()
        if not key or not declared.get("name"):
            raise AimServerError(
                f"The server no longer knows participant {old_id}: it was "
                "wiped or its database was recreated. Local state has been "
                "cleared — register again with aim_register (include "
                "client_session_key so this recovery becomes automatic)."
            )
        response = await self.client.register(
            name=declared["name"],
            machine=declared.get("machine", "unknown"),
            client_type=declared.get("client_type", "chat"),
            agent_type=declared.get("agent_type", "claude"),
            client_session_key=key,
        )
        self.config.participant_id = response["participant_id"]
        self.config.registered_at = response["registered_at"]
        self.config.save()
        return (
            f"The server had no participant {old_id} (it was wiped or its "
            "database recreated): this identity re-registered automatically "
            f"with its conversation key and is now participant "
            f"{response['participant_id']}. Followed chats and checkpoints "
            "were reset — re-create or re-follow chats as needed."
        )

    async def _identified(self, attempt):
        """Run an identified call; on 'our ID no longer exists', rebirth and
        retry exactly once (never a retry loop, §4.3)."""
        try:
            return await attempt()
        except AimServerError as exc:
            if (
                exc.code != "unknown_participant"
                or exc.participant_id is None
                or exc.participant_id != self.config.participant_id
            ):
                raise
            note = await self._rebirth()
            result = await attempt()
            if isinstance(result, dict):
                result["identity_note"] = note
            return result

    # --------------------------------------------------------------- chats

    async def create_chat(
        self, name: str, description: str | None = None
    ) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            pid = self.config.require_participant_id()
            response = await self.client.create_chat(pid, name, description)
            self.config.upsert_followed(response["chat_id"], response["name"])
            self.config.save()
            return response

        return await self._identified(attempt)

    async def list_chats(
        self,
        query: str | None = None,
        include_last_message: bool = False,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            # Default since: the client's own global checkpoint, so
            # messages_since means "new since I last checked anything"
            # (§9.3) — computed by the server, which stays stateless
            # about reads.
            return await self.client.list_chats(
                participant_id=self.config.participant_id,
                query=query,
                include_last_message=include_last_message or None,
                since=since if since is not None else self.config.last_checked_at,
                limit=limit,
                offset=offset,
            )

        return await self._identified(attempt)

    async def _resolve_chat_id(
        self, chat_id: int | None, chat_name: str | None
    ) -> int:
        if (chat_id is None) == (chat_name is None):
            raise AimServerError(
                "Provide exactly one of chat_id or chat_name."
            )
        if chat_id is not None:
            return chat_id
        assert chat_name is not None
        listing = await self.client.list_chats(query=chat_name, limit=200)
        exact = [
            chat
            for chat in listing["chats"]
            if chat["name"].casefold() == chat_name.casefold()
        ]
        if exact:
            return exact[0]["id"]
        similar = [chat["name"] for chat in listing["chats"]]
        hint = f" Similar names: {similar}." if similar else ""
        raise AimServerError(
            f"No chat named {chat_name!r} exists.{hint} Use aim_list_chats "
            "to discover chats, or aim_create_chat to found it."
        )

    async def follow_chat(
        self, chat_id: int | None = None, chat_name: str | None = None
    ) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            pid = self.config.require_participant_id()
            resolved = await self._resolve_chat_id(chat_id, chat_name)
            response = await self.client.follow_chat(resolved, pid)
            self.config.upsert_followed(response["chat_id"], response["chat_name"])
            self.config.save()
            return response

        return await self._identified(attempt)

    async def leave_chat(self, chat_id: int) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            pid = self.config.require_participant_id()
            response = await self.client.leave_chat(chat_id, pid)
            # Drop the local mirror entry; the server keeps the membership
            # row with its explicit "left" marker, and re-following resumes
            # the same participant ID.
            self.config.followed_chats.pop(chat_id, None)
            self.config.save()
            return response

        return await self._identified(attempt)

    # ------------------------------------------------------------ messages

    async def send_message(
        self,
        chat_id: int,
        text: str,
        mentions: list[int] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            pid = self.config.require_participant_id()
            response = await self.client.send_message(
                chat_id, pid, text, mentions or []
            )
            if reply_to_message_id is not None:
                # Reply-and-archive (lesson from mcp-talk): answering a
                # message marks it — and everything before it — as read.
                if self.config.advance_checkpoint(chat_id, reply_to_message_id):
                    self.config.save()
                    response["checkpoint_advanced_to"] = reply_to_message_id
            return response

        return await self._identified(attempt)

    async def introduce(
        self,
        chat_id: int,
        text: str,
        who: str,
        works_for: str,
        goal: str,
        seeking: str,
    ) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            pid = self.config.require_participant_id()
            return await self.client.introduce(
                chat_id,
                pid,
                text,
                {
                    "who": who,
                    "works_for": works_for,
                    "goal": goal,
                    "seeking": seeking,
                },
            )

        return await self._identified(attempt)

    async def get_messages(
        self,
        chat_id: int | None = None,
        after: str | None = None,
        before: str | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        from_id: int | None = None,
        query: str | None = None,
        only_mentions: bool = False,
        limit: int = 50,
        mark_read: bool = True,
    ) -> dict[str, Any]:
        return await self._identified(
            lambda: self._get_messages_attempt(
                chat_id, after, before, after_id, before_id,
                from_id, query, only_mentions, limit, mark_read,
            )
        )

    async def _get_messages_attempt(
        self,
        chat_id: int | None,
        after: str | None,
        before: str | None,
        after_id: int | None,
        before_id: int | None,
        from_id: int | None,
        query: str | None,
        only_mentions: bool,
        limit: int,
        mark_read: bool,
    ) -> dict[str, Any]:
        pid = self.config.require_participant_id()

        explicit_cursor = any(
            value is not None for value in (after, before, after_id, before_id)
        )
        historical = any(
            value is not None for value in (before, before_id, from_id, query)
        )
        # only_mentions reads only a slice of the flow, so it must never
        # advance the general checkpoints (that would silently mark unseen
        # normal messages as read). The global mentions workflow gets its
        # own dedicated checkpoint instead.
        checkpoint_flow = (
            mark_read and not explicit_cursor and not historical and not only_mentions
        )
        mentions_flow = (
            mark_read
            and only_mentions
            and chat_id is None
            and not explicit_cursor
            and not historical
        )

        if checkpoint_flow:
            # The §9.1 cycle: read checkpoint → get(after=checkpoint).
            if chat_id is not None:
                followed = self.config.followed_chats.get(chat_id)
                if followed and followed.last_read_message_id is not None:
                    after_id = followed.last_read_message_id
            elif self.config.last_checked_at is not None:
                after = self.config.last_checked_at
        elif mentions_flow and self.config.last_mentions_checked_at is not None:
            after = self.config.last_mentions_checked_at

        params = {
            "participant_id": pid,
            "after": after,
            "before": before,
            "after_id": after_id,
            "before_id": before_id,
            "from_id": from_id,
            "query": query,
            "only_mentions": only_mentions or None,
            "limit": limit,
        }
        if chat_id is not None:
            response = await self.client.get_chat_messages(chat_id, **params)
        else:
            response = await self.client.get_inbox(**params)

        for message in response["messages"]:
            message["is_me"] = message["sender"]["id"] == pid

        if checkpoint_flow and response["messages"]:
            raw = response["messages"]
            fresh = raw
            if chat_id is None:
                # The inbox composes with the per-chat markers: anything a
                # chat-scoped read (or a reply-and-archive) already consumed
                # must not be served as new again. Filter with the markers
                # as they are BEFORE this call advances them.
                def already_read(message: dict[str, Any]) -> bool:
                    followed = self.config.followed_chats.get(message["chat_id"])
                    return (
                        followed is not None
                        and followed.last_read_message_id is not None
                        and message["id"] <= followed.last_read_message_id
                    )

                fresh = [m for m in raw if not already_read(m)]

            # ...→ present → rewrite the checkpoint (§8.1). Checkpoints
            # advance over everything the server returned — it was all
            # either presented now or read before — and anchor to server
            # timestamps/IDs, never to the local clock (§3.1).
            advanced: dict[int, int] = {}
            for message in raw:
                if self.config.advance_checkpoint(
                    message["chat_id"], message["id"], message["created_at"]
                ):
                    advanced[message["chat_id"]] = max(
                        advanced.get(message["chat_id"], 0), message["id"]
                    )
            if chat_id is None:
                self.config.last_checked_at = raw[0]["created_at"]  # newest
            self.config.save()
            response["messages"] = fresh
            response["count"] = len(fresh)
            if not fresh:
                response["notice"] = "No messages to display."
            if advanced:
                response["checkpoints_advanced"] = advanced
        elif mentions_flow and response["messages"]:
            self.config.last_mentions_checked_at = response["messages"][0][
                "created_at"
            ]
            self.config.save()
            response["mentions_checkpoint_advanced_to"] = (
                self.config.last_mentions_checked_at
            )
        return response

    # -------------------------------------------------------- participants

    async def list_participants(self, chat_id: int) -> dict[str, Any]:
        response = await self.client.list_participants(chat_id)
        pid = self.config.participant_id
        for participant in response["participants"]:
            participant["is_me"] = participant["id"] == pid
        return response
