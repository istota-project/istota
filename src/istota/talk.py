"""Nextcloud Talk API client."""

import logging
import re

import httpx

from .config import Config

logger = logging.getLogger("istota.talk")

# Backward-compat exports: a few tests / callers still import these. The
# resolver no longer drives off them — it walks messageParameters and
# dispatches on each param's `type` (see _resolve_param) — but the bare-{file}
# leak (ISSUE-132) is why the old `\d+`-anchored file pattern is gone.
FILE_PLACEHOLDER_PATTERN = re.compile(r'\{file\d*\}')
MENTION_PLACEHOLDER_PATTERN = re.compile(r'\{(mention-(?:user|call|federated-user)\d+)\}')


def _resolve_param(key: str, param: dict, bot_username: str | None) -> str | None:
    """Render a single Nextcloud Talk rich-object param to its display form.

    Dispatches on the param's `type` so the whole class of objects is covered
    at once instead of one regex per type (ISSUE-132). `key` (the placeholder
    name, e.g. ``file0`` / ``mention-user1``) is a fallback when a cached/legacy
    param omits `type`. Returns the replacement string, or ``None`` to mean
    "strip" (the bot's own mention).
    """
    obj_type = param.get("type", "")
    name = param.get("name") or param.get("id") or ""

    # Legacy / cache params often omit `type`; fall back to the key prefix so a
    # bare {file0} (name-only param) still renders as a file attachment.
    if not obj_type:
        if key.startswith("file"):
            obj_type = "file"
        elif key.startswith("mention-"):
            obj_type = "user"

    if obj_type in ("user", "federated-user"):
        if bot_username is not None and param.get("id") == bot_username:
            return None  # strip the bot's own mention from the prompt
        return f"@{name}" if name else ""
    if obj_type == "guest":
        return f"@{name}" if name else ""
    if obj_type in ("call", "mention-call"):
        return "@all"
    if obj_type == "file":
        return f"[{param.get('name', 'file')}]"
    if obj_type == "talk-poll":
        return f"[poll: {name}]" if name else "[poll]"
    if obj_type == "deck-card":
        return f"[card: {name}]" if name else "[card]"
    if obj_type in ("geo-location", "location"):
        return f"[location: {name}]" if name else "[location]"
    # Unknown rich object: best-effort name/id, else strip the token.
    return name


def clean_message_content(message: dict, bot_username: str | None = None) -> str:
    """
    Clean up message content, replacing rich-object placeholders with readable text.

    Walks ``messageParameters`` and substitutes every ``{key}`` it finds in the
    body with the param's display form, dispatched on its ``type`` — files,
    @mentions (user / federated / guest / call), polls, deck cards, locations,
    and an id/name fallback for anything else. This resolves the whole family of
    Nextcloud Talk rich objects rather than the two narrow cases (``{fileN}`` /
    ``{mention-…N}``) the old regex pair handled, so single-file ``{file}``
    shares and every non-mention object stop leaking literal tokens into the web
    transcript (ISSUE-132).

    When ``bot_username`` is provided, the bot's own @mention is stripped from
    the body (so its prompt reads naturally); other mentions become @DisplayName.
    """
    content = message.get("message", "")
    message_params = message.get("messageParameters", {})

    # Handle case where messageParameters is an empty list instead of dict.
    if not isinstance(message_params, dict) or not message_params:
        return content

    stripped_any = False
    for key, param in message_params.items():
        token = "{" + key + "}"
        if token not in content or not isinstance(param, dict):
            continue
        replacement = _resolve_param(key, param, bot_username)
        if replacement is None:
            replacement = ""
            stripped_any = True
        content = content.replace(token, replacement)

    # Collapse whitespace left behind by a stripped bot mention. Gated on the
    # strip actually happening so other content is returned byte-for-byte.
    if stripped_any:
        content = re.sub(r'  +', ' ', content).strip()

    return content


