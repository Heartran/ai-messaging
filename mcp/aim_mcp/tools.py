"""Tool logic, independent of the MCP wiring.

Every method takes the calling conversation's `client_session_key` first
(§4.4): the MCP process is shared by all conversations on a machine and
cannot know by itself which one is calling — only the agent knows, so it
passes the key and the client selects that identity from the user_config
dictionary. Missing or unknown key → explicit error, never a fallback on
whichever identity happens to be loaded.

Every method returns a plain dict (the MCP layer serializes it). The split
of responsibilities follows design §4.5: the agent brings only content and
intention; identity and ordering come from the server. This layer adds the
client-side state: per-identity read checkpoints, the followed-chats
mirror, and the "is_me" marking that lets an agent tell its own messages
apart (§9.1).
"""

from __future__ import annotations

import socket
from typing import Any

from .client import AimClient, AimServerError
from .user_config import Identity, UserConfig

INTRO_FIELDS = ("who", "works_for", "goal", "seeking")


class AimTools:
    def __init__(self, config: UserConfig, client: AimClient):
        self.config = config
        self.client = client

    # ------------------------------------------------------------ identity

    async def register(
        self,
        client_session_key: str,
        name: str,
        client_type: str,
        agent_type: str,
        machine: str | None = None,
    ) -> dict[str, Any]:
        key = (client_session_key or "").strip()
        if not key:
            raise AimServerError(
                "client_session_key is required: it is this conversation's "
                "identity-continuity key (§4.3/§4.4). For a Claude chat, "
                "use the conversation ID from the URL."
            )
        machine = (machine or socket.gethostname()).strip()
        response = await self.client.register(
            name=name,
            machine=machine,
            client_type=client_type,
            agent_type=agent_type,
            client_session_key=key,
        )
        if "resumed" not in response:
            raise AimServerError(
                "The central server silently ignored client_session_key: it "
                "predates identity continuity and would mint a new ghost "
                "identity on every machine. Update it to aim-server >= "
                "0.3.0 (git pull && pip install --upgrade ./server) and "
                "restart it, then register again."
            )

        identity = self.config.upsert_identity(key)
        previous_id = identity.participant_id
        new_id = response["participant_id"]
        if previous_id is not None and previous_id != new_id:
            # This identity's stored state belongs to a participant the
            # server no longer honors (e.g. a wipe happened between calls):
            # reading twice is safe, skipping a message is not (§4.3).
            identity.reset_checkpoints()
            response["note"] = (
                f"This identity's stored state referred to participant "
                f"{previous_id}, but the server resolved the key to "
                f"participant {new_id}: stored checkpoints were discarded."
            )
        # On a resume the server's stored identity wins over the declared one.
        identity.declared = {
            "name": response.get("name", name),
            "machine": machine,
            "client_type": response.get("client_type", client_type),
            "agent_type": response.get("agent_type", agent_type),
        }
        identity.participant_id = new_id
        identity.registered_at = response["registered_at"]
        self.config.save()
        return response

    def whoami(self, client_session_key: str | None = None) -> dict[str, Any]:
        if client_session_key:
            identity = self.config.identity_for(client_session_key)
            return {
                "client_session_key": identity.key,
                "registered": identity.registered,
                "participant_id": identity.participant_id,
                "declared": identity.declared,
                "registered_at": identity.registered_at,
                "last_checked_at": identity.last_checked_at,
                "last_mentions_checked_at": identity.last_mentions_checked_at,
                "followed_chats": [
                    chat.to_dict()
                    for chat in sorted(
                        identity.followed_chats.values(), key=lambda c: c.chat_id
                    )
                ],
                "server": self.client.base_url,
            }
        # Without a key: an overview of every identity this client holds.
        # Keys are credentials of OTHER conversations too — only previews.
        return {
            "identities": [
                {
                    "key_preview": identity.key_preview(),
                    "participant_id": identity.participant_id,
                    "name": identity.declared.get("name"),
                    "client_type": identity.declared.get("client_type"),
                    "followed_chats": len(identity.followed_chats),
                }
                for _, identity in sorted(self.config.identities.items())
            ],
            "count": len(self.config.identities),
            "server": self.client.base_url,
            "note": "Pass your client_session_key for the full detail of "
            "your own identity. Key previews are shown because full keys "
            "are credentials of other conversations on this machine.",
        }

    # ------------------------------------------------------- wipe recovery

    async def _rebirth(self, identity: Identity) -> str:
        """The cache is wrong by definition: the server does not know this
        identity's participant ID.

        Wipe-safe recovery (§4.3), scoped to ONE identity: zero its state
        and re-register with its own client_session_key — same logical
        identity, new ID. Other identities in the dictionary are untouched.
        """
        declared = dict(identity.declared)
        old_id = identity.participant_id
        identity.participant_id = None
        identity.registered_at = None
        identity.reset_checkpoints()
        self.config.save()
        if not declared.get("name"):
            raise AimServerError(
                f"The server no longer knows participant {old_id} (it was "
                "wiped or its database recreated). This identity's local "
                "state has been cleared — register again with aim_register "
                "and your client_session_key."
            )
        response = await self.client.register(
            name=declared["name"],
            machine=declared.get("machine", "unknown"),
            client_type=declared.get("client_type", "chat"),
            agent_type=declared.get("agent_type", "claude"),
            client_session_key=identity.key,
        )
        identity.participant_id = response["participant_id"]
        identity.registered_at = response["registered_at"]
        self.config.save()
        return (
            f"The server had no participant {old_id} (it was wiped or its "
            "database recreated): this identity re-registered automatically "
            f"with its conversation key and is now participant "
            f"{response['participant_id']}. Followed chats and checkpoints "
            "were reset — re-create or re-follow chats as needed."
        )

    async def _identified(self, identity: Identity, attempt):
        """Run an identified call; on 'this ID no longer exists', rebirth
        and retry exactly once (never a retry loop, §4.3)."""
        try:
            return await attempt()
        except AimServerError as exc:
            if (
                exc.code != "unknown_participant"
                or exc.participant_id is None
                or exc.participant_id != identity.participant_id
            ):
                raise
            note = await self._rebirth(identity)
            result = await attempt()
            if isinstance(result, dict):
                result["identity_note"] = note
            return result

    # --------------------------------------------------------------- chats

    async def create_chat(
        self,
        client_session_key: str,
        name: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)

        async def attempt() -> dict[str, Any]:
            pid = identity.require_participant_id()
            response = await self.client.create_chat(pid, name, description)
            identity.upsert_followed(response["chat_id"], response["name"])
            self.config.save()
            return response

        return await self._identified(identity, attempt)

    async def list_chats(
        self,
        client_session_key: str,
        query: str | None = None,
        include_last_message: bool = False,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)

        async def attempt() -> dict[str, Any]:
            # Default since: this identity's own global checkpoint, so
            # messages_since means "new since I last checked anything"
            # (§9.3) — computed by the server, which stays stateless
            # about reads.
            return await self.client.list_chats(
                participant_id=identity.participant_id,
                query=query,
                include_last_message=include_last_message or None,
                since=since if since is not None else identity.last_checked_at,
                limit=limit,
                offset=offset,
            )

        return await self._identified(identity, attempt)

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
        self,
        client_session_key: str,
        chat_id: int | None = None,
        chat_name: str | None = None,
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)

        async def attempt() -> dict[str, Any]:
            pid = identity.require_participant_id()
            resolved = await self._resolve_chat_id(chat_id, chat_name)
            response = await self.client.follow_chat(resolved, pid)
            identity.upsert_followed(response["chat_id"], response["chat_name"])
            self.config.save()
            return response

        return await self._identified(identity, attempt)

    async def leave_chat(
        self, client_session_key: str, chat_id: int
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)

        async def attempt() -> dict[str, Any]:
            pid = identity.require_participant_id()
            response = await self.client.leave_chat(chat_id, pid)
            # Drop the local mirror entry; the server keeps the membership
            # row with its explicit "left" marker, and re-following resumes
            # the same participant ID.
            identity.followed_chats.pop(chat_id, None)
            self.config.save()
            return response

        return await self._identified(identity, attempt)

    # ------------------------------------------------------------ messages

    async def send_message(
        self,
        client_session_key: str,
        chat_id: int,
        text: str,
        mentions: list[int] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)

        async def attempt() -> dict[str, Any]:
            pid = identity.require_participant_id()
            response = await self.client.send_message(
                chat_id, pid, text, mentions or []
            )
            if reply_to_message_id is not None:
                # Reply-and-archive (lesson from mcp-talk): answering a
                # message marks it — and everything before it — as read.
                if identity.advance_checkpoint(chat_id, reply_to_message_id):
                    self.config.save()
                    response["checkpoint_advanced_to"] = reply_to_message_id
            return response

        return await self._identified(identity, attempt)

    async def introduce(
        self,
        client_session_key: str,
        chat_id: int,
        text: str,
        who: str,
        works_for: str,
        goal: str,
        seeking: str,
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)

        async def attempt() -> dict[str, Any]:
            pid = identity.require_participant_id()
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

        return await self._identified(identity, attempt)

    async def get_messages(
        self,
        client_session_key: str,
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
        identity = self.config.identity_for(client_session_key)
        return await self._identified(
            identity,
            lambda: self._get_messages_attempt(
                identity, chat_id, after, before, after_id, before_id,
                from_id, query, only_mentions, limit, mark_read,
            ),
        )

    async def _get_messages_attempt(
        self,
        identity: Identity,
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
        pid = identity.require_participant_id()

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
                followed = identity.followed_chats.get(chat_id)
                if followed and followed.last_read_message_id is not None:
                    after_id = followed.last_read_message_id
            elif identity.last_checked_at is not None:
                after = identity.last_checked_at
        elif mentions_flow and identity.last_mentions_checked_at is not None:
            after = identity.last_mentions_checked_at

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
                    followed = identity.followed_chats.get(message["chat_id"])
                    return (
                        followed is not None
                        and followed.last_read_message_id is not None
                        and message["id"] <= followed.last_read_message_id
                    )

                fresh = [m for m in raw if not already_read(m)]

            # ...→ present → rewrite the checkpoint (§9.1). Checkpoints
            # advance over everything the server returned — it was all
            # either presented now or read before — and anchor to server
            # timestamps/IDs, never to the local clock (§3.1).
            advanced: dict[int, int] = {}
            for message in raw:
                if identity.advance_checkpoint(
                    message["chat_id"], message["id"], message["created_at"]
                ):
                    advanced[message["chat_id"]] = max(
                        advanced.get(message["chat_id"], 0), message["id"]
                    )
            if chat_id is None:
                identity.last_checked_at = raw[0]["created_at"]  # newest
            self.config.save()
            response["messages"] = fresh
            response["count"] = len(fresh)
            if not fresh:
                response["notice"] = "No messages to display."
            if advanced:
                response["checkpoints_advanced"] = advanced
        elif mentions_flow and response["messages"]:
            identity.last_mentions_checked_at = response["messages"][0][
                "created_at"
            ]
            self.config.save()
            response["mentions_checkpoint_advanced_to"] = (
                identity.last_mentions_checked_at
            )
        return response

    # -------------------------------------------------------- participants

    async def list_participants(
        self, client_session_key: str, chat_id: int
    ) -> dict[str, Any]:
        identity = self.config.identity_for(client_session_key)
        response = await self.client.list_participants(chat_id)
        for participant in response["participants"]:
            participant["is_me"] = participant["id"] == identity.participant_id
        return response
