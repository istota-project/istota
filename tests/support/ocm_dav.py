"""A stdlib WebDAV double of one Open Cloud Mesh share root, on loopback.

It serves one share root at Nextcloud's public WebDAV base and answers only
Basic auth whose user is the share secret, with an empty password, as
Nextcloud 30 does. The `shares` skill's tests and the Files page's share
browsing tests both drive it, so the two readers of a share are held to one
server's behaviour. `127.0.0.1` is a `sharing` grant in `[private_hosts]` in both.
"""

from __future__ import annotations

import base64
import threading
from email.utils import formatdate
from urllib.parse import quote, unquote, urlsplit

from istota.credentials import local
from istota.sharing import sources

SECRET = "synthetic-share-secret-41c9"
BASE = "/public.php/webdav/"


# --------------------------------------------------------------------------- #
# The WebDAV double
# --------------------------------------------------------------------------- #


def _prop_xml(rel: str, *, folder: bool, size: int | None, permissions: str) -> str:
    href = BASE + "/".join(quote(part) for part in rel.split("/") if part)
    if folder and rel:
        href += "/"
    kind = "<d:collection/>" if folder else ""
    length = f"<d:getcontentlength>{size}</d:getcontentlength>" if size is not None else ""
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<d:resourcetype>{kind}</d:resourcetype>{length}"
        f"<d:getlastmodified>{formatdate(1700000000, usegmt=True)}</d:getlastmodified>"
        f"<oc:permissions>{permissions}</oc:permissions>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


