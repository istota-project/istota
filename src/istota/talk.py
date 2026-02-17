"""Nextcloud Talk API client."""

import logging

import httpx

from .config import Config

logger = logging.getLogger("istota.talk")


class TalkClient:
    """Client for Nextcloud Talk user API (not bot API)."""

    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.nextcloud.url.rstrip("/")
        self.auth = (config.nextcloud.username, config.nextcloud.app_password)

    async def send_message(
        self,
        conversation_token: str,
        message: str,
        reply_to: int | None = None,
    ) -> dict:
        """Send a message to a Talk conversation using user API."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{conversation_token}"

        data = {"message": message}
        if reply_to:
            data["replyTo"] = reply_to

        logger.debug("Sending message to %s (%d chars)", conversation_token, len(message))
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                auth=self.auth,
                headers={
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=data,
            )
            response.raise_for_status()
            return response.json()

    async def list_conversations(self) -> list[dict]:
        """List all conversations the user is part of."""
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v4/room"

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                auth=self.auth,
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
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

        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.get(
                url,
                auth=self.auth,
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                params=params,
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

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                auth=self.auth,
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                params={"lookIntoFuture": 0, "limit": 1},
            )
            response.raise_for_status()
            messages = response.json().get("ocs", {}).get("data", [])
            if messages:
                return messages[0].get("id")
            return None

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

        async with httpx.AsyncClient() as client:
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
