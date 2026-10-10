"""First-run installer (``istota setup``), in two halves.

**The container half** (``--shape container``, the default inside the image,
which sets ``ISTOTA_SETUP_SHAPE``) is how every install of the one deployment
shape gets its config. It runs inside the istota image, as the daemon's uid,
and writes ``/data/config/config.toml``, ``/data/config/admins`` and
``/data/.secret_key``; with ``--vm-dir`` it also writes the stack's ``.env``,
``host.env`` and one file per credential under ``secrets/``, which compose mounts
at ``/run/secrets`` and the entrypoint reads into the daemon's environment.
Nothing renders ``config.toml`` after this: the operator owns it.

**The standalone half** (``--shape standalone``) is the local single-user
install: ``config.toml`` plus a secrets ``istota.env`` on the standard config
search path, a DB, a seeded workspace. Deprecated, and kept until the Mac app
replaces it; nothing in it depends on the container half.

Both keep the wizard logic apart from I/O: prompts go through an injectable
``input_fn``, secrets through ``getpass_fn``, and the renderers are pure
functions of an answers object.
"""

from __future__ import annotations

import getpass
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from istota.lib import toml_write

logger = logging.getLogger("istota.setup")


DEFAULT_WORKSPACE = "~/.istota"
DEFAULT_PORT = 8766
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "istota" / "config.toml"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"


@dataclass
class Answers:
    workspace: Path = field(default_factory=lambda: Path(DEFAULT_WORKSPACE).expanduser())
    user_id: str = "local"
    display_name: str = ""
    timezone: str = "UTC"
    web_port: int = DEFAULT_PORT
    brain_kind: str = "claude_code"          # "claude_code" | "native"
    native_base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    native_model: str = ""
    native_api_key: str = ""                 # written to the env file, never TOML
    email_enabled: bool = False
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    location_enabled: bool = False
    # Opt-out modules: installed, working with no extra setup, so on by default.
    money_enabled: bool = True
    health_enabled: bool = True
    feeds_enabled: bool = True
    briefings_enabled: bool = True
    #: An external CalDAV server, in place of the [nextcloud] derivation a
    #: standalone install has no Nextcloud for. All three blank => no [caldav].
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    session_secret: str = ""                 # generated; written to the env file
    #: The secrets store's master Fernet key. Generated here and *preserved*
    #: across a ``--force`` re-run; see ``_carry_forward_secrets``.
    secret_key: str = ""
    #: ``webui/tokens.py``'s separate Fernet key, for the ``web_user_tokens``
    #: rows under ``[web] token_storage = "encrypted"``. The wizard never
    #: *generates* one — that shape is opt-in and standalone does not use it —
    #: but it is carried forward when an existing env file has one, since the
    #: rewrite would otherwise delete a key with no recovery.
    web_token_key: str = ""
    #: Absolute path of the admins file, filled in by ``run_setup`` once the
    #: config directory is known. Named in the env file as ISTOTA_ADMINS_FILE.
    admins_file: str = ""
    #: An explicit backup destination carried across a config rewrite. Fresh
    #: installs leave this blank and use the consolidated path below.
    db_backup_dir_override: Path | None = None

    @property
    def disabled_modules(self) -> list[str]:
        """Modules turned off in setup. Everything ships installed; a module is
        on unless listed here (mirrors the server's ``disabled_modules`` model).

        Four of ``modules.MODULE_NAMES``; ``location`` is deliberately absent
        because it is gated by its own ``[location] enabled`` key rather than
        by the module list, and putting it in both would give one answer two
        homes that can disagree.

        The set comes off ``_OPT_OUT_MODULES``, which is also what the prompts
        walk, so the two cannot drift; sorted rather than left in prompt order
        so two runs with the same answers render the same line, whatever order
        the questions end up being asked in.
        """
        return sorted(
            module
            for field_name, module, _question in _OPT_OUT_MODULES
            if not getattr(self, field_name)
        )

    @property
    def db_path(self) -> Path:
        # Keep the framework DB inside the workspace so the whole install is one
        # folder to back up / move; module DBs derive from db_path.parent.
        return self.workspace / "istota.db"

    @property
    def db_backup_dir(self) -> Path:
        if self.db_backup_dir_override is not None:
            return self.db_backup_dir_override
        return self.workspace / "Backups" / "db" / "snapshots"

    @property
    def temp_dir(self) -> Path:
        return self.workspace / "tmp"


# ---------------------------------------------------------------------------
# Pure renderers
# ---------------------------------------------------------------------------


#: Basic-string escaping for the hand-assembled standalone config below. The one
#: TOML writer is `istota.lib.toml_write`; this is its escaper under the name the
#: renderer has always used. Why the control-character arm matters here: every
#: value came off a terminal prompt, and an ESC or DEL in a pasted credential
#: would otherwise produce a config that fails `load_config` after every other
#: file had been written.
_toml_str = toml_write.toml_string


def render_config_toml(a: Answers) -> str:
    """Render the local ``config.toml`` for these answers (pure)."""
    lines: list[str] = [
        "# Istota local single-user install — generated by `istota setup`.",
        "# Re-run `istota setup --force` to regenerate. Secrets live in the",
        "# sibling istota.env file, never here.",
        "",
        "bot_name = \"Istota\"",
        "emissaries_enabled = false  # constitutional principles doc; off for local single-user",
        f"db_path = {_toml_str(str(a.db_path))}",
        f"workspace_path = {_toml_str(str(a.workspace))}",
        f"temp_dir = {_toml_str(str(a.temp_dir))}",
        "",
        "[web]",
        "enabled = true",
        "auth = \"none\"        # single-user local: no login (loopback bind only)",
        f"port = {a.web_port}",
        "",
        "[talk]",
        "enabled = false",
        "",
        "[email]",
        f"enabled = {'true' if a.email_enabled else 'false'}",
    ]
    if a.email_enabled:
        lines += [
            f"imap_host = {_toml_str(a.imap_host)}",
            f"imap_user = {_toml_str(a.imap_user)}",
            f"smtp_host = {_toml_str(a.smtp_host or a.imap_host)}",
            "# IMAP/SMTP passwords come from istota.env "
            "(ISTOTA_EMAIL_IMAP_PASSWORD / ISTOTA_EMAIL_SMTP_PASSWORD).",
        ]
    lines += [
        "",
        "[location]",
        f"enabled = {'true' if a.location_enabled else 'false'}",
    ]
    if a.caldav_url:
        lines += [
            "",
            "# An external CalDAV server (Radicale, Fastmail, Google) in place of",
            "# the [nextcloud] derivation, which a standalone install has no",
            "# Nextcloud for. Any field set here wins over that derivation.",
            "[caldav]",
            f"url = {_toml_str(a.caldav_url)}",
            f"username = {_toml_str(a.caldav_username)}",
            "# Password comes from istota.env (ISTOTA_CALDAV_PASSWORD).",
        ]
    lines += [
        "",
        "# Trusted single-user posture: no sandbox / proxies. "
        "See docs/getting-started/local-install.md.",
        "[security]",
        "sandbox_enabled = false",
        "skill_proxy_enabled = false",
        "",
        "[security.network]",
        "enabled = false",
        "",
        "[scheduler]",
        f"db_backup_dir = {_toml_str(str(a.db_backup_dir))}",
        "",
        "[brain]",
        f"kind = {_toml_str(a.brain_kind)}",
    ]
    if a.brain_kind == "native":
        lines += [
            "",
            "[brain.native]",
            f"base_url = {_toml_str(a.native_base_url)}",
            f"model = {_toml_str(a.native_model)}",
            "# API key comes from istota.env (ISTOTA_BRAIN_NATIVE_API_KEY).",
        ]
    lines += [
        "",
        f"[users.{a.user_id}]",
        f"display_name = {_toml_str(a.display_name or a.user_id)}",
        f"timezone = {_toml_str(a.timezone)}",
    ]
    if a.disabled_modules:
        rendered = ", ".join(_toml_str(m) for m in a.disabled_modules)
        lines.append(f"disabled_modules = [{rendered}]")
    lines.append("")
    return "\n".join(lines)


def render_env_file(a: Answers) -> str:
    """Render the sibling secrets ``istota.env`` (pure)."""
    lines = [
        "# Istota local secrets — generated by `istota setup`. Sourced by",
        "# `istota serve`. Keep this file private (chmod 600).",
        "",
        "# Master key for the encrypted secrets store (Garmin, Monarch, ntfy,",
        "# Google Workspace tokens, …). Everything stored is encrypted with it,",
        "# so replacing it makes every stored credential permanently",
        "# unreadable. `istota setup --force` preserves whatever is here.",
        f"ISTOTA_SECRET_KEY={a.secret_key}",
        "",
        "# Web runs over plain http on loopback; sessions are unused in no-auth mode.",
        "ISTOTA_WEB_INSECURE_COOKIES=1",
        f"ISTOTA_WEB_SESSION_SECRET_KEY={a.session_secret}",
    ]
    if a.web_token_key:
        # Never generated here — only carried forward from an existing file, so
        # a re-run cannot delete a key the web token store depends on.
        lines += [
            "",
            "# Separate key for encrypted web user tokens ([web] token_storage).",
            f"ISTOTA_WEB_TOKEN_KEY={a.web_token_key}",
        ]
    if a.admins_file:
        lines += [
            "",
            "# Who may write shared content (shared briefing blocks) and reach the",
            "# admin dashboard. One user id per line; # comments.",
            f"ISTOTA_ADMINS_FILE={a.admins_file}",
        ]
    if a.brain_kind == "native" and a.native_api_key:
        lines.append(f"ISTOTA_BRAIN_NATIVE_API_KEY={a.native_api_key}")
    if a.email_enabled and a.imap_password:
        lines.append(f"ISTOTA_EMAIL_IMAP_PASSWORD={a.imap_password}")
        lines.append(f"ISTOTA_EMAIL_SMTP_PASSWORD={a.imap_password}")
    if a.caldav_url and a.caldav_password:
        lines += [
            "",
            "# External CalDAV server ([caldav] in config.toml holds the url and",
            "# username; only the password lives here).",
            f"ISTOTA_CALDAV_PASSWORD={a.caldav_password}",
        ]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive collection
