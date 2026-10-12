"""The first-run setup's connection checks: does what the user typed work.

Each check is handed a ``Config`` built from exactly the document setup would
write (``config_for_check``: ``container_config_document`` with the
credentials inline, mapped by ``config.config_from_document``, the mapping
``load_config`` uses), and calls the client istota already uses for that
service: the native brain's one-shot completion, the Nextcloud OCS and WebDAV
helpers, ``skills.email``'s IMAP session and SMTP opener, and the calendar
skill's CalDAV client. A check that passes has tested the config, not a
parallel reimplementation of it.

What the browser gets is a fixed plain-language message per outcome. The raw
failure (an exception, a server's error body) is logged at INFO with every
credential in the config replaced, and never returned: an upstream body can
quote what it was sent. A check writes nothing.

Every check works to one deadline. The checks are synchronous apart from the
model's, and the setup server runs them on a worker thread under the same
deadline plus a little, so a client that ignores its own timeout still cannot
hold the response.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from istota.config import Config, config_from_document

logger = logging.getLogger("istota.setup_web")

CHECK_SECONDS = 10.0

#: The Nextcloud check's lines, in the order the screen's checklist asks.
NEXTCLOUD_LINES = ("server", "signin", "admin", "files", "talk", "user")

_INTERNAL_ADDRESS_HINT = (
    " If this server reaches Nextcloud on another address, enter it under More, "
    "in “Address this server uses to reach Nextcloud”."
)


@dataclass(frozen=True)
class CheckLine:
    key: str
    ok: bool
    message: str
    warning: bool = False

    def to_json(self) -> dict:
        return {"key": self.key, "ok": self.ok, "warning": self.warning, "message": self.message}


@dataclass
class CheckResult:
    ok: bool
    message: str
    warning: bool = False
    lines: list[CheckLine] | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        body: dict = {"ok": self.ok, "warning": self.warning, "message": self.message}
        if self.lines is not None:
            body["lines"] = [line.to_json() for line in self.lines]
        body.update(self.extra)
        return body


class Deadline:
    """Seconds left for one check, shared by every request it makes."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def timeout(self) -> float:
        """A per-request timeout: what is left, never zero."""
        return max(0.5, self.remaining())


def config_for_check(answers) -> Config:
    """The ``Config`` the setup would write for ``answers``, credentials inline."""
    from istota import setup_wizard  # noqa: PLC0415 - setup_wizard is heavy and not needed to import this

    return config_from_document(setup_wizard.container_config_document(answers, inline_credentials=True))


def _credentials(config: Config) -> list[str]:
    values = [
        config.brain.native.api_key,
        config.nextcloud.app_password,
        config.email.imap_password,
        config.email.smtp_password,
        config.caldav.password,
    ]
    return [value for value in values if value]


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


def log_failure(kind: str, step: str, detail, config: Config) -> None:
    """The raw reason a check failed, for the operator, credentials removed."""
    if isinstance(detail, BaseException):
        detail = f"{type(detail).__name__}: {_chain_text(detail)}"
    logger.info("setup: check %s %s failed: %s", kind, step, _redact(str(detail), _credentials(config)))


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        parts.append(f"{type(node).__name__} {node}")
        node = node.__cause__ or node.__context__
    return " | ".join(parts)


def _chain_types(exc: BaseException | None) -> list[type]:
    types: list[type] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        types.append(type(exc))
        exc = exc.__cause__ or exc.__context__
    return types


_DNS_MARKERS = (
    "name or service not known", "nodename nor servname", "temporary failure in name resolution",
    "no address associated", "failed to resolve", "getaddrinfo failed", "errno -2]", "errno -3]",
    "errno -5]", "errno 8]", "errno 11001]", "nameresolutionerror", "gaierror",
)
_REFUSED_MARKERS = ("connection refused", "errno 61]", "errno 111]", "errno 10061]")
_TIMEOUT_MARKERS = ("timed out", "timeout")
_TLS_MARKERS = ("certificate", "ssl", "tls", "wrong_version_number")


