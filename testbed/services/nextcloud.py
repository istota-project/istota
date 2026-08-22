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
- `reset()` is a documented no-op **for now**. Stage 5 of the spec owns it,
  together with the scope it can honestly claim: a real server cannot be
  restored to a byte-identical state the way a truncate would, and a `reset`
  that attempts completeness and half-succeeds is worse than one whose limits
  are written down. Nothing in Stage 3's provisioning suite mutates Nextcloud,
  so an empty reset is true today; it stops being true the moment a scenario
  creates a room.

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
import shlex
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
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
        """A documented no-op until Stage 5 gives it a scope it can keep.

        Not `raise NotImplementedError`: `Stack.reset` calls this on every
        service in the profile before every test, and a service that refused
        would make the shape unusable for the provisioning suite that proves the
        shape works. Not silently absent either — a `Service` without `reset` is
        a protocol violation nothing would catch until a scenario leaked.
        """

    def close(self) -> None:
        """Nothing to stop. The container belongs to the stack."""

    def describe(self) -> str:
        """For `Stack.diagnostics`. Cheap, and never raises.

        A failed full-shape scenario is expensive to reproduce, so this reports
        what it can and says what it could not reach rather than replacing a
        test failure with an error from the diagnostic.
        """
        lines = []
        for label, reader in (
            ("users", self.users),
            ("apps", self.enabled_apps),
        ):
            try:
                lines.append(f"{label}: {sorted(reader())}")
            except Exception as exc:  # pragma: no cover - diagnostic
                lines.append(f"{label}: unavailable ({exc})")
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

    def _ocs(self, path: str, *, user: str = "", method: str = "GET") -> OcsResponse:
        """One OCS call as admin, or as whoever the caller names.

        The `OCS-APIRequest` header is not optional: without it Nextcloud
        answers a 401 with an HTML login page, which reads as bad credentials.
        """
        actor = user or self.admin_user
        if actor not in self._passwords:
            raise NextcloudError(
                f"no password for {actor!r}; this service knows "
                f"{sorted(self._passwords)}"
            )
        url = f"{self.base_url}{path}"
        separator = "&" if "?" in path else "?"
        request = Request(f"{url}{separator}format=json", method=method)
        request.add_header("OCS-APIRequest", "true")
        _add_basic_auth(request, actor, self._passwords[actor])

        # Retried on a *connection* error only, and only for a bounded window.
        # These calls do not reach Nextcloud directly — they go through the
        # stack's nginx, which is not in the tier's readiness set because it
        # legitimately restarts while its `web` upstream is coming up. So the
        # first call after a boot can land on a moment when nothing is listening
        # on the published port, and an unretried failure arrives as whichever
        # assertion happened to run first, saying "connection refused" about
        # Nextcloud. An `HTTPError` is *not* retried: that is Nextcloud
        # answering, and answering wrongly is what the caller wants told.
        deadline = time.monotonic() + CONNECT_RETRY_SECONDS
        while True:
            try:
                with urlopen(request, timeout=TIMEOUT) as response:
                    payload = json.loads(response.read() or b"{}")
                break
            except HTTPError as exc:
                raise NextcloudError(
                    f"{method} {path} answered HTTP {exc.code}: "
                    f"{exc.read()[:400].decode('utf-8', 'replace')}"
                ) from None
            except URLError as exc:
                if time.monotonic() >= deadline:
                    raise NextcloudError(
                        f"{method} {path} never connected within "
                        f"{CONNECT_RETRY_SECONDS}s (the stack's nginx, on "
                        f"{self.base_url}): {exc}"
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