# ---------------------------------------------------------------------------


def _ask(input_fn, prompt: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input_fn(f"{prompt}{suffix}: ").strip()
    return raw or default


def _ask_yes_no(input_fn, prompt: str, default: bool, *, out=None, attempts: int = 3) -> bool:
    """A yes/no prompt. Empty takes the default; an unrecognised answer re-asks.

    Re-asking rather than reading anything unrecognised as "no", which is what
    this did. On a default-*No* prompt that reads as the default and so is
    invisible; on a default-*Yes* one it flips **away** from the answer the
    ``[Y/n]`` it just printed promised, so ``1``, ``true`` or ``yeah``
    silently disabled a module and wrote it into both the TOML and the profile
    row. The opt-out module prompts made that four questions rather than one.

    Bounded, and the last attempt takes the default rather than looping: a
    non-interactive ``input_fn`` that keeps returning the same value would
    otherwise spin forever, and a wizard that cannot be exited is worse than
    one that falls back to the answer it already printed.
    """
    d = "Y/n" if default else "y/N"
    for attempt in range(attempts):
        raw = input_fn(f"{prompt} [{d}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        if out is not None and attempt < attempts - 1:
            out(f"  Please answer y or n (Enter takes {'yes' if default else 'no'}).")
    return default


def _ask_port(input_fn, prompt: str, default: int, *, out=None, attempts: int = 3) -> int:
    """A port prompt. Empty takes the default; anything unusable re-asks.

    ``int(_ask(...))`` was the whole of this, so a typo, a pasted URL or an
    answer given one prompt out of step raised ``ValueError`` out of
    ``collect_answers``. Nothing is half-written when that happens — it is
    ahead of every write — but a first-run installer ending in a stack trace
    is a poor advertisement for the thing being installed, and this is the
    only other prompt that parses its answer. ``_ask_yes_no`` took the same
    treatment for the same class of input.

    The range is checked as well as the syntax, because the value reaches a
    bind: ``0`` and ``70000`` both parse and both fail later, at ``istota
    serve``, a long way from the question that produced them.

    Bounded rather than looping, exactly as ``_ask_yes_no`` is: a
    non-interactive ``input_fn`` that keeps returning the same value would
    otherwise spin forever, and the default is a working answer.
    """
    for attempt in range(attempts):
        raw = _ask(input_fn, prompt, str(default))
        try:
            port = int(raw)
        except (TypeError, ValueError):
            reason = f"'{raw}' is not a whole number"
        else:
            if 1 <= port <= 65535:
                return port
            reason = f"{port} is not a usable port"
        if out is not None and attempt < attempts - 1:
            out(f"  {reason}; enter a number between 1 and 65535 (Enter takes {default}).")
    if out is not None:
        out(f"  Falling back to {default}.")
    return default


def _flush_terminal_input() -> None:
    """Discard any pending terminal input before a secret prompt.

    A pasted value (e.g. the model id) can leave stray newlines queued in the
    terminal's input buffer; without this they auto-answer the next prompt with
    an empty line. Best-effort — a no-op on a non-tty / non-POSIX stdin.
    """
    try:
        import sys
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # pragma: no cover - platform / non-tty dependent
        pass


#: The re-prompt tail for the native brain's API key. A parameter rather than a
#: literal in the loop because every secret the wizard reads is read here now,
#: and "re-run with --native-api-key" is wrong advice for the other two.
_NATIVE_KEY_HINT = (
    "for the native brain — please enter it "
    "(or Ctrl-C and re-run with --native-api-key)."
)


def _read_secret(
    getpass_fn, label: str, out, *, attempts: int = 3, hint: str = _NATIVE_KEY_HINT,
) -> str:
    """Read a required secret from the terminal (no echo), re-prompting if empty.

    Flushes buffered terminal input first so a stray newline can't silently
    accept an empty value, and reads via ``getpass_fn`` so the secret isn't
    echoed. Returns "" only if the user gives up (empty every attempt, or
    EOF); the caller's validation then surfaces the clear "no API key" error.

    **``KeyboardInterrupt`` propagates**, and is the difference between the
    hint below being true and being a lie. Swallowing it returned "" and the
    wizard carried on to write a complete install around a blank credential:
    for the IMAP password that was a regression, since the plain ``input()``
    this replaced let Ctrl-C out to ``cli.cmd_setup``, which catches it and
    exits 1 with "Setup cancelled". EOF is still swallowed, because the two
    mean different things — Ctrl-C is "stop", a closed stdin is "there is no
    more input", which is the give-up case this function's retry is for.
    """
    _flush_terminal_input()
    for attempt in range(attempts):
        try:
            value = getpass_fn(f"{label}: ").strip()
        except EOFError:
            return ""
        if value:
            return value
        if attempt < attempts - 1:
            out(f"{label} is required {hint}")
    return ""


def _is_valid_timezone(name: str) -> bool:
    """True if ``name`` is a zone ``ZoneInfo`` accepts (a real IANA name).

    Abbreviations like ``PDT`` / ``EST`` are NOT valid — ``ZoneInfo`` only
    knows names like ``America/Los_Angeles``. Storing an abbreviation makes
    the executor's ``_resolve_user_tz`` silently fall back to UTC.
    """
    if not name:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


def _default_timezone() -> str:
    """Best-effort IANA zone name for the host.

    ``datetime.now().astimezone().tzinfo`` is a fixed-offset ``datetime.timezone``
    on many systems (notably macOS), whose ``str()`` is an abbreviation like
    ``PDT`` — not a name ``ZoneInfo`` accepts. Resolve the real IANA name from
    ``TZ`` or the ``/etc/localtime`` symlink (which points into the zoneinfo
    tree on both Linux and macOS), validating every candidate before returning.
    """
    # 1. TZ env var, if it names a real zone.
    tz_env = os.environ.get("TZ", "").strip()
    if _is_valid_timezone(tz_env):
        return tz_env

    # 2. /etc/localtime symlink → .../zoneinfo/<Area>/<Location>.
    try:
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            name = real.split("zoneinfo/", 1)[1]
            if _is_valid_timezone(name):
                return name
    except Exception:  # pragma: no cover - defensive
        pass

    # 3. A ZoneInfo-backed local tzinfo (Linux where astimezone yields one).
    try:
        from datetime import datetime

        key = getattr(datetime.now().astimezone().tzinfo, "key", None)
        if key and _is_valid_timezone(key):
            return key
    except Exception:  # pragma: no cover - defensive
        pass

    return "UTC"


def collect_answers(args, *, input_fn, which_fn, out, getpass_fn, prior_caldav=None) -> Answers:
    """Build an ``Answers`` from flags + (unless ``--yes``) interactive prompts.

    ``prior_caldav`` is the ``[caldav]`` block already in the config this run
    is about to overwrite, if any; see :func:`_collect_caldav`.
    """
    interactive = not getattr(args, "yes", False)
    a = Answers()

    # 1. Workspace
    ws = getattr(args, "workspace", None)
    if not ws and interactive:
        ws = _ask(input_fn, "Workspace directory", DEFAULT_WORKSPACE)
    a.workspace = Path(ws or DEFAULT_WORKSPACE).expanduser().resolve()

    # 2. Brain
    _collect_brain(
        a, args, interactive=interactive, input_fn=input_fn,
        which_fn=which_fn, out=out, getpass_fn=getpass_fn,
    )

    # 3. User identity
    os_user = getpass.getuser() or "local"
    uid = getattr(args, "user", None)
    if not uid and interactive:
        uid = _ask(input_fn, "User id", os_user)
    a.user_id = uid or os_user
    dn = getattr(args, "display_name", None)
    if not dn and interactive:
        dn = _ask(input_fn, "Display name", a.user_id)
    a.display_name = dn or a.user_id
    tz_default = _default_timezone()
    tz = getattr(args, "timezone", None)
    if not tz and interactive:
        tz = _ask(input_fn, "Timezone", tz_default)
    tz = tz or tz_default
    # An abbreviation ("PDT") or typo isn't a name ZoneInfo accepts; storing it
    # makes every task's clock silently fall back to UTC. Reject it up front.
    if not _is_valid_timezone(tz):
        out(
            f"  '{tz}' is not a valid IANA timezone (use e.g. America/Los_Angeles);"
            f" falling back to {tz_default if _is_valid_timezone(tz_default) else 'UTC'}."
        )
        tz = tz_default if _is_valid_timezone(tz_default) else "UTC"
    a.timezone = tz

    # 4. Web port. `--port` is `type=int` at the parser, so only the prompt
    # can carry something unparseable — see `_ask_port`.
    port = getattr(args, "port", None)
    if port is None and interactive:
        port = _ask_port(input_fn, "Web port", DEFAULT_PORT, out=out)
    a.web_port = int(port or DEFAULT_PORT)

    # 5. Modules & surfaces. Everything ships installed; here we only choose
    # what's *enabled*. The default follows a simple rule: on when a module works
    # with no extra setup (money), off when it needs external configuration
    # (location webhooks, email credentials). Grouped so the "which pieces"
    # decisions live in one place instead of being split across the installer.

    # Location (GPS tracking) — off unless asked; it needs an Overland ingest
    # token to actually receive pings, so an enabled-but-unconfigured tab is empty.
    if getattr(args, "location", False):
        a.location_enabled = True
    elif interactive:
        a.location_enabled = _ask_yes_no(
            input_fn, "Enable GPS/location tracking?", False, out=out,
        )

    # The rest of `modules.MODULE_NAMES`: each is installed and works out of the
    # box, so each is on by default and the prompt is an opt-out, matching the
    # server's module model. `location` is not in this loop — it is gated by
    # `[location] enabled` above rather than by `disabled_modules`.
    _collect_modules(a, args, interactive=interactive, input_fn=input_fn, out=out)

    # Email (IMAP/SMTP) — off unless asked; needs credentials to do anything.
    if getattr(args, "email", False):
        a.email_enabled = True
    elif interactive:
        a.email_enabled = _ask_yes_no(
            input_fn, "Enable email (IMAP/SMTP)?", False, out=out,
        )
    if a.email_enabled:
        if interactive:
            a.imap_host = _ask(input_fn, "IMAP host", a.imap_host)
            a.imap_user = _ask(input_fn, "IMAP user", a.imap_user)
            # Asked rather than forced equal to the IMAP host: a submission
            # service on another hostname is the ordinary case, and defaulting
            # to the IMAP host keeps the common one a single keystroke.
            a.smtp_host = _ask(input_fn, "SMTP host", a.imap_host)
            # Read last, and through the same no-echo reader as the API key:
            # `input_fn` echoes it to the terminal and leaves it in shell
            # history when the wizard is driven from a pipe.
            a.imap_password = _read_secret(
                getpass_fn, "IMAP password", out,
                hint="— please enter it, or Ctrl-C to abort.",
            )
        else:
            a.smtp_host = a.smtp_host or a.imap_host

    _collect_caldav(
        a, prior_caldav or {}, interactive=interactive, input_fn=input_fn,
        getpass_fn=getpass_fn, out=out,
    )

    # Stable session secret so restarts don't invalidate cookies (unused in
    # no-auth but written for cleanliness / any residual cookie use).
    a.session_secret = secrets.token_hex(32)
    # Master key for the encrypted secrets store. 64 hex chars, comfortably
    # over secrets_store._MIN_KEY_LEN. Both of these are replaced by whatever
    # an existing istota.env already holds; see `_carry_forward_secrets`.
    a.secret_key = secrets.token_hex(32)
    return a


#: The opt-out modules, in the order they are asked about and rendered:
#: ``(Answers field, module name, question)``. ``location`` is absent — see
#: ``Answers.disabled_modules``.
_OPT_OUT_MODULES: tuple[tuple[str, str, str], ...] = (
    ("money_enabled", "money", "Enable the money module (double-entry accounting)?"),
    ("health_enabled", "health", "Enable the health module (body stats, lab results, documents)?"),
    ("feeds_enabled", "feeds", "Enable the feeds module (RSS/Atom/Tumblr reader)?"),
    ("briefings_enabled", "briefings", "Enable the briefings module (scheduled digests)?"),
)


def read_existing_caldav(config_path: Path, env_path: Path | None = None) -> dict[str, str]:
    """The ``[caldav]`` settings an existing install already holds.

    **Two files, because the block is split across two by design.** The url and
    the username are ordinary config and live in ``config.toml``; the password
    is a credential and lives in the sibling ``istota.env`` as
    ``ISTOTA_CALDAV_PASSWORD``, which ``load_config``'s ``_env_secret_overrides``
    table resolves onto ``caldav.password`` — the same channel every other
    credential in the tree uses. Reading only the TOML would recover a server
    it has no password for, and ``_collect_caldav`` would then write a
    ``[caldav]`` block that cannot authenticate.

    The environment outranks the env file, matching ``_carry_forward_secrets``
    and, behind it, ``serve.load_env_file``'s non-clobbering resolution: an
    exported value is the one the daemon is actually using.

    A password still sitting in the TOML is honoured as a last resort. That is
    a migration path rather than a supported location — ``render_config_toml``
    no longer writes one — and without it a hand-written ``[caldav]`` block,
    which is what the docs told standalone users to add, would lose its
    password to the first ``--force``.

    Best-effort throughout: an absent, unreadable or unparseable file is no
    values, since the caller's fallback is to ask. Non-string members are
    coerced, because this is read from a file a person may have edited by hand.
    """
    import tomllib  # noqa: PLC0415 - only this path needs it

    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    block = data.get("caldav")
    if not isinstance(block, dict):
        return {}
    found = {key: str(block.get(key) or "") for key in ("url", "username", "password")}

    from_env = os.environ.get("ISTOTA_CALDAV_PASSWORD", "").strip()
    from_file = ""
    if env_path is not None:
        from_file = _read_env_values(env_path).get("ISTOTA_CALDAV_PASSWORD", "").strip()
    if from_env or from_file:
        found["password"] = from_env or from_file
    return found


def read_existing_db_backup_dir(config_path: Path) -> Path | None:
    """Return an explicit backup destination before setup rewrites the config."""
    import tomllib  # noqa: PLC0415 - only this path needs it

    try:
        with open(config_path, "rb") as config_file:
            data = tomllib.load(config_file)
    except (OSError, ValueError):
        return None
    scheduler = data.get("scheduler")
    if not isinstance(scheduler, dict):
        return None
    value = scheduler.get("db_backup_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def _collect_caldav(a: Answers, prior, *, interactive, input_fn, getpass_fn, out) -> None:
    """Decide the ``[caldav]`` block: keep the existing one, set one up, or none.

    The section exists for exactly this install shape and no generator has ever
    offered it, so a standalone user has had to add it by hand. Opt-in, since
    without it calendar derives from ``[nextcloud]``, which a standalone
    install does not have.

    **A re-run over an install that already has one asks a different question,
    and that is a data-loss fix rather than a nicety.** ``--force`` rewrites
    ``config.toml`` wholesale and ``render_config_toml`` is a pure function of
    ``Answers``, so a run that collected nothing emits no block — and because
    the password's only home is that file (there is no ``[caldav]`` entry in
    ``load_config``'s ``_env_secret_overrides`` table), dropping the block
    destroys a credential with no copy anywhere else. ``_carry_forward_secrets``
    exists for precisely this hazard on the env file; this is the same rule for
    the one secret that does not live there. So the question becomes "keep the
    one you have", defaulting to yes, and declining it falls through to setting
    up a different server — which is what makes the block editable rather than
    merely preserved.

    Non-interactively there is nothing to ask and no flag to answer with, so an
    existing block is carried forward unconditionally. ``--yes`` resetting a
    *preference* to its default is what ``--yes`` means; silently deleting a
    credential is not.

    A URL with no password is not written at all. It would override the
    ``[nextcloud]`` derivation with something that cannot authenticate, which
    breaks calendar more thoroughly than leaving the section out.
    """
    prior_url = (prior or {}).get("url", "")
    keep = bool(prior_url)
    if keep and interactive:
        keep = _ask_yes_no(
            input_fn, f"Keep the configured CalDAV server ({prior_url})?", True, out=out,
        )
    if keep:
        a.caldav_url = prior_url
        a.caldav_username = prior.get("username", "")
        a.caldav_password = prior.get("password", "")
        return

    asked = interactive and _ask_yes_no(
        input_fn, "Point calendar at an external CalDAV server?", False, out=out,
    )
    if asked:
        a.caldav_url = _ask(input_fn, "CalDAV URL", "")
        if a.caldav_url:
            a.caldav_username = _ask(input_fn, "CalDAV username", "")
            a.caldav_password = _read_secret(
                getpass_fn, "CalDAV password", out,
                hint="— please enter it, or Ctrl-C to abort.",
            )
            if not a.caldav_password:
                out(
                    "  No CalDAV password given; leaving [caldav] out rather "
                    "than writing one that cannot authenticate."
                )
                a.caldav_url = ""
                a.caldav_username = ""
        else:
            out("  No CalDAV URL given; leaving [caldav] out of the config.")
    if prior_url and not a.caldav_url:
        out(
            f"  Dropping the existing [caldav] block ({prior_url}); the stored "
            f"password goes with it."
        )


def _collect_modules(a: Answers, args, *, interactive, input_fn, out) -> None:
    """Ask about each opt-out module, honouring its ``--no-<name>`` flag.

    **A module whose install extra is missing is neither asked about nor
    recorded**, and the second half is the one that matters. ``money`` is the
    only entry in ``modules.MODULE_DEPENDENCIES`` today; without ``beancount``
    ``module_available()`` already hides it everywhere, so the prompt would be
    a question with no good answer — and writing ``money`` into
    ``disabled_modules`` because of it would turn a transient install state
    into a stored decision that survives a later ``uv tool install
    'istota[money]'``, with nothing on the machine saying why the module is
    still dark. The explicit flag still wins, because that is a decision the
    operator made rather than one derived from the environment.
    """
    from .modules import module_available  # noqa: PLC0415 - keep the import graph lean

    for field_name, module, question in _OPT_OUT_MODULES:
        if getattr(args, f"no_{module}", False):
            setattr(a, field_name, False)
            continue
        if not module_available(module):
            out(
                f"  The {module} module's optional dependencies are not "
                f"installed; leaving it out of setup (it stays hidden until "
                f"they are)."
            )
            continue
        if interactive:
            setattr(a, field_name, _ask_yes_no(input_fn, question, True, out=out))


def _collect_brain(a: Answers, args, *, interactive, input_fn, which_fn, out, getpass_fn) -> None:
    """Pick the model backend. Flags win; else detect ``claude`` and offer it."""
    forced = getattr(args, "brain", None)
    if forced in ("claude_code", "native"):
        a.brain_kind = forced
        if forced == "native":
            _collect_native(
                a, args, interactive=interactive, input_fn=input_fn,
                getpass_fn=getpass_fn, out=out,
            )
        return

    claude_path = which_fn("claude")
    if not interactive:
        # Non-interactive: prefer claude if present, else require native + key.
        if claude_path:
            a.brain_kind = "claude_code"
        else:
            a.brain_kind = "native"
            _collect_native(
                a, args, interactive=False, input_fn=input_fn,
                getpass_fn=getpass_fn, out=out,
            )
        return

    if claude_path:
        use_it = _ask_yes_no(
            input_fn,
            "Detected the Claude CLI. Use your Claude Code subscription for the "
            "model backend?",
            True,
        )
        if use_it:
            a.brain_kind = "claude_code"
            out(
                "Using the Claude CLI. (If it isn't logged in yet, run `claude` "
                "once to authenticate.)"
            )
            return
    else:
        out("No Claude CLI detected on PATH.")

    # Fall to native.
    a.brain_kind = "native"
    _collect_native(
        a, args, interactive=interactive, input_fn=input_fn,
        getpass_fn=getpass_fn, out=out,
    )


def _collect_native(a: Answers, args, *, interactive, input_fn, getpass_fn, out) -> None:
    base = getattr(args, "native_base_url", None)
    model = getattr(args, "native_model", None)
    key = getattr(args, "native_api_key", None) or os.environ.get("ISTOTA_BRAIN_NATIVE_API_KEY", "")
    if interactive:
        base = base or _ask(input_fn, "API base URL", DEFAULT_ANTHROPIC_BASE_URL)
        model = model or _ask(input_fn, "Model id", "claude-sonnet-4-6")
        if not key:
            # Read as a secret (no echo) and re-prompt on empty — a stray newline
            # from the pasted model id must not silently leave the key blank.
            key = _read_secret(getpass_fn, "API key", out)
    a.native_base_url = base or DEFAULT_ANTHROPIC_BASE_URL
    a.native_model = model or ""
    a.native_api_key = key or ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class SetupError(RuntimeError):
    """A setup-blocking condition, reported to the user."""


def _read_env_values(path: Path) -> dict[str, str]:
    """Parse an existing ``istota.env`` into a mapping.

    Mirrors ``serve.load_env_file``'s grammar (``export`` prefix, ``#``
    comments, optional quoting) so a file written by hand and sourced at boot
    is read the same way here. Best-effort: an unreadable or non-UTF-8 file is
    no values, since the caller's fallback is to generate a fresh key.

    **First occurrence wins, and that is load-bearing rather than arbitrary.**
    ``load_env_file`` skips a name already in ``os.environ``, so the first line
    for a name is the one the daemon ends up using and every later line is
    dead. A last-wins parse here would read a duplicated ``ISTOTA_SECRET_KEY``
    — the exact shape an operator produces by appending the line a remedy told
    them to add to a file that already had one — as the *other* key, and the
    caller would then rewrite the file with it, orphaning everything encrypted
    under the one the daemon was actually using.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            values.setdefault(key, value.strip().strip('"').strip("'"))
    return values


def _carry_forward_secrets(a: Answers, env_path: Path, out=print) -> None:
    """Keep the generated secrets an existing install already holds.

    ``--force`` rewrites the whole file, and for a Fernet key that is a
    destructive operation rather than a regeneration: everything encrypted
    under the old one is orphaned with no way back. Docker draws the same line
    — ``entrypoint.sh`` generates once into ``/data/.secret_key`` and never
    overwrites.

    Three names, and the first two are keys with no recovery.
    ``ISTOTA_SECRET_KEY`` is the secrets store's master key (Garmin, Monarch,
    ntfy, the Google Workspace tokens). ``ISTOTA_WEB_TOKEN_KEY`` is a
    *separate* key with identical semantics — ``webui/tokens.py`` derives its own
    Fernet from it for the ``web_user_tokens`` rows under ``[web]
    token_storage = "encrypted"`` — and the wizard has never written it, so it
    is only ever here because an operator added it and would be deleted by the
    rewrite. ``ISTOTA_WEB_SESSION_SECRET_KEY`` is carried for consistency
    rather than for harm: losing it only invalidates cookies. The rule is the
    same for all three — a re-run is a config rewrite, not a key rotation.

    **The environment outranks the file, because that is the order the daemon
    resolves them in.** ``serve.load_env_file`` is non-clobbering, so an
    exported value wins over the file's and is the key actually in use;
    preserving the file's instead would write a value the daemon ignores, and
    the install would silently switch keys the day the export went away.

    Only a *usable* value is preserved. A blank or truncated line is the broken
    state this fixes, and pinning it would make the guard the bug — safe to
    discard, since nothing below the floor can have encrypted anything. The
    floor is the secrets store's own, never a second copy of it.

    What it deliberately does **not** do is preserve arbitrary unrecognised
    lines. ``render_env_file`` is a pure function of ``Answers``, and carrying
    unknown names through would resurrect variables the wizard has stopped
    writing; the file's own comment says only that these are preserved.
    """
    from istota.credentials.store import _MIN_KEY_LEN

    existing = _read_env_values(env_path)

    def resolve(var: str) -> str:
        from_env = os.environ.get(var, "").strip()
        from_file = existing.get(var, "").strip()
        if from_env and from_file and from_env != from_file:
            # Not a failure — the daemon has an unambiguous answer — but the
            # operator is about to have the losing value deleted, so say which
            # one survived. Names only, never values.
            out(
                f"  {var} differs between your environment and {env_path};"
                f" keeping the environment's, which is the one the daemon uses."
            )
        return from_env or from_file

    for field_name, var in (
        ("secret_key", "ISTOTA_SECRET_KEY"),
        ("web_token_key", "ISTOTA_WEB_TOKEN_KEY"),
        ("session_secret", "ISTOTA_WEB_SESSION_SECRET_KEY"),
    ):
        prior = resolve(var)
        if len(prior) >= _MIN_KEY_LEN:
            setattr(a, field_name, prior)

    # The CalDAV password is deliberately *not* here, even though it is carried
    # across a re-run for the same reason. It travels with the url and the
    # username in `read_existing_caldav`, because `_collect_caldav` has to
    # decide all three together — a block kept with no password recovered would
    # name a server it cannot authenticate to — and that decision is made
    # inside `collect_answers`, which runs before this does.


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, never visible at a wider mode than 0600.

    ``write_text`` then ``chmod`` leaves the file world-readable with secrets
    in it for the interval between, which is a window on a multi-user host.

    Two narrowing steps, because ``O_CREAT``'s mode applies **only** to a file
    this call creates. On the ``--force`` re-run — the one path where the file
    pre-exists, and the path this change adds — the mode argument is ignored
    entirely, so an ``istota.env`` sitting at 0644 (hand-made, or written by a
    wizard older than this) would be truncated and refilled with the master key
    at its original mode and only narrowed afterwards: the same window, on the
    only shape that has it. ``fchmod`` on the open descriptor closes it before
    any byte is written. The ``path.chmod`` after is the fallback for a
    platform without ``fchmod``.

    UTF-8 explicitly: the rendered text carries an em dash, and ``os.fdopen``
    would otherwise encode with the locale's codec and raise under ``LC_ALL=C``
    *after* ``O_TRUNC`` had already emptied the file.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass


def _ensure_admins_file(path: Path, user_id: str, out=print) -> None:
    """Create ``path`` naming ``user_id``, and never modify one that exists.

    An empty allowlist is read as "everyone is admin" by ``Config.is_admin``
    and as "nobody" by ``is_shared_kv_writer`` and the web admin gate, so a
    standalone install with no file at all could not write a shared briefing
    block. This gives it a real, editable authorization artifact instead of
    leaning on the exemption in ``is_shared_kv_writer``.

    **It only ever creates.** An existing file is left byte for byte alone,
    even when it does not name the user — appending would be a silent
    authorization widening, and the path is derived from ``config_path.parent``
    with nothing asserting the standalone shape, so ``istota setup -c
    /etc/istota/config.toml --force`` would append the wizard's user to the
    *server's* production allowlist (``load_admin_users`` defaults to exactly
    ``/etc/istota/admins``). That widening would survive the operator restoring
    ``config.toml`` from Ansible, since the play manages the two files
    separately. Refusing costs a standalone user nothing they cannot fix in one
    edit, and the line printed here tells them what to add.

    Membership is asked of ``load_admin_users`` rather than re-parsed, so the
    writer and the reader of this file cannot drift on what a line means.
    """
    if path.exists():
        from .config import load_admin_users  # noqa: PLC0415

        if user_id not in load_admin_users(str(path)):
            out(
                f"  {path} exists and does not name '{user_id}'; leaving it "
                f"untouched. Add that line yourself to allow shared-content "
                f"writes and the admin dashboard."
            )
        return
    path.write_text(
        "# Istota admin user ids - one per line, # comments.\n"
        "# Written by `istota setup`; edit freely.\n"
        f"{user_id}\n",
        encoding="utf-8",
    )


def _validate(a: Answers) -> None:
    if a.brain_kind == "native":
        if not a.native_model:
            raise SetupError(
                "Native brain selected but no model was given. Re-run with "
                "--brain native --native-model <id> --native-api-key <key>."
            )
        if not a.native_api_key:
            raise SetupError(
                "Native brain selected but no API key was given (set "
                "ISTOTA_BRAIN_NATIVE_API_KEY or pass --native-api-key)."
            )


def setup_shape(args) -> str:
    """Which half runs: the flag, else ``ISTOTA_SETUP_SHAPE`` (the image sets
    ``container``), else ``standalone``. ``--vm-dir`` only means anything to
    the container half, so it selects it."""
    shape = getattr(args, "shape", None) or os.environ.get("ISTOTA_SETUP_SHAPE", "")
    if not shape and getattr(args, "vm_dir", None):
        shape = "container"
    shape = shape or "standalone"
    if shape not in ("standalone", "container"):
        raise SetupError(f"Unknown setup shape {shape!r}; use standalone or container.")
    return shape


def run_setup(args, *, input_fn=input, which_fn=None, out=print, getpass_fn=None) -> int:
    """Run the setup wizard. Returns a process exit code (0 = success)."""
    import shutil as _shutil

    if setup_shape(args) == "container":
        return run_container_setup(
            args, input_fn=input_fn, out=out,
            getpass_fn=getpass_fn or getpass.getpass,
        )

    if which_fn is None:
        which_fn = _shutil.which
    if getpass_fn is None:
        # getpass reads from /dev/tty with echo off — the right way to collect a
        # secret, and robust in the curl-pipe / reattached-stdin install path.
        getpass_fn = getpass.getpass

    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    env_path = config_path.parent / "istota.env"

    # Clobber guard.
    if config_path.exists() and not getattr(args, "force", False):
        if getattr(args, "yes", False):
            raise SetupError(
                f"A config already exists at {config_path}. Re-run with --force "
                "to overwrite it."
            )
        update = _ask_yes_no(
            input_fn, f"A config already exists at {config_path}. Update it in place?", False,
        )
        if not update:
            out("Setup aborted; existing config left untouched.")
            return 1

    # Read before the rewrite: both files this run is about to replace are the
    # only copies of the [caldav] settings, and they are split across the two —
    # url and username in the TOML, password in istota.env.
    a = collect_answers(
        args, input_fn=input_fn, which_fn=which_fn, out=out, getpass_fn=getpass_fn,
        prior_caldav=read_existing_caldav(config_path, env_path),
    )
    a.db_backup_dir_override = read_existing_db_backup_dir(config_path)
    _validate(a)

    admins_path = config_path.parent / "admins"
    a.admins_file = str(admins_path)
    # A re-run must not replace the master key: every stored credential is
    # encrypted under it. Read before anything is written.
    _carry_forward_secrets(a, env_path, out=out)

    # Create workspace + config dirs.
    a.workspace.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Write config + env. The env file is created 0600 rather than chmod'd
    # afterwards, so it is never on disk world-readable holding secrets.
    #
    # config.toml holds no credential at all and stays at the umask default, so
    # an operator can read it without sudo. It used to take a private write on
    # the one shape that carried a [caldav] password; that password now has an
    # `_env_secret_overrides` row and goes to istota.env with the rest, so the
    # exception is gone rather than merely unused — one rule, not two.
    config_path.write_text(render_config_toml(a), encoding="utf-8")
    _write_private(env_path, render_env_file(a))
    _ensure_admins_file(admins_path, a.user_id, out=out)

    # Bootstrap: DB, user profile row, workspace directories + memory.
    config = _bootstrap(a, config_path)

    # Confirm the install works rather than only that it was written. After the
    # bootstrap, because half of what doctor reads is what the bootstrap made.
    _run_self_check(config, config_path, out)

    _print_next_steps(a, config_path, out)
    return 0


def _bootstrap(a: Answers, config_path: Path):
    """Initialize the DB, upsert the user profile, seed the workspace.

    Returns the freshly-loaded ``Config``, so the closing self-check reads the
    same object this function already paid to load rather than loading it a
    second time.
    """
    from . import db
    from . import user_profiles
    from .config import load_config
    from .storage import ensure_workspace_for_user

    a.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Both of these are named in the config this run just wrote and were
    # created by nothing: the scheduler's backup pass and the executor's
    # per-task control tree each make their own on first use, so a fresh
    # install had two configured paths that did not exist — which reads as a
    # broken install to an operator and to `doctor`'s writable-dirs checks.
    #
    # 0700 rather than the umask default, for what they hold. `temp_dir` is the
    # parent of `.control/{user}/task_{id}/`, whose own 0700 `execute_task`
    # sets on the three levels it creates but not on this one; it also holds
    # every task's prepared attachment renditions. `db_backup_dir` holds whole
    # database snapshots, and `db_backup` writes 0700/0600 for that reason.
    # `mkdir(mode=...)` is masked by the umask and ignored outright when the
    # directory exists, so the mode is set explicitly afterwards — but never
    # through a symlink. `mkdir(exist_ok=True)` succeeds on one pointing at a
    # directory and `Path.chmod` follows it, so a re-run over an install where
    # the operator symlinked `Backups/db/snapshots` at a shared volume would silently
    # narrow that volume instead. The surrounding code is careful about this
    # in the same way (`_write_private`'s fchmod, `execute_task`'s O_NOFOLLOW).
    for directory in (a.temp_dir, a.db_backup_dir):
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            continue
        try:
            directory.chmod(0o700)
        except OSError:  # pragma: no cover - platform / ownership dependent
            pass
    db.init_db(a.db_path)

    user_profiles.ensure_profile(
        a.db_path, a.user_id, display_name=a.display_name, timezone=a.timezone,
    )
    # disabled_modules must land on the profile row too: is_module_enabled reads
    # the DB row before the TOML [users.X] block, so the row is the effective one.
    user_profiles.update_profile(
        a.db_path, a.user_id, display_name=a.display_name, timezone=a.timezone,
        disabled_modules=a.disabled_modules,
    )

    # Load the freshly-written config so the workspace seeder sees the real
    # paths (mount + bot_dir), then seed directories + memory.
    os.environ["ISTOTA_CONFIG_PATH"] = str(config_path)
    config = load_config(config_path)
    ensure_workspace_for_user(config, a.user_id)
    return config


def _run_self_check(config, config_path: Path, out) -> None:
    """Run doctor against the install this run just wrote, and print failures.

    This is what turns setup from "wrote some files" into "wrote some files and
    confirmed they work". Four decisions, each of which is easy to get wrong in
    a way that makes the run worse than not doing it:

    **Doctor's own entry point, not a subprocess.** ``istota doctor`` is
    ``cli.cmd_doctor``, which is exactly this sequence; shelling out would be a
    second way to run doctor, with its own environment and its own answer.
    ``failing()`` is what gives results to filter, and ``render_text`` renders
    the same lines the command does — including its redaction pass, which
    matters here because the config may hold a CalDAV password.

    **Explicit config path.** ``config_visibility`` is the gate that stops a
    run with no config resolved from reporting on a default ``Config`` while
    reading exactly like a run about this deployment. It is asked with
    ``requested=config_path`` for the same reason ``cmd_doctor`` asks it: a
    config that failed to load must say so rather than answer about defaults.

    **``probe=False``.** A probing run spawns a subprocess per binary check
    with a 10s ceiling each, and the operator is sitting at a prompt. Nothing
    that matters on a fresh install needs a spawn — the paths, the database,
    the secret key, the control directory and the static build are all read
    from the filesystem — and a check that would exec says so in its detail.
    ``deep`` and ``live`` are left off by their defaults for the same reason,
    doubled: one spawns a namespace and the other bills for a model call.

    **A failure never fails the install.** ``run_setup`` returns a process exit
    code and setup did succeed: the files are written and the database is
    initialized. A red check is information the operator needs, not a reason to
    unwind a working install, so it is printed prominently and the exit code is
    unchanged. The whole call is also wrapped — a diagnostic that raises must
    not be the thing that breaks setup.
    """
    from . import doctor  # noqa: PLC0415 - a heavy import, and only this path needs it

    try:
        gate = doctor.config_visibility(config, requested=config_path)
        results = [gate] if gate is not None else doctor.run_checks(config, probe=False)
        _, summary = doctor.verdict(results)
        failures = doctor.failing(results)
        secrets = doctor.config_secrets(config)
        out("")
        out(f"Self-check ({summary}):")
        if not failures:
            # Only failures are printed, so a warning count with nothing under
            # it would be a number the operator cannot act on. Name where the
            # rest is instead of either hiding the count or printing warnings
            # that are expected on this shape — a closing check that always
            # has something in it teaches the operator to skip the block.
            warned = doctor.summarize(results).get(doctor.WARN, 0)
            tail = f", {warned} warning{'' if warned == 1 else 's'}" if warned else ""
            out(f"  no failures{tail}.")
            if warned:
                out(f"  Full report: istota -c {config_path} doctor")
            return
        out(doctor.render_text(failures, secrets=secrets))
        out("")
        out(
            "  Setup itself succeeded — the config, secrets and database are "
            "written. The checks above are what still needs a look."
        )
        out(f"  Full report: istota -c {config_path} doctor")
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not fail the install
        logger.debug("setup self-check raised", exc_info=True)
        # Redacted for the same reason `render_text` demands `secrets`: an
        # exception out of a check can carry a config value in its message (a
        # URL with userinfo, a path), and terminal output is where a pasted
        # credential ends up in a bug report. Best-effort — this is already the
        # failure path, so a redaction that itself raises must not replace one
        # unhelpful line with a traceback.
        detail = f"{type(exc).__name__}: {exc}"
        try:
            detail = doctor._redact(detail, doctor.config_secrets(config))
        except Exception:  # noqa: BLE001 - see above
            detail = type(exc).__name__
        out("")
        out(
            f"Self-check could not run ({detail}); setup itself succeeded. "
            f"Try `istota -c {config_path} doctor`."
        )


def _print_next_steps(a: Answers, config_path: Path, out) -> None:
    out("")
    out("Setup complete.")
    out(f"  Config:    {config_path}")
    out(f"  Workspace: {a.workspace}")
    out(f"  User:      {a.user_id}")
    out(f"  Brain:     {a.brain_kind}")
    out("")
    out("  Start it:  istota serve")
    out(f"  Then open: http://127.0.0.1:{a.web_port}/istota")
    out("")
    out(
        "  Trust model: this is a single-user, unsandboxed install — the agent "
        "runs with your account's full privileges. Only give it content and "
        "instructions you trust."
    )


# ===========================================================================
# The container half: the one deployment shape
# ===========================================================================

#: The image's state volume, and the layout every container config names.
CONTAINER_DATA_DIR = Path("/data")
CONTAINER_DB_PATH = "/data/db/istota.db"
CONTAINER_TEMP_DIR = "/data/tmp"
CONTAINER_REPOS_DIR = "/data/repos"
#: The devbox socket directories, one per-user volume mounted under each. Under
#: /data because the root phase hands writable mounts there to uid 10001, and a
#: fresh volume is root-owned (`devbox/compose_file.py` refuses anywhere else).
CONTAINER_DEVBOX_EXEC_DIR = "/data/devbox/exec"
CONTAINER_DEVBOX_CRED_DIR = "/data/devbox/cred"
#: The workspace in local storage mode: a directory on the state volume.
CONTAINER_LOCAL_WORKSPACE = "/data/workspace"
#: The workspace in full Nextcloud integration: the VM's rclone mount of the
#: bot's files, bound here. The entrypoint refuses to start unless it is one.
CONTAINER_NEXTCLOUD_WORKSPACE = "/mnt/shared"
#: Where the image installs the real forge binaries, off PATH.
CONTAINER_FORGE_DIR = "/usr/local/lib/istota_forge"
#: The `browser` service's container name and API port.
CONTAINER_BROWSER_API_URL = "http://istota-browser:9223"
#: The `signaling` service as the daemon reaches it on the stack network.
CONTAINER_SIGNALING_URL = "http://signaling:8080"
CONTAINER_WEB_PORT = 8766

#: Every credential the container half can write, by the name of the file it
#: becomes under ``secrets/`` (compose mounts it at ``/run/secrets/<name>``).
#: The name is the environment variable the daemon reads it from, lowercased,
#: and ``istota-secrets`` in the image exports it under the uppercase name. All
#: but the two Claude Code ones are ``load_config``'s existing
#: ``ISTOTA_<SECTION>_<FIELD>`` overrides. ``docker-compose.yml`` declares
#: exactly this set (``tests/test_setup_container.py`` holds the two equal),
#: and compose refuses to start a service whose secret file is missing, so the
#: wizard writes every one of them, empty when unused.
SECRET_NAMES: tuple[str, ...] = (
    "anthropic_api_key",
    "claude_code_oauth_token",
    "istota_brain_native_api_key",
    "istota_nextcloud_app_password",
    "istota_web_oauth2_client_secret",
    "istota_web_session_secret_key",
    "istota_email_imap_password",
    "istota_caldav_password",
    "istota_developer_gitlab_token",
    "istota_developer_github_token",
)

#: Ingress modes, as `host.env`'s INGRESS names them, mapped to the hop count the
#: web app subtracts from X-Forwarded-For: one for the compose nginx, plus one
#: for an upstream proxy in `proxied`.
INGRESS_HOPS = {"direct": 1, "proxied": 2, "local": 1}

#: Compose profiles the wizard can turn on. `location` starts the webhook
#: receiver; the rest start the service of the same name.
CONTAINER_PROFILES = ("browser", "signaling", "location", "whatsapp-baileys")


@dataclass
class ContainerAnswers:
    """Everything the container half asks, and what it generates."""

    bot_name: str = "Istota"
    user_id: str = ""
    display_name: str = ""
    timezone: str = "UTC"
    user_email: str = ""
    #: The public name the stack is reached by: nginx's server_name, the
    #: OAuth redirect, `[site] hostname`.
    hostname: str = "localhost"
    ingress: str = "local"
    upstream_proxy: str = ""
    listen_addr: str = ""
    listen_port: int = 0
    tls_cert_source: str = "acme"
    # Nextcloud, full integration only. Empty url = local storage.
    nextcloud_url: str = ""
    nextcloud_public_url: str = ""
    nextcloud_username: str = "istota"
    nextcloud_app_password: str = ""
    nextcloud_dav_prefix: str = ""
    nextcloud_auto_share_bot_dir: bool = True
    talk_enabled: bool = True
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    email_login: bool = True
    # Brain.
    brain_kind: str = "claude_code"
    claude_code_oauth_token: str = ""
    anthropic_api_key: str = ""
    native_base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    native_model: str = ""
    native_api_key: str = ""
    # Email.
    email_enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    bot_email: str = ""
    # Calendar without Nextcloud.
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    # Modules and profiles.
    location_enabled: bool = False
    money_enabled: bool = True
    health_enabled: bool = True
    feeds_enabled: bool = True
    briefings_enabled: bool = True
    developer_enabled: bool = False
    gitlab_token: str = ""
    github_token: str = ""
    profiles: tuple[str, ...] = ()
    # Generated, and carried forward on a re-run.
    session_secret: str = ""

    @property
    def disabled_modules(self) -> list[str]:
        return sorted(
            module
            for field_name, module, _question in _OPT_OUT_MODULES
            if not getattr(self, field_name)
        )

    @property
    def uses_nextcloud(self) -> bool:
        return bool(self.nextcloud_url)

    @property
    def public_scheme(self) -> str:
        return "http" if self.ingress == "local" else "https"

    @property
    def web_auth(self) -> list[str]:
        methods = []
        if self.uses_nextcloud and self.oauth_client_id:
            methods.append("nextcloud")
        if self.email_login or not methods:
            methods.append("email")
        return methods

    @property
    def compose_profiles(self) -> list[str]:
        chosen = set(self.profiles)
        if self.location_enabled:
            chosen.add("location")
        return [name for name in CONTAINER_PROFILES if name in chosen]


def container_secret_values(a: ContainerAnswers) -> dict[str, str]:
    """Every file under ``secrets/``, name to content, empty when unused."""
    values = {
        "anthropic_api_key": a.anthropic_api_key if a.brain_kind != "native" else "",
        "claude_code_oauth_token": (
            a.claude_code_oauth_token if a.brain_kind != "native" else ""
        ),
        "istota_brain_native_api_key": a.native_api_key if a.brain_kind == "native" else "",
        "istota_nextcloud_app_password": a.nextcloud_app_password if a.uses_nextcloud else "",
        "istota_web_oauth2_client_secret": (
            a.oauth_client_secret if "nextcloud" in a.web_auth else ""
        ),
        "istota_web_session_secret_key": a.session_secret,
        "istota_email_imap_password": a.imap_password if a.email_enabled else "",
        "istota_caldav_password": (
            a.caldav_password if a.caldav_url and not a.uses_nextcloud else ""
        ),
        "istota_developer_gitlab_token": a.gitlab_token if a.developer_enabled else "",
        "istota_developer_github_token": a.github_token if a.developer_enabled else "",
    }
    assert tuple(values) == SECRET_NAMES
    return values


def container_config_document(a: ContainerAnswers, *, inline_credentials: bool) -> dict:
    """The container ``config.toml`` as a document (pure).

    Only what the answers decide, plus the container's fixed layout; every
    other key is left to the dataclass default, which is the only default.

    ``inline_credentials`` is the run without ``--vm-dir``: there is no
    secrets directory for compose to mount, so a credential with a config key
    is written into the file instead. The two Claude Code credentials have no
    config key and are never inlined.
    """
    secret = container_secret_values(a)

    def credential(name: str) -> bool:
        return inline_credentials and bool(secret[name])

    doc: dict = {
        "bot_name": a.bot_name,
        "db_path": CONTAINER_DB_PATH,
        "workspace_path": (
            CONTAINER_NEXTCLOUD_WORKSPACE if a.uses_nextcloud else CONTAINER_LOCAL_WORKSPACE
        ),
        "temp_dir": CONTAINER_TEMP_DIR,
        "security": {"sandbox_enabled": True, "skill_proxy_enabled": True},
    }
    if a.uses_nextcloud:
        # A real mount, so `runtime.mount_liveness` and the backup's mount
        # guard have something to check.
        doc["nextcloud_mount_path"] = CONTAINER_NEXTCLOUD_WORKSPACE

    brain: dict = {"kind": a.brain_kind}
    if a.brain_kind == "native":
        native = {"base_url": a.native_base_url, "model": a.native_model}
        if credential("istota_brain_native_api_key"):
            native["api_key"] = secret["istota_brain_native_api_key"]
        brain["native"] = native
    doc["brain"] = brain

    if a.uses_nextcloud:
        nextcloud = {
            "url": a.nextcloud_url,
            "username": a.nextcloud_username,
            "dav_prefix": a.nextcloud_dav_prefix,
            "auto_share_bot_dir": a.nextcloud_auto_share_bot_dir,
        }
        if credential("istota_nextcloud_app_password"):
            nextcloud["app_password"] = secret["istota_nextcloud_app_password"]
        doc["nextcloud"] = nextcloud
        talk: dict = {"enabled": a.talk_enabled, "bot_username": a.nextcloud_username}
        if a.talk_enabled and "signaling" in a.compose_profiles:
            talk["signaling"] = {"enabled": True, "url": CONTAINER_SIGNALING_URL}
        doc["talk"] = talk
    else:
        doc["talk"] = {"enabled": False}
        if a.caldav_url:
            caldav = {"url": a.caldav_url, "username": a.caldav_username}
            if credential("istota_caldav_password"):
                caldav["password"] = secret["istota_caldav_password"]
            doc["caldav"] = caldav

    email: dict = {"enabled": a.email_enabled}
    if a.email_enabled:
        email.update({
            "imap_host": a.imap_host,
            "imap_port": a.imap_port,
            "imap_user": a.imap_user,
            "smtp_host": a.smtp_host or a.imap_host,
            "smtp_port": a.smtp_port,
            "bot_email": a.bot_email or a.imap_user,
        })
        if credential("istota_email_imap_password"):
            email["imap_password"] = secret["istota_email_imap_password"]
    doc["email"] = email

    doc["location"] = {"enabled": a.location_enabled}
    if "browser" in a.compose_profiles:
        doc["browser"] = {"enabled": True, "api_url": CONTAINER_BROWSER_API_URL}
    if "whatsapp-baileys" in a.compose_profiles:
        doc["whatsapp"] = {"enabled": True, "provider": "baileys"}

    if a.developer_enabled:
        developer = {
            "enabled": True,
            "repos_dir": CONTAINER_REPOS_DIR,
            "gh_bin_path": f"{CONTAINER_FORGE_DIR}/gh",
            "glab_bin_path": f"{CONTAINER_FORGE_DIR}/glab",
            "devbox_proxy_socket_dir": CONTAINER_DEVBOX_CRED_DIR,
            "container": {"exec_socket_dir": CONTAINER_DEVBOX_EXEC_DIR},
        }
        if credential("istota_developer_gitlab_token"):
            developer["gitlab_token"] = secret["istota_developer_gitlab_token"]
        if credential("istota_developer_github_token"):
            developer["github_token"] = secret["istota_developer_github_token"]
        doc["developer"] = developer

    web: dict = {
        "enabled": True,
        "port": CONTAINER_WEB_PORT,
        "auth": a.web_auth,
        "trusted_proxy_hops": INGRESS_HOPS[a.ingress],
        # The web service is the only holder of its key (`/data/.web_token_key`,
        # generated by the entrypoint), so encrypted storage costs nothing here.
        "token_storage": "encrypted",
    }
    if credential("istota_web_session_secret_key"):
        web["session_secret_key"] = secret["istota_web_session_secret_key"]
    if "nextcloud" in a.web_auth:
        public = (a.nextcloud_public_url or a.nextcloud_url).rstrip("/")
        internal = a.nextcloud_url.rstrip("/")
        web.update({
            "oauth2_provider": public,
            "oauth2_client_id": a.oauth_client_id,
            "oauth2_token_endpoint": f"{internal}/index.php/apps/oauth2/api/v1/token",
            "oauth2_userinfo_endpoint": f"{internal}/ocs/v2.php/cloud/user?format=json",
            "oauth2_redirect_uri": f"{a.public_scheme}://{a.hostname}/istota/callback",
        })
        if credential("istota_web_oauth2_client_secret"):
            web["oauth2_client_secret"] = secret["istota_web_oauth2_client_secret"]
    doc["web"] = web
    doc["site"] = {"hostname": a.hostname}

    user: dict = {
        "display_name": a.display_name or a.user_id,
        "timezone": a.timezone,
    }
    if a.user_email:
        user["email_addresses"] = [a.user_email]
    if a.disabled_modules:
        user["disabled_modules"] = a.disabled_modules
    doc["users"] = {a.user_id: user}
    return doc


def render_container_config(a: ContainerAnswers, *, inline_credentials: bool) -> str:
    """The container ``config.toml`` text (pure)."""
    if inline_credentials:
        where = "Credentials are in this file, which is why it is 0600."
    else:
        where = (
            "Credentials are not in this file: each is a file under the stack's "
            "secrets/ directory."
        )
    header = (
        "# Istota configuration, written once by `istota setup` and yours to edit.\n"
        "# Nothing regenerates it. Unknown keys warn at load; a key left out takes\n"
        "# its default. Reference: config/config.example.toml.\n"
        f"# {where}\n\n"
    )
    return header + toml_write.dumps(container_config_document(a, inline_credentials=inline_credentials))


#: What compose publishes for a port slot nginx does not listen on in this
#: mode: an ephemeral port on loopback, since compose has no way to leave one of
#: the two `ports:` entries out.
UNUSED_PUBLISH = "127.0.0.1::{port}"


def tls_cert_source(a: ContainerAnswers) -> str:
    """Where nginx's certificate comes from: `acme` or `files` for `direct`,
    `files` or nothing for `proxied`, nothing for `local`."""
    if a.ingress == "direct":
        return a.tls_cert_source
    if a.ingress == "proxied" and a.tls_cert_source == "files":
        return "files"
    return ""


def nginx_publish(a: ContainerAnswers) -> tuple[str, str]:
    """The two compose port specs for nginx: the plain-HTTP slot and the TLS slot.

    `direct` listens on both, on every address. `proxied` listens on one
    private address, on the TLS slot when it terminates TLS itself (files) and
    the plain slot otherwise. `local` listens on loopback at the hostname's port.
    """
    if a.ingress == "direct":
        return "80:80", "443:443"
    if a.ingress == "proxied":
        listen = f"{a.listen_addr}:{a.listen_port or 8080}"
        if tls_cert_source(a) == "files":
            return UNUSED_PUBLISH.format(port=80), f"{listen}:443"
        return f"{listen}:80", UNUSED_PUBLISH.format(port=443)
    _, _, port = a.hostname.rpartition(":")
    port = port if port.isdigit() else "8080"
    return f"127.0.0.1:{port}:80", UNUSED_PUBLISH.format(port=443)


def stack_env_values(a: ContainerAnswers) -> dict[str, str]:
    """The compose ``.env`` keys the wizard owns: stack-level, never istota config."""
    plain, tls = nginx_publish(a)
    return {
        "COMPOSE_PROJECT_NAME": "istota",
        "COMPOSE_PROFILES": ",".join(a.compose_profiles),
        "DOMAIN": a.hostname,
        "ISTOTA_SECRETS_DIR": "./secrets",
        "NGINX_PUBLISH": plain,
        "NGINX_PUBLISH_TLS": tls,
        # Plain http on loopback needs it: a Secure cookie is never sent back.
        "ISTOTA_WEB_INSECURE_COOKIES": "1" if a.ingress == "local" else "0",
    }


def host_env_values(a: ContainerAnswers) -> dict[str, str]:
    """The ``host.env`` keys: what the VM's provisioning reads, and what the nginx
    and istota containers read for the ingress mode."""
    return {
        "STORAGE": "nextcloud" if a.uses_nextcloud else "local",
        "INGRESS": a.ingress,
        "DOMAIN": a.hostname,
        "TLS_CERT_SOURCE": tls_cert_source(a),
        "UPSTREAM_PROXY": a.upstream_proxy,
        "LISTEN_ADDR": a.listen_addr,
        "LISTEN_PORT": str(a.listen_port or ""),
    }


_STACK_ENV_HEADER = (
    "# Compose settings for this stack. `istota setup` updates the keys it owns\n"
    "# and keeps every other line. istota's own configuration is\n"
    "# /data/config/config.toml on the state volume; its credentials are the\n"
    "# files under secrets/.\n"
)
_HOST_ENV_HEADER = (
    "# Host settings for this install, read by host/provision.sh for the mount,\n"
    "# firewall and certificate units. `istota setup` updates the keys it owns.\n"
)


def merge_env_text(existing: str, owned: dict[str, str], header: str) -> str:
    """``existing`` with each owned key set, every other line kept as it was.

    The operator owns the file: a browser limit or a bundled-service password
    they added survives a re-run. An owned key that is missing is appended.
    """
    if any("\n" in value or "\r" in value for value in owned.values()):
        raise SetupError("A value for the stack's .env contains a newline.")
    lines = existing.splitlines() if existing else header.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in owned:
            if key in seen:
                continue
            out.append(f"{key}={owned[key]}")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in owned.items() if key not in seen)
    return "\n".join(out) + "\n"


def render_stack_env(a: ContainerAnswers, existing: str = "") -> str:
    """The compose ``.env`` after this run."""
    return merge_env_text(existing, stack_env_values(a), _STACK_ENV_HEADER)


def render_host_env(a: ContainerAnswers, existing: str = "") -> str:
    """``host.env`` after this run."""
    return merge_env_text(existing, host_env_values(a), _HOST_ENV_HEADER)


def _env_credential(name: str) -> str:
    """A credential for a non-interactive run, from the variable the daemon
    itself reads it from, so no secret ever has to be on the command line."""
    return os.environ.get(name.upper(), "").strip()


def _ask_secret_optional(getpass_fn, label: str) -> str:
    """A credential the operator may skip: one no-echo read, Enter skips."""
    _flush_terminal_input()
    try:
        return getpass_fn(f"{label} (Enter to skip): ").strip()
    except EOFError:
        return ""


def _ask_choice(input_fn, prompt: str, choices: tuple[str, ...], default: str, *, out) -> str:
    for attempt in range(3):
        raw = _ask(input_fn, f"{prompt} ({'/'.join(choices)})", default).strip().lower()
        if raw in choices:
            return raw
        if attempt < 2:
            out(f"  Please answer one of {', '.join(choices)}.")
    return default


def collect_container_answers(args, *, input_fn, out, getpass_fn) -> ContainerAnswers:
    """Build ``ContainerAnswers`` from flags and (unless ``--yes``) prompts.

    Non-interactively, credentials come from the environment under the names
    the daemon reads them by (``ISTOTA_NEXTCLOUD_APP_PASSWORD``,
    ``ANTHROPIC_API_KEY``, ...), never from argv.
    """
    interactive = not getattr(args, "yes", False)
    a = ContainerAnswers()

    def flag(name: str, default=None):
        value = getattr(args, name, None)
        return default if value is None else value

    def text(name: str, prompt: str, default: str) -> str:
        value = flag(name)
        if value is None and interactive:
            value = _ask(input_fn, prompt, default)
        return (value if value is not None else default).strip()

    def secret(name: str, label: str) -> str:
        value = _env_credential(name)
        if not value and interactive:
            value = _ask_secret_optional(getpass_fn, label)
        return value

    a.bot_name = text("bot_name", "Bot name", "Istota") or "Istota"

    a.user_id = text("user", "First admin's user id (their login name)", "")
    if not a.user_id:
        raise SetupError("A user id is required for the first admin (--user).")
    a.display_name = text("display_name", "Display name", a.user_id) or a.user_id
    tz = text("timezone", "Timezone (IANA, e.g. Europe/Berlin)", "UTC")
    if not _is_valid_timezone(tz):
        out(f"  '{tz}' is not a valid IANA timezone; using UTC.")
        tz = "UTC"
    a.timezone = tz
    a.user_email = text("user_email", "Their email address (optional)", "")

    # Ingress and the public name.
    a.hostname = text("hostname", "Public hostname (as browsers reach it)", "localhost")
    default_ingress = "local" if a.hostname.split(":")[0] in ("localhost", "127.0.0.1") else "direct"
    ingress = flag("ingress")
    if ingress is None and interactive:
        out("  Ingress: direct (this VM terminates TLS), proxied (behind your own")
        out("  reverse proxy on a private network), local (loopback, a laptop VM).")
        ingress = _ask_choice(input_fn, "Ingress", tuple(INGRESS_HOPS), default_ingress, out=out)
    a.ingress = ingress or default_ingress
    if a.ingress not in INGRESS_HOPS:
        raise SetupError(f"Unknown ingress {a.ingress!r}; use direct, proxied or local.")
    if a.ingress == "proxied":
        a.upstream_proxy = text(
            "upstream_proxy", "Upstream proxy address(es) or CIDR(s), comma-separated", "",
        )
        if not a.upstream_proxy:
            raise SetupError(
                "Proxied ingress needs the upstream proxy's address (--upstream-proxy): "
                "the listener must refuse every other source, or a client can forge "
                "X-Forwarded-For past the login throttle."
            )
        a.listen_addr = text("listen_addr", "Address to listen on (the VM's private interface)", "")
        if not a.listen_addr or a.listen_addr in ("0.0.0.0", "::"):
            raise SetupError("Proxied ingress listens on one private address, never 0.0.0.0.")
        a.listen_port = int(flag("listen_port") or (
            _ask_port(input_fn, "Port to listen on", 8080, out=out) if interactive else 8080
        ))
        if interactive:
            out("  The hop from the proxy to this VM is plain HTTP unless you set up")
            out("  certificates here too; that is only acceptable on a network you control.")
        # No ACME here: the public name points at the upstream, not this VM.
        source = flag("tls_cert_source")
        if source is None and interactive:
            source = "files" if _ask_yes_no(
                input_fn, "Terminate TLS here too, from certificate files?", False, out=out,
            ) else ""
        if source not in (None, "", "files"):
            raise SetupError("Proxied ingress takes certificates from files or none, never ACME.")
        a.tls_cert_source = source or ""
    if a.ingress == "direct":
        a.tls_cert_source = _ask_choice(
            input_fn, "Certificates from", ("acme", "files"), "acme", out=out,
        ) if flag("tls_cert_source") is None and interactive else (flag("tls_cert_source") or "acme")

    # Nextcloud: full integration, or none.
    nc_url = flag("nextcloud_url")
    if nc_url is None and interactive:
        use_nc = _ask_yes_no(
            input_fn, "Connect to an existing Nextcloud (files and Talk)?", False, out=out,
        )
        nc_url = _ask(input_fn, "Nextcloud URL as this stack reaches it", "") if use_nc else ""
    a.nextcloud_url = (nc_url or "").strip().rstrip("/")
    if a.uses_nextcloud:
        a.nextcloud_public_url = text(
            "nextcloud_public_url", "Nextcloud URL as browsers reach it", a.nextcloud_url,
        ).rstrip("/")
        a.nextcloud_username = text("nextcloud_user", "The bot's Nextcloud user", "istota")
        a.nextcloud_app_password = secret("istota_nextcloud_app_password", "Its app password")
        a.nextcloud_dav_prefix = text(
            "nextcloud_dav_prefix",
            "Folder in the bot's files that holds the workspace (blank for the root)", "",
        )
        if flag("no_nextcloud_auto_share", False):
            a.nextcloud_auto_share_bot_dir = False
        elif interactive:
            a.nextcloud_auto_share_bot_dir = _ask_yes_no(
                input_fn, "Share each user's bot folder back to them over OCS?",
                not a.nextcloud_dav_prefix, out=out,
            )
        else:
            a.nextcloud_auto_share_bot_dir = not a.nextcloud_dav_prefix
        if flag("no_talk", False):
            a.talk_enabled = False
        elif interactive:
            a.talk_enabled = _ask_yes_no(input_fn, "Use Nextcloud Talk?", True, out=out)
        a.oauth_client_id = text(
            "oauth_client_id", "OAuth2 client id for Nextcloud login (blank to skip)", "",
        )
        if a.oauth_client_id:
            a.oauth_client_secret = secret("istota_web_oauth2_client_secret", "OAuth2 client secret")
            if not a.oauth_client_secret:
                out("  No client secret; Nextcloud login stays off.")
                a.oauth_client_id = ""
    if flag("no_email_login", False):
        a.email_login = False

    # Brain.
    kind = flag("brain")
    if kind is None and interactive:
        kind = _ask_choice(input_fn, "Model backend", ("claude_code", "native"), "claude_code", out=out)
    a.brain_kind = kind or "claude_code"
    if a.brain_kind == "native":
        a.native_base_url = text("native_base_url", "API base URL", DEFAULT_ANTHROPIC_BASE_URL)
        a.native_model = text("native_model", "Model id", "claude-sonnet-4-6" if interactive else "")
        a.native_api_key = secret("istota_brain_native_api_key", "API key")
        if not a.native_model or not a.native_api_key:
            raise SetupError(
                "The native brain needs a model (--native-model) and an API key "
                "(ISTOTA_BRAIN_NATIVE_API_KEY)."
            )
    else:
        a.claude_code_oauth_token = secret("claude_code_oauth_token", "Claude Code OAuth token")
        if not a.claude_code_oauth_token:
            a.anthropic_api_key = secret("anthropic_api_key", "Anthropic API key")
        if not (a.claude_code_oauth_token or a.anthropic_api_key):
            out("  No Claude credential given; add one to secrets/ before the first task.")

    # Email.
    if flag("email", False):
        a.email_enabled = True
    elif interactive:
        a.email_enabled = _ask_yes_no(input_fn, "Enable email (IMAP/SMTP)?", False, out=out)
    if a.email_enabled:
        a.imap_host = text("imap_host", "IMAP host", "")
        a.imap_user = text("imap_user", "IMAP user", "")
        a.smtp_host = text("smtp_host", "SMTP host", a.imap_host)
        a.bot_email = text("bot_email", "The bot's email address", a.imap_user)
        a.imap_password = secret("istota_email_imap_password", "IMAP password")

    # Calendar, where there is no Nextcloud to derive it from.
    if not a.uses_nextcloud:
        caldav = flag("caldav_url")
        if caldav is None and interactive and _ask_yes_no(
            input_fn, "Point calendar at a CalDAV server?", False, out=out,
        ):
            caldav = _ask(input_fn, "CalDAV URL", "")
        a.caldav_url = (caldav or "").strip()
        if a.caldav_url:
            a.caldav_username = text("caldav_username", "CalDAV username", "")
            a.caldav_password = secret("istota_caldav_password", "CalDAV password")
            if not a.caldav_password:
                out("  No CalDAV password; leaving [caldav] out.")
                a.caldav_url = a.caldav_username = ""

    # Modules.
    if flag("location", False):
        a.location_enabled = True
    elif interactive:
        a.location_enabled = _ask_yes_no(input_fn, "Enable GPS/location tracking?", False, out=out)
    _collect_modules(a, args, interactive=interactive, input_fn=input_fn, out=out)
    if flag("developer", False):
        a.developer_enabled = True
    elif interactive:
        a.developer_enabled = _ask_yes_no(input_fn, "Enable the developer skill?", False, out=out)
    if a.developer_enabled:
        a.gitlab_token = secret("istota_developer_gitlab_token", "GitLab token")
        a.github_token = secret("istota_developer_github_token", "GitHub token")

    # Compose profiles.
    profiles = list(flag("profile", []) or [])
    if not profiles and interactive:
        if _ask_yes_no(input_fn, "Run the browser container?", False, out=out):
            profiles.append("browser")
        if a.uses_nextcloud and a.talk_enabled and _ask_yes_no(
            input_fn, "Run the Talk signaling server?", False, out=out,
        ):
            profiles.append("signaling")
        if _ask_yes_no(input_fn, "Run the WhatsApp (Baileys) sidecar?", False, out=out):
            profiles.append("whatsapp-baileys")
    unknown = [p for p in profiles if p not in CONTAINER_PROFILES]
    if unknown:
        raise SetupError(f"Unknown profile(s) {unknown}; known: {list(CONTAINER_PROFILES)}")
    if "signaling" in profiles and not (a.uses_nextcloud and a.talk_enabled):
        raise SetupError("The signaling profile needs Nextcloud Talk.")
    a.profiles = tuple(profiles)
    return a


def _write_secret_file(path: Path, value: str) -> None:
    """Write one credential file, 0400 from creation.

    Replaced rather than rewritten, because the existing file is 0400 and
    opening it for writing would need a chmod first. The owner is whoever runs
    this: inside the image that is uid 10001, which is the one reader.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)


