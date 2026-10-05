"""Persist WhatsApp self-sends from the trusted host-side skill process.

Questions to other users are the `relay` skill's; this one only ever addresses
the caller's own binding.
"""

import argparse
import os
from pathlib import Path
from urllib.parse import quote

from .._cli import parse_and_resolve, run_skill_cli
from .._hostpath import EGRESS, host_path


def with_file(text: str, path: str) -> str:
    """`text` with `path` appended as an embedded `/chat/files` image.

    The send path reads the embed back out and sends the file as an image
    (ISSUE-639), resolving it against the caller's workspace with the
    `/chat/files` rule. So the file has to be in that workspace: the deferred
    directory passes the `EGRESS` stamp but is swept and is not served, and is
    refused here rather than at send time, where the image would silently
    become its file name.
    """
    from istota.relay.requests import RequestError
    from istota.sandbox.host_paths import user_workspace_root

    root = user_workspace_root()
    user = os.environ.get("ISTOTA_USER_ID", "")
    if root is None or not user:
        raise RequestError("file_not_in_workspace")
    try:
        relative = Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        raise RequestError("file_not_in_workspace") from None
    if not relative.parts:
        raise RequestError("file_not_in_workspace")
    # `_` is encoded too: the WhatsApp renderer turns `__` into `_` on every
    # line, which would point the stored embed at a different file.
    workspace_path = quote(
        f"/Users/{user}/{relative.as_posix()}", safe="",
    ).replace("_", "%5F")
    label = "".join(
        " " if ch in "[]" or not ch.isprintable() else ch for ch in relative.name
    ).strip() or "image"
    embed = f"![{label}](/istota/api/chat/files?path={workspace_path})"
    return f"{text}\n\n{embed}" if text else embed


def _dispatch(args):
    from ... import db
    from ...config import load_config
    from istota.relay.requests import enqueue_self_send, get_request, RequestError

    actor = os.environ.get("ISTOTA_USER_ID", "")
    task = os.environ.get("ISTOTA_TASK_ID", "")
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not actor or not task.isdecimal() or not path:
        raise RequestError("task_unavailable")
    with db.get_db(Path(path)) as conn:
        owned_task = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND user_id=? AND status='running'",
            (int(task), actor),
        ).fetchone()
        if owned_task is None:
            raise RequestError("task_unavailable")
        if args.command == "send":
            text = args.text
            if args.file:
                text = with_file(text, args.file)
            return enqueue_self_send(conn, load_config(), actor_user_id=actor, task_id=int(task),
                                     request_key=args.request_key, text=text)
        row = get_request(conn, actor_user_id=actor, request_id=args.request_id)
        # A relay question's status is behind the relay skill's private-origin
        # gate; answering it here would skip that gate.
        if row is None or row["kind"] != "self_send":
            raise RequestError("request_unavailable")
        return {"status": "ok", "request": row}


def build_parser():
    parser = argparse.ArgumentParser(description="Send WhatsApp messages to yourself")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send")
    send.add_argument("--request-key", required=True)
    host_path(send, "--file", mode=EGRESS, help="An image in your workspace to send with the text")
    send.add_argument("text", nargs="?", default="")
    status = commands.add_parser("status")
    status.add_argument("request_id")
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    return run_skill_cli({name: _dispatch for name in ("send", "status")}, args)
