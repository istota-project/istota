"""Pin validation and the fenced personal prompt block."""

import os

from istota.google.drive import DRIVE_ID, safe_web_url
from istota.lib.untrusted import frame_untrusted
from istota.skills._loader import open_overlay_dir
from istota.webui.chat_files import ChatFileError, resolve_chat_file
from istota.workspace.listing import ListingError


def workspace_pin_kind(config, username, ref, roots) -> bool:
    """Return whether a readable reference is a directory; refuse otherwise."""
    try:
        resolve_chat_file(config, username, ref, roots=roots)
        return False
    except ChatFileError as exc:
        if exc.status != 400:
            raise ListingError(404, "File or folder not found.") from exc
    matching = [(view, root) for view, root in roots if ref == view or ref.startswith(view.rstrip("/") + "/")]
    if matching:
        view, root = max(matching, key=lambda item: len(item[0]))
        parts = ref[len(view):].lstrip("/").split("/") if ref != view else []
        try:
            fd = open_overlay_dir(root, *parts)
        except OSError:
            fd = None
        if fd is not None:
            os.close(fd)
            return True
    raise ListingError(404, "File or folder not found.")


def validate_pin(config, username, body, roots) -> tuple[dict, bool]:
    if (not isinstance(body, dict)
            or any(not isinstance(body.get(k), str) or not body[k].strip() for k in ("provider", "ref", "name"))
            or body["provider"] not in {"workspace", "drive"}
            or any(body.get(k) is not None and not isinstance(body[k], str) for k in ("mime_type", "web_url"))):
        raise ListingError(422, "Invalid pin.")
    ref = body["ref"]
    pin = {"provider": body["provider"], "ref": ref, "name": body["name"][:200],
           "mime_type": (body.get("mime_type") or "")[:100] or None, "web_url": None}
    if pin["provider"] == "workspace":
        if not ref.startswith("/") or any(part in {"", ".", ".."} for part in ref[1:].split("/")):
            raise ListingError(404, "File or folder not found.")
        is_dir = workspace_pin_kind(config, username, ref, roots)
    else:
        if not DRIVE_ID.fullmatch(ref):
            raise ListingError(400, "Invalid Drive id.")
        pin["web_url"] = safe_web_url(body.get("web_url"))
        is_dir = pin["mime_type"] == "application/vnd.google-apps.folder"
    return pin, is_dir


def pins_prompt_block(pins: list[dict]) -> str:
    if not pins:
        return ""
    lines = []
    for pin in pins:
        if pin["provider"] == "workspace":
            lines.append(f"- {pin['ref']}")
        else:
            mime = pin.get("mime_type")
            kind = {"application/vnd.google-apps.spreadsheet": "Google Sheets",
                    "application/vnd.google-apps.document": "Google Docs",
                    "application/vnd.google-apps.presentation": "Google Slides",
                    "application/vnd.google-apps.folder": "folder"}.get(mime, mime)
            lines.append(f"- Drive: {pin['name']} (id {pin['ref']}{', ' + kind if kind else ''})")
    return ("\n## Pinned files\n\n"
            "Files the user pinned (ask before assuming their contents; read them with the available tools):\n\n"
            + frame_untrusted("\n".join(lines), "pinned files") + "\n\n")