def _read_secret_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _ensure_master_key(path: Path, out) -> None:
    """Create ``/data/.secret_key`` (0600) unless a usable one is there.

    Never replaced: every credential in the secrets table is encrypted under
    it. The same rule the entrypoint applies when it finds none.
    """
    from istota.credentials.store import _MIN_KEY_LEN  # noqa: PLC0415

    if len(_read_secret_file(path)) >= _MIN_KEY_LEN:
        out(f"  Keeping the existing master key at {path}.")
        return
    if path.exists():
        raise SetupError(
            f"{path} exists but holds no usable key. Move it aside yourself; "
            "setup does not replace a master key."
        )
    _write_private(path, secrets.token_hex(32))


def _carry_forward_session_secret(a: ContainerAnswers, config_path: Path, secrets_dir: Path | None) -> None:
    """Keep the web session key a previous run wrote, so a re-run does not
    sign every user out. From the secrets file, else the config."""
    import tomllib  # noqa: PLC0415

    prior = ""
    if secrets_dir is not None:
        prior = _read_secret_file(secrets_dir / "istota_web_session_secret_key")
    if not prior:
        try:
            with open(config_path, "rb") as fh:
                prior = str(tomllib.load(fh).get("web", {}).get("session_secret_key") or "")
        except (OSError, ValueError):
            prior = ""
    a.session_secret = prior.strip() or secrets.token_hex(32)


