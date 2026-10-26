"""Folders users shared with Istota over Open Cloud Mesh, reached over WebDAV.

A share is a `shared_sources` row of kind ``ocm_share`` that the user
accepted in Settings (`sharing.ocm.accept`); its bearer secret is the local
credential ``ocm_share_<id>``. This CLI runs host-side, spawned by the skill
proxy, and is the only task-side reader of a share's files.

**Reach.** A share is named ``share:<id>`` and resolved through
`sharing.sources.sources_for_task` on the calling task's own row, so a guest's
turn, a task nobody asked in a shared room, another user's id and an id that
does not exist all answer the same ``source_not_in_scope``.

**The secret** is read by name over the invocation's private credential
channel (``ISTOTA_CRED_FD``) and nowhere else: with no channel the CLI refuses
rather than asking the model-facing socket. One read per invocation, which is
why the verbs take several paths. The proxy applies the task's snapshot, the
fetch budget and, with the broker on, the credential grant. The secret goes to
the share's own verified WebDAV base only, as the Basic user with an empty
password, through `sandbox.guarded_http`.

**Access is read from the provider on every invocation.** Each invocation on a
share starts with `sharing.ocm_files.check_root`, the depth-0 PROPFIND of its
root that the Files page makes too, and refuses a write verb on a read share
with ``read_only_share`` before any write request. A 403 on a write re-reads
it and is reported the same way. A 401 or 404 on the root revokes the share.

**Remote paths** follow `sharing.ocm_files`' rules, checked before anything is
spent: no absolute path, no ``.`` or ``..``, no empty segment, no backslash
or control character, each segment percent-encoded on the wire, and every
``href`` in a listing reduced to a path under the share root or left out and
counted. The provider is the boundary; these checks keep a request from
naming anything but the share.

**A share of one file** (OCM offers a file and a folder alike) lists as that
one file at ``share:<id>``, named by the share, and ``get share:<id>``
downloads it under that name made safe for a filesystem.

**Names are third-party text**: every file and folder name, and the share's
own name and owner, is fenced as ``shared folder``. A listing's ``path`` is a
reference to pass back to this CLI and is not fenced.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from istota.sharing.ocm_files import (  # noqa: F401 -- re-exported, the skill's path rules
    LISTING_BODY,
    LISTING_MAX_BYTES,
    MAX_SEGMENT_CHARS,
    MAX_TRANSFER_BYTES,
    REQUEST_DEADLINE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    relative_parts,
    url_for,
    valid_segment,
)
from istota.skills._cli import parse_and_resolve, run_skill_cli
from istota.skills._hostpath import EGRESS, REMOTE, WRITE, host_path

logger = logging.getLogger("istota.skills.shares")

SCOPE = "shares"
LABEL = "shared folder"
PREFIX = "share:"
REPLY_MAX_BYTES = 64 * 1024

_REF = re.compile(r"\Ashare:([0-9]{1,18})(?::(.*))?\Z", re.DOTALL)
_REMOTE_NOTE = (
    "share:<id>[:<path>] names a path inside a folder share on the sharer's "
    "server, which scopes it; it is never a host path"
)

_MESSAGES = {
    "invalid_path": "That is not a usable share path. Use share:<id> or share:<id>:<path>, "
                    "with no leading slash and no . or .. segments.",
    "mixed_shares": "One call works on one share; name every path in the same share.",
    "source_not_in_scope": "There is no folder share by that name in reach of this task.",
    "credential_channel_unavailable": "The share's credential can only be read by a skill the "
                                      "proxy started; nothing was sent.",
    "secret_missing": "The share's credential was deleted, so the share was removed. "
                      "The user can ask the sharer to share the folder again.",
    "secret_not_in_task": "This share was accepted after the task started; it can be used "
                          "from the next task.",
    "share_not_granted": "The share's credential is not granted to this task "
                         "(Settings > Credentials decides where it may be used).",
    "credential_refused": "The share's credential could not be read for this task.",
    "credential_host_mismatch": "The share's credential is not bound to the share's server; "
                                "nothing was sent.",
    "share_revoked": "The sharer's server no longer accepts this share, so it was removed.",
    "read_only_share": "This folder is shared read-only.",
    "not_found": "Nothing exists at that path in the share.",
    "exists": "Something already exists there; pass --overwrite to replace it.",
    "not_a_folder": "That path is a file, not a folder.",
    "too_large": "The file is larger than the transfer limit.",
    "not_a_file": "A local source is not a regular file.",
    "confirmation_required": "Deleting from a share needs --confirmed; nothing was deleted.",
    "invalid_response": "The sharer's server answered something that is not WebDAV.",
}


class Refusal(Exception):
    """A refusal with a fixed code; ``extra`` joins the envelope."""

    def __init__(self, reason: str, message: str | None = None, **extra) -> None:
        super().__init__(message or _MESSAGES.get(reason) or
                         f"The sharer's server could not be used ({reason}).")
        self.reason = reason
        self.extra = extra


def _envelope(exc: Refusal) -> dict:
    return {"status": "error", "reason": exc.reason, "error": str(exc), **exc.extra}


def _fence(text: object) -> str:
    from istota.lib.untrusted import frame_untrusted

    return frame_untrusted("" if text is None else str(text), LABEL)


# ----------------------------------------------------------------- the paths

@dataclass(frozen=True)
class Ref:
    source_id: int
    parts: tuple[str, ...]

    def text(self, parts: tuple[str, ...] | None = None) -> str:
        path = "/".join(self.parts if parts is None else parts)
        return f"{PREFIX}{self.source_id}" + (f":{path}" if path else "")


def parse_ref(raw: object) -> Ref:
    """``share:<id>[:<path>]`` to a `Ref`, or `Refusal` ``invalid_path``."""
    match = _REF.fullmatch(raw) if isinstance(raw, str) else None
    if match is None:
        raise Refusal("invalid_path")
    path = match.group(2) or ""
    if path.startswith("/"):
        raise Refusal("invalid_path")
    if path.endswith("/"):
        path = path[:-1]
    parts = tuple(path.split("/")) if path else ()
    if not all(valid_segment(part) for part in parts):
        raise Refusal("invalid_path")
    return Ref(int(match.group(1)), parts)


def _one_share(raws) -> list[Ref]:
    refs = [parse_ref(raw) for raw in raws]
    if len({ref.source_id for ref in refs}) > 1:
        raise Refusal("mixed_shares")
    return refs


# ------------------------------------------------------------------ the task

def _withheld_from_env() -> set[str]:
    from istota.sandbox.host_paths import WITHHELD_SCOPES_VAR

    raw = os.environ.get(WITHHELD_SCOPES_VAR, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


@dataclass
class _Context:
    config: object
    db_path: Path
    user_id: str
    task_id: int
    skill_index: dict


def _context() -> _Context:
    """The calling task's config and database; `Refusal` where no share can be in reach."""
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    try:
        task_id = int(os.environ.get("ISTOTA_TASK_ID", "") or 0)
    except ValueError:
        task_id = 0
    if not user_id or task_id <= 0 or SCOPE in _withheld_from_env():
        raise Refusal("source_not_in_scope")

    from istota.config import load_config
    from istota.skills._loader import load_skill_index

    config = load_config()
    db_path = Path(os.environ.get("ISTOTA_DB_PATH") or config.db_path)
    if not db_path.exists():
        raise Refusal("source_not_in_scope")
    skill_index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    return _Context(config=config, db_path=db_path, user_id=user_id, task_id=task_id,
                    skill_index=skill_index)