def unreachable_message(host: str, *, exc: BaseException | None = None, text: str = "",
                        seconds: float = CHECK_SECONDS) -> str:
    """Why ``host`` could not be reached, from the failure's type and text.

    The text only chooses among fixed messages; none of it is returned.
    """
    types = _chain_types(exc)
    haystack = (text + " " + (_chain_text(exc) if exc else "")).lower()
    if socket.gaierror in types or any(marker in haystack for marker in _DNS_MARKERS):
        return f"Couldn't reach {host}: the name doesn't resolve."
    if ConnectionRefusedError in types or any(marker in haystack for marker in _REFUSED_MARKERS):
        return f"Couldn't reach {host}: the connection was refused."
    if any(issubclass(t, ssl.SSLError) for t in types) or any(m in haystack for m in _TLS_MARKERS):
        return f"Couldn't make a secure connection to {host}."
    if any(issubclass(t, TimeoutError) for t in types) or any(m in haystack for m in _TIMEOUT_MARKERS):
        return timed_out_message(host, seconds)
    return f"Couldn't reach {host}."


def timed_out_message(host: str, seconds: float = CHECK_SECONDS) -> str:
    return f"{host} didn't answer within {_seconds(seconds)}."


def _seconds(seconds: float) -> str:
    whole = math.ceil(seconds) if seconds >= 1 else seconds
    return f"{whole:g} seconds" if whole != 1 else "1 second"


# --- the model -----------------------------------------------------------------


async def check_model(config: Config, *, seconds: float = CHECK_SECONDS) -> CheckResult:
    """One small completion through the native brain's provider and one-shot path."""
    from istota.llm import make_provider  # noqa: PLC0415
    from istota.llm.oneshot import acomplete_message  # noqa: PLC0415
    from istota.session.retry import extract_status_code  # noqa: PLC0415

    native = config.brain.native
    host = host_of(native.base_url)
    provider = make_provider(native)
    try:
        message = await asyncio.wait_for(
            acomplete_message(provider, "", "Reply with the word OK.", model=native.model, max_tokens=16),
            timeout=seconds,
        )
    except asyncio.TimeoutError:
        log_failure("model", "completion", "timed out", config)
        return CheckResult(False, timed_out_message(host, seconds))
    except Exception as exc:  # noqa: BLE001 - every failure becomes a plain message
        log_failure("model", "completion", exc, config)
        return CheckResult(False, unreachable_message(host, exc=exc, seconds=seconds))
    finally:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - closing a pool must not turn a result into an error
            pass

    if message is not None and message.stop_reason != "error":
        return CheckResult(True, "The key works, and the model answered.")
    error_text = (message.error_message if message is not None else "") or ""
    log_failure("model", "completion", error_text or "no answer", config)
    status = extract_status_code(error_text)
    if status in (401, 403):
        return CheckResult(False, "That API key was not accepted.")
    if status == 402:
        return CheckResult(False, "The key was accepted, but the account has no credit left.")
    if status == 404:
        return CheckResult(False, f"The service doesn't know a model called “{native.model}”.")
    if status == 429:
        return CheckResult(False, "The service is turning away requests from this key right now. Try again in a minute.")
    if status is not None and status >= 500:
        return CheckResult(False, "The service had a problem answering. Try again in a minute.")
    if status is not None:
        return CheckResult(False, "The service refused the test request. Check the model name.")
    return CheckResult(False, unreachable_message(host, text=error_text, seconds=seconds))


# --- Nextcloud -----------------------------------------------------------------


