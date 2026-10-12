"""Read-only Drive directory listing for the workspace page."""
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
RECONNECT_MESSAGE = "Google rejected the stored credential. Reconnect in Settings."


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    modified: str | None
    size: int | None
    web_url: str | None
    is_folder: bool


class DriveError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def validate_folder(folder: str) -> None:
    if folder not in {"root", "shared"} and not DRIVE_ID.fullmatch(folder):
        raise DriveError(400, "Invalid Drive folder.")


def safe_web_url(value: str | None) -> str | None:
    if isinstance(value, str) and value.startswith(("https://drive.google.com/", "https://docs.google.com/")):
        return value
    return None


def list_folder(token: str, folder: str, page_token: str | None) -> tuple[list[DriveItem], str | None]:
    validate_folder(folder)
    query = "sharedWithMe=true" if folder == "shared" else f"'{folder}' in parents"
    params = {
        "q": f"{query} and trashed=false",
        "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink)",
        "pageSize": "200", "orderBy": "folder,name",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    try:
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            response = client.get("https://www.googleapis.com/drive/v3/files", params=params,
                                  headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            logger.warning("Google Drive listing failed status=%s", response.status_code)
            message = RECONNECT_MESSAGE if response.status_code == 401 else "Could not load Google Drive. Try again."
            raise DriveError(502, message)
        data = response.json()
        items = []
        for row in data.get("files", []):
            items.append(DriveItem(
                id=row["id"], name=row["name"], mime_type=row["mimeType"],
                modified=row.get("modifiedTime"),
                size=int(row["size"]) if "size" in row else None,
                web_url=safe_web_url(row.get("webViewLink")),
                is_folder=row["mimeType"] == "application/vnd.google-apps.folder",
            ))
        return items, data.get("nextPageToken")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("Google Drive listing failed type=%s", type(exc).__name__)
        raise DriveError(502, "Could not load Google Drive. Try again.") from exc


def parent_folder(token: str, file_id: str) -> str:
    if not DRIVE_ID.fullmatch(file_id):
        raise DriveError(400, "Invalid Drive file.")
    try:
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            response = client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"fields": "parents", "supportsAllDrives": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            logger.warning("Google Drive parent lookup failed status=%s", response.status_code)
            message = RECONNECT_MESSAGE if response.status_code == 401 else "Could not find the containing folder in Google Drive."
            raise DriveError(502, message)
        parents = response.json().get("parents", [])
        if not parents:
            raise DriveError(404, "Containing folder is unavailable.")
        if not isinstance(parents, list) or not isinstance(parents[0], str) or not DRIVE_ID.fullmatch(parents[0]):
            raise ValueError("invalid parent")
        return parents[0]
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Google Drive parent lookup failed type=%s", type(exc).__name__)
        raise DriveError(502, "Could not find the containing folder in Google Drive.") from exc
