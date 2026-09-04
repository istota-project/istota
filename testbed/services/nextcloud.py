"""The full shape's own Nextcloud, as a `Service`.

The odd member of the registry, and worth being explicit about: `attach` starts
nothing. `docker-compose.yml` already runs `nextcloud:30-apache`, provisioned by
the shipped `provision-nc.sh`, and this binds an admin client to it and
implements `Service` over the result. Modelling it as a service anyway means the
fixture, the profile list and `diagnostics` need no special case for the one
member that is a real server.

Two consequences of it being real rather than a stub, both stated rather than
worked around:

- `config_env()` is **empty**, and that is not an oversight. `docker-compose.yml`
  already points the daemon at `http://nextcloud` and `entrypoint.sh` derives
  the app password from `BOT_PASSWORD`. A service inventing an `ISTOTA_*`
  variable to announce its own presence would be the fixture side-loading
  config, which is the property this tier is built to keep. What does vary with
  the profile — whether Talk is on at all — is `FULL_MODULE_SWITCHES`' job.
- `reset()` deletes the Talk rooms this object created, and claims nothing
  else. A real server cannot be restored to a byte-identical state the way a
  truncate would, and a `reset` that attempts completeness and half-succeeds is
  worse than one whose limits are written down. The limits are in its docstring,
  enumerated rather than gestured at, because the deployment itself creates most
  of what a naive reset would try to remove.

**Where this talks to Nextcloud, and why there are two channels.** Most reads go
over HTTP through nginx on the ephemeral host port, because that is a real
round trip through the deployment's own front door. Three do not: the enabled
apps, the external-storage mounts and the registered OAuth2 clients have no HTTP
API in stock Nextcloud — `provision-nc.sh:94-96` says so about the last one, and
it is why that script reaches for `php` rather than `occ`. Those go through
`docker compose exec` into the `nextcloud` container, which is also what makes
them able to answer the question the provisioning suite is really asking: every
`occ` call in that script is `|| true`, so the script writes its flag and
reports success having done nothing at all.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

#: The address a process *inside* the compose network reaches Nextcloud on.
#: `docker-compose.yml` sets `NC_INTERNAL_URL: http://nextcloud` and the
#: nextcloud service publishes no port of its own — nginx fronts it.
CONTAINER_URL = "http://nextcloud"

#: How long one OCS call may take. Generous, because a freshly installed
#: Nextcloud is still warming its opcache and the alternative to a slow answer
#: is a flake.
TIMEOUT = 30

#: How long a *connection* to the stack's nginx may keep failing before a read
#: gives up. Not the request timeout: this covers nginx not being up yet.
CONNECT_RETRY_SECONDS = 60

#: The user ids the full shape provisions, and the defaults `attach` uses.
#: They mirror `stack.FULL_IDENTITY`, which is what compose is actually given;
#: the two cannot import each other (stack imports services), so a unit test
#: pins them together instead of a shared constant nobody could place.
DEFAULT_ADMIN_USER = "admin"
DEFAULT_BOT_USER = "istota"
DEFAULT_TEST_USER = "testuser"

#: Talk's conversation types. 2 is a group room — not 3, which is public and
#: therefore joinable by anyone holding its token.
ROOM_TYPE_GROUP = 2

#: The bot's `files_external` mount point (`provision-nc.sh:56`), which is what
#: a WebDAV path under `/mnt/shared` is prefixed with in the *bot's* tree. The
#: human user's mount is a second one, named after `ISTOTA_BOT_NAME` rather than
#: written down, so there is no constant for it: a test asserting on that tree
#: derives the name from the running config, since a hardcoded one would pass
#: for the wrong reason after a rename.
#:
#: Neither mount point is `/Users/...`, which is both the shape `storage.py`
#: writes on disk and the shape `resolve_scoped_path` produces — see `files()`.
BOT_MOUNT_POINT = "Shared Files"

_HREF = re.compile(r"<d:href>([^<]*)</d:href>")

_PROPFIND_BODY = (
    b'<?xml version="1.0"?>\n'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
    b"<d:getcontentlength/></d:prop></d:propfind>\n"
)


class NextcloudError(RuntimeError):
    """Nextcloud answered, and the answer was not one a caller can use."""


@dataclass(frozen=True)
class OcsResponse:
    status_code: int
    message: str
    data: Any


def attach(
    *,
    base_url: str,
    admin_password: str,
    bot_password: str,
    test_password: str,
    admin_user: str = DEFAULT_ADMIN_USER,
    bot_user: str = DEFAULT_BOT_USER,
    test_user: str = DEFAULT_TEST_USER,
) -> NextcloudService:
    """Bind to the running Nextcloud. Starts nothing.

    `base_url` is the *host*-side address — `http://localhost:<NC_PORT>`, which
    is nginx — rather than `http://nextcloud`, which resolves only inside the
    compose network. The distinction matters and is easy to get backwards:
    `container_url` is what the daemon uses and what this object reports to the
    fixture; `base_url` is what this object itself uses.

    All three passwords, because "which rooms exist" has no admin-side answer in
    Talk: the question is always "which rooms does *this* user see", and both
    the bot and the human user are subjects of the provisioning assertions.
    """
    return NextcloudService(
        base_url=base_url.rstrip("/"),
        admin_user=admin_user,
        admin_password=admin_password,
        bot_user=bot_user,
        bot_password=bot_password,
        test_user=test_user,
        test_password=test_password,
    )


class NextcloudService:
    """`Service` over the full stack's own Nextcloud container."""

    name = "nextcloud"

    def __init__(
        self,
        *,
        base_url: str,
        admin_user: str,
        admin_password: str,
        bot_user: str,
        bot_password: str,
        test_user: str,
        test_password: str,
    ) -> None:
        self.base_url = base_url
        self.admin_user = admin_user
        self.bot_user = bot_user
        self.test_user = test_user
        # Never public attributes. This object reaches a `Stack`, and a `Stack`
        # reaches a pytest failure report; three generated passwords rendered
        # into one on a public repo is the same class of leak the pre-commit
        # hook exists for. `_passwords` is looked up by user id rather than
        # threaded through every call site so no caller ever holds one.
        self._passwords = {
            admin_user: admin_password,
            bot_user: bot_password,
            test_user: test_password,
        }
        self._stack = None
        #: Talk rooms `create_room` handed out, and the actor that owns each.
        #: This list *is* `reset`'s scope: a room whose token this object never
        #: returned is the profile's baseline and is left alone.
        self._created_rooms: list[tuple[str, str]] = []

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return f"NextcloudService(base_url={self.base_url!r}, <3 redacted>)"

    # -- the protocol -----------------------------------------------------

    @property
    def container_url(self) -> str:
        """`http://nextcloud` — the compose service name, not the host port."""
        return CONTAINER_URL

    def config_env(self) -> dict[str, str]:
        """Nothing. See the module docstring; this is deliberate."""
        return {}

    def reset(self) -> None:
        """Delete the Talk rooms this object created. That is the whole scope.

        **Measured, not assumed** (Stage 5's first task, spec Open question 3).
        `DELETE /ocs/v2.php/apps/spreed/api/v4/room/{token}` as the room's owner
        answers 200 and the room then disappears from *both* participants'
        listings, taking its messages and the invite notification it raised with
        it. So the spec's fallback — a per-test room-name prefix, with every
        assertion scoped to it — is not needed, and "no room was created" stays
        an assertion a scenario can make.

        **What is deliberately outside the scope, and why none of it needs
        undoing.** A real server cannot be restored to a byte-identical state
        the way a truncate would, and a `reset` that attempts completeness and
        half-succeeds is worse than one whose limits are written down.

        - *The four rooms the boot made.* `entrypoint.sh:229-315` creates a 1:1
          plus `#general`, `#logs` and `#alerts`, seeds `CHANNEL.md` for
          `#general`, and posts an intro message into `#alerts`. Talk adds two
          of its own per account (`Talk updates`, `Note to self`). All of it is
          the profile's **baseline**: the daemon polls those four for the whole
          session and writes its execution log and confirmation traffic into two
          of them, so `rooms()` and `messages()` see a great deal no scenario
          created. A scenario asserts on a room it made, never on a count.
        - *Chat traffic in those rooms.* Talk's message delete is time-bounded
          and leaves a tombstone, so undoing it is neither possible nor useful.
        - *Files under `/mnt/shared`.* Nothing is cleared, and on this shape
          that is a decision rather than an omission — `.istota-provisioned`
          lives there and `entrypoint.sh` sources it at every boot, so a
          wholesale clear would break the stack rather than reset it. A storage
          scenario writes under a name it generated and asserts on that name,
          which is the same discipline `Probe.rows_above` enforces for tables.
        - *Shares and files in the bot's own Nextcloud home.* Same rule: a
          unique name per test, and `list_shares(path=…)` is path-scoped, so a
          leftover share is invisible to every other scenario.

        Deleting is best-effort about a room that is *already* gone (a scenario
        may have deleted its own) and loud about anything else: a room that
        survives a reset is a cross-test dependency, and those get diagnosed as
        flake rather than as leakage.

        **Three things about the failure path, each of which was wrong in the
        first version and each of which fails in the same direction.** Every
        room is attempted, so one bad room does not hide the rest. The list is
        cleared whatever happens, because a reset that left it populated would
        re-attempt the same delete before *every* remaining test in the session
        and report the first test's problem against all of them. And a 404 is
        confirmed rather than believed: Talk answers 404 for a room the actor
        can no longer see as well as for one that is gone, so the token is
        looked for in the bot's own room list before it is written off.

        `reset` runs *before* each test rather than after, so the last test of a
        session leaves its rooms behind. That is deliberate — a failed test's
        state is still there to inspect — and it leaks nothing, because the
        stack is torn down with its volumes at session end and `tests/full/`
        refuses to run under `ISTOTA_TESTBED_KEEP` at all.
        """
        failures = []
        unconfirmed = []
        try:
            for token, actor in list(self._created_rooms):
                try:
                    answer = self._ocs(
                        f"/ocs/v2.php/apps/spreed/api/v4/room/{token}",
                        user=actor,
                        method="DELETE",
                        tolerate=(404,),
                    )
                except NextcloudError as exc:
                    # OCS v2 reflects its status in the HTTP status, so a
                    # refusal arrives here as an exception rather than as an
                    # `OcsResponse` — which is why this is a `try` and not an
                    # `if` on the status code.
                    failures.append(f"{token}: {exc}")
                    continue
                if answer.status_code == 404:
                    unconfirmed.append((token, actor))
                elif answer.status_code not in (100, 200):
                    failures.append(
                        f"{token}: {answer.status_code} {answer.message}"
                    )
        finally:
            self._created_rooms.clear()
        failures.extend(self._still_visible(unconfirmed))
        if failures:
            raise NextcloudError(
                "these rooms survived the reset, so the next test would see "
                f"them: {failures}"
            )

    def _still_visible(self, rooms: list[tuple[str, str]]) -> list[str]:
        """Of the rooms that answered 404, the ones the bot can still see.

        Talk answers 404 both for a room that is gone and for one the actor is
        no longer a participant of, and only the first is what `reset` wants to
        conclude. The bot is in every room a scenario made — that is what makes
        the room pollable in the first place — so its own listing settles it.
        One call, and only when something 404'd.
        """
        if not rooms:
            return []
        try:
            visible = {room.get("token", "") for room in self.rooms()}
        except NextcloudError as exc:  # pragma: no cover - diagnostic
            return [f"could not confirm {[t for t, _ in rooms]} were deleted: {exc}"]
        return [
            f"{token}: answered 404 but the bot still sees it"
            for token, _ in rooms
            if token in visible
        ]

    def close(self) -> None:
        """Nothing to stop. The container belongs to the stack."""

    def describe(self) -> str:
        """For `Stack.diagnostics`. Cheap, and never raises.

        A failed full-shape scenario is expensive to reproduce, so this reports
        what it can and says what it could not reach rather than replacing a
        test failure with an error from the diagnostic.
        """
        lines = [f"rooms this test created: {[t for t, _ in self._created_rooms]}"]
        for label, reader in (
            ("users", self.users),
            ("apps", self.enabled_apps),
        ):
            try:
                lines.append(f"{label}: {sorted(reader())}")
            except Exception as exc:  # pragma: no cover - diagnostic
                lines.append(f"{label}: unavailable ({exc})")
        for token, _ in self._created_rooms:
            try:
                recent = [
                    f"{row.get('actorId')}: {(row.get('message') or '')[:60]}"
                    for row in self.messages(token, limit=10)
                ]
                lines.append(f"{token}: {recent}")
            except Exception as exc:  # pragma: no cover - diagnostic
                lines.append(f"{token}: unreadable ({exc})")
        return "\n".join(lines)

    # -- wiring -----------------------------------------------------------

    def bind_stack(self, stack) -> None:
        """Hand this service the stack it is attached to.

        Called by `StackPool` once the containers are up, because the three
        readers that need `occ` need a way into the `nextcloud` container and
        `services.build` runs before any container exists. One hook rather than
        threading an exec callable through a factory signature that five other
        services have no use for.
        """
        self._stack = stack

    # -- reading it back over HTTP ----------------------------------------

    def _ocs(
        self,
        path: str,
        *,
        user: str = "",
        method: str = "GET",
        body: dict | None = None,
        tolerate: tuple[int, ...] = (),
    ) -> OcsResponse:
        """One OCS call as admin, or as whoever the caller names.

        The `OCS-APIRequest` header is not optional: without it Nextcloud
        answers a 401 with an HTML login page, which reads as bad credentials.

        **Everything else non-2xx raises**, and that is not the shape it looks
        like from the `OcsResponse` return type. OCS v2 reflects the OCS status
        in the HTTP status — which is why `src/istota/nextcloud/_http.py:213`
        has an explicit `http_status >= 400` branch — so a refusal arrives here
        as an `HTTPError` and leaves as a `NextcloudError`. A caller that means
        to branch on a status has to name it in `tolerate`; the status-code
        guards elsewhere in this file only fire for the narrower case of a 2xx
        carrying a non-2xx OCS status.

        `tolerate` is for the two statuses that are answers rather than
        failures: 404, when `reset` deletes a room a scenario already deleted,
        and 304, which is how Talk says a room has no messages.
        """
        actor = user or self.admin_user
        if actor not in self._passwords:
            raise NextcloudError(
                f"no password for {actor!r}; this service knows "
                f"{sorted(self._passwords)}"
            )
        url = f"{self.base_url}{path}"
        separator = "&" if "?" in path else "?"
        payload_bytes = json.dumps(body).encode() if body is not None else None
        request = Request(f"{url}{separator}format=json", method=method,
                          data=payload_bytes)
        request.add_header("OCS-APIRequest", "true")
        if payload_bytes is not None:
            request.add_header("Content-Type", "application/json")
        _add_basic_auth(request, actor, self._passwords[actor])

        # Retried on a *connection* error only, only for a bounded window, and
        # only for a read. These calls do not reach Nextcloud directly — they go
        # through the stack's nginx, which is not in the tier's readiness set
        # because it legitimately restarts while its `web` upstream is coming
        # up. So the first call after a boot can land on a moment when nothing
        # is listening on the published port, and an unretried failure arrives
        # as whichever assertion happened to run first, saying "connection
        # refused" about Nextcloud.
        #
        # An `HTTPError` is *not* retried: that is Nextcloud answering, and
        # answering wrongly is what the caller wants told. Neither is a write,
        # and that restriction arrived with the write verbs: a `URLError` can be
        # raised after the request reached the server, so replaying a POST is
        # how you get a second room whose token nothing recorded — which `reset`
        # then cannot delete — or a duplicate inbound message, which turns
        # "exactly one reply" into a flake. A write that cannot connect fails
        # immediately and says so.
        retryable = method in ("GET", "PROPFIND")
        deadline = time.monotonic() + (CONNECT_RETRY_SECONDS if retryable else 0)
        while True:
            try:
                with urlopen(request, timeout=TIMEOUT) as response:
                    payload = json.loads(response.read() or b"{}")
                break
            except HTTPError as exc:
                if exc.code in tolerate:
                    return OcsResponse(
                        status_code=exc.code, message=exc.reason or "", data=None
                    )
                raise NextcloudError(
                    f"{method} {path} answered HTTP {exc.code}: "
                    f"{exc.read()[:400].decode('utf-8', 'replace')}"
                ) from None
            except URLError as exc:
                if time.monotonic() >= deadline:
                    waited = (
                        f"within {CONNECT_RETRY_SECONDS}s" if retryable
                        else "and a write is not retried"
                    )
                    raise NextcloudError(
                        f"{method} {path} never connected {waited} (the "
                        f"stack's nginx, on {self.base_url}): {exc}"
                    ) from None
                time.sleep(1.0)
            except ValueError as exc:
                raise NextcloudError(
                    f"{method} {path} did not answer JSON: {exc}"
                ) from None

        meta = (payload.get("ocs") or {}).get("meta") or {}
        return OcsResponse(
            status_code=int(meta.get("statuscode", 0)),
            message=str(meta.get("message", "")),
            data=(payload.get("ocs") or {}).get("data"),
        )

    def users(self) -> list[str]:
        """Every user id the instance knows, through the provisioning API."""
        answer = self._ocs("/ocs/v2.php/cloud/users")
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"listing users: {answer.message}")
        return list((answer.data or {}).get("users") or [])

    def rooms(self, *, user: str = "") -> list[dict]:
        """The Talk rooms one user participates in.

        As a *user*, not as admin: Talk has no admin room listing, and "which
        rooms does this user see" is the question the provisioning assertions
        actually want answered.
        """
        answer = self._ocs(
            "/ocs/v2.php/apps/spreed/api/v4/room",
            user=user or self.bot_user,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"listing rooms: {answer.message}")
        return list(answer.data or [])

    def participants(self, token: str, *, user: str = "") -> list[str]:
        """Actor ids in one room."""
        answer = self._ocs(
            f"/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants",
            user=user or self.bot_user,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"listing participants of {token}: {answer.message}")
        return [
            str(row.get("actorId") or row.get("userId") or "")
            for row in (answer.data or [])
        ]

    def participant_rows(self, token: str, *, user: str = "") -> list[dict]:
        """Every participant row of one room, whole.

        `participants()` reduces each row to an actor id, which is the right
        answer for "is the bot in this room" and cannot answer the three
        questions the signaling transport raises: whether the bot's session is
        in a *call* (`inCall`), whether the signaling server's ping loop is
        keeping the Talk session alive (`lastPing`), and whether there is a
        session at all (`sessionIds`). Presence is what this design gives up in
        exchange for Nextcloud authorizing every join, so "present" is a word
        that has to be checked in its parts rather than as a yes.
        """
        answer = self._ocs(
            f"/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants",
            user=user or self.bot_user,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"listing participants of {token}: {answer.message}")
        return list(answer.data or [])

    def signaling_settings(self, *, user: str = "") -> dict:
        """Talk's own answer to "where is the signaling server and who am I".

        The same call the daemon makes before every non-resume hello: it carries
        the HPB URL, the signaling mode, and both hello parameter sets — the
        hello-v2 JWT among them, which is why a harness client can authenticate
        as a real Nextcloud user rather than through the internal-client door.
        """
        answer = self._ocs(
            "/ocs/v2.php/apps/spreed/api/v3/signaling/settings",
            user=user or self.bot_user,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"reading signaling settings: {answer.message}")
        return dict(answer.data or {})

    def join_room_session(
        self, token: str, *, user: str = "", tolerate: tuple[int, ...] = ()
    ) -> str:
        """`POST …/participants/active`, and the Talk session id it mints.

        This is what puts a client in the room's participant list and what the
        signaling server's ping loop then keeps warm.

        **`force` supersedes the caller's own previous session in that room, and
        the caller is the bot by default** — which on a stack with the signaling
        supervisor running means this call can terminate the daemon's watcher
        session while its WebSocket stays up. That is the silent-loss state
        `lastPing` exists to detect, so a scenario calling this as the bot in a
        room the daemon watches is inducing it deliberately or by accident. The
        daemon repairs it at its next reconnect; a scenario that cannot wait
        should pass `user=` a human instead.

        `tolerate` is how the authorization-boundary case reads a refusal: for a
        room the caller is not a participant of, Talk raises `Unauthorized` and
        answers 404 — the same generic answer it gives for a room that does not
        exist, deliberately, so that room existence does not leak.
        """
        answer = self._ocs(
            f"/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants/active",
            user=user or self.bot_user,
            method="POST",
            body={"force": True},
            tolerate=tolerate,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(
                f"joining {token} as a session: {answer.status_code} {answer.message}"
            )
        return str((answer.data or {}).get("sessionId") or "")

    # -- driving Talk, as a person rather than as the bot -----------------

    def create_room(
        self, *, name: str, participants: Sequence[str] = (), actor: str = ""
    ) -> str:
        """A group room (`roomType=2`) owned by `actor`, and its token.

        As the **test user** by default, not as the bot, because rooms are
        user-created only: nothing in istota mints one during ordinary
        operation — `create_conversation` has three callers and all three are
        outside the daemon loop (the web promote, the `provision-rooms` CLI, and
        the agent-facing skill). A fixture that created a room as the bot would
        be staging a state the product does not produce.

        Group (2) rather than public (3) for the reason `entrypoint.sh` and
        `provision_rooms.py` both give: a public room is joinable by anyone
        holding its token.

        The token is recorded, and that record is `reset`'s entire scope.
        """
        owner = actor or self.test_user
        answer = self._ocs(
            "/ocs/v2.php/apps/spreed/api/v4/room",
            user=owner,
            method="POST",
            body={"roomType": ROOM_TYPE_GROUP, "roomName": name},
        )
        token = str((answer.data or {}).get("token") or "")
        if not token:
            raise NextcloudError(
                f"creating room {name!r} as {owner}: {answer.status_code} "
                f"{answer.message}"
            )
        self._created_rooms.append((token, owner))
        for participant in participants:
            self.invite(token, participant, actor=owner)
        return token

    def invite(self, token: str, participant: str, *, actor: str = "") -> None:
        """Add one user to a room, as the room's owner."""
        answer = self._ocs(
            f"/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants",
            user=actor or self.test_user,
            method="POST",
            body={"newParticipant": participant, "source": "users"},
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(
                f"inviting {participant} to {token}: {answer.status_code} "
                f"{answer.message}"
            )

    def post_message(
        self, token: str, *, actor: str = "", message: str, reply_to: int = 0
    ) -> int:
        """Post one chat message and return its Talk id.

        The id is what the daemon stores as `tasks.talk_message_id` and what a
        threaded reply carries as `replyTo`, so a scenario that does not keep it
        cannot assert on either.
        """
        body: dict = {"message": message}
        if reply_to:
            body["replyTo"] = reply_to
        answer = self._ocs(
            f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}",
            user=actor or self.test_user,
            method="POST",
            body=body,
        )
        posted = (answer.data or {}).get("id")
        if not posted:
            raise NextcloudError(
                f"posting to {token}: {answer.status_code} {answer.message}"
            )
        return int(posted)

    def messages(self, token: str, *, user: str = "", limit: int = 50) -> list[dict]:
        """A room's recent messages, newest first, as Talk returns them.

        Three parameters that are not defaults:

        - `lookIntoFuture=0`, because the daemon's own poller uses the long-poll
          form and a test that did the same would block for the poll timeout on
          a quiet room.
        - `setReadMarker=0`, because Talk's receive endpoint advances the
          reading account's read marker by default — and the account doing the
          reading here is the one the daemon is concurrently polling. A
          diagnostic that mutates the state under test is not a diagnostic, and
          `describe()` issues this for every created room on every failure.
        - `304` tolerated, because that is how Talk says "nothing to return".
          `urllib` raises for anything outside 2xx, so an empty room would
          otherwise come back as an exception from a method whose signature
          promises a list.
        """
        answer = self._ocs(
            f"/ocs/v2.php/apps/spreed/api/v1/chat/{token}"
            f"?lookIntoFuture=0&setReadMarker=0&limit={int(limit)}",
            user=user or self.bot_user,
            tolerate=(304,),
        )
        if answer.status_code == 304:
            return []
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"reading {token}: {answer.message}")
        return list(answer.data or [])

    def notifications(self, user: str = "") -> list[dict]:
        """One account's undismissed Nextcloud notifications.

        The same endpoint `src/istota/nextcloud/notifications.py` reads. That
        module has no *send* path and deliberately so — its docstring says
        sending needs the `admin_notifications` app and admin rights, and the
        bot already has two push channels of its own — so the honest witness for
        it is the read, against notifications the deployment itself raised.
        """
        answer = self._ocs(
            "/ocs/v2.php/apps/notifications/api/v2/notifications",
            user=user or self.bot_user,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"listing notifications for {user}: {answer.message}")
        return list(answer.data or [])

    # -- files and shares -------------------------------------------------

    def shares(self, *, user: str = "", shared_with_me: bool = False) -> list[dict]:
        """Shares one account owns, or (with `shared_with_me`) receives."""
        query = "?shared_with_me=true" if shared_with_me else ""
        answer = self._ocs(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares{query}",
            user=user or self.bot_user,
        )
        if answer.status_code not in (100, 200):
            raise NextcloudError(f"listing shares: {answer.message}")
        return list(answer.data or [])

    def files(self, path: str = "", *, user: str = "", depth: str = "1") -> list[str]:
        """WebDAV PROPFIND under `path`, as a flat list of decoded paths.

        Relative to the account's own DAV root, so a caller compares against
        names rather than against hrefs carrying `/remote.php/dav/files/<user>/`.

        **The requested collection is dropped, and that matters more than it
        looks.** A depth-1 PROPFIND answers with the collection itself first, so
        leaving it in makes `assert service.files(some_dir)` non-empty for an
        *empty* directory — an assertion that cannot fail, which is the shape
        this repo has already been bitten by twice.

        **The paths are not the ones `storage.py` writes.** On this shape the
        daemon writes POSIX paths under `/mnt/shared`, and Nextcloud serves that
        volume through the two `files_external` mounts `provision-nc.sh` makes:
        the whole volume to the bot at `Shared Files/`, and only the bot
        workspace to the human user at `<bot name>/`. So `/Users/x/inbox/y` on
        disk is `Shared Files/Users/x/inbox/y` here. Measured rather than
        reasoned, and it is the reason this accessor exists at all.
        """
        actor = user or self.bot_user
        body, status = self._dav(
            path, user=actor, method="PROPFIND", body=_PROPFIND_BODY,
            headers={"Depth": depth, "Content-Type": "application/xml"},
        )
        if status != 207:
            raise NextcloudError(
                f"PROPFIND {path!r} as {actor} answered HTTP {status}: "
                f"{body[:300].decode('utf-8', 'replace')}"
            )
        prefix = f"/remote.php/dav/files/{actor}/"
        requested = path.strip("/")
        found = []
        for href in _HREF.findall(body.decode("utf-8", "replace")):
            decoded = unquote(href)
            # Checked rather than sliced blind: stripping by length cannot fail
            # loudly, and every way it goes wrong — an absolute-URL href, an
            # instance served under a path prefix, a `%` in the account name —
            # produces a plausible-looking wrong path list rather than an error.
            if not decoded.startswith(prefix):
                raise NextcloudError(
                    f"a PROPFIND href is not under {prefix!r}: {decoded!r}"
                )
            relative = decoded[len(prefix):].rstrip("/")
            if relative and relative != requested:
                found.append(relative)
        return sorted(found)

    def read_file(self, path: str, *, user: str = "") -> bytes:
        """One file's bytes over WebDAV, as the named account."""
        actor = user or self.bot_user
        body, status = self._dav(path, user=actor)
        if status != 200:
            raise NextcloudError(
                f"GET {path!r} as {actor} answered HTTP {status}: "
                f"{body[:300].decode('utf-8', 'replace')}"
            )
        return body

    def _dav(
        self,
        path: str,
        *,
        user: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, int]:
        """One WebDAV request against `user`'s own file root.

        Returns the body and the status rather than raising on a non-2xx: a 404
        is an answer two callers want to see, and an exception carrying it would
        have to be unpicked to get back to the same fact.
        """
        if user not in self._passwords:
            raise NextcloudError(f"no password for {user!r}")
        url = (
            f"{self.base_url}/remote.php/dav/files/{quote(user, safe='')}/"
            f"{quote(path.lstrip('/'), safe='/')}"
        )
        request = Request(url, method=method, data=body)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        _add_basic_auth(request, user, self._passwords[user])
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                return response.read(), response.status
        except HTTPError as exc:
            return exc.read(), exc.code
        except URLError as exc:
            raise NextcloudError(
                f"{method} {url} never connected: {exc}"
            ) from None

    # -- reading it back through occ --------------------------------------

    def _occ(self, *argv: str) -> str:
        """One `occ` call inside the nextcloud container, as www-data.

        `-u www-data` matters: occ refuses to run as root, and the message it
        prints when it does ("Console has to be executed with the user that owns
        the file config/config.php") is not one anybody reads as "wrong `-u`".
        """
        return self._exec(["php", "/var/www/html/occ", *argv])

    def _exec(self, argv: list[str]) -> str:
        if self._stack is None:
            raise NextcloudError(
                "this service has no stack bound, so it cannot reach `occ`; "
                "StackPool.bind_stack does that once the containers are up"
            )
        result = self._stack.exec(
            argv, service="nextcloud", timeout=120, user="www-data"
        )
        if result.returncode != 0:
            raise NextcloudError(
                f"`{shlex.join(argv)}` in the nextcloud container exited "
                f"{result.returncode}\n--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )
        return result.stdout

    # -- avatars ----------------------------------------------------------

    def avatar_response(
        self, uid: str, *, size: int = 192, user: str = "",
    ) -> tuple[int, dict[str, str], int]:
        """One GET of Nextcloud's own avatar endpoint: status, headers, length.

        The endpoint `src/istota/nextcloud/avatars.py` reads, over the same
        front door the daemon uses. It exists for one question the default suite
        cannot answer: Nextcloud answers this with an image whether or not the
        user set one — with none it generates a coloured letter — and the only
        thing distinguishing the two is a response header. Every unit test of
        that module scripts the header it is checking for, so a wrong spelling
        passes them all and imports Nextcloud's placeholder as everybody's face.

        Headers rather than a parsed verdict, and a *length* rather than the
        body: the caller is asserting on which header arrived and how it was
        spelled, and a picture in a pytest failure report helps nobody.
        """
        url = (
            f"{self.base_url}/index.php/avatar/{quote(uid, safe='')}/{int(size)}"
        )
        request = Request(url, method="GET")
        actor = user or self.admin_user
        if actor not in self._passwords:
            raise NextcloudError(f"no password for {actor!r}")
        _add_basic_auth(request, actor, self._passwords[actor])
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                return response.status, dict(response.headers), len(response.read())
        except HTTPError as exc:
            return exc.code, dict(exc.headers), len(exc.read())
        except URLError as exc:
            raise NextcloudError(f"GET {url} never connected: {exc}") from None

    def set_avatar(self, uid: str, image: bytes) -> None:
        """Give `uid` a *custom* avatar, through Nextcloud's own `IAvatar` API.

        There is no HTTP route a test can use for this and no `occ` verb either:
        `POST /index.php/avatar` is a session-and-CSRF-guarded web route, which
        is the same fact that stops the daemon pushing an avatar *to* Nextcloud.
        So this goes the way `oauth_clients` goes — raw PHP against the server's
        own service container, inside the container — because calling
        `IAvatar::set` is exactly what the web route does once it has a body,
        including the bookkeeping that makes `isCustomAvatar()` true afterwards.

        Both arguments travel base64 in the argv — the picture because a file
        copy would need cleaning up, and the *uid* because that leaves nothing
        in the generated PHP but `[A-Za-z0-9+/=]`. A PHP double-quoted literal
        interpolates `$`, and a single-quoted one still has a backslash and a
        `'` to escape, so a uid substituted as text is a quoting question with
        two wrong answers. Keep the picture small; this is an argument list.
        """
        import base64

        php = _SET_AVATAR_PHP % (
            base64.b64encode(uid.encode()).decode("ascii"),
            base64.b64encode(image).decode("ascii"),
        )
        out = self._exec(["php", "-r", php])
        if "ISTOTA-AVATAR-SET" not in out:
            raise NextcloudError(
                f"setting {uid!r}'s avatar produced no confirmation:\n{out[:800]}"
            )

    def enabled_apps(self) -> list[str]:
        """App ids Nextcloud reports as enabled.

        The outcome assertion `provision-nc.sh`'s three `app:enable … || true`
        calls need. Two of the three — `spreed` and `calendar` — are not bundled
        in `nextcloud:30-apache` and are fetched from the app store at
        provisioning time, so this going red is the tier's only signal that the
        boot happened without the network it silently depends on.
        """
        raw = self._occ("app:list", "--output=json")
        return sorted((json.loads(raw or "{}").get("enabled") or {}).keys())

    def external_mounts(self) -> list[dict]:
        """Configured external-storage mounts, with their applicable users."""
        raw = self._occ("files_external:list", "--output=json", "--all")
        parsed = json.loads(raw or "[]")
        return list(parsed) if isinstance(parsed, list) else []

    def oauth_clients(self) -> list[dict]:
        """Registered OAuth2 clients, straight out of the `oauth2_clients` table.

        No `occ` verb and no HTTP API exists for this: stock Nextcloud's oauth2
        app exposes only `ImportLegacyOcClient`, and the admin UI is the only
        documented path. `provision-nc.sh` works around that with raw PHP
        against the QueryBuilder, and so does this — the same workaround on the
        read side, which is the honest way to assert on what that one wrote.

        Only `name` and `redirect_uri` come back. `client_identifier` is not an
        assertion any scenario needs and `secret` is a credential; a reader that
        returns one puts it in a pytest failure report.
        """
        raw = self._exec(["php", "-r", _OAUTH_CLIENTS_PHP])
        for line in reversed((raw or "").splitlines()):
            line = line.strip()
            if line.startswith("["):
                return list(json.loads(line))
        raise NextcloudError(
            f"could not read a client list out of the PHP output:\n{raw[:800]}"
        )


# Reads the table NC's own `SettingsController::addClient()` writes, through the
# same QueryBuilder `provision-nc.sh` uses to write it. `HTTP_HOST` is set
# because `base.php` reads it and PHP-CLI supplies none; the provisioning script
# does the same for the same reason.
_OAUTH_CLIENTS_PHP = """
$_SERVER['HTTP_HOST'] = 'localhost';
require '/var/www/html/lib/base.php';
\\OC_App::loadApp('oauth2');
$db = \\OC::$server->get(\\OCP\\IDBConnection::class);
$qb = $db->getQueryBuilder();
$rows = $qb->select('name', 'redirect_uri')->from('oauth2_clients')
    ->executeQuery()->fetchAll();
echo "\\n" . json_encode(array_values($rows)) . "\\n";
"""


# Sets a user's avatar the way the web route does — `IAvatar::set` — so the
# `generated` bookkeeping `isCustomAvatar()` reads is written too. `HTTP_HOST` is
# set for the same reason `_OAUTH_CLIENTS_PHP` sets it: `base.php` reads it and
# PHP-CLI supplies none. Both `%s` are base64, so nothing substituted in here
# can close a literal, interpolate a variable or escape anything.
_SET_AVATAR_PHP = """
$_SERVER['HTTP_HOST'] = 'localhost';
require '/var/www/html/lib/base.php';
\\OC_App::loadApps();
$uid = base64_decode('%s');
$data = base64_decode('%s');
$manager = \\OC::$server->get(\\OCP\\IAvatarManager::class);
$manager->getAvatar($uid)->set($data);
echo "\\nISTOTA-AVATAR-SET\\n";
"""


def _add_basic_auth(request: Request, user: str, password: str) -> None:
    """Basic auth without `HTTPBasicAuthHandler`.

    The handler variant only sends credentials *after* a 401 challenge, and
    Nextcloud answers an unauthenticated OCS request with a 200 carrying an
    error status code rather than a 401 — so the retry never happens and the
    call reads as a permissions problem.
    """
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