def check_nextcloud(config: Config, own_user_id: str, *, seconds: float = CHECK_SECONDS) -> CheckResult:
    """The Nextcloud screen's six lines, each with its own result.

    A line that cannot be checked because an earlier one failed says so and
    counts as failed; a warning is ``ok`` with ``warning`` set.
    """
    from istota.nextcloud._http import OcsError, dav_files_url, dav_request  # noqa: PLC0415
    from istota.nextcloud.capabilities import fetch_account, fetch_capabilities, summarize  # noqa: PLC0415
    from istota.nextcloud.shares import search_sharees  # noqa: PLC0415

    deadline = Deadline(seconds)
    host = host_of(config.nextcloud.url)
    username = config.nextcloud.username
    lines: list[CheckLine] = []

    def unreachable(exc: BaseException) -> str:
        cause = exc.__cause__ or exc
        return unreachable_message(host, exc=cause, seconds=seconds)

    def finish(talk_available: bool = False) -> CheckResult:
        done = {line.key for line in lines}
        for key in NEXTCLOUD_LINES:
            if key not in done:
                lines.append(CheckLine(key, False, "Not checked, because a step above failed."))
        failed = next((line for line in lines if not line.ok), None)
        if failed is not None:
            return CheckResult(False, failed.message, lines=lines,
                               extra={"talk_available": talk_available})
        warned = any(line.warning for line in lines)
        summary = "Signed in, and Talk is available." if talk_available else "Signed in."
        return CheckResult(True, summary, warning=warned, lines=lines,
                           extra={"talk_available": talk_available})

    # 1. The address answers as a Nextcloud.
    capabilities: dict = {}
    try:
        capabilities = fetch_capabilities(config, timeout=deadline.timeout())
        version = summarize(capabilities)["server"]["version"]
        lines.append(CheckLine("server", True, f"Found Nextcloud {version}." if version else "Found Nextcloud."))
    except OcsError as exc:
        log_failure("nextcloud", "capabilities", exc, config)
        if exc.ocs_status is not None or exc.http_status == 401:
            # It spoke OCS, or refused the credentials: a Nextcloud, and the
            # next line says what is wrong with the sign-in.
            lines.append(CheckLine("server", True, "Found Nextcloud."))
        elif exc.http_status is None:
            lines.append(CheckLine("server", False, unreachable(exc) + _INTERNAL_ADDRESS_HINT))
            return finish()
        else:
            lines.append(CheckLine("server", False, "Couldn't find a Nextcloud at this address."))
            return finish()

    # 2. The username and app password sign in, as the account named.
    try:
        account = fetch_account(config, timeout=deadline.timeout())
    except OcsError as exc:
        log_failure("nextcloud", "account", exc, config)
        if exc.http_status == 401 or exc.ocs_status == 997:
            message = "That username and app password weren't accepted. Check step 3."
        elif exc.http_status is None:
            message = unreachable(exc)
        else:
            message = "Nextcloud refused to show the account's details."
        lines.append(CheckLine("signin", False, message))
        return finish()
    account_id = str(account.get("id") or "")
    if account_id != username:
        lines.append(CheckLine(
            "signin", False,
            f"Those details sign in as “{account_id}”. Enter that as the assistant's username.",
        ))
        return finish()
    lines.append(CheckLine("signin", True, f"Signed in as {username}."))

    # 3. The account is not an administrator: a warning, not a refusal.
    groups = account.get("groups") or []
    if "admin" in groups:
        lines.append(CheckLine(
            "admin", True,
            "This account is an administrator. Istota works better with an ordinary account (step 1).",
            warning=True,
        ))
    else:
        lines.append(CheckLine("admin", True, "The account is not an administrator."))

    # 4. The files answer over WebDAV, at the path the rclone mount uses.
    try:
        dav_request(
            config, "PROPFIND", dav_files_url(config, "", prefixed=False) + "/",
            headers={"Depth": "0"}, timeout=deadline.timeout(),
        )
        lines.append(CheckLine("files", True, "The account's files are reachable."))
    except OcsError as exc:
        log_failure("nextcloud", "files", exc, config)
        lines.append(CheckLine("files", False, "Signed in, but couldn't open the account's files."))

    # 5. Talk.
    talk_available = bool(summarize(capabilities)["talk"]["available"]) if capabilities else False
    if talk_available:
        lines.append(CheckLine("talk", True, "Talk is installed."))
    else:
        lines.append(CheckLine(
            "talk", True,
            "Talk isn't installed, so you'll chat with Istota in the web app only.",
            warning=True,
        ))

    # 6. The user's own account exists, through the sharee search an ordinary
    # account may call.
    try:
        sharees = search_sharees(config, own_user_id, timeout=deadline.timeout())
        candidates = list((sharees.get("exact") or {}).get("users") or []) + list(sharees.get("users") or [])
        found = any(
            isinstance(entry, dict)
            and isinstance(entry.get("value"), dict)
            and entry["value"].get("shareWith") == own_user_id
            for entry in candidates
        )
        if found:
            lines.append(CheckLine("user", True, f"Found your Nextcloud account, {own_user_id}."))
        else:
            lines.append(CheckLine("user", True, f"Couldn't find a Nextcloud user named {own_user_id}.", warning=True))
    except OcsError as exc:
        log_failure("nextcloud", "sharees", exc, config)
        lines.append(CheckLine(
            "user", True, f"Couldn't look up Nextcloud users to check {own_user_id}.", warning=True,
        ))
    return finish(talk_available)


