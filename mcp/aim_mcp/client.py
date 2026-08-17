"""HTTP client toward the central server.

Thin async wrapper over the server's REST API. Errors are translated
into actionable messages for the agent: what failed, and what to do next.
"""

from __future__ import annotations

from typing import Any

import httpx

# Oldest central-server API this client can talk to (identity continuity
# via client_session_key and presence arrived in 0.3.0).
MIN_SERVER_VERSION = "0.3.0"


class AimServerError(Exception):
    """A server call failed; the message is agent-actionable."""


class AimClient:
    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        # `transport` is injectable so tests can mount the FastAPI app
        # in-process (httpx.ASGITransport) and exercise the full stack.
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, path, json=json, params=params
            )
        except httpx.ConnectError as exc:
            raise AimServerError(
                f"Central server unreachable at {self.base_url}: {exc}. "
                "Check that the server is running on its host machine and "
                "that this machine is connected to the tailnet "
                "(the server only listens inside it)."
            ) from exc
        except httpx.TimeoutException as exc:
            raise AimServerError(
                f"Request to {self.base_url} timed out. The server may be "
                "overloaded or the tailnet link may be down; retry shortly."
            ) from exc
        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text
            if response.status_code == 404 and detail == "Not Found":
                # The server's own 404s always carry a speaking detail
                # ("Unknown chat ID 3. …"); a bare "Not Found" means the
                # ROUTE does not exist — a server older than this client.
                raise AimServerError(
                    f"The server at {self.base_url} does not know the "
                    f"endpoint {path!r} at all: it is running an older "
                    "build than this client. On the server machine, update "
                    "and reinstall, then restart it: git pull && pip "
                    "install --upgrade ./server && python -m aim_server. "
                    f"This client needs aim-server >= {MIN_SERVER_VERSION}; "
                    "GET /health reports the running version."
                )
            raise AimServerError(
                f"Server refused the call ({response.status_code}): {detail}"
            )
        return response.json()

    # ------------------------------------------------------------ endpoints

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def register(
        self,
        name: str,
        machine: str,
        client_type: str,
        agent_type: str,
        client_session_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/register",
            json={
                "name": name,
                "machine": machine,
                "client_type": client_type,
                "agent_type": agent_type,
                "client_session_key": client_session_key,
            },
        )

    async def create_chat(
        self, participant_id: int, name: str, description: str | None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/chats",
            json={
                "participant_id": participant_id,
                "name": name,
                "description": description,
            },
        )

    async def list_chats(self, **params: Any) -> dict[str, Any]:
        return await self._request(
            "GET", "/chats", params=_drop_none(params)
        )

    async def follow_chat(self, chat_id: int, participant_id: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/chats/{chat_id}/follow",
            json={"participant_id": participant_id},
        )

    async def leave_chat(self, chat_id: int, participant_id: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/chats/{chat_id}/leave",
            json={"participant_id": participant_id},
        )

    async def send_message(
        self, chat_id: int, sender_id: int, text: str, mentions: list[int]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/chats/{chat_id}/messages",
            json={"sender_id": sender_id, "text": text, "mentions": mentions},
        )

    async def introduce(
        self, chat_id: int, sender_id: int, text: str, payload: dict[str, str]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/chats/{chat_id}/introductions",
            json={"sender_id": sender_id, "text": text, "payload": payload},
        )

    async def get_chat_messages(self, chat_id: int, **params: Any) -> dict[str, Any]:
        return await self._request(
            "GET", f"/chats/{chat_id}/messages", params=_drop_none(params)
        )

    async def get_inbox(self, **params: Any) -> dict[str, Any]:
        return await self._request("GET", "/messages", params=_drop_none(params))

    async def list_participants(self, chat_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/chats/{chat_id}/participants")

    async def participant_chats(self, participant_id: int) -> dict[str, Any]:
        return await self._request(
            "GET", f"/participants/{participant_id}/chats"
        )


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}
