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
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

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
    "model": ("brain_kind", "native_base_url", "native_model", "native_api_key", "claude_code_oauth_token"),
    "address": ("hostname", "ingress", "upstream_proxy", "listen_addr", "listen_port", "tls_cert_source"),
    "nextcloud": (
        "storage", "nextcloud_url", "nextcloud_public_url", "nextcloud_username",
        "nextcloud_app_password", "talk_enabled", "user_id",
    ),
    "connections": (
        "email_enabled", "imap_host", "imap_user", "imap_password", "smtp_host", "bot_email",
        "caldav_url", "caldav_username", "caldav_password",
    ),
    "features": (*_MODULE_FIELDS, "profiles"),
}
SETUP_STEPS["review"] = tuple(dict.fromkeys(f for fields in SETUP_STEPS.values() for f in fields))

_ALREADY_SET_UP = (
    "istota is already set up; use the admin Configuration page or "
    "`istota-stack setup --force`."
)
_CODE_REFUSED = (
    "That setup code is not valid, or it has already been used. Run "
    "`sudo istota-stack setup --web` again for a new one."
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


def _require_origin(request: Request) -> None:
    if origin_refusal(request.headers, request.headers.get("host", "")):
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


def step_errors(draft: dict, step: str) -> list[FieldError]:
    """The draft's errors on ``step``'s fields, plus any form-level error."""
    answers = {key: value for key, value in draft.items() if key != PASSWORD_FIELD}
    errors: list[FieldError] = []
    try:
        a = setup_wizard.answers_from_payload(answers)
    except PayloadError as exc:
        errors.extend(exc.errors)
    else:
        errors.extend(setup_wizard.validate_container_answers(a))
    if PASSWORD_FIELD in SETUP_STEPS[step]:
        errors.extend(_password_errors(draft))
    wanted = set(SETUP_STEPS[step])
    return [e for e in errors if e.field == "" or e.field in wanted]


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


def create_setup_app(
    *, data_dir: Path, vm_dir: Path, code_path: Path, local: bool,
    clock: Callable[[], float] = time.monotonic,
    check_seconds: float = setup_checks.CHECK_SECONDS,
) -> FastAPI:
    """The setup app over ``data_dir``, refusing when a config already exists."""
    data_dir = Path(data_dir)
    if (data_dir / "config" / "config.toml").exists():
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
        check_seconds=check_seconds,
    )

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
        return {
            "phase": "open" if open_here else "locked",
            "local": state.local,
            "host": request.headers.get("host", ""),
            "modules_available": [
                {"field": name, "module": module}
                for name, module, _question in setup_wizard._OPT_OUT_MODULES
                if module_available(module)
            ],
            "memory_total_mib": _memory_total_mib(),
            "defaults": {"bot_name": "Istota", "storage": "local", "profiles": ["browser"]},
        }

    @app.post("/istota/api/setup/unlock")
    async def unlock(request: Request):
        _require_origin(request)
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
        _require_origin(request)
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
        return {"draft": _redacted_draft(state.draft)}

    @app.post("/istota/api/setup/validate")
    async def validate(request: Request):
        _require_session(state, request)
        _require_origin(request)
        body = await _read_json(request)
        step = body.get("step") if isinstance(body, dict) else None
        if step not in SETUP_STEPS:
            return _error(400, "unknown_step", "Unknown setup step.")
        errors = step_errors(state.draft, step)
        return {
            "ok": not errors,
            "fields": [{"field": e.field, "message": e.message} for e in errors],
        }

    @app.post("/istota/api/setup/check/{kind}")
    async def check(kind: str, request: Request):
        _require_session(state, request)
        _require_origin(request)
        if kind not in CHECK_KINDS:
            return _error(400, "unknown_check", "Unknown connection check.")
        answers = {key: value for key, value in state.draft.items() if key != PASSWORD_FIELD}
        try:
            a = setup_wizard.answers_from_payload(answers)
        except PayloadError as exc:
            return _error(400, "invalid", "Some answers need fixing before this can be tested.", exc.errors)
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
    if getattr(args, "force", False):
        return (
            "--force does not go with --web: the web setup is for a first run only. "
            "Use `istota-stack setup --force` to replace an existing config."
        )
    if setup_wizard.setup_shape(args) != "container":
        return "--web sets up the container; it does not go with --shape standalone."
    if not getattr(args, "vm_dir", None):
        return "--web needs --vm-dir, the staging directory istota-stack hands it."
    return None


def serve(args, *, out=print) -> int:
    """``istota setup --web [--local]``: run the setup server to completion.

    Refuses, before binding, when the arguments cannot mean a web setup or a
    config already exists.
    """
    refusal = _argument_refusal(args)
    if refusal:
        raise SetupError(refusal)
    data_dir = Path(getattr(args, "data_dir", None) or setup_wizard.CONTAINER_DATA_DIR)
    vm_dir = Path(args.vm_dir)
    local = bool(getattr(args, "local", False))
    app = create_setup_app(
        data_dir=data_dir, vm_dir=vm_dir, code_path=vm_dir / "setup-code", local=local,
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