def _shares_in_reach(context: _Context, conn) -> list:
    from istota import db
    from istota.sharing import sources

    task = db.get_task(conn, context.task_id)
    if task is None or task.user_id != context.user_id:
        return []
    return sources.sources_for_task(conn, task, kind="ocm_share", skill_index=context.skill_index)


def _source(context: _Context, source_id: int):
    from istota import db

    with db.get_db(context.db_path) as conn:
        for source in _shares_in_reach(context, conn):
            if source.id == source_id:
                return source
    raise Refusal("source_not_in_scope", f"There is no folder share {PREFIX}{source_id} "
                                         "in reach of this task.")


# ------------------------------------------------------------ the row's state

def _revoke(context: _Context, source, reason: str) -> None:
    from istota.sharing import ocm_files

    ocm_files.revoke(context.db_path, source, reason)


def _record_error(context: _Context, source, reason: str) -> None:
    from istota.sharing import ocm_files

    ocm_files.record_error(context.db_path, source, reason)


# ---------------------------------------------------------------- the secret

def _fetch_secret(context: _Context, source):
    """The share's secret, boxed, from the private channel only."""
    from istota.credentials.broker.bindings import credential_host
    from istota.sandbox.credential_shim import ProxyError, fetch_credential
    from istota.sharing import sources

    from .._credref import SecretValue

    fd = os.environ.get("ISTOTA_CRED_FD")
    if not fd:
        raise Refusal("credential_channel_unavailable")
    name = sources.credential_name(source.id)
    try:
        value, hosts = fetch_credential(name, "skill", binding=True, credential_fd=fd)
    except ProxyError as exc:
        reason = exc.reason
        if reason == "vault_credential_not_present":
            from istota import db
            from istota.credentials import local

            with db.get_db(context.db_path) as conn:
                stored = local.is_local(conn, context.user_id, name)
            if stored:
                raise Refusal("secret_not_in_task") from None
            _revoke(context, source, "secret_missing")
            raise Refusal("secret_missing") from None
        if reason in ("credential_not_granted", "credential_changed"):
            raise Refusal("share_not_granted") from None
        logger.info("shares source=%s credential refused reason=%s", source.id, reason)
        raise Refusal("credential_refused") from None
    try:
        bound = credential_host(f"https://{source.remote_host}")
    except ValueError:
        bound = None
    if bound is None or bound not in hosts:
        raise Refusal("credential_host_mismatch")
    return SecretValue(name, value, hosts)