class TalkClient:
    """Client for Nextcloud Talk user API (not bot API).

    Two auth modes: the default basic-auth mode acts as the configured bot
    account; ``bearer_token`` switches every request to
    ``Authorization: Bearer <token>`` — a *user-scoped* OAuth2 access token,
    so the client acts as that user (post-as-user mirroring, read-marker
    sync). Bearer instances are short-lived, constructed per request in the
    web process; the daemon's persistent singleton stays basic-auth.
    """

    # Default timeout for short API calls (list rooms, send message, etc.).
    # httpx default is 5s which is too aggressive when the server is busy
    # (e.g. during task execution).
    DEFAULT_TIMEOUT = 15

    def __init__(
        self,
        config: Config,
        bearer_token: str | None = None,
        timeout: float | None = None,
    ):
        self.config = config
        self.base_url = config.nextcloud.url.rstrip("/")
        self.bearer_token = bearer_token
        # Per-instance default timeout override — the short-lived bearer
        # clients in the web request path use a tighter bound (~5s) than the
        # daemon's DEFAULT_TIMEOUT so a slow NC can't stall a web request.
        self._timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        # httpx treats auth=None as "no auth" — bearer mode carries its
        # credential in the Authorization header instead.
        self.auth = (
            None
            if bearer_token
            else (config.nextcloud.username, config.nextcloud.app_password)
        )
        # Persistent httpx client, created lazily on the persistent asyncio loop
        # via get_talk_client(). Until Stage 6 the methods below still open a
        # transient client per call; this one is created and idle.
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    def _headers(self, *, json_body: bool = False) -> dict:
        """Standard OCS headers, plus the bearer credential when in user mode."""
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def _ensure_open(self) -> httpx.AsyncClient:
        """Return the persistent httpx client, creating it on first use.

        Must be awaited on the loop that will run the requests (the persistent
        runtime loop), so the client's connection pool is bound to that loop.
        """
        if self._closed:
            raise RuntimeError("TalkClient is closed")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the persistent httpx client. Idempotent."""
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def send_message(
        self,
        conversation_token: str,
        message: str,
        reply_to: int | None = None,
        reference_id: str | None = None,
    ) -> dict:
        """Send a message to a Talk conversation using user API."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"

        data = {"message": message}
        if reply_to:
            data["replyTo"] = reply_to
        if reference_id:
            data["referenceId"] = reference_id

        logger.debug("Sending message to %s (%d chars)", conversation_token, len(message))
        client = await self._ensure_open()
        response = await client.post(
            url,
            auth=self.auth,
            headers=self._headers(json_body=True),
            json=data,
        )
        response.raise_for_status()
        return response.json()

    async def edit_message(
        self,
        conversation_token: str,
        message_id: int,
        message: str,
    ) -> dict:
        """Edit an existing message in a Talk conversation."""
        url = (
            f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat"
            f"/{conversation_token}/{message_id}"
        )

        logger.debug(
            "Editing message %d in %s (%d chars)",
            message_id, conversation_token, len(message),
        )
        client = await self._ensure_open()
        response = await client.put(
            url,
            auth=self.auth,
            headers=self._headers(json_body=True),
            json={"message": message},
        )
        response.raise_for_status()
        return response.json()

    async def create_conversation(
        self, name: str, room_type: int = 2,
    ) -> dict:
        """Create a Talk conversation (default roomType=2, a group room) owned
        by the configured account. Returns the new room's ``ocs.data`` dict
        (carrying its ``token``). Used by the web "Also open in Talk" promote."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"
        client = await self._ensure_open()
        response = await client.post(
            url,
            auth=self.auth,
            headers=self._headers(json_body=True),
            json={"roomType": room_type, "roomName": name[:200]},
        )
        response.raise_for_status()
        return response.json().get("ocs", {}).get("data", {})

    async def add_participant(
        self, conversation_token: str, participant: str, source: str = "users",
    ) -> dict:
        """Add a participant to a conversation (default source=users — a
        Nextcloud user id)."""
        url = (
            f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"
            f"/{conversation_token}/participants"
        )
        client = await self._ensure_open()
        response = await client.post(
            url,
            auth=self.auth,
            headers=self._headers(json_body=True),
            json={"newParticipant": participant, "source": source},
        )
        response.raise_for_status()
        return response.json().get("ocs", {}).get("data", {})

    async def rename_conversation(
        self, conversation_token: str, name: str,
    ) -> None:
        """Rename a Talk conversation (propagates a web room rename to Talk)."""
        url = (
            f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"
            f"/{conversation_token}"
        )
        client = await self._ensure_open()
        response = await client.put(
            url,
            auth=self.auth,
            headers=self._headers(json_body=True),
            json={"roomName": name[:200]},
        )
        response.raise_for_status()

    async def mark_conversation_read(self, conversation_token: str) -> bool:
        """Mark a whole conversation read for the authenticated identity.

        ``POST /chat/{token}/read`` with no ``lastReadMessage`` marks everything
        read (Talk capability ``chat-read-last``). Used with a user bearer token
        to sync the web UI's mark-read into Nextcloud Talk. Returns a success
        bool and never raises to callers — a Talk failure must not fail the web
        request that triggered it.
        """
        url = (
            f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat"
            f"/{conversation_token}/read"
        )
        try:
            client = await self._ensure_open()
            response = await client.post(
                url,
                auth=self.auth,
                headers=self._headers(),
                timeout=5,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.warning(
                "mark_conversation_read failed for %s: %s", conversation_token, e,
            )
            return False

    async def list_conversations(self) -> list[dict]:
        """List all conversations the user is part of."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"

        client = await self._ensure_open()
        response = await client.get(
            url,
            auth=self.auth,
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json().get("ocs", {}).get("data", [])

    async def poll_messages(
        self,
        conversation_token: str,
        last_known_message_id: int | None = None,
        timeout: int = 30,
        limit: int = 50,
    ) -> list[dict]:
        """
        Poll for messages in a conversation.

        If last_known_message_id is provided:
            Uses lookIntoFuture=1 for long-polling - blocks until new messages
            arrive or timeout is reached. Returns empty list on timeout (304).

        If last_known_message_id is None or 0:
            Fetches recent message history (lookIntoFuture=0). Returns messages
            in oldest-first order for processing.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"

        if last_known_message_id:
            # Normal long-poll for new messages
            params = {
                "lookIntoFuture": 1,
                "timeout": timeout,
                "limit": limit,
                "lastKnownMessageId": last_known_message_id,
            }
            request_timeout = timeout + 10
        else:
            # First poll - fetch recent history (non-blocking)
            params = {
                "lookIntoFuture": 0,
                "limit": limit,
            }
            request_timeout = 30  # standard timeout for history fetch

        client = await self._ensure_open()
        # Per-request timeout override: the persistent client's default is
        # DEFAULT_TIMEOUT, but a long-poll needs timeout+10 (history fetch 30).
        response = await client.get(
            url,
            auth=self.auth,
            headers=self._headers(),
            params=params,
            timeout=request_timeout,
        )
        # 304 means no new messages (timeout)
        if response.status_code == 304:
            return []
        response.raise_for_status()

        messages = response.json().get("ocs", {}).get("data", [])

        # History fetch returns newest-first, reverse for oldest-first processing
        if not last_known_message_id and messages:
            messages = list(reversed(messages))

        return messages

    async def get_latest_message_id(self, conversation_token: str) -> int | None:
        """
        Get the ID of the most recent message in a conversation.

        Used for initializing poll state without processing historical messages.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"

        client = await self._ensure_open()
        response = await client.get(
            url,
            auth=self.auth,
            headers=self._headers(),
            params={"lookIntoFuture": 0, "limit": 1},
        )
        response.raise_for_status()
        messages = response.json().get("ocs", {}).get("data", [])
        if messages:
            return messages[0].get("id")
        return None

    async def fetch_chat_history(
        self, conversation_token: str, limit: int = 100,
    ) -> list[dict]:
        """Fetch recent chat messages for context building.

        Returns up to ``limit`` messages in oldest-first order.
        Uses lookIntoFuture=0 (history fetch) without lastKnownMessageId
        to get the most recent messages.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"

        client = await self._ensure_open()
        response = await client.get(
            url,
            auth=self.auth,
            headers=self._headers(),
            params={"lookIntoFuture": 0, "limit": limit},
            timeout=30,
        )
        response.raise_for_status()
        messages = response.json().get("ocs", {}).get("data", [])
        # History fetch returns newest-first, reverse for oldest-first
        if messages:
            messages = list(reversed(messages))
        return messages

    async def get_participants(self, conversation_token: str) -> list[dict]:
        """Get participants of a conversation."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room/{conversation_token}/participants"

        client = await self._ensure_open()
        response = await client.get(
            url,
            auth=self.auth,
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json().get("ocs", {}).get("data", [])

    async def get_conversation_info(self, conversation_token: str) -> dict:
        """Get conversation metadata (displayName, type, etc.)."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room/{conversation_token}"

        client = await self._ensure_open()
        response = await client.get(
            url,
            auth=self.auth,
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json().get("ocs", {}).get("data", {})

    async def fetch_full_history(
        self, conversation_token: str, batch_size: int = 200,
    ) -> list[dict]:
        """Fetch complete message history by paginating backwards.

        Returns all messages in oldest-first order.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"
        all_messages: list[dict] = []
        last_known_id: int | None = None

        client = await self._ensure_open()
        while True:
            params: dict = {"lookIntoFuture": 0, "limit": batch_size}
            if last_known_id is not None:
                params["lastKnownMessageId"] = last_known_id

            response = await client.get(
                url,
                auth=self.auth,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            if response.status_code == 304:
                break
            response.raise_for_status()

            messages = response.json().get("ocs", {}).get("data", [])
            if not messages:
                break

            # API returns newest-first; collect all then reverse at end
            all_messages.extend(messages)
            # The last item in the batch (oldest) — go further back
            last_known_id = messages[-1]["id"]

            if len(messages) < batch_size:
                break

        # Reverse to oldest-first order
        all_messages.reverse()
        return all_messages

    async def fetch_messages_since(
        self, conversation_token: str, since_id: int, batch_size: int = 200,
    ) -> list[dict]:
        """Fetch messages newer than since_id by paginating forward.

        Returns messages in oldest-first order.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"
        all_messages: list[dict] = []
        current_id = since_id

        client = await self._ensure_open()
        while True:
            params = {
                "lookIntoFuture": 1,
                "timeout": 0,
                "limit": batch_size,
                "lastKnownMessageId": current_id,
            }

            response = await client.get(
                url,
                auth=self.auth,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            if response.status_code == 304:
                break
            response.raise_for_status()

            messages = response.json().get("ocs", {}).get("data", [])
            if not messages:
                break

            all_messages.extend(messages)
            current_id = messages[-1]["id"]

            if len(messages) < batch_size:
                break

        return all_messages

    async def download_attachment(
        self,
        file_path: str,
        local_path: str,
    ) -> None:
        """Download a file attachment from Nextcloud via WebDAV.

        Note: This only works for files in the bot user's own storage.
        For Talk attachments, files are automatically synced to the bot's
        Talk folder when the bot user is a conversation participant.
        """
        url = f"{self.base_url}/remote.php/webdav/{file_path.lstrip('/')}"

        client = await self._ensure_open()
        response = await client.get(url, auth=self.auth)
        response.raise_for_status()

        with open(local_path, "wb") as f:
            f.write(response.content)


def split_message(message: str, max_length: int = 4000) -> list[str]:
    """Split a message into chunks that fit Talk's character limit.

    Splits intelligently on paragraph boundaries (double newline), then single
    newlines, then sentence endings. Each part except the last gets a page
    indicator like "(1/3)".
    """
    if len(message) <= max_length:
        return [message]

    parts = []
    remaining = message

    while remaining:
        if len(remaining) <= max_length:
            parts.append(remaining)
            break

        # Reserve space for page indicator suffix like " (1/3)"
        # Use conservative estimate — 10 chars covers up to " (99/99)"
        effective_max = max_length - 10

        # Try splitting at paragraph boundary (double newline)
        chunk = remaining[:effective_max]
        split_pos = chunk.rfind("\n\n")

        # Try single newline if no paragraph break found
        if split_pos < effective_max // 2:
            split_pos = chunk.rfind("\n")

        # Try sentence boundary (. ! ?) followed by space or newline
        if split_pos < effective_max // 2:
            for sep in (". ", "! ", "? "):
                pos = chunk.rfind(sep)
                if pos >= effective_max // 2:
                    split_pos = pos + len(sep) - 1  # include the punctuation
                    break

        # Hard split as last resort
        if split_pos < effective_max // 2:
            split_pos = effective_max

        parts.append(remaining[:split_pos].rstrip())
        remaining = remaining[split_pos:].lstrip("\n")

    if len(parts) > 1:
        total = len(parts)
        parts = [f"{part} ({i + 1}/{total})" for i, part in enumerate(parts)]

    return parts


def truncate_message(message: str, max_length: int = 4000) -> str:
    """Truncate a message to fit Talk's limits, adding indicator if truncated.

    Deprecated: prefer split_message() for sending multiple parts.
    """
    if len(message) <= max_length:
        return message

    truncation_notice = "\n\n[Message truncated - full response available in task log]"
    return message[: max_length - len(truncation_notice)] + truncation_notice