class Dav:
    """One share root, as Nextcloud's public WebDAV endpoint serves it.

    ``files`` maps a path under the root to its bytes and ``dirs`` holds the
    folders, ``""`` being the root. ``permissions`` is the root's
    ``oc:permissions``. ``forbid_writes`` answers every write 403 and, as a
    permission change on the provider would, turns ``permissions`` to ``RG``.
    ``extra_hrefs`` are raw hrefs added to every listing. A share of one file
    is ``files = {"": data}`` with ``dirs`` empty. A GET honours one
    ``Range`` and answers ``content_type``. Every request is recorded with the
    Basic user it carried.
    """

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {""}
        self.permissions = "RGDNVCK"
        self.revoked = False
        self.root_status: int | None = None
        self.forbid_writes = False
        self.extra_hrefs: list[str] = []
        self.content_type = "application/octet-stream"
        # A status a PROPFIND of a path below the root answers instead of a listing.
        self.statuses: dict[str, int] = {}
        self.received: list[dict] = []
        dav = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, payload=b"", content_type=None):
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            def _begin(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                scheme, _, encoded = (headers.get("authorization") or "").partition(" ")
                user = password = None
                if scheme.lower() == "basic":
                    user, _, password = base64.b64decode(encoded).decode().partition(":")
                dav.received.append({"method": self.command, "path": self.path,
                                     "headers": headers, "body": body, "user": user})
                if not self.path.startswith(BASE):
                    self._answer(404)
                    return None
                if dav.revoked or user != SECRET or password != "":
                    self._answer(401)
                    return None
                return unquote(self.path[len(BASE):]).strip("/"), headers, body

            def _forbidden(self) -> bool:
                if dav.forbid_writes or not set("CKW") & set(dav.permissions):
                    dav.permissions = "RG"
                    self._answer(403)
                    return True
                return False

            def do_PROPFIND(self):
                begun = self._begin()
                if begun is None:
                    return
                rel, headers, _ = begun
                if rel == "" and dav.root_status is not None:
                    return self._answer(dav.root_status)
                if rel in dav.statuses:
                    return self._answer(dav.statuses[rel])
                if rel in dav.files:
                    parts = [_prop_xml(rel, folder=False, size=len(dav.files[rel]),
                                       permissions=dav.permissions)]
                elif rel in dav.dirs:
                    parts = [_prop_xml(rel, folder=True, size=None, permissions=dav.permissions)]
                    if headers.get("depth") == "1":
                        for path in sorted(dav.dirs):
                            if path and path.rpartition("/")[0] == rel:
                                parts.append(_prop_xml(path, folder=True, size=None,
                                                       permissions=dav.permissions))
                        for path, data in sorted(dav.files.items()):
                            if path.rpartition("/")[0] == rel:
                                parts.append(_prop_xml(path, folder=False, size=len(data),
                                                       permissions=dav.permissions))
                        for href in dav.extra_hrefs:
                            parts.append(
                                f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                                "<d:resourcetype/></d:prop>"
                                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
                else:
                    return self._answer(404)
                payload = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                           'xmlns:oc="http://owncloud.org/ns">' + "".join(parts)
                           + "</d:multistatus>").encode()
                self._answer(207, payload, "application/xml; charset=utf-8")

            def do_GET(self):
                begun = self._begin()
                if begun is None:
                    return
                rel, headers, _ = begun
                if rel not in dav.files:
                    return self._answer(404)
                data = dav.files[rel]
                wanted = headers.get("range", "")
                if wanted.startswith("bytes=") and "-" in wanted:
                    first, _, last = wanted[6:].partition("-")
                    start = int(first or 0)
                    end = min(int(last), len(data) - 1) if last else len(data) - 1
                    self.send_response(206)
                    self.send_header("Content-Type", dav.content_type)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    self.wfile.write(data[start:end + 1])
                    return
                self._answer(200, data, dav.content_type)

            def do_PUT(self):
                begun = self._begin()
                if begun is None or self._forbidden():
                    return
                rel, headers, body = begun
                if rel.rpartition("/")[0] not in dav.dirs:
                    return self._answer(409)
                if headers.get("if-none-match") == "*" and rel in dav.files:
                    return self._answer(412)
                created = rel not in dav.files
                dav.files[rel] = body
                self._answer(201 if created else 204)

            def do_MKCOL(self):
                begun = self._begin()
                if begun is None or self._forbidden():
                    return
                rel = begun[0]
                if rel in dav.dirs or rel in dav.files:
                    return self._answer(405)
                if rel.rpartition("/")[0] not in dav.dirs:
                    return self._answer(409)
                dav.dirs.add(rel)
                self._answer(201)

            def do_MOVE(self):
                begun = self._begin()
                if begun is None or self._forbidden():
                    return
                rel, headers, _ = begun
                target = urlsplit(headers.get("destination") or "")
                if (target.netloc != f"127.0.0.1:{dav.port}"
                        or not target.path.startswith(BASE)):
                    return self._answer(502)
                to = unquote(target.path[len(BASE):]).strip("/")
                if headers.get("overwrite") == "F" and (to in dav.files or to in dav.dirs):
                    return self._answer(412)
                if rel in dav.files:
                    dav.files[to] = dav.files.pop(rel)
                elif rel in dav.dirs and rel:
                    dav.dirs = {to + p[len(rel):] if p == rel or p.startswith(rel + "/") else p
                                for p in dav.dirs}
                    dav.files = {(to + p[len(rel):] if p.startswith(rel + "/") else p): v
                                 for p, v in dav.files.items()}
                else:
                    return self._answer(404)
                self._answer(201)

            def do_DELETE(self):
                begun = self._begin()
                if begun is None or self._forbidden():
                    return
                rel = begun[0]
                if rel in dav.files:
                    del dav.files[rel]
                elif rel in dav.dirs and rel:
                    dav.dirs = {p for p in dav.dirs if p != rel and not p.startswith(rel + "/")}
                    dav.files = {p: v for p, v in dav.files.items() if not p.startswith(rel + "/")}
                else:
                    return self._answer(404)
                self._answer(204)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def methods(self) -> list[str]:
        return [r["method"] for r in self.received]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def seed_share(conn, dav, *, user="alice", key="1", access="write", name="Taxes 2026",
                credential=True) -> int:
    source_id = sources.create_source(
        conn, user_id=user, kind="ocm_share", state="pending", remote_host=dav.host,
        remote_key=f"{dav.host}|{key}", access=access,
        extras={"name": name, "owner": f"carol@http://{dav.host}", "provider_id": key,
                "webdav": f"http://{dav.host}{BASE}", "share_path": ""},
    )
    if credential:
        local.create(
            conn, user,
            local.LocalCredential(name=sources.credential_name(source_id), value=SECRET,
                                  url=f"https://{dav.host}"),
            access={"scope_mode": "all", "allow_scheduled": False},
            system_prefix="ocm_share_",
        )
    sources.set_state(conn, source_id, user, "active")
    return source_id
