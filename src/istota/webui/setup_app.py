"""The first-run setup server: ``istota setup --web``.

A one-shot FastAPI app that runs before any config exists, as uid 10001 in a
``compose run`` of the daemon image, with the config directory writable and a
staging directory at ``--vm-dir``. It serves the built SvelteKit bundle and the
setup API under ``/istota/api/setup/``, and nothing else, so it does not import
``webui/app.py``, which loads config at import and wires every router.

The setup code is the boundary. ``istota-stack`` (root) mints it and copies it
into the staging directory as ``setup-code``; the browser posts it once to
``/unlock`` and gets the one session this process will ever hand out. A wrong
code, a spent one and a locked-out unlock all get the same 403. The draft is
held in memory only: it carries credentials, so nothing reaches disk before
Finish, and an hour with no request ends the session and the process (exit 3).

With ``--force`` it is a re-run over a configured stack: the draft starts as
what the stack's files say (``setup_wizard.prior_answers``), every answer no
screen asks is kept, and Finish rewrites the config and the secret files but
creates no login.

Bodies are read by hand under a 64 KiB cap and never declared as pydantic
parameters, so a refusal can never echo a submitted credential back. Uvicorn's
access log is off and the request log names the path without its query.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from istota import setup_wizard
from istota.setup_wizard import FieldError, PayloadError, SetupError

from . import setup_checks
from .request_guards import BodyError, origin_refusal, read_bounded_body
from .static_dir import cache_header_statics, resolve_static_dir

logger = logging.getLogger("istota.setup_web")

SESSION_COOKIE = "istota_setup"
BODY_LIMIT = 64 * 1024
IDLE_SECONDS = 60 * 60
EXIT_TIMED_OUT = 3
EXIT_LOCKED_OUT = 4
UNLOCK_WINDOW_SECONDS = 60
UNLOCK_FAILURES_PER_WINDOW = 5
UNLOCK_FAILURES_TOTAL = 50
_IDLE_POLL_SECONDS = 30

TLS_PORT = 8443
LOCAL_PORT = 8080

#: Each check makes an outbound request with the user's credentials.
CHECKS_PER_MINUTE = 20
_CHECK_WINDOW_SECONDS = 60
#: How long past its own deadline a check's worker thread is waited for.
_CHECK_GRACE_SECONDS = 2.0

#: The identity password: a draft key that is not an answers field, because
#: it is written to the identity row and nowhere else.
PASSWORD_FIELD = "password"
#: Draft keys whose values `GET /draft` never returns.
CREDENTIAL_FIELDS = frozenset({
    "nextcloud_app_password",
    "native_api_key",
    "claude_code_oauth_token",
    "anthropic_listing_key",
    "imap_password",
    "caldav_password",
    PASSWORD_FIELD,
})
DRAFT_FIELDS = frozenset(setup_wizard.WEB_SETUP_FIELDS) | {PASSWORD_FIELD}

_MODULE_FIELDS = tuple(name for name, _module, _question in setup_wizard._OPT_OUT_MODULES)

#: Which answer fields each screen asks, so `/validate` returns a screen its
#: own errors. Lives here rather than in the frontend so the two cannot
#: disagree. `user_id` is on two screens: the Nextcloud one replaces it.
SETUP_STEPS: dict[str, tuple[str, ...]] = {
    "welcome": ("bot_name", "display_name", "user_email", "timezone", "user_id"),
    "account": (PASSWORD_FIELD,),
    "model": (
        "brain_kind", "native_base_url", "native_model", "native_api_key", "claude_code_oauth_token",
        "anthropic_listing_key",
    ),
    "address": ("hostname", "ingress", "upstream_proxy", "listen_addr", "listen_port", "tls_cert_source"),
    "nextcloud": (
        "storage", "workspace", "nextcloud_url", "nextcloud_public_url", "nextcloud_username",
        "nextcloud_app_password", "talk_enabled", "user_id",
    ),
    "connections": (
        "email_enabled", "imap_host", "imap_user", "imap_password", "smtp_host", "bot_email",
        "authserv_id", "caldav_url", "caldav_username", "caldav_password",
    ),
    "features": (*_MODULE_FIELDS, "profiles"),
}
SETUP_STEPS["review"] = tuple(dict.fromkeys(f for fields in SETUP_STEPS.values() for f in fields))

#: The screens a re-run leaves out: the password belongs to the login the
#: first run made, which a re-run does not touch. The page's re-run list is
#: held to this one.
RERUN_SKIPPED_STEPS: tuple[str, ...] = ("account",)
RERUN_SETUP_STEPS: dict[str, tuple[str, ...]] = {
    step: fields for step, fields in SETUP_STEPS.items()
    if step not in RERUN_SKIPPED_STEPS and step != "review"
}
RERUN_SETUP_STEPS["review"] = tuple(dict.fromkeys(f for fields in RERUN_SETUP_STEPS.values() for f in fields))


def setup_steps(rerun: bool) -> dict[str, tuple[str, ...]]:
    """The screens, in order, with the fields each asks."""
    return RERUN_SETUP_STEPS if rerun else SETUP_STEPS


_ALREADY_SET_UP = (
    "istota is already set up; use the admin Configuration page, "
    "`istota-stack setup --force`, or `istota-stack setup --web --force` to change it in a browser."
)
_NOTHING_TO_CHANGE = (
    "There is no config to change; run `istota-stack setup --web` for a first setup."
)
_RERUN_UNPARSEABLE = (
    "config.toml does not parse ({reason}); fix it, or re-run in the terminal with "
    "`istota-stack setup --force`."
)
#: A re-run's stored credential, by the field holding the host it was stored
#: for. A changed host drops it from the draft (`_drop_moved_credentials`).
# The SMTP password falls back to the IMAP one, so either host moves it.
_CREDENTIAL_HOSTS: dict[str, tuple[str, ...]] = {
    "nextcloud_app_password": ("nextcloud_url",),
    "imap_password": ("imap_host", "smtp_host"),
    "caldav_password": ("caldav_url",),
    "native_api_key": ("native_base_url",),
}
_CODE_REFUSED = (
    "That setup code is not valid, or it has already been used. To get a new one, "
    "stop the running setup (Ctrl-C, or `sudo systemctl stop istota-setup`) and run "
    "`sudo istota-stack setup --web` again."
)
_LOCKED_OUT = (
    "Too many wrong setup codes; setup has stopped. Run "
    "`sudo istota-stack setup --web` again for a new code."
)

# Crockford base32 reads O as 0 and I and L as 1, so a code read off a console
# is the same code however those were typed.
_CROCKFORD = str.maketrans({"O": "0", "I": "1", "L": "1"})


def normalize_code(raw: str) -> str:
    """A setup code as compared: upper case, no hyphens or spaces, Crockford's
    look-alikes folded."""
    return "".join(raw.split()).replace("-", "").upper().translate(_CROCKFORD)


@dataclass
class SetupState:
    """Everything the server remembers, all of it in memory."""

    code: str
    local: bool
    data_dir: Path
    vm_dir: Path
    clock: Callable[[], float]
    #: host.env's LOCAL_FORWARD_PORT at start, 0 off the local VM; every
    #: draft-to-answers call carries it (`_answers_for`).
    forwarded_port: int = 0
    #: host.env's ISTOTA_DESKTOP_APP at start: the Mac app's VM, whose setup
    #: adds the app's login codes (`ContainerAnswers.web_auth`).
    desktop_app: bool = False
    #: A re-run (`--force`): `prior` is what the stack's files said at start,
    #: the base of every draft-to-answers call, and `existing_user` its user.
    rerun: bool = False
    prior: setup_wizard.ContainerAnswers | None = None
    existing_user: str = ""
    #: Credential fields still holding the value `prior` seeded, not one the
    #: page sent; only these are dropped when their host changes.
    stored_credentials: set[str] = field(default_factory=set)
    code_spent: bool = False
    session_id: str | None = None
    last_activity: float = 0.0
    draft: dict = field(default_factory=dict)
    failures_total: int = 0
    failures_by_client: dict[str, list[float]] = field(default_factory=dict)
    check_times: list[float] = field(default_factory=list)
    check_seconds: float = setup_checks.CHECK_SECONDS
    exit_code: int | None = None
    #: Called once with the exit code when the server should stop; `serve`
    #: points it at uvicorn's `should_exit`.
    on_exit: Callable[[int], None] | None = None
    #: Set once Finish has written everything; the server is on its way out.
    finished: bool = False
    finish_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: OpenRouter's model ids, asked for once per process; empty if that failed.
    model_ids: list[str] | None = None
    model_ids_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def request_exit(state: SetupState, code: int) -> None:
    if state.exit_code is not None:
        return
    state.exit_code = code
    if state.on_exit is not None:
        state.on_exit(code)


def expire_if_idle(state: SetupState) -> bool:
    """End the session and the server after an idle hour. True if it did."""
    if state.session_id is None:
        return False
    if state.clock() - state.last_activity <= IDLE_SECONDS:
        return False
    state.session_id = None
    # The draft holds credentials; it goes with the session.
    state.draft.clear()
    logger.info("setup timed out after %d idle minutes", IDLE_SECONDS // 60)
    request_exit(state, EXIT_TIMED_OUT)
    return True


def _error(status: int, code: str, message: str, fields: list[FieldError] | None = None) -> JSONResponse:
    body: dict = {"error": code, "message": message}
    if fields is not None:
        body["fields"] = [{"field": e.field, "message": e.message} for e in fields]
    return JSONResponse(body, status_code=status)


class _Refusal(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def _has_session(state: SetupState, request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if state.session_id is None or not cookie:
        return False
    return hmac.compare_digest(cookie.encode(), state.session_id.encode())


def _require_session(state: SetupState, request: Request) -> None:
    expire_if_idle(state)
    if not _has_session(state, request):
        raise _Refusal(_error(401, "no_session", "Setup is locked. Enter your setup code to continue."))
    state.last_activity = state.clock()


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_name(host: str) -> str:
    """The name half of a ``Host`` header, IPv6 brackets stripped."""
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.rpartition(":")[0] if host.count(":") == 1 else host


def _require_origin(state: SetupState, request: Request) -> None:
    host = request.headers.get("host", "")
    # A local run is reached only through a loopback publish. Holding Host to a
    # loopback name stops a page the operator visits from rebinding its own
    # name to 127.0.0.1 and passing the same-host Origin check.
    if state.local and _host_name(host).lower() not in _LOOPBACK_HOSTS:
        raise _Refusal(_error(403, "forbidden", "Open the setup page at localhost."))
    if origin_refusal(request.headers, host):
        raise _Refusal(_error(403, "forbidden", "This request did not come from the setup page."))


async def _read_json(request: Request):
    try:
        raw = await read_bounded_body(request, BODY_LIMIT)
    except BodyError as exc:
        message = "That request is too large." if exc.status == 413 else "The request was interrupted."
        raise _Refusal(_error(exc.status, "bad_body", message)) from None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Refusal(_error(400, "bad_body", "The request body is not JSON.")) from None


def _redacted_draft(draft: dict) -> dict:
    shown = {}
    for key, value in draft.items():
        shown[key] = {"set": bool(value)} if key in CREDENTIAL_FIELDS else value
    return shown


def _memory_total_mib() -> int:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _password_errors(draft: dict) -> list[FieldError]:
    from istota.config import Config  # noqa: PLC0415 - the auth rules, not a loaded config

    from . import auth  # noqa: PLC0415

    password = draft.get(PASSWORD_FIELD)
    if not isinstance(password, str) or not password:
        return [FieldError(PASSWORD_FIELD, "Choose a password.")]
    message = auth.password_policy_error(
        password, auth.policy_from_config(Config()),
        email=str(draft.get("user_email") or ""), user_id=str(draft.get("user_id") or ""),
    )
    return [FieldError(PASSWORD_FIELD, message)] if message else []


def _email_errors(draft: dict) -> list[FieldError]:
    # The login identity: `upsert_identity` refuses an empty one, after the
    # settings were already written, so it is asked for here.
    email = str(draft.get("user_email") or "").strip()
    local, at, domain = email.partition("@")
    if not email:
        return [FieldError("user_email", "Enter your email address. It is how you will sign in.")]
    if not (at and local and domain) or any(c.isspace() for c in email):
        return [FieldError("user_email", "That doesn't look like an email address.")]
    return []


def _username_errors(a: setup_wizard.ContainerAnswers) -> list[FieldError]:
    """The username rule for where the files live.

    The terminal wizard keeps accepting the ids it always has; this rule is
    the web setup's, because the web setup also creates the first login.
    With local storage the username is the native login's, so it takes
    ``valid_new_user_id``. With Nextcloud it is the user's Nextcloud id, and
    ``istota user ensure`` checks none: what holds an id is
    ``is_scopable_user_id``, which every path and task made from it applies
    (``db.create_task`` refuses the rest), plus the admins file, which reads
    a line starting with ``#`` as a comment.
    """
    from istota.sandbox.user_scope import is_scopable_user_id  # noqa: PLC0415

    from . import auth  # noqa: PLC0415

    if not a.user_id:
        return []
    if a.uses_nextcloud:
        if is_scopable_user_id(a.user_id) and not a.user_id.startswith("#"):
            return []
        return [FieldError(
            "user_id",
            "Istota can't use this username, even where Nextcloud accepts it: it can't start "
            "or end with a space, start with #, contain a slash, or be “.” or “..”.",
        )]
    if auth.valid_new_user_id(a.user_id):
        return []
    return [FieldError(
        "user_id",
        "A username is up to 32 lowercase letters, digits, dots, hyphens or underscores, "
        "and starts with a letter or digit.",
    )]


def _kept_username_errors(a: setup_wizard.ContainerAnswers, data_dir: Path) -> list[FieldError]:
    """Refuse a username other than the one an earlier Finish already wrote.

    The admins file is only ever created, so after a Finish that failed past
    the settings step, a retry under another username would make a first user
    who is not an admin.
    """
    from istota.config import load_admin_users  # noqa: PLC0415

    admins_path = Path(data_dir) / "config" / "admins"
    if not a.user_id or not admins_path.exists():
        return []
    kept = sorted(load_admin_users(str(admins_path)))
    if a.user_id in kept:
        return []
    named = " or ".join(f"“{user_id}”" for user_id in kept) or "another username"
    return [FieldError(
        "user_id",
        f"An earlier try at finishing setup already saved settings for {named}, "
        "so the username has to stay as it was.",
    )]


_LOOPBACK_NAMES = ("", "localhost", "127.0.0.1")


def _address_errors(a: setup_wizard.ContainerAnswers) -> list[FieldError]:
    # `answers_from_payload` reads a missing address as `localhost`, which a
    # certificate cannot be issued for and nobody else can reach.
    if a.ingress not in ("direct", "proxied"):
        return []
    if a.hostname.split(":")[0].strip().lower() not in _LOOPBACK_NAMES:
        return []
    return [FieldError("hostname", "Enter the web address people will use, like istota.example.com.")]


#: The web setup's wording for the shared validator's refusals, which keep the
#: terminal's operator wording (option names, environment variables) because
#: the terminal is where those mean something.
_WEB_MESSAGES: dict[str, str] = {
    "user_id": "Choose a username.",
    "timezone": "That isn't a time zone Istota knows. Choose one from the list.",
    "ingress": "Choose how people will reach Istota.",
    "hostname": "Enter the web address people will use, like istota.example.com.",
    "upstream_proxy": "Enter your proxy's address. Istota accepts connections from it and nothing else.",
    "listen_addr": "Enter one private address for Istota to listen on, not 0.0.0.0.",
    "listen_port": "Enter a port number.",
    "tls_cert_source": "Behind a proxy, choose a self-signed certificate, your own certificate files, or none.",
    "storage": "Choose where Istota keeps its files.",
    "workspace": "Choose whether Istota keeps its files in Nextcloud or on this server.",
    "nextcloud_url": "Enter your Nextcloud's address.",
    "nextcloud_public_url": "Enter the address you open Nextcloud at.",
    "talk_enabled": "Choose whether to chat with Istota in Nextcloud Talk.",
    "brain_kind": "Choose an AI service.",
    "native_provider": "Choose an AI service.",
    "native_provider_kind": "Choose an AI service.",
    "native_base_url": "Enter the service's address, starting with https://.",
    "native_model": "Enter a model name.",
    "native_api_key": "Enter your API key.",
    "authserv_id": "Enter only the name before the first semicolon, like mx.example.com.",
    "caldav_password": "Enter the calendar's password.",
    "profiles": "Choose from the features listed.",
}
_WEB_MESSAGE_FALLBACK = "This answer can't be used. Check it and try again."


def _web_message(error: FieldError, a: setup_wizard.ContainerAnswers | None) -> str:
    if not error.field:
        return error.message
    if a is not None and error.field == "hostname" and a.ingress == "local" and a.forwarded_port:
        address = setup_wizard.local_address(a.forwarded_port)
        return f"This computer reaches Istota at {address}. Use that address."
    if a is not None and error.field == "user_id" and a.uses_nextcloud:
        return "Enter your own Nextcloud username."
    if a is not None and error.field == "tls_cert_source" and a.ingress == "direct":
        return "A self-signed certificate works only behind a proxy you run."
    return _WEB_MESSAGES.get(error.field, _WEB_MESSAGE_FALLBACK)


def _plain(errors: list[FieldError], a: setup_wizard.ContainerAnswers | None) -> list[FieldError]:
    """The shared validator's refusals in the web setup's words."""
    return [FieldError(e.field, _web_message(e, a)) for e in errors]


def _subscription_errors(a: setup_wizard.ContainerAnswers) -> list[FieldError]:
    # The terminal keeps an existing token file when the answer is empty; a
    # first web setup has no file to keep. A re-run's base may hold an
    # Anthropic API key instead, which the web asks for nowhere.
    if a.brain_kind == "claude_code" and not (a.claude_code_oauth_token or a.anthropic_api_key):
        return [FieldError(
            "claude_code_oauth_token", "Paste the token that `claude setup-token` printed.",
        )]
    return []


def _same_host(value: object) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _host_moved(state: SetupState, credential: str, draft: dict) -> bool:
    """Whether the draft names another host than the one ``credential`` was
    stored for. A host the draft does not hold is the prior's.

    The prior is compared as the page saw it (`payload_from_answers`): native's
    provider reaches the draft as its preset URL, not as the answer field.
    """
    if state.prior is None:
        return False
    seen = setup_wizard.payload_from_answers(state.prior)
    return any(
        host in draft and _same_host(draft[host]) != _same_host(seen.get(host, ""))
        for host in _CREDENTIAL_HOSTS[credential]
    )


def _drop_moved_credentials(state: SetupState) -> None:
    """Drop a stored credential whose host the draft has changed, so no
    check and no Finish sends it to a host it was not stored for."""
    for credential in sorted(state.stored_credentials):
        if _host_moved(state, credential, state.draft):
            state.draft[credential] = ""
            state.stored_credentials.discard(credential)


def _moved_credential_errors(state: SetupState, a: setup_wizard.ContainerAnswers) -> list[FieldError]:
    """Ask again for a credential dropped by a host change: left empty, the
    writer would keep the stored file and send it to the new host."""
    if state.prior is None:
        return []
    in_use = {
        "nextcloud_app_password": a.uses_nextcloud,
        "imap_password": a.email_enabled,
        "native_api_key": a.brain_kind == "native",
        # The shared validator already asks for a calendar's password.
        "caldav_password": False,
    }
    return [
        FieldError(credential, "Enter it again: the one saved before was for the old address.")
        for credential, used in in_use.items()
        if used and getattr(state.prior, credential) and not getattr(a, credential)
        and _host_moved(state, credential, state.draft)
    ]


def _rerun_username_errors(a: setup_wizard.ContainerAnswers, existing_user: str) -> list[FieldError]:
    if a.user_id == existing_user:
        return []
    return [FieldError(
        "user_id", f"This changes an existing setup, so the username stays “{existing_user}”.",
    )]


def _answers_for(state: SetupState, draft: dict) -> setup_wizard.ContainerAnswers:
    """The answers ``draft`` makes on this machine: the one place the server
    turns a draft into answers, so what the draft cannot carry (the VM's
    forwarded port, a re-run's prior answers) reaches validation, the checks
    and Finish alike.

    Raises ``PayloadError``, its ``answers`` carrying the same.
    """
    payload = {key: value for key, value in draft.items() if key != PASSWORD_FIELD}
    try:
        a = setup_wizard.answers_from_payload(payload, base=state.prior)
    except PayloadError as exc:
        if exc.answers is not None:
            _on_this_machine(state, exc.answers)
        raise
    _on_this_machine(state, a)
    return a


def _on_this_machine(state: SetupState, a: setup_wizard.ContainerAnswers) -> None:
    a.forwarded_port = state.forwarded_port
    a.desktop_app = state.desktop_app
    # The OAuth pair was registered with the prior Nextcloud and no screen
    # asks it, so another Nextcloud turns Nextcloud login off, as the
    # terminal does for a client id with no secret.
    if state.prior is not None and _same_host(a.nextcloud_url) != _same_host(state.prior.nextcloud_url):
        a.oauth_client_id = a.oauth_client_secret = ""


def draft_errors(
    state: SetupState, draft: dict,
) -> tuple[setup_wizard.ContainerAnswers | None, list[FieldError]]:
    """Every error in the draft, and its answers when there are none.

    A payload refusal on one field does not hide the validator's errors on
    the others: the validator runs over what the rest of the payload made,
    and only its errors on a field the payload already refused are dropped.
    A username other than the one an earlier Finish wrote in the state's
    data directory is refused too.

    A re-run creates no login, so it asks for no email or password, and it
    keeps the existing username whatever rule made it; it refuses any other.
    """
    errors: list[FieldError] = []
    try:
        a = _answers_for(state, draft)
    except PayloadError as exc:
        a = exc.answers
        errors.extend(_plain(exc.errors, a))
    if a is not None:
        refused = {e.field for e in errors}
        web_rules = [*_address_errors(a), *_subscription_errors(a)]
        if state.rerun:
            web_rules += [
                *_rerun_username_errors(a, state.existing_user), *_moved_credential_errors(state, a),
            ]
        else:
            web_rules += [*_username_errors(a), *_kept_username_errors(a, state.data_dir)]
        shared = _plain(setup_wizard.validate_container_answers(a), a)
        for error in (*shared, *web_rules):
            if error.field not in refused:
                errors.append(error)
    if not state.rerun:
        errors.extend(_email_errors(draft))
        errors.extend(_password_errors(draft))
    return (None if errors else a), errors


def step_errors(state: SetupState, draft: dict, step: str) -> list[FieldError]:
    """The draft's errors on ``step``'s fields, plus any form-level error."""
    _answers, errors = draft_errors(state, draft)
    steps = setup_steps(state.rerun)
    # A field two screens ask (the username) is reported on the one that owns
    # it for this draft, so it is not refused under a label that doesn't ask it.
    wanted = {f for f in steps[step] if step == "review" or step_of(f, draft, steps) == step}
    return [e for e in errors if e.field == "" or e.field in wanted]


def step_of(field_name: str, draft: dict, steps: dict[str, tuple[str, ...]] = SETUP_STEPS) -> str:
    """The screen that asks ``field_name``, for the page to go back to."""
    if field_name == "user_id" and draft.get("storage") == "nextcloud":
        # With Nextcloud the username is the Nextcloud screen's.
        return "nextcloud"
    for step, fields in steps.items():
        if step != "review" and field_name in fields:
            return step
    return "review"


#: What each check needs before it has anything to test: the field, and what
#: to say under it when it is empty.
_CHECK_NEEDS: dict[str, dict[str, str]] = {
    "model": {
        "native_model": "Enter a model name.",
        "native_api_key": "Enter an API key to test.",
    },
    "nextcloud": {
        "nextcloud_url": "Enter your Nextcloud's address.",
        "nextcloud_username": "Enter the assistant's Nextcloud username.",
        "nextcloud_app_password": "Enter the assistant's app password.",
        "user_id": "Enter your own Nextcloud username.",
    },
    "email": {
        "imap_host": "Enter the mail server's address.",
        "imap_user": "Enter the email account's username.",
        "imap_password": "Enter the email account's password.",
    },
    "caldav": {
        "caldav_url": "Enter the calendar's address.",
        "caldav_username": "Enter the calendar username.",
        "caldav_password": "Enter the calendar password.",
    },
    "address": {
        "hostname": "Enter the web address.",
    },
}
CHECK_KINDS = tuple(_CHECK_NEEDS)


def _check_refusal(kind: str, a) -> JSONResponse | None:
    """Why ``kind`` has nothing to test for answers ``a``, or None."""
    if kind == "model" and a.brain_kind != "native":
        return _error(
            400, "no_live_check",
            "A Claude subscription can't be tested here: it needs Claude Code, which "
            "Istota installs when setup finishes. The token is checked for shape only.",
        )
    if kind == "nextcloud" and not a.uses_nextcloud:
        return _error(400, "nothing_to_check", "Choose “Use my Nextcloud” to test a Nextcloud connection.")
    if kind == "email" and not a.email_enabled:
        return _error(400, "nothing_to_check", "Add an email account to test it.")
    if kind == "caldav" and a.uses_nextcloud:
        return _error(400, "nothing_to_check", "With Nextcloud connected, the calendar comes from Nextcloud.")
    if kind == "address" and a.ingress == "local":
        return _error(400, "nothing_to_check", "There is no web address to check for “Only on this computer”.")
    missing = [
        FieldError(name, message)
        for name, message in _CHECK_NEEDS[kind].items()
        if not getattr(a, name)
    ]
    if missing:
        return _error(400, "incomplete", "Fill in the fields above to test the connection.", missing)
    return None


async def _run_check(kind: str, a, browser_host: str, seconds: float) -> setup_checks.CheckResult:
    """Run one check against the config ``a`` would write, under its deadline."""
    if kind == "address":
        host = a.hostname
        work = functools.partial(setup_checks.check_address, a.hostname, browser_host)
    else:
        config = setup_checks.config_for_check(a)
        if kind == "model":
            return await setup_checks.check_model(config, seconds=seconds)
        if kind == "nextcloud":
            host = setup_checks.host_of(config.nextcloud.url)
            work = functools.partial(setup_checks.check_nextcloud, config, a.user_id, seconds=seconds)
        elif kind == "email":
            host = config.email.imap_host
            work = functools.partial(setup_checks.check_email, config, seconds=seconds)
        else:
            host = setup_checks.host_of(config.caldav_url)
            work = functools.partial(setup_checks.check_caldav, config, seconds=seconds)
    try:
        # The thread is not stopped by the timeout; each client's own timeout
        # ends it. This only stops it holding the response.
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=seconds + _CHECK_GRACE_SECONDS)
    except asyncio.TimeoutError:
        logger.info("setup: check %s passed its deadline", kind)
        return setup_checks.CheckResult(False, setup_checks.timed_out_message(host, seconds))


async def openrouter_model_ids(seconds: float) -> list[str]:
    """OpenRouter's model ids, for the model field's suggestions; ``[]`` on
    any failure, since the list is a convenience and the Test button is what
    checks a model."""
    from istota.llm import discovery  # noqa: PLC0415

    try:
        catalog = await asyncio.wait_for(
            discovery.fetch_openrouter_catalog(
                setup_wizard.DEFAULT_OPENROUTER_BASE_URL, timeout=seconds,
            ),
            timeout=seconds + _CHECK_GRACE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is no suggestions
        logger.info("setup: no OpenRouter model list (%s)", type(exc).__name__)
        return []
    return sorted(catalog)


def _prior_for_rerun(data_dir: Path, vm_dir: Path) -> setup_wizard.ContainerAnswers:
    """What a re-run starts from, or the reason it cannot start."""
    import tomllib  # noqa: PLC0415

    config_path = data_dir / "config" / "config.toml"
    if not config_path.is_file():
        raise SetupError(_NOTHING_TO_CHANGE)
    try:
        prior = setup_wizard.prior_answers(data_dir, vm_dir)
    except setup_wizard.PriorConfigUnreadable as exc:
        raise SetupError(_RERUN_UNPARSEABLE.format(reason=exc)) from None
    if prior is None:
        raise SetupError(_NOTHING_TO_CHANGE)
    if not prior.user_id:
        doc = tomllib.loads(config_path.read_text(encoding="utf-8"))
        ids = setup_wizard.prior_user_ids(doc, config_path.parent / "admins")
        if ids:
            named = ", ".join(ids)
            raise SetupError(
                f"config.toml has more than one admin with a [users] block ({named}), and the "
                "browser setup cannot ask which one it is for. Re-run in the terminal with "
                "`istota-stack setup --force`."
            )
        raise SetupError(
            "No user in config.toml is both in config/admins and has a [users] block, so the "
            "browser setup does not know whose it is. Re-run in the terminal with "
            "`istota-stack setup --force`."
        )
    return prior


def create_setup_app(
    *, data_dir: Path, vm_dir: Path, code_path: Path, local: bool, rerun: bool = False,
    clock: Callable[[], float] = time.monotonic,
    check_seconds: float = setup_checks.CHECK_SECONDS,
) -> FastAPI:
    """The setup app over ``data_dir``.

    A first run refuses when a config already exists. A re-run (``rerun``)
    needs one, and starts from what the stack's files say: the draft is
    seeded with it, and every answer the form does not carry is kept.
    """
    data_dir = Path(data_dir)
    prior = None
    if rerun:
        prior = _prior_for_rerun(data_dir, Path(vm_dir))
    elif (data_dir / "config" / "config.toml").exists():
        raise SetupError(_ALREADY_SET_UP)
    try:
        code = normalize_code(Path(code_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupError(
            f"Could not read the setup code at {code_path} ({exc.__class__.__name__}); "
            "start setup with `istota-stack setup --web`."
        ) from None
    if not code:
        raise SetupError(f"The setup code file {code_path} is empty.")

    state = SetupState(
        code=code, local=local, data_dir=data_dir, vm_dir=Path(vm_dir), clock=clock,
        forwarded_port=setup_wizard.forwarded_port(Path(vm_dir)), check_seconds=check_seconds,
        desktop_app=setup_wizard.desktop_app(Path(vm_dir)),
        rerun=rerun, prior=prior, existing_user=prior.user_id if prior is not None else "",
    )
    if prior is not None:
        state.draft = setup_wizard.payload_from_answers(prior)
        state.stored_credentials = {name for name in _CREDENTIAL_HOSTS if state.draft.get(name)}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def watch_idle() -> None:
            while state.exit_code is None:
                await asyncio.sleep(_IDLE_POLL_SECONDS)
                expire_if_idle(state)

        watcher = asyncio.create_task(watch_idle())
        try:
            yield
        finally:
            watcher.cancel()

    app = FastAPI(
        title="Istota setup", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.setup = state

    @app.exception_handler(_Refusal)
    async def _refused(_request: Request, exc: _Refusal):
        return exc.response

    @app.middleware("http")
    async def _log_request(request: Request, call_next):
        response = await call_next(request)
        # The path only: a query string could carry what a browser put there.
        logger.info("setup: %s %s %d", request.method, request.url.path, response.status_code)
        return response

    @app.get("/")
    @app.get("/istota")
    @app.get("/istota/")
    async def _to_setup_page():
        return RedirectResponse("/istota/setup", status_code=302)

    @app.get("/istota/api/setup/state")
    async def setup_state(request: Request):
        from istota.modules import module_available  # noqa: PLC0415

        expire_if_idle(state)
        open_here = _has_session(state, request)
        if open_here:
            state.last_activity = state.clock()
        if state.finished:
            phase = "finishing"
        else:
            phase = "open" if open_here else "locked"
        body = {
            "phase": phase,
            "rerun": state.rerun,
            "local": state.local,
            "host": request.headers.get("host", ""),
            # What "Only on this computer" answers at: the port Lima forwards.
            "local_address": setup_wizard.local_address(state.forwarded_port or setup_wizard.DEFAULT_LOCAL_PORT),
            "modules_available": [
                {"field": name, "module": module}
                for name, module, _question in setup_wizard._OPT_OUT_MODULES
                if module_available(module)
            ],
            "memory_total_mib": _memory_total_mib(),
            "defaults": {
                "bot_name": "Istota", "storage": "local", "profiles": ["browser"],
                # The two API-key services on the AI service screen.
                "models": {
                    "anthropic": {
                        "base_url": setup_wizard.DEFAULT_ANTHROPIC_BASE_URL,
                        "model": setup_wizard.default_anthropic_model(),
                    },
                    "openrouter": {
                        "base_url": setup_wizard.DEFAULT_OPENROUTER_BASE_URL,
                        "model": setup_wizard.default_openrouter_model(),
                    },
                },
            },
        }
        # This route needs no session and a server's is public, so whose
        # setup this is goes only to the session.
        if state.rerun and open_here:
            body["user_id"] = state.existing_user
        return body

    @app.post("/istota/api/setup/unlock")
    async def unlock(request: Request):
        _require_origin(state, request)
        client = request.client.host if request.client else ""
        now = state.clock()
        recent = [t for t in state.failures_by_client.get(client, []) if now - t < UNLOCK_WINDOW_SECONDS]
        state.failures_by_client[client] = recent
        if len(recent) >= UNLOCK_FAILURES_PER_WINDOW:
            return _error(429, "throttled", "Too many tries. Wait a minute and try again.")
        body = await _read_json(request)
        submitted = body.get("code") if isinstance(body, dict) else None
        if not isinstance(submitted, str) or not submitted.strip():
            return _error(400, "bad_body", "Enter your setup code.")
        if state.code_spent or state.failures_total >= UNLOCK_FAILURES_TOTAL:
            return _error(403, "forbidden", _CODE_REFUSED)
        if not hmac.compare_digest(normalize_code(submitted).encode(), state.code.encode()):
            state.failures_total += 1
            recent.append(now)
            logger.info("setup: a wrong setup code (%d so far)", state.failures_total)
            if state.failures_total >= UNLOCK_FAILURES_TOTAL and state.session_id is None:
                # Stop rather than refuse forever: a server nobody can unlock
                # holds the setup lock and the port, so no new run could start.
                logger.info("setup: too many wrong setup codes; stopping")
                response = _error(403, "locked_out", _LOCKED_OUT)
                response.background = BackgroundTask(request_exit, state, EXIT_LOCKED_OUT)
                return response
            return _error(403, "forbidden", _CODE_REFUSED)
        state.code_spent = True
        state.session_id = secrets.token_urlsafe(32)
        state.last_activity = now
        logger.info("setup: unlocked")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE, state.session_id,
            httponly=True, samesite="strict", path="/istota", secure=not state.local,
        )
        return response

    @app.get("/istota/api/setup/draft")
    async def get_draft(request: Request):
        _require_session(state, request)
        return {"draft": _redacted_draft(state.draft)}

    @app.put("/istota/api/setup/draft")
    async def put_draft(request: Request):
        _require_session(state, request)
        _require_origin(state, request)
        body = await _read_json(request)
        if not isinstance(body, dict):
            return _error(400, "bad_body", "The setup answers must be a JSON object.")
        # A key is not echoed: it is somebody's input, and could be a pasted secret.
        if any(key not in DRAFT_FIELDS for key in body):
            return _error(400, "unknown_field", "The request named a field setup does not ask.")
        password = body.get(PASSWORD_FIELD)
        if password is not None and not isinstance(password, str):
            return _error(
                400, "bad_body", "The password must be text.",
                [FieldError(PASSWORD_FIELD, "The password must be text.")],
            )
        for key, value in body.items():
            if value is None:
                # A credential sent as null keeps what is stored, so a page
                # that only ever saw `{set: true}` cannot erase it.
                if key not in CREDENTIAL_FIELDS:
                    state.draft.pop(key, None)
                continue
            state.draft[key] = value
            state.stored_credentials.discard(key)
        _drop_moved_credentials(state)
        return {"draft": _redacted_draft(state.draft)}

    @app.post("/istota/api/setup/validate")
    async def validate(request: Request):
        _require_session(state, request)
        _require_origin(state, request)
        body = await _read_json(request)
        step = body.get("step") if isinstance(body, dict) else None
        if step not in setup_steps(state.rerun):
            return _error(400, "unknown_step", "Unknown setup step.")
        errors = step_errors(state, state.draft, step)
        return {
            "ok": not errors,
            "fields": [{"field": e.field, "message": e.message} for e in errors],
        }

    @app.get("/istota/api/setup/models")
    async def models(request: Request):
        _require_session(state, request)
        async with state.model_ids_lock:
            if state.model_ids is None:
                state.model_ids = await openrouter_model_ids(state.check_seconds)
        return {"models": state.model_ids}

    @app.post("/istota/api/setup/check/{kind}")
    async def check(kind: str, request: Request):
        _require_session(state, request)
        _require_origin(state, request)
        if kind not in CHECK_KINDS:
            return _error(400, "unknown_check", "Unknown connection check.")
        try:
            a = _answers_for(state, state.draft)
        except PayloadError as exc:
            return _error(
                400, "invalid", "Some answers need fixing before this can be tested.",
                _plain(exc.errors, exc.answers),
            )
        refusal = _check_refusal(kind, a)
        if refusal is not None:
            return refusal
        now = state.clock()
        state.check_times = [t for t in state.check_times if now - t < _CHECK_WINDOW_SECONDS]
        if len(state.check_times) >= CHECKS_PER_MINUTE:
            return _error(429, "throttled", "That's a lot of tests in a minute. Wait a moment and try again.")
        state.check_times.append(now)

        result = await _run_check(kind, a, request.headers.get("host", ""), state.check_seconds)
        logger.info("setup: check %s ok=%s", kind, result.ok)
        return result.to_json()

    @app.post("/istota/api/setup/finish")
    async def finish_route(request: Request):
        _require_session(state, request)
        _require_origin(state, request)
        # One Finish at a time: a double click must not run the writers twice
        # side by side.
        async with state.finish_lock:
            if state.finished:
                return _error(409, "finished", "Setup has already finished.")
            a, errors = draft_errors(state, state.draft)
            if errors:
                return _invalid_at_finish(errors, state.draft, setup_steps(state.rerun))
            try:
                await asyncio.to_thread(
                    finish, a, state.draft.get(PASSWORD_FIELD, ""),
                    data_dir=state.data_dir, vm_dir=state.vm_dir, rerun=state.rerun,
                )
            except FinishError as exc:
                return JSONResponse(
                    {"error": "write_failed", "message": exc.message, "step": exc.step},
                    status_code=500,
                )
            state.finished = True
            # The draft holds every credential and the password; nothing
            # needs them now.
            state.draft.clear()
        result = {"login_url": login_url(a)}
        if not state.rerun:
            result["email"] = auth_email(a.user_email)
        # Asked for after the response is sent, so the page gets it first.
        return JSONResponse(result, background=BackgroundTask(request_exit, state, 0))

    @app.api_route(
        "/istota/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def _no_such_api(rest: str):
        return _error(404, "not_found", "Not found.")

    static_dir = resolve_static_dir()
    if static_dir.is_dir():
        app.mount("/istota", cache_header_statics(static_dir), name="web-static")
    return app


def _invalid_at_finish(
    errors: list[FieldError], draft: dict, steps: dict[str, tuple[str, ...]],
) -> JSONResponse:
    """Every error with the screen that asks it, and the first such screen."""
    where = [step_of(e.field, draft, steps) for e in errors]
    order = list(steps)
    body = {
        "error": "invalid",
        "message": "Some answers need fixing before setup can finish.",
        "step": min(where, key=order.index),
        "fields": [
            {"field": e.field, "message": e.message, "step": step}
            for e, step in zip(errors, where)
        ],
    }
    return JSONResponse(body, status_code=400)


class FinishError(Exception):
    """A Finish step that failed, with what to tell the user."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


#: What each Finish step says when it fails, before the reason.
_FINISH_FAILURES = {
    "settings": "Couldn't save the settings",
    "account": "Couldn't create your account",
    "marker": "Couldn't record that setup finished",
}
#: The marker `istota-stack` reads to tell a finished run from an aborted one.
SETUP_DONE = "setup-done"


def _reason(exc: BaseException) -> str:
    """A failure's reason without its errno or path: ``strerror`` for an OSError."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


def _failed(step: str, exc: BaseException) -> FinishError:
    logger.warning("setup: finish: %s failed: %s: %s", step, type(exc).__name__, _reason(exc))
    return FinishError(step, f"{_FINISH_FAILURES[step]}: {_reason(exc)}")


def db_path_under(data_dir: Path) -> Path:
    """The stack's database path, under ``data_dir`` rather than ``/data``."""
    return setup_wizard.container_db_path(data_dir)


def login_url(a: setup_wizard.ContainerAnswers) -> str:
    return f"{a.public_scheme}://{a.hostname}/istota/login"


def auth_email(raw: str) -> str:
    from . import auth  # noqa: PLC0415

    return auth.normalize_email(raw)


def finish(
    a: setup_wizard.ContainerAnswers, password: str, *, data_dir: Path, vm_dir: Path,
    rerun: bool = False,
) -> None:
    """Write everything ``a`` decides, the first admin's login, and the marker.

    Stops at the first failure with a ``FinishError`` naming the step. Every
    step is safe to run again: the writers rewrite the config and env files
    and keep an existing master key and admins file, the profile and the
    identity are ensured, and a second ``set_password`` only bumps an epoch
    nobody has signed in under yet.

    A re-run (``rerun``) backs the config up before replacing it and leaves
    the database alone: the login, the profile and the password are the
    first run's, and the profile row wins over ``[users.X]`` anyway.
    """
    # The web form offers no host folder, so its "local" on a VM whose
    # workspace is the Mac's folder keeps that folder, as the terminal does.
    if not a.nextcloud_url and setup_wizard.current_storage(vm_dir) == "host-folder":
        a.host_folder = True
    try:
        setup_wizard.write_container_setup(
            a, data_dir=data_dir, vm_dir=vm_dir, out=lambda line: logger.info("setup: %s", line.strip()),
            backup=setup_wizard.RERUN_CONFIG_BACKUPS if rerun else None,
        )
    except (OSError, SetupError) as exc:
        raise _failed("settings", exc) from None
    logger.info("setup: finish: settings written")
    if not rerun:
        _finish_account(a, password, data_dir=data_dir)
    _write_marker(vm_dir)


def _finish_account(a: setup_wizard.ContainerAnswers, password: str, *, data_dir: Path) -> None:
    """The first admin's database rows: the profile, the identity, the password."""
    import sqlite3  # noqa: PLC0415

    from istota import db, user_profiles  # noqa: PLC0415
    from istota.config import config_from_document  # noqa: PLC0415

    from . import auth  # noqa: PLC0415

    db_path = db_path_under(data_dir)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db.init_db(db_path)
        # The row the entrypoint makes for an empty table, seeded from the
        # `[users.<id>]` block just written, as the entrypoint seeds it.
        seed = config_from_document(
            setup_wizard.container_config_document(a, inline_credentials=False)
        ).users.get(a.user_id)
        user_profiles.ensure_profile(
            db_path, a.user_id, display_name=a.display_name, timezone=a.timezone, seed_from=seed,
        )
        auth.upsert_identity(db_path, a.user_id, a.user_email, create_profile=False)
        auth.set_password(db_path, a.user_id, password)
    except (OSError, sqlite3.Error, ValueError, db.SchemaTooNew) as exc:
        raise _failed("account", exc) from None
    logger.info("setup: finish: account ready for %s", a.user_id)


def _write_marker(vm_dir: Path) -> None:
    marker = Path(vm_dir) / SETUP_DONE
    try:
        marker.unlink(missing_ok=True)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            os.fchmod(fd, 0o400)
        finally:
            os.close(fd)
    except OSError as exc:
        raise _failed("marker", exc) from None
    logger.info("setup: finish: done")


def _run_server(app: FastAPI, settings: dict) -> int:
    """Run uvicorn until the app asks it to stop; the process's exit code."""
    import uvicorn  # noqa: PLC0415

    server = uvicorn.Server(uvicorn.Config(app, **settings))
    state: SetupState = app.state.setup

    def stop(_code: int) -> None:
        server.should_exit = True

    state.on_exit = stop
    server.run()
    return state.exit_code or 0


def _argument_refusal(args) -> str | None:
    if not getattr(args, "web", False):
        return "--local goes with --web."
    if not getattr(args, "vm_dir", None):
        return "--web needs --vm-dir, the staging directory istota-stack hands it."
    return None


def serve(args, *, out=print) -> int:
    """``istota setup --web [--force] [--local]``: run the setup server to completion.

    Refuses, before binding, when the arguments cannot mean a web setup, when
    a first run finds a config, or when a re-run (``--force``) finds none or
    one it cannot start from.
    """
    refusal = _argument_refusal(args)
    if refusal:
        raise SetupError(refusal)
    data_dir = Path(getattr(args, "data_dir", None) or setup_wizard.CONTAINER_DATA_DIR)
    vm_dir = Path(args.vm_dir)
    local = bool(getattr(args, "local", False))
    app = create_setup_app(
        data_dir=data_dir, vm_dir=vm_dir, code_path=vm_dir / "setup-code", local=local,
        rerun=bool(getattr(args, "force", False)),
    )
    settings: dict = {
        "host": "0.0.0.0",
        "port": LOCAL_PORT if local else TLS_PORT,
        "log_level": "info",
        "access_log": False,
    }
    if not local:
        cert, key = vm_dir / "cert.pem", vm_dir / "key.pem"
        missing = [p.name for p in (cert, key) if not p.is_file()]
        if missing:
            raise SetupError(
                f"No {' or '.join(missing)} in {vm_dir}; start setup with "
                "`istota-stack setup --web`, or pass --local for plain HTTP on this machine."
            )
        settings["ssl_certfile"] = str(cert)
        settings["ssl_keyfile"] = str(key)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out(f"Setup server listening on port {settings['port']}{'' if local else ' (TLS)'}.")
    return _run_server(app, settings)
