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
    ) -> dict[str, Any]:
        if self.config.registered:
            return {
                "already_registered": True,
                "participant_id": self.config.participant_id,
                "declared": self.config.declared,
                "note": "Registration is one-time and this identity is "
                "already stored in user_config. There is nothing to do; "
                "use aim_whoami to inspect it.",
            }
        machine = (machine or socket.gethostname()).strip()
        response = await self.client.register(
            name=name,
            machine=machine,
            client_type=client_type,
            agent_type=agent_type,
        )
        self.config.declared = {
            "name": name,
            "machine": machine,
            "client_type": client_type,
            "agent_type": agent_type,
        }
        self.config.participant_id = response["participant_id"]
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

    # --------------------------------------------------------------- chats

    async def create_chat(
        self, name: str, description: str | None = None
    ) -> dict[str, Any]:
        pid = self.config.require_participant_id()
        response = await self.client.create_chat(pid, name, description)
        self.config.upsert_followed(response["chat_id"], response["name"])
        self.config.save()
        return response

    async def list_chats(
        self,
        query: str | None = None,
        include_last_message: bool = False,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        pid = self.config.participant_id
        if since is None:
            # Default: the client's own global checkpoint, so messages_since
            # means "new since I last checked anything" (§8.3) — computed by
            # the server, which stays stateless about reads.
            since = self.config.last_checked_at
        return await self.client.list_chats(
            participant_id=pid,
            query=query,
            include_last_message=include_last_message or None,
            since=since,
            limit=limit,
            offset=offset,
        )

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
        pid = self.config.require_participant_id()
        resolved = await self._resolve_chat_id(chat_id, chat_name)
        response = await self.client.follow_chat(resolved, pid)
        self.config.upsert_followed(response["chat_id"], response["chat_name"])
        self.config.save()
        return response

    async def leave_chat(self, chat_id: int) -> dict[str, Any]:
        pid = self.config.require_participant_id()
        response = await self.client.leave_chat(chat_id, pid)
        # Drop the local mirror entry; the server keeps the membership row
        # with its explicit "left" marker, and re-following resumes the
        # same participant ID.
        self.config.followed_chats.pop(chat_id, None)
        self.config.save()
        return response

    # ------------------------------------------------------------ messages

    async def send_message(
        self,
        chat_id: int,
        text: str,
        mentions: list[int] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        pid = self.config.require_participant_id()
        response = await self.client.send_message(
            chat_id, pid, text, mentions or []
        )
        if reply_to_message_id is not None:
            # Reply-and-archive (lesson from mcp-talk): answering a message
            # marks it — and everything before it — as read.
            if self.config.advance_checkpoint(chat_id, reply_to_message_id):
                self.config.save()
                response["checkpoint_advanced_to"] = reply_to_message_id
        return response

    async def introduce(
        self,
        chat_id: int,
        text: str,
        who: str,
        works_for: str,
        goal: str,
        seeking: str,
    ) -> dict[str, Any]:
        pid = self.config.require_participant_id()
        return await self.client.introduce(
            chat_id,
            pid,
            text,
            {"who": who, "works_for": works_for, "goal": goal, "seeking": seeking},
        )

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
            # The §8.1 cycle: read checkpoint → get(after=checkpoint).
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