# --------------------------------------------------------------- the session

@dataclass
class _Session:
    context: _Context
    source: object
    secret: object
    root: str
    client: object
    access: str = "read"
    root_kind: str = "folder"

    def close(self) -> None:
        self.client.close()


def _read_access(session: _Session) -> str:
    """The depth-0 PROPFIND of the root: records and returns the share's access."""
    from istota.sharing import ocm_files

    try:
        root = ocm_files.check_root(session.context.db_path, session.context.config,
                                    session.source, session.secret.reveal())
    except ocm_files.ShareRefused as exc:
        raise Refusal(exc.reason) from None
    session.access = root.access
    session.root_kind = root.kind
    return root.access


def _open(ref: Ref, *, write: bool) -> _Session:
    """Reach, secret and the root PROPFIND, in that order; a write checked against fresh access."""
    from istota.sandbox import guarded_http
    from istota.sharing import ocm_files

    context = _context()
    source = _source(context, ref.source_id)
    try:
        root = ocm_files.checked_root(source)
    except ocm_files.ShareRefused as exc:
        raise Refusal(exc.reason) from None
    secret = _fetch_secret(context, source)
    session = _Session(context=context, source=source, secret=secret, root=root,
                       client=guarded_http.new_client(timeout=REQUEST_TIMEOUT_SECONDS))
    try:
        if _read_access(session) == "read" and write:
            raise Refusal("read_only_share")
    except BaseException:
        session.close()
        raise
    return session