def run_container_setup(args, *, input_fn, out, getpass_fn) -> int:
    """The container half. Returns a process exit code."""
    data_dir = Path(getattr(args, "data_dir", None) or CONTAINER_DATA_DIR)
    vm_dir = Path(args.vm_dir) if getattr(args, "vm_dir", None) else None
    config_path = data_dir / "config" / "config.toml"
    secrets_dir = vm_dir / "secrets" if vm_dir is not None else None

    if config_path.exists() and not getattr(args, "force", False):
        if getattr(args, "yes", False):
            raise SetupError(
                f"{config_path} already exists and is yours now; edit it, or re-run "
                "with --force to replace it."
            )
        if not _ask_yes_no(input_fn, f"{config_path} exists. Replace it?", False, out=out):
            out("Setup aborted; the existing config is untouched.")
            return 1

    a = collect_container_answers(args, input_fn=input_fn, out=out, getpass_fn=getpass_fn)
    _carry_forward_session_secret(a, config_path, secrets_dir)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    inline = secrets_dir is None
    _write_private(config_path, render_container_config(a, inline_credentials=inline))
    _ensure_admins_file(config_path.parent / "admins", a.user_id, out=out)
    _ensure_master_key(data_dir / ".secret_key", out)

    if vm_dir is not None:
        secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(secrets_dir, 0o700)
        for name, value in container_secret_values(a).items():
            path = secrets_dir / name
            # An empty answer on a re-run is a credential this run was not
            # given, not one to delete: keep what a previous run wrote.
            if not value and _read_secret_file(path):
                out(f"  Keeping the existing {name}; delete the file to remove it.")
                continue
            _write_secret_file(path, value)
        for name, render in ((".env", render_stack_env), ("host.env", render_host_env)):
            path = vm_dir / name
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text(render(a, existing), encoding="utf-8")
    elif a.brain_kind != "native" and (a.claude_code_oauth_token or a.anthropic_api_key):
        out(
            "  The Claude credential has no config key; without --vm-dir it was not "
            "written. Put it in the stack's secrets/ directory."
        )

    out("")
    out("Setup complete.")
    out(f"  Config:  {config_path} (yours to edit; nothing regenerates it)")
    out(f"  Admins:  {config_path.parent / 'admins'}")
    if vm_dir is not None:
        out(f"  Stack:   {vm_dir / '.env'}, {vm_dir / 'host.env'}, {secrets_dir}/")
    out(f"  First admin: {a.user_id}")
    return 0