# --- email ---------------------------------------------------------------------


def check_email(config: Config, *, seconds: float = CHECK_SECONDS) -> CheckResult:
    """An IMAP login and logout, then SMTP's EHLO, STARTTLS and AUTH; no message."""
    try:
        from istota.mail.support import get_email_config  # noqa: PLC0415
        from istota.skills.email import _open_smtp, imap_session  # noqa: PLC0415
    except ImportError as exc:
        log_failure("email", "import", exc, config)
        return CheckResult(False, "This installation can't test an email connection.")

    deadline = Deadline(seconds)
    email = get_email_config(config)
    lines: list[CheckLine] = []

    email.imap_timeout = math.ceil(deadline.timeout())
    try:
        with imap_session(email):
            pass
        lines.append(CheckLine("imap", True, f"Signed in to {email.imap_host}."))
    except ImportError as exc:
        log_failure("email", "imap", exc, config)
        return CheckResult(False, "This installation can't test an email connection.")
    except Exception as exc:  # noqa: BLE001 - every failure becomes a plain message
        log_failure("email", "imap", exc, config)
        if type(exc).__name__ == "MailboxLoginError":
            message = "The mail server didn't accept that username and password."
        else:
            message = unreachable_message(email.imap_host, exc=exc, seconds=seconds)
        lines.append(CheckLine("imap", False, message))
        lines.append(CheckLine("smtp", False, "Not checked, because a step above failed."))
        return CheckResult(False, message, lines=lines)

    smtp_host = email.smtp_host
    stage = "connect"
    try:
        server = _open_smtp(email, timeout=deadline.timeout())
        try:
            stage = "auth"
            server.login(email.effective_smtp_user, email.effective_smtp_password)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - a failed goodbye is not a failed check
                server.close()
        lines.append(CheckLine("smtp", True, f"Signed in to {smtp_host} for sending."))
    except smtplib.SMTPAuthenticationError as exc:
        log_failure("email", "smtp", exc, config)
        message = "The outgoing mail server didn't accept that username and password."
        lines.append(CheckLine("smtp", False, message))
        return CheckResult(False, message, lines=lines)
    except smtplib.SMTPNotSupportedError as exc:
        log_failure("email", "smtp", exc, config)
        if stage == "connect":
            message = f"The outgoing mail server at {smtp_host} doesn't offer a secure connection."
        else:
            message = f"The outgoing mail server at {smtp_host} doesn't accept a sign-in."
        lines.append(CheckLine("smtp", False, message))
        return CheckResult(False, message, lines=lines)
    except smtplib.SMTPException as exc:
        log_failure("email", "smtp", exc, config)
        message = f"The outgoing mail server at {smtp_host} refused the test."
        lines.append(CheckLine("smtp", False, message))
        return CheckResult(False, message, lines=lines)
    except Exception as exc:  # noqa: BLE001
        log_failure("email", "smtp", exc, config)
        message = unreachable_message(smtp_host, exc=exc, seconds=seconds)
        lines.append(CheckLine("smtp", False, message))
        return CheckResult(False, message, lines=lines)
    return CheckResult(True, "Signed in for receiving and sending mail.", lines=lines)