def _send(session: _Session, method: str, url: str, *, max_bytes: int = REPLY_MAX_BYTES,
          headers: dict | None = None, content: bytes | None = None):
    from istota.sandbox import guarded_http

    try:
        response = guarded_http.guarded_send(
            session.client, method, url,
            private_hosts=session.context.config.sharing.private_hosts,
            max_bytes=max_bytes, timeout=REQUEST_TIMEOUT_SECONDS,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
            auth=(session.secret.reveal(), ""), headers=headers, content=content,
        )
    except guarded_http.GuardRefused as exc:
        if exc.code != "too_large":
            _record_error(session.context, session.source, exc.code)
        raise Refusal(exc.code) from None
    if response.status_code == 401:
        _revoke(session.context, session.source, "share_revoked")
        raise Refusal("share_revoked")
    return response


def _write_refused(session: _Session):
    """A 403 on a write: the provider's access is read again, and the write reported refused."""
    _read_access(session)
    return Refusal("read_only_share")


def _status_refusal(status: int, mapping: dict[int, str]) -> Refusal:
    return Refusal(mapping.get(status, f"http_{status}"))


# ----------------------------------------------------------------- the verbs

def cmd_list(args) -> dict:
    from istota import db

    try:
        context = _context()
    except Refusal:
        return {"status": "ok", "shares": []}
    with db.get_db(context.db_path) as conn:
        found = _shares_in_reach(context, conn)
    return {
        "status": "ok",
        "shares": [
            {
                "share": f"{PREFIX}{source.id}",
                "host": source.remote_host,
                "access": source.access,
                "state": source.state,
                "last_reason": source.last_reason,
                "name": _fence(source.extras.get("name") or ""),
                "owner": _fence(source.extras.get("owner") or ""),
            }
            for source in found
        ],
    }


def _share_name(source) -> str:
    """The share's own name, one line: what a file share's one file is called."""
    name = source.extras.get("name")
    return " ".join(name.split()) if isinstance(name, str) else ""


def _entry(ref: Ref, parts: tuple[str, ...], props: dict, name: str | None = None) -> dict:
    from istota.sharing.ocm_files import entry_fields

    folder, size, modified = entry_fields(props)
    return {
        "name": _fence(parts[-1] if name is None else name),
        "path": ref.text(parts),
        "type": "folder" if folder else "file",
        "size": size,
        "modified": modified.isoformat() if modified is not None else None,
    }