# --- calendar ------------------------------------------------------------------


def check_caldav(config: Config, *, seconds: float = CHECK_SECONDS) -> CheckResult:
    """A ``PROPFIND`` on the calendar address, through the calendar skill's client."""
    try:
        from caldav.lib import error as caldav_error  # noqa: PLC0415

        from istota.skills.calendar import get_caldav_client  # noqa: PLC0415
    except ImportError as exc:
        log_failure("caldav", "import", exc, config)
        return CheckResult(False, "This installation can't test a calendar connection.")

    url = config.caldav_url
    host = host_of(url)
    try:
        client = get_caldav_client(
            url, config.caldav_username, config.caldav_password, timeout=math.ceil(seconds),
        )
        response = client.propfind(url, depth=0)
    except caldav_error.AuthorizationError as exc:
        log_failure("caldav", "propfind", exc, config)
        return CheckResult(False, "That username and password weren't accepted.")
    except caldav_error.NotFoundError as exc:
        log_failure("caldav", "propfind", exc, config)
        return CheckResult(False, "Nothing at that address answered as a calendar.")
    except caldav_error.DAVError as exc:
        log_failure("caldav", "propfind", exc, config)
        return CheckResult(False, "The calendar server refused the test.")
    except Exception as exc:  # noqa: BLE001
        log_failure("caldav", "propfind", exc, config)
        return CheckResult(False, unreachable_message(host, exc=exc, seconds=seconds))
    status = getattr(response, "status", None)
    if isinstance(status, int) and 200 <= status < 300:
        return CheckResult(True, "Signed in to the calendar.")
    log_failure("caldav", "propfind", f"status {status}", config)
    if status == 401:
        return CheckResult(False, "That username and password weren't accepted.")
    return CheckResult(False, "Nothing at that address answered as a calendar.")


# --- the web address -----------------------------------------------------------


def _bare_host(value: str) -> str:
    """A ``Host``-style value without its port or IPv6 brackets."""
    value = value.strip()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _addresses(name: str) -> set[str]:
    try:
        return {str(ipaddress.ip_address(name))}
    except ValueError:
        pass
    infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    return {str(ipaddress.ip_address(info[4][0].split("%", 1)[0])) for info in infos}


def check_address(hostname: str, browser_host: str) -> CheckResult:
    """Whether ``hostname`` resolves, here, to the address the browser used.

    Never a refusal: a name may still be propagating, so a mismatch or a name
    that does not resolve yet is a warning.
    """
    name = _bare_host(hostname)
    here = _bare_host(browser_host)
    try:
        resolved = _addresses(name)
    except (OSError, UnicodeError) as exc:
        logger.info("setup: check address resolve failed: %s", _chain_text(exc))
        return CheckResult(
            False,
            f"{name} doesn't resolve yet. Point it at {here} with your domain provider; "
            "it can take a while to take effect.",
            warning=True,
        )
    try:
        expected = _addresses(here)
    except (OSError, UnicodeError):
        expected = set()
    shown = ", ".join(sorted(resolved))
    if resolved & expected:
        return CheckResult(True, f"{name} points at this server ({shown}).")
    return CheckResult(
        False,
        f"{name} points at {shown}, not at this server's address, {here}. "
        "If you changed it recently, it can take a while to take effect.",
        warning=True,
    )