def cmd_ls(args) -> dict:
    from istota.lib import dav_xml

    ref = parse_ref(args.target)
    session = _open(ref, write=False)
    try:
        folder = bool(ref.parts) or session.root_kind == "folder"
        response = _send(session, "PROPFIND", url_for(session.root, ref.parts, folder=folder),
                         max_bytes=LISTING_MAX_BYTES, content=LISTING_BODY,
                         headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if response.status_code != 207:
            raise _status_refusal(response.status_code, {404: "not_found"})
        try:
            rows = dav_xml.parse_multistatus(response.content)
        except dav_xml.DavXmlError:
            raise Refusal("invalid_response") from None
        entries, skipped = [], 0
        for href, props in rows:
            parts = relative_parts(session.root, href)
            if parts is None:
                skipped += 1
            elif parts == ref.parts:
                if not ref.parts and session.root_kind == "file":
                    entries.append(_entry(ref, parts, props, _share_name(session.source)))
                elif ref.parts and _entry(ref, parts, props)["type"] == "file":
                    entries.append(_entry(ref, parts, props))
            elif parts[:-1] == ref.parts:
                entries.append(_entry(ref, parts, props))
            else:
                skipped += 1
    finally:
        session.close()
    entries.sort(key=lambda entry: (entry["type"] != "folder", entry["path"]))
    return {"status": "ok", "share": ref.text(()), "access": session.access,
            "path": ref.text(), "entries": entries, "skipped": skipped}


def cmd_get(args) -> dict:
    from istota.sandbox.host_paths import write_resolved

    from istota.lib.filenames import safe_filename

    refs = _one_share(args.targets)
    whole = any(not ref.parts for ref in refs)
    if whole and len(refs) > 1:
        raise Refusal("invalid_path", "A share of one file is fetched on its own.")
    dest = Path(args.dest)
    for ref in refs:
        if ref.parts and (dest / ref.parts[-1]).exists() and not args.overwrite:
            raise Refusal("exists", path=ref.text())
    session = _open(refs[0], write=False)
    saved = []
    try:
        if whole and session.root_kind != "file":
            raise Refusal("invalid_path", "get takes file paths, not the share itself.")
        # A file share's one file is named by the share, which the sharer chose.
        names = {ref: ref.parts[-1] if ref.parts else
                 safe_filename(_share_name(session.source), fallback="shared-file")
                 for ref in refs}
        if whole and (dest / names[refs[0]]).exists() and not args.overwrite:
            raise Refusal("exists", path=refs[0].text())
        for ref in refs:
            response = _send(session, "GET", url_for(session.root, ref.parts),
                             max_bytes=MAX_TRANSFER_BYTES)
            if response.status_code != 200:
                raise _status_refusal(response.status_code, {404: "not_found"})
            target = dest / names[ref]
            try:
                write_resolved(target, response.content, exclusive=not args.overwrite)
            except FileExistsError:
                raise Refusal("exists", path=ref.text(), saved=saved) from None
            saved.append({"path": ref.text(), "saved": str(target), "bytes": len(response.content)})
    except Refusal as exc:
        exc.extra.setdefault("saved", saved)
        raise
    finally:
        session.close()
    return {"status": "ok", "share": refs[0].text(()), "saved": saved}


def _open_source(path: str) -> int:
    """A local source opened without following a link, checked as a regular file under the cap."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Refusal("not_a_file", path=path) from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise Refusal("not_a_file", path=path)
    if info.st_size > MAX_TRANSFER_BYTES:
        os.close(fd)
        raise Refusal("too_large", path=path)
    return fd


def _read_fd(fd: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_TRANSFER_BYTES:
            raise Refusal("too_large")
        chunks.append(chunk)


def cmd_put(args) -> dict:
    folder = parse_ref(args.to)
    names = [Path(path).name for path in args.sources]
    if not all(valid_segment(name) for name in names):
        raise Refusal("invalid_path", "A local file name cannot be used in the share.")
    opened: list[int] = []
    uploaded = []
    try:
        for path in args.sources:
            opened.append(_open_source(path))
        session = _open(folder, write=True)
        try:
            for fd, name in zip(opened, names):
                parts = folder.parts + (name,)
                headers = {} if args.overwrite else {"If-None-Match": "*"}
                response = _send(session, "PUT", url_for(session.root, parts),
                                 headers=headers, content=_read_fd(fd))
                if response.status_code == 403:
                    raise _write_refused(session)
                if response.status_code not in (200, 201, 204):
                    raise _status_refusal(response.status_code, {412: "exists", 409: "not_found"})
                uploaded.append(folder.text(parts))
        finally:
            session.close()
    except Refusal as exc:
        exc.extra.setdefault("uploaded", uploaded)
        raise
    finally:
        for fd in opened:
            os.close(fd)
    return {"status": "ok", "share": folder.text(()), "uploaded": uploaded}


def cmd_mkdir(args) -> dict:
    ref = parse_ref(args.target)
    if not ref.parts:
        raise Refusal("invalid_path", "mkdir takes a folder path inside the share.")
    session = _open(ref, write=True)
    try:
        response = _send(session, "MKCOL", url_for(session.root, ref.parts, folder=True))
        if response.status_code == 403:
            raise _write_refused(session)
        if response.status_code not in (200, 201):
            raise _status_refusal(response.status_code, {405: "exists", 409: "not_found"})
    finally:
        session.close()
    return {"status": "ok", "created": ref.text()}


def cmd_move(args) -> dict:
    source, target = _one_share([args.source, args.target])
    if not source.parts or not target.parts:
        raise Refusal("invalid_path", "move takes paths inside the share, not the share itself.")
    session = _open(source, write=True)
    try:
        headers = {"Destination": url_for(session.root, target.parts),
                   "Overwrite": "T" if args.overwrite else "F"}
        response = _send(session, "MOVE", url_for(session.root, source.parts), headers=headers)
        if response.status_code == 403:
            raise _write_refused(session)
        if response.status_code not in (200, 201, 204):
            raise _status_refusal(response.status_code,
                                  {412: "exists", 404: "not_found", 409: "not_found"})
    finally:
        session.close()
    return {"status": "ok", "moved": source.text(), "to": target.text()}


def cmd_delete(args) -> dict:
    refs = _one_share(args.targets)
    if any(not ref.parts for ref in refs):
        raise Refusal("invalid_path", "delete takes paths inside the share, not the share itself.")
    if not args.confirmed:
        raise Refusal("confirmation_required", would_delete=[ref.text() for ref in refs])
    session = _open(refs[0], write=True)
    deleted = []
    try:
        for ref in refs:
            response = _send(session, "DELETE", url_for(session.root, ref.parts))
            if response.status_code == 403:
                raise _write_refused(session)
            if response.status_code not in (200, 204):
                raise _status_refusal(response.status_code, {404: "not_found"})
            deleted.append(ref.text())
    except Refusal as exc:
        exc.extra.setdefault("deleted", deleted)
        raise
    finally:
        session.close()
    return {"status": "ok", "deleted": deleted}


# ------------------------------------------------------------------ the CLI

def _refusing(handler):
    def run(args):
        try:
            return handler(args)
        except Refusal as exc:
            return _envelope(exc)
    run.__name__ = handler.__name__
    return run


COMMANDS = {
    "list": _refusing(cmd_list),
    "ls": _refusing(cmd_ls),
    "get": _refusing(cmd_get),
    "put": _refusing(cmd_put),
    "mkdir": _refusing(cmd_mkdir),
    "move": _refusing(cmd_move),
    "delete": _refusing(cmd_delete),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shares", description="Folders users shared with Istota from their own server",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List the folder shares this task can reach")

    p = sub.add_parser("ls", help="List a folder in a share")
    host_path(p, "target", mode=REMOTE, note=_REMOTE_NOTE,
              help="share:<id> or share:<id>:<folder path>")

    p = sub.add_parser("get", help="Download files from a share")
    host_path(p, "targets", nargs="+", mode=REMOTE, note=_REMOTE_NOTE,
              help="share:<id>:<file path>, one or more, all in one share")
    host_path(p, "--dest", required=True, mode=WRITE,
              help="Local folder to save into (inside your workspace)")
    p.add_argument("--overwrite", action="store_true", help="Replace local files of the same name")

    p = sub.add_parser("put", help="Upload local files into a folder of a share")
    host_path(p, "sources", nargs="+", mode=EGRESS,
              help="Local files to upload (inside your workspace)")
    host_path(p, "--to", required=True, mode=REMOTE, note=_REMOTE_NOTE,
              help="share:<id> or share:<id>:<folder path> to upload into")
    p.add_argument("--overwrite", action="store_true", help="Replace remote files of the same name")

    p = sub.add_parser("mkdir", help="Create a folder in a share")
    host_path(p, "target", mode=REMOTE, note=_REMOTE_NOTE, help="share:<id>:<folder path>")

    p = sub.add_parser("move", help="Move or rename a file or folder within a share")
    host_path(p, "source", mode=REMOTE, note=_REMOTE_NOTE, help="share:<id>:<path>")
    host_path(p, "target", mode=REMOTE, note=_REMOTE_NOTE, help="share:<id>:<new path>")
    p.add_argument("--overwrite", action="store_true", help="Replace what is at the new path")

    p = sub.add_parser("delete", help="Delete files or folders from a share")
    host_path(p, "targets", nargs="+", mode=REMOTE, note=_REMOTE_NOTE,
              help="share:<id>:<path>, one or more, all in one share")
    p.add_argument("--confirmed", action="store_true", help="The user confirmed the deletion")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    run_skill_cli(COMMANDS, args)


if __name__ == "__main__":
    main()
