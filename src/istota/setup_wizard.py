"""First-run setup (``istota setup``) for the one deployment shape.

It runs inside the istota image, as the daemon's uid, and writes
``/data/config/config.toml``, ``/data/config/admins`` and
``/data/.secret_key``; with ``--vm-dir`` it also writes the stack's ``.env``,
``host.env`` and one file per credential under ``secrets/``, which compose mounts
at ``/run/secrets`` and the entrypoint reads into the daemon's environment.
Nothing renders ``config.toml`` after this: the operator owns it. The terminal
wizard here and the web setup (``webui/setup_app.py``) share one answers object,
one validator and one writer.

The wizard logic is kept apart from I/O: prompts go through an injectable
``input_fn``, secrets through ``getpass_fn``, and the renderers are pure
functions of the answers.
"""

from __future__ import annotations

import dataclasses
import getpass
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from istota.lib import toml_write
from istota.lib.rclone_conf import webdav_remote_for

logger = logging.getLogger("istota.setup")


DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
#: The same model as `DEFAULT_ANTHROPIC_MODEL`, by OpenRouter's name for it.
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.6"


# ---------------------------------------------------------------------------
# Interactive prompts
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
    answer given one prompt out of step raised ``ValueError`` out of the
    wizard. Nothing is half-written when that happens — it is
    ahead of every write — but a first-run installer ending in a stack trace
    is a poor advertisement for the thing being installed, and this is the
    only other prompt that parses its answer. ``_ask_yes_no`` took the same
    treatment for the same class of input.

    The range is checked as well as the syntax, because the value reaches a
    bind: ``0`` and ``70000`` both parse and both fail later, when the stack
    starts, a long way from the question that produced them.

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


#: The opt-out modules, in the order they are asked about and rendered:
#: ``(ContainerAnswers field, module name, question)``. ``location`` is absent —
#: see ``ContainerAnswers.disabled_modules``.
_OPT_OUT_MODULES: tuple[tuple[str, str, str], ...] = (
    ("money_enabled", "money", "Enable the money module (double-entry accounting)?"),
    ("health_enabled", "health", "Enable the health module (body stats, lab results, documents)?"),
    ("feeds_enabled", "feeds", "Enable the feeds module (RSS/Atom/Tumblr reader)?"),
    ("briefings_enabled", "briefings", "Enable the briefings module (scheduled digests)?"),
)


def _collect_modules(a: ContainerAnswers, args, *, interactive, input_fn, out) -> None:
    """Ask about each opt-out module, honouring its ``--no-<name>`` flag.

    **A module whose install extra is missing is neither asked about nor
    recorded**, and the second half is the one that matters. ``money`` is the
    only entry in ``modules.MODULE_DEPENDENCIES`` today; without ``beancount``
    ``module_available()`` already hides it everywhere, so the prompt would be
    a question with no good answer — and writing ``money`` into
    ``disabled_modules`` because of it would turn a transient install state
    into a stored decision that survives a later install of the extra, with
    nothing on the machine saying why the module is
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
            setattr(a, field_name, _ask_yes_no(input_fn, question, getattr(a, field_name), out=out))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class SetupError(RuntimeError):
    """A setup-blocking condition, reported to the user."""


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, never visible at a wider mode than 0600.

    ``write_text`` then ``chmod`` leaves the file world-readable with secrets
    in it for the interval between, which is a window on a multi-user host.

    Two narrowing steps, because ``O_CREAT``'s mode applies **only** to a file
    this call creates. On the ``--force`` re-run, where the file pre-exists, the
    mode argument is ignored entirely, so a ``config.toml`` sitting at 0644
    would be truncated and refilled with credentials at its original mode and
    only narrowed afterwards. ``fchmod`` on the open descriptor closes it before
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
    on a single-user config and as "nobody" by ``is_shared_kv_writer`` and
    the web admin gate, so an install with no file at all could not write a
    shared briefing block. This gives it a real, editable authorization
    artifact.

    **It only ever creates.** An existing file is left byte for byte alone,
    even when it does not name the user: appending would be a silent
    authorization widening, and on a re-run it would add whoever answered the
    wizard this time to an allowlist the operator manages. Refusing costs
    nothing that one edit cannot fix, and the line printed here says what to
    add.

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


#: What a run outside the image is told. The standalone install it used to
#: set up was removed; the local VM replaced it.
NOT_IN_THE_IMAGE = (
    "istota setup configures the stack and runs inside the istota image "
    "(`istota-stack setup`). The standalone install was removed: on an Apple "
    "silicon Mac, install the local VM with `install.sh --local`, and move an "
    "existing standalone install into it with `istota-stack import-standalone`."
)


def setup_shape(args) -> str:
    """``container``: the flag, ``ISTOTA_SETUP_SHAPE`` (the image sets it) or
    ``--vm-dir``. Anything else is a run outside the image, refused before
    anything is written, since the half that ran there is gone."""
    shape = getattr(args, "shape", None) or os.environ.get("ISTOTA_SETUP_SHAPE", "")
    if not shape and getattr(args, "vm_dir", None):
        shape = "container"
    if shape != "container":
        raise SetupError(NOT_IN_THE_IMAGE)
    return shape


def run_setup(args, *, input_fn=input, out=print, getpass_fn=None) -> int:
    """Run the setup wizard. Returns a process exit code (0 = success)."""
    if getattr(args, "web", False) or getattr(args, "local", False):
        from .webui import setup_app  # noqa: PLC0415 - FastAPI only for the web setup

        return setup_app.serve(args, out=out)

    setup_shape(args)
    return run_container_setup(
        args, input_fn=input_fn, out=out,
        getpass_fn=getpass_fn or getpass.getpass,
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
#: The workspace when the VM provides it as a mount, bound here: the rclone
#: mount of the bot's files in full Nextcloud integration, or the local VM's
#: host folder. The entrypoint refuses to start unless it is the mount expected.
CONTAINER_NEXTCLOUD_WORKSPACE = "/mnt/shared"
#: Where the workspace lives when Nextcloud is configured: the rclone mount of
#: the bot's files, or a directory on the state volume. Talk and the nextcloud
#: skill work in both.
WORKSPACE_CHOICES = ("mount", "local")
#: Where the image installs the real forge binaries, off PATH.
CONTAINER_FORGE_DIR = "/usr/local/lib/istota_forge"
#: The `browser` service's container name and API port.
CONTAINER_BROWSER_API_URL = "http://istota-browser:9223"
#: The `signaling` service as the daemon reaches it on the stack network.
CONTAINER_SIGNALING_URL = "http://signaling:8080"
CONTAINER_WEB_PORT = 8766
#: How many ``config.toml.bak.<stamp>`` copies a re-run keeps: istota-stack's
#: ``CONFIG_APPLY_BACKUPS``, the same number ``istota apply`` keeps.
RERUN_CONFIG_BACKUPS = 10

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
    "istota_email_smtp_password",
    "istota_sms_telnyx_api_key",
    "istota_sms_telnyx_public_key",
    "istota_sms_telnyx_messaging_profile_id",
    "istota_google_workspace_client_secret",
)

#: Ingress modes, as `host.env`'s INGRESS names them, mapped to the hop count the
#: web app subtracts from X-Forwarded-For: one for the compose nginx, plus one
#: for an upstream proxy in `proxied`.
INGRESS_HOPS = {"direct": 1, "proxied": 2, "local": 1}

#: `[scheduler] cron_max_staleness_minutes` under local ingress, which is the
#: local VM: it pauses while the Mac sleeps, and a job or briefing missed by
#: up to twelve hours then runs once on wake, an older one is skipped. A server
#: keeps the dataclass default.
LOCAL_CRON_MAX_STALENESS_MINUTES = 720

#: Compose profiles the wizard can turn on; each starts the service of the same
#: name. `location` starts nothing now that the web process serves the webhook
#: routes, and stays accepted so an existing `.env` naming it still validates.
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
    #: The workspace is a folder the host shares in (the local VM's ~/Istota),
    #: rather than the state volume. Never with a Nextcloud.
    host_folder: bool = False
    # Nextcloud, full integration only. Empty url = local storage.
    nextcloud_url: str = ""
    nextcloud_public_url: str = ""
    nextcloud_username: str = "istota"
    nextcloud_app_password: str = ""
    nextcloud_dav_prefix: str = ""
    nextcloud_auto_share_bot_dir: bool = True
    #: "mount" or "local"; read only when a Nextcloud URL is given.
    workspace: str = "mount"
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
    #: The receiving mail server's authserv-id. Set, the sender check reads
    #: that server's DMARC verdict instead of trusting `From:` (ISSUE-746).
    authserv_id: str = ""
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
    def workspace_is_mount(self) -> bool:
        """The workspace is the bot's Nextcloud files, on the rclone mount."""
        return self.uses_nextcloud and self.workspace == "mount"

    @property
    def storage(self) -> str:
        """host.env's STORAGE: what the VM provides at /mnt/shared, if anything."""
        if self.workspace_is_mount:
            return "nextcloud"
        return "host-folder" if self.host_folder else "local"

    @property
    def mounted_workspace(self) -> bool:
        return self.storage != "local"

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
        # Asked by no question here: the SMTP password falls back to the IMAP
        # one, and the rest are written by an operator or by
        # `istota migrate import-bare-metal`. A re-run keeps a non-empty file.
        "istota_email_smtp_password": "",
        "istota_sms_telnyx_api_key": "",
        "istota_sms_telnyx_public_key": "",
        "istota_sms_telnyx_messaging_profile_id": "",
        "istota_google_workspace_client_secret": "",
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
            CONTAINER_NEXTCLOUD_WORKSPACE if a.mounted_workspace else CONTAINER_LOCAL_WORKSPACE
        ),
        "temp_dir": CONTAINER_TEMP_DIR,
        "security": {"sandbox_enabled": True, "skill_proxy_enabled": True},
    }
    if a.mounted_workspace:
        # A real mount, so `runtime.mount_liveness` and the backup's mount
        # guard have something to check.
        doc["workspace_mount_path"] = CONTAINER_NEXTCLOUD_WORKSPACE

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
        if a.authserv_id:
            email["authserv_id"] = a.authserv_id
            email["confirm_sender_match"] = "verify"
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
    if a.ingress == "local":
        doc["scheduler"] = {"cron_max_staleness_minutes": LOCAL_CRON_MAX_STALENESS_MINUTES}

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


#: The certificate sources that terminate TLS on the proxied listener.
#: `self-signed` is made once by provision.sh for the reverse proxy to pin.
PROXIED_TLS_SOURCES = ("self-signed", "files")


def tls_cert_source(a: ContainerAnswers) -> str:
    """Where nginx's certificate comes from: `acme` or `files` for `direct`,
    `self-signed`, `files` or nothing for `proxied`, nothing for `local`."""
    if a.ingress == "direct":
        return a.tls_cert_source if a.tls_cert_source in ("acme", "files") else "acme"
    if a.ingress == "proxied" and a.tls_cert_source in PROXIED_TLS_SOURCES:
        return a.tls_cert_source
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
        if tls_cert_source(a) in PROXIED_TLS_SOURCES:
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
        # Secure in every mode: `local` is http://localhost, which browsers
        # treat as a secure context. Owned here so a re-run resets an older 1.
        "ISTOTA_WEB_INSECURE_COOKIES": "0",
    }


#: The remote's name in the ``rclone.conf`` setup stages, and ``host.env``'s
#: ``RCLONE_REMOTE``, which the VM's mount unit reads.
RCLONE_REMOTE = "nextcloud"


def host_env_values(a: ContainerAnswers) -> dict[str, str]:
    """The ``host.env`` keys: what the VM's provisioning reads, and what the nginx
    and daemon containers read for the ingress mode."""
    return {
        "STORAGE": a.storage,
        "INGRESS": a.ingress,
        "DOMAIN": a.hostname,
        "TLS_CERT_SOURCE": tls_cert_source(a),
        "UPSTREAM_PROXY": a.upstream_proxy,
        "LISTEN_ADDR": a.listen_addr,
        "LISTEN_PORT": str(a.listen_port or ""),
        "RCLONE_REMOTE": RCLONE_REMOTE if a.workspace_is_mount else "",
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


def _host_env_value(text: str, key: str) -> str:
    """One key of ``host.env`` as provision.sh's ``host_env`` reads it: the
    last non-empty value wins, and the file is never executed."""
    value = ""
    for line in text.splitlines():
        name, sep, rest = line.partition("=")
        if sep and name == key and rest:
            value = rest
    return value


def current_storage(vm_dir: Path | None) -> str:
    """host.env's STORAGE as it stands before a setup run, or ``""``."""
    if vm_dir is None or not (vm_dir / "host.env").is_file():
        return ""
    return _host_env_value((vm_dir / "host.env").read_text(encoding="utf-8"), "STORAGE")


class PriorConfigUnreadable(SetupError):
    """The stack's config.toml exists but does not parse."""


def prior_user_ids(doc: dict, admins_path: Path) -> list[str]:
    """Every id with both a line in the admins file and a ``[users.<id>]``
    block: the user a re-run keeps when there is exactly one."""
    from .config import load_admin_users  # noqa: PLC0415

    if not admins_path.is_file():
        return []
    users = doc.get("users")
    if not isinstance(users, dict):
        return []
    admins = load_admin_users(str(admins_path))
    return sorted(user_id for user_id in users if user_id in admins)


def _env_file_values(path: Path) -> dict[str, str]:
    """A ``KEY=value`` file as ``_host_env_value`` reads one, every key."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition("=")
        if sep and not name.lstrip().startswith("#") and rest:
            values[name.strip()] = rest
    return values


def prior_answers(data_dir: Path, vm_dir: Path | None) -> ContainerAnswers | None:
    """What the stack's current files say, or None when there is no config.toml.

    The inverse of ``container_config_document``, ``host_env_values``,
    ``stack_env_values`` and ``container_secret_values`` for what those write;
    the config is authoritative. A value setup cannot write is refused by name
    rather than mapped to a default, so a re-run cannot quietly change it.
    Raises ``PriorConfigUnreadable`` for a config that does not parse.
    """
    import tomllib  # noqa: PLC0415

    config_path = data_dir / "config" / "config.toml"
    if not config_path.is_file():
        return None
    try:
        doc = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PriorConfigUnreadable(f"{config_path} does not parse ({exc})") from None

    def table(*keys: str) -> dict:
        node = doc
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else {}

    def string(node: dict, key: str, default: str = "") -> str:
        value = node.get(key)
        return value if isinstance(value, str) else default

    secrets_dir = vm_dir / "secrets" if vm_dir is not None else None

    def credential(name: str, node: dict, key: str) -> str:
        if secrets_dir is not None:
            value = _read_secret_file(secrets_dir / name)
            if value:
                return value
        return string(node, key)

    def cannot_write(key: str, value: object) -> SetupError:
        return SetupError(
            f"{key} = {value!r} in {config_path.name} is not something setup can write. "
            f"Change it on the admin Configuration page or in config.toml first, "
            f"then re-run setup."
        )

    a = ContainerAnswers()
    a.bot_name = string(doc, "bot_name", a.bot_name)

    brain = table("brain")
    a.brain_kind = string(brain, "kind", "claude_code")
    if a.brain_kind not in _BRAIN_KINDS:
        raise cannot_write("brain.kind", a.brain_kind)
    native = table("brain", "native")
    if a.brain_kind == "native":
        a.native_base_url = string(native, "base_url", DEFAULT_ANTHROPIC_BASE_URL)
        a.native_model = string(native, "model")
        a.native_api_key = credential("istota_brain_native_api_key", native, "api_key")
    else:
        a.claude_code_oauth_token = credential("claude_code_oauth_token", {}, "")
        a.anthropic_api_key = credential("anthropic_api_key", {}, "")

    mounted = "workspace_mount_path" in doc
    nextcloud = table("nextcloud")
    web = table("web")
    a.nextcloud_url = string(nextcloud, "url")
    if a.uses_nextcloud:
        a.nextcloud_username = string(nextcloud, "username", a.nextcloud_username)
        a.nextcloud_dav_prefix = string(nextcloud, "dav_prefix")
        a.nextcloud_auto_share_bot_dir = nextcloud.get("auto_share_bot_dir", True) is not False
        a.nextcloud_app_password = credential("istota_nextcloud_app_password", nextcloud, "app_password")
        a.workspace = "mount" if mounted else "local"
        a.talk_enabled = table("talk").get("enabled", True) is not False
        a.oauth_client_id = string(web, "oauth2_client_id")
        a.nextcloud_public_url = string(web, "oauth2_provider") or a.nextcloud_url
        a.oauth_client_secret = credential(
            "istota_web_oauth2_client_secret", web, "oauth2_client_secret",
        )
    else:
        a.host_folder = mounted
        caldav = table("caldav")
        a.caldav_url = string(caldav, "url")
        a.caldav_username = string(caldav, "username")
        a.caldav_password = credential("istota_caldav_password", caldav, "password")
    auth = web.get("auth")
    if isinstance(auth, list):
        a.email_login = "email" in auth

    email = table("email")
    a.email_enabled = email.get("enabled") is True
    if a.email_enabled:
        a.imap_host = string(email, "imap_host")
        a.imap_user = string(email, "imap_user")
        a.smtp_host = string(email, "smtp_host")
        a.bot_email = string(email, "bot_email")
        a.authserv_id = string(email, "authserv_id")
        for key in ("imap_port", "smtp_port"):
            value = email.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(a, key, value)
        a.imap_password = credential("istota_email_imap_password", email, "imap_password")

    a.location_enabled = table("location").get("enabled") is True
    developer = table("developer")
    a.developer_enabled = developer.get("enabled") is True
    if a.developer_enabled:
        a.gitlab_token = credential("istota_developer_gitlab_token", developer, "gitlab_token")
        a.github_token = credential("istota_developer_github_token", developer, "github_token")
    a.hostname = string(table("site"), "hostname", a.hostname)

    host_env = _env_file_values(vm_dir / "host.env") if vm_dir is not None else {}
    if host_env.get("INGRESS"):
        a.ingress = host_env["INGRESS"]
    elif web.get("trusted_proxy_hops") == INGRESS_HOPS["proxied"]:
        a.ingress = "proxied"
    else:
        a.ingress = "local" if "scheduler" in doc else "direct"
    a.tls_cert_source = host_env.get("TLS_CERT_SOURCE", "")
    a.upstream_proxy = host_env.get("UPSTREAM_PROXY", "")
    a.listen_addr = host_env.get("LISTEN_ADDR", "")
    port = host_env.get("LISTEN_PORT", "")
    a.listen_port = int(port) if port.isdigit() else 0

    stack_env = _env_file_values(vm_dir / ".env") if vm_dir is not None else {}
    profiles = [p for p in stack_env.get("COMPOSE_PROFILES", "").split(",") if p]
    unknown = [p for p in profiles if p not in CONTAINER_PROFILES]
    if unknown:
        raise SetupError(
            f"COMPOSE_PROFILES in the stack's .env names {', '.join(unknown)}, which setup "
            f"does not know (it knows {', '.join(CONTAINER_PROFILES)}). Remove it from .env "
            f"first, then re-run setup."
        )
    # `location` follows location.enabled; keep it only where it does not.
    a.profiles = tuple(p for p in profiles if not (p == "location" and a.location_enabled))

    matches = prior_user_ids(doc, config_path.parent / "admins")
    if len(matches) == 1:
        a.user_id = matches[0]
        user = table("users", a.user_id)
        a.display_name = string(user, "display_name", a.user_id)
        a.timezone = string(user, "timezone", a.timezone)
        addresses = user.get("email_addresses")
        if isinstance(addresses, list) and addresses and isinstance(addresses[0], str):
            a.user_email = addresses[0]
        disabled = user.get("disabled_modules")
        if isinstance(disabled, list):
            for field_name, module, _question in _OPT_OUT_MODULES:
                setattr(a, field_name, module not in disabled)
    return a


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


#: What the terminal says before asking for the authserv-id. The web setup
#: says the same in its own words, in `StepConnections.svelte`.
AUTHSERV_ID_EXPLANATION = (
    "  Sender check. Mail from your own address is trusted on its From: line",
    "  alone, unless the bot can read your mail server's own verdict on it.",
    "  For that it needs the server's authserv-id: the name your mail server",
    "  writes at the start of the Authentication-Results header it adds to",
    "  every message it receives, before the first semicolon.",
    "  To find it, open the raw source of a message this mailbox received",
    "  (\"Show original\" or \"View source\" in most mail apps) and read the",
    "  topmost Authentication-Results line:",
    "      Authentication-Results: mx.example.com; dkim=pass ...; dmarc=pass ...",
    "  Here it is mx.example.com. For Gmail and Google Workspace it is mx.google.com.",
    "  Given one, mail claiming your address that your server did not verify",
    "  is held for you to confirm. Leave it blank to trust From: as before; the",
    "  bot then logs the value it sees on the first message that passes.",
)


def _ask_secret_optional(getpass_fn, label: str, *, keeps: bool = False) -> str:
    """A credential the operator may skip: one no-echo read. Enter skips, or
    keeps the stored one when ``keeps`` (the caller keeps it)."""
    _flush_terminal_input()
    hint = "Enter keeps the current one" if keeps else "Enter to skip"
    try:
        return getpass_fn(f"{label} ({hint}): ").strip()
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


@dataclass(frozen=True)
class FieldError:
    """One refusal of a set of answers, on the field it is about."""

    #: A ``ContainerAnswers`` attribute name, or ``""`` for the whole form.
    field: str
    message: str


class PayloadError(SetupError):
    """A web payload that cannot become answers: an unknown key, a wrong type,
    a choice with no value it can mean.

    ``answers`` is what the rest of the payload made, with each refused key
    taken as unasked, so a caller can still validate the fields that were
    fine. None when the payload was not an object at all.
    """

    def __init__(self, errors: list[FieldError], answers: "ContainerAnswers | None" = None):
        super().__init__(errors[0].message)
        self.errors = errors
        self.answers = answers


_BRAIN_KINDS = ("claude_code", "native")


def validate_container_answers(a: ContainerAnswers) -> list[FieldError]:
    """Every refusal of ``a``, in the order the terminal wizard asks.

    The one rule set for both front ends: the terminal raises the first error
    as a ``SetupError`` and the web setup shows them all, so the wording here
    is the terminal's. A rule added to only one front end is how two of them
    drift apart.
    """
    errors: list[FieldError] = []

    def refuse(field_name: str, message: str) -> None:
        errors.append(FieldError(field_name, message))

    if not a.user_id:
        refuse("user_id", "A user id is required for the first admin (--user).")
    if not _is_valid_timezone(a.timezone):
        refuse("timezone", f"'{a.timezone}' is not a valid IANA timezone.")
    if a.ingress not in INGRESS_HOPS:
        refuse("ingress", f"Unknown ingress {a.ingress!r}; use direct, proxied or local.")
    if a.ingress == "proxied":
        if not a.upstream_proxy:
            refuse(
                "upstream_proxy",
                "Proxied ingress needs the upstream proxy's address (--upstream-proxy): "
                "the listener must refuse every other source, or a client can forge "
                "X-Forwarded-For past the login throttle.",
            )
        if not a.listen_addr or a.listen_addr in ("0.0.0.0", "::"):
            refuse("listen_addr", "Proxied ingress listens on one private address, never 0.0.0.0.")
        if not 1 <= a.listen_port <= 65535:
            refuse("listen_port", f"The listen port must be between 1 and 65535, not {a.listen_port}.")
        if a.tls_cert_source not in ("", *PROXIED_TLS_SOURCES):
            refuse(
                "tls_cert_source",
                "Proxied ingress takes a self-signed certificate, files or none, never ACME.",
            )
    if a.ingress == "direct" and a.tls_cert_source == "self-signed":
        refuse(
            "tls_cert_source",
            "A self-signed certificate is only for proxied ingress, behind a reverse proxy "
            "that pins it; a public client cannot.",
        )
    if a.brain_kind not in _BRAIN_KINDS:
        refuse("brain_kind", f"Unknown model backend {a.brain_kind!r}; use claude_code or native.")
    if a.brain_kind == "native":
        needs = (
            "The native brain needs a model (--native-model) and an API key "
            "(ISTOTA_BRAIN_NATIVE_API_KEY)."
        )
        if not a.native_model:
            refuse("native_model", needs)
        if not a.native_api_key:
            refuse("native_api_key", needs)
    from .config import authserv_id_is_one_token  # noqa: PLC0415 - keep the import graph lean

    if a.email_enabled and a.authserv_id and not authserv_id_is_one_token(a.authserv_id):
        refuse(
            "authserv_id",
            "The authserv-id is the single name before the first semicolon of the "
            "Authentication-Results header, without the version number or anything "
            "after it (--authserv-id).",
        )
    # The terminal leaves [caldav] out rather than ask again; the web asks.
    if a.caldav_url and not a.uses_nextcloud and not a.caldav_password:
        refuse("caldav_password", "A calendar needs its password.")
    unknown = [p for p in a.profiles if p not in CONTAINER_PROFILES]
    if unknown:
        refuse("profiles", f"Unknown profile(s) {unknown}; known: {list(CONTAINER_PROFILES)}")
    if "signaling" in a.profiles and not (a.uses_nextcloud and a.talk_enabled):
        refuse("profiles", "The signaling profile needs Nextcloud Talk.")
    return errors


def _refuse_on(a: ContainerAnswers, *fields: str) -> None:
    """Raise the validator's first error on ``fields``, so the terminal refuses
    at the question that caused it rather than after every other one."""
    for error in validate_container_answers(a):
        if not fields or error.field in fields:
            raise SetupError(error.message)


#: Storage choices on the web setup, as `host.env`'s STORAGE names them.
STORAGE_CHOICES = ("local", "nextcloud")
#: The compose profiles the web setup offers; the rest are operator features.
WEB_SETUP_PROFILES = ("browser",)

#: Every key the web setup may send, with its JSON type: the questions it asks,
#: plus the storage choice, which no single answers field holds. Everything
#: else takes the value a `--yes` run gives it.
WEB_SETUP_FIELDS: dict[str, type] = {
    "bot_name": str,
    "user_id": str,
    "display_name": str,
    "timezone": str,
    "user_email": str,
    "hostname": str,
    "ingress": str,
    "upstream_proxy": str,
    "listen_addr": str,
    "listen_port": int,
    "tls_cert_source": str,
    "storage": str,
    "workspace": str,
    "nextcloud_url": str,
    "nextcloud_public_url": str,
    "nextcloud_username": str,
    "nextcloud_app_password": str,
    "talk_enabled": bool,
    "brain_kind": str,
    "native_base_url": str,
    "native_model": str,
    "native_api_key": str,
    "claude_code_oauth_token": str,
    "email_enabled": bool,
    "imap_host": str,
    "imap_user": str,
    "imap_password": str,
    "smtp_host": str,
    "bot_email": str,
    "authserv_id": str,
    "caldav_url": str,
    "caldav_username": str,
    "caldav_password": str,
    **{field_name: bool for field_name, _module, _question in _OPT_OUT_MODULES},
    "profiles": list,
}


def _payload_type_ok(value, expected: type) -> bool:
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is list:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, expected)


def answers_from_payload(payload: dict) -> ContainerAnswers:
    """The web setup's answers as ``ContainerAnswers``.

    Only ``WEB_SETUP_FIELDS`` are accepted, and an unknown key or a value of
    the wrong type raises ``PayloadError`` rather than being dropped. A key
    left out, or sent as ``null``, is an unasked question and takes what a
    ``--yes`` run of ``collect_container_answers`` gives it, so the two front
    ends agree on what unasked means. Validation is
    ``validate_container_answers``'s, not this function's.
    """
    if not isinstance(payload, dict):
        raise PayloadError([FieldError("", "The setup answers must be a JSON object.")])
    errors: list[FieldError] = []
    values = {}
    for key, value in payload.items():
        expected = WEB_SETUP_FIELDS.get(key)
        if expected is None:
            errors.append(FieldError("", f"Unknown setup field {key!r}."))
        elif value is None:
            continue
        elif not _payload_type_ok(value, expected):
            errors.append(FieldError(key, f"{key} must be a {expected.__name__}."))
        else:
            values[key] = value

    def text(key: str) -> str:
        return str(values.get(key, "")).strip()

    a = ContainerAnswers()
    a.bot_name = text("bot_name") or "Istota"
    a.user_id = text("user_id")
    a.display_name = text("display_name") or a.user_id
    a.timezone = text("timezone") or "UTC"
    a.user_email = text("user_email")

    a.hostname = text("hostname") or "localhost"
    default_ingress = "local" if a.hostname.split(":")[0] in ("localhost", "127.0.0.1") else "direct"
    a.ingress = text("ingress") or default_ingress
    if a.ingress == "proxied":
        a.upstream_proxy = text("upstream_proxy")
        a.listen_addr = text("listen_addr")
        a.listen_port = values.get("listen_port") or 8080
        source = text("tls_cert_source")
        a.tls_cert_source = "" if source == "none" else source
    elif a.ingress == "direct":
        a.tls_cert_source = text("tls_cert_source") or "acme"

    storage = text("storage") or "local"
    if storage not in STORAGE_CHOICES:
        errors.append(FieldError("storage", f"Unknown storage {storage!r}; use local or nextcloud."))
    elif storage == "nextcloud":
        a.nextcloud_url = text("nextcloud_url").rstrip("/")
        if not a.nextcloud_url:
            errors.append(FieldError("nextcloud_url", "Enter your Nextcloud's address."))
        a.nextcloud_public_url = (text("nextcloud_public_url") or a.nextcloud_url).rstrip("/")
        a.nextcloud_username = text("nextcloud_username") or "istota"
        a.nextcloud_app_password = text("nextcloud_app_password")
        a.talk_enabled = values.get("talk_enabled", True)
        # The terminal's `--yes` answers: the mount, its root as the bot
        # folder, shared back; a local workspace has nothing in Nextcloud to share.
        a.workspace = text("workspace") or "mount"
        if a.workspace not in WORKSPACE_CHOICES:
            errors.append(FieldError("workspace", f"Unknown workspace {a.workspace!r}; use mount or local."))
        elif a.workspace == "local":
            a.nextcloud_auto_share_bot_dir = False

    a.brain_kind = text("brain_kind") or "claude_code"
    if a.brain_kind == "native":
        a.native_base_url = text("native_base_url") or DEFAULT_ANTHROPIC_BASE_URL
        a.native_model = text("native_model")
        a.native_api_key = text("native_api_key")
    else:
        a.claude_code_oauth_token = text("claude_code_oauth_token")

    a.email_enabled = values.get("email_enabled", False)
    if a.email_enabled:
        a.imap_host = text("imap_host")
        a.imap_user = text("imap_user")
        a.smtp_host = text("smtp_host") or a.imap_host
        a.bot_email = text("bot_email") or a.imap_user
        a.imap_password = text("imap_password")
        a.authserv_id = text("authserv_id")

    if not a.uses_nextcloud:
        a.caldav_url = text("caldav_url")
        if a.caldav_url:
            a.caldav_username = text("caldav_username")
            a.caldav_password = text("caldav_password")

    from .modules import module_available  # noqa: PLC0415 - keep the import graph lean

    # A module whose extra is missing is neither offered nor recorded: the
    # terminal wizard's rule in `_collect_modules`.
    for field_name, module, _question in _OPT_OUT_MODULES:
        if field_name in values and module_available(module):
            setattr(a, field_name, values[field_name])

    profiles = list(values.get("profiles", []))
    offered = [p for p in profiles if p not in WEB_SETUP_PROFILES]
    if offered:
        errors.append(FieldError("profiles", f"The web setup does not offer {offered}."))
    a.profiles = tuple(profiles)

    if errors:
        raise PayloadError(errors, a)
    return a


def _follow(prior_value: str, prior_source: str, new_source: str) -> str:
    """A default that tracks another answer: the new source when the prior
    value was empty or was the prior source, else the prior value."""
    return new_source if prior_value in ("", prior_source) else prior_value


def collect_container_answers(
    args, *, input_fn, out, getpass_fn, current_storage: str = "",
    prior: ContainerAnswers | None = None,
) -> ContainerAnswers:
    """Build ``ContainerAnswers`` from flags and (unless ``--yes``) prompts.

    Non-interactively, credentials come from the environment under the names
    the daemon reads them by (``ISTOTA_NEXTCLOUD_APP_PASSWORD``,
    ``ANTHROPIC_API_KEY``, ...), never from argv. Every refusal is
    ``validate_container_answers``'s, raised at the question it is about.

    ``prior`` is what the stack's files say on a re-run (``prior_answers``).
    The answers start from a copy of it, so every prompt default and every
    ``--yes`` fallback is the current value, a question never asked keeps it,
    and an empty credential answer keeps the stored one. Flags still win. The
    user and their per-user settings are kept and shown, not asked: those live
    in the profile row, which a config edit does not change.

    ``current_storage`` is host.env's STORAGE before this run, the fallback
    when there is no prior. A host folder is kept when ``--storage`` is not
    given, so a re-run does not move the workspace off the user's folder; it is
    asked for by no question, since only the local VM's ``create`` sets one up.
    """
    interactive = not getattr(args, "yes", False)
    a = dataclasses.replace(prior) if prior is not None else ContainerAnswers()

    def flag(name: str, default=None):
        value = getattr(args, name, None)
        return default if value is None else value

    def text(name: str, prompt: str, default: str) -> str:
        value = flag(name)
        if value is None and interactive:
            value = _ask(input_fn, prompt, default)
        return (value if value is not None else default).strip()

    def secret(name: str, label: str, current: str) -> str:
        value = _env_credential(name)
        if not value and interactive:
            value = _ask_secret_optional(getpass_fn, label, keeps=bool(current))
        return value or current

    a.bot_name = text("bot_name", "Bot name", a.bot_name) or "Istota"

    kept_user = bool(a.user_id) and flag("user") in (None, a.user_id)
    if kept_user:
        out(f"  First admin: {a.user_id} ({a.display_name}, {a.timezone}"
            f"{', ' + a.user_email if a.user_email else ''}).")
        out("  Their name, timezone, email and modules are changed in Settings, not here.")
        a.display_name = flag("display_name", a.display_name)
        a.timezone = flag("timezone", a.timezone)
        a.user_email = flag("user_email", a.user_email)
    else:
        a.user_id = text("user", "First admin's user id (their login name)", "")
        _refuse_on(a, "user_id")
        a.display_name = text("display_name", "Display name", a.user_id) or a.user_id
        tz = text("timezone", "Timezone (IANA, e.g. Europe/Berlin)", "UTC")
        if not _is_valid_timezone(tz):
            out(f"  '{tz}' is not a valid IANA timezone; using UTC.")
            tz = "UTC"
        a.timezone = tz
        a.user_email = text("user_email", "Their email address (optional)", "")

    # Ingress and the public name.
    a.hostname = text("hostname", "Public hostname (as browsers reach it)", a.hostname)
    if prior is not None and a.hostname == prior.hostname:
        default_ingress = prior.ingress
    else:
        default_ingress = "local" if a.hostname.split(":")[0] in ("localhost", "127.0.0.1") else "direct"
    ingress = flag("ingress")
    if ingress is None and interactive:
        out("  Ingress: direct (this VM terminates TLS), proxied (behind your own")
        out("  reverse proxy on a private network), local (loopback, a laptop VM).")
        ingress = _ask_choice(input_fn, "Ingress", tuple(INGRESS_HOPS), default_ingress, out=out)
    a.ingress = ingress or default_ingress
    _refuse_on(a, "ingress")
    was_proxied = prior is not None and prior.ingress == "proxied"
    if a.ingress == "proxied":
        a.upstream_proxy = text(
            "upstream_proxy", "Upstream proxy address(es) or CIDR(s), comma-separated",
            a.upstream_proxy,
        )
        _refuse_on(a, "upstream_proxy")
        a.listen_addr = text(
            "listen_addr", "Address to listen on (the VM's private interface)", a.listen_addr,
        )
        _refuse_on(a, "listen_addr")
        port_default = a.listen_port or 8080
        a.listen_port = int(flag("listen_port") or (
            _ask_port(input_fn, "Port to listen on", port_default, out=out) if interactive else port_default
        ))
        _refuse_on(a, "listen_port")
        if interactive:
            out("  The hop from the proxy to this VM: self-signed encrypts it with a")
            out("  certificate made here, which you pin in the proxy (provision.sh prints")
            out("  how); files uses one you provide; none is plain HTTP, acceptable only")
            out("  on a network nothing else shares.")
        # No ACME here: the public name points at the upstream, not this VM.
        kept_source = a.tls_cert_source if was_proxied else ""
        source = flag("tls_cert_source")
        if source is None and interactive:
            source = _ask_choice(
                input_fn, "TLS on the hop from the proxy", ("self-signed", "files", "none"),
                (kept_source or "none") if was_proxied else "self-signed", out=out,
            )
        if source is None:
            source = kept_source
        if source == "none":
            source = ""
        a.tls_cert_source = source or ""
        _refuse_on(a, "tls_cert_source")
    if a.ingress == "direct":
        kept_source = a.tls_cert_source if a.tls_cert_source in ("acme", "files") else "acme"
        a.tls_cert_source = _ask_choice(
            input_fn, "Certificates from", ("acme", "files"), kept_source, out=out,
        ) if flag("tls_cert_source") is None and interactive else (flag("tls_cert_source") or kept_source)
        _refuse_on(a, "tls_cert_source")

    # Where the workspace is: a folder the host shares in, a Nextcloud, or the
    # state volume.
    storage = flag("storage")
    if storage is None:
        if prior is not None:
            storage = "host-folder" if prior.host_folder else None
        elif current_storage == "host-folder":
            storage = "host-folder"
    if storage not in (None, "local", "host-folder"):
        raise SetupError(f"Unknown storage {storage!r}; use local or host-folder.")
    a.host_folder = storage == "host-folder"
    if a.host_folder and flag("nextcloud_url"):
        raise SetupError(
            "--storage host-folder is a folder the host shares in as the workspace; "
            "it cannot be combined with a Nextcloud (--nextcloud-url)."
        )

    # Nextcloud, or none; with it, the workspace on the mount or local.
    prior_nc_url = a.nextcloud_url
    nc_url = flag("nextcloud_url")
    if nc_url is None and a.host_folder:
        nc_url = ""
    if nc_url is None and interactive:
        use_nc = _ask_yes_no(
            input_fn, "Connect to an existing Nextcloud (files and Talk)?", bool(prior_nc_url), out=out,
        )
        nc_url = _ask(input_fn, "Nextcloud URL as this stack reaches it", prior_nc_url) if use_nc else ""
    if nc_url is None:
        nc_url = prior_nc_url
    a.nextcloud_url = (nc_url or "").strip().rstrip("/")
    if a.uses_nextcloud:
        a.nextcloud_public_url = text(
            "nextcloud_public_url", "Nextcloud URL as browsers reach it",
            _follow(a.nextcloud_public_url, prior_nc_url, a.nextcloud_url),
        ).rstrip("/")
        a.nextcloud_username = text("nextcloud_user", "The bot's Nextcloud user", a.nextcloud_username)
        a.nextcloud_app_password = secret(
            "istota_nextcloud_app_password", "Its app password", a.nextcloud_app_password,
        )
        workspace = flag("workspace")
        if workspace is None and interactive:
            out("  Where should the workspace live? mount uses the bot account's files in")
            out("  Nextcloud through an rclone mount on the VM. local keeps files on this VM")
            out("  and shows them in the web UI.")
            workspace = _ask_choice(input_fn, "Workspace", WORKSPACE_CHOICES, a.workspace, out=out)
        a.workspace = workspace or a.workspace
        if a.workspace not in WORKSPACE_CHOICES:
            raise SetupError(f"Unknown workspace {a.workspace!r}; use mount or local.")
        if a.workspace == "local":
            # Nothing of the workspace is in Nextcloud, so there is no folder
            # to name and nothing to share back.
            a.nextcloud_dav_prefix = ""
            a.nextcloud_auto_share_bot_dir = False
        else:
            a.nextcloud_dav_prefix = text(
                "nextcloud_dav_prefix",
                "Folder in the bot's files that holds the workspace (blank for the root)",
                a.nextcloud_dav_prefix,
            )
            if prior is not None and prior.workspace_is_mount:
                share_default = prior.nextcloud_auto_share_bot_dir
            else:
                share_default = not a.nextcloud_dav_prefix
            if flag("no_nextcloud_auto_share", False):
                a.nextcloud_auto_share_bot_dir = False
            elif interactive:
                a.nextcloud_auto_share_bot_dir = _ask_yes_no(
                    input_fn, "Share each user's bot folder back to them over OCS?",
                    share_default, out=out,
                )
            else:
                a.nextcloud_auto_share_bot_dir = share_default
        if flag("no_talk", False):
            a.talk_enabled = False
        elif interactive:
            a.talk_enabled = _ask_yes_no(input_fn, "Use Nextcloud Talk?", a.talk_enabled, out=out)
        a.oauth_client_id = text(
            "oauth_client_id", "OAuth2 client id for Nextcloud login (blank to skip)", a.oauth_client_id,
        )
        if a.oauth_client_id:
            a.oauth_client_secret = secret(
                "istota_web_oauth2_client_secret", "OAuth2 client secret", a.oauth_client_secret,
            )
            if not a.oauth_client_secret:
                out("  No client secret; Nextcloud login stays off.")
                a.oauth_client_id = ""
    elif flag("workspace") not in (None, "local"):
        raise SetupError("A mount workspace needs Nextcloud (--nextcloud-url).")
    if flag("no_email_login", False):
        a.email_login = False

    # Brain.
    kind = flag("brain")
    if kind is None and interactive:
        kind = _ask_choice(input_fn, "Model backend", ("claude_code", "native"), a.brain_kind, out=out)
    a.brain_kind = kind or a.brain_kind
    if a.brain_kind == "native":
        a.native_base_url = text("native_base_url", "API base URL", a.native_base_url)
        a.native_model = text(
            "native_model", "Model id", a.native_model or (DEFAULT_ANTHROPIC_MODEL if interactive else ""),
        )
        a.native_api_key = secret("istota_brain_native_api_key", "API key", a.native_api_key)
        _refuse_on(a, "brain_kind", "native_model", "native_api_key")
    else:
        a.claude_code_oauth_token = secret(
            "claude_code_oauth_token", "Claude Code OAuth token", a.claude_code_oauth_token,
        )
        if not a.claude_code_oauth_token:
            a.anthropic_api_key = secret("anthropic_api_key", "Anthropic API key", a.anthropic_api_key)
        if not (a.claude_code_oauth_token or a.anthropic_api_key):
            out("  No Claude credential given; add one to secrets/ before the first task.")

    # Email.
    if flag("email", False):
        a.email_enabled = True
    elif interactive:
        a.email_enabled = _ask_yes_no(input_fn, "Enable email (IMAP/SMTP)?", a.email_enabled, out=out)
    if a.email_enabled:
        prior_imap_host, prior_imap_user = a.imap_host, a.imap_user
        a.imap_host = text("imap_host", "IMAP host", a.imap_host)
        a.imap_user = text("imap_user", "IMAP user", a.imap_user)
        a.smtp_host = text("smtp_host", "SMTP host", _follow(a.smtp_host, prior_imap_host, a.imap_host))
        a.bot_email = text(
            "bot_email", "The bot's email address", _follow(a.bot_email, prior_imap_user, a.imap_user),
        )
        a.imap_password = secret("istota_email_imap_password", "IMAP password", a.imap_password)
        if interactive and flag("authserv_id") is None:
            for line in AUTHSERV_ID_EXPLANATION:
                out(line)
        a.authserv_id = text("authserv_id", "Mail server's authserv-id (Enter to skip)", a.authserv_id)
        _refuse_on(a, "authserv_id")

    # Calendar, where there is no Nextcloud to derive it from.
    if not a.uses_nextcloud:
        caldav = flag("caldav_url")
        if caldav is None and interactive:
            if _ask_yes_no(input_fn, "Point calendar at a CalDAV server?", bool(a.caldav_url), out=out):
                caldav = _ask(input_fn, "CalDAV URL", a.caldav_url)
            else:
                caldav = ""
        if caldav is None:
            caldav = a.caldav_url
        a.caldav_url = (caldav or "").strip()
        if a.caldav_url:
            a.caldav_username = text("caldav_username", "CalDAV username", a.caldav_username)
            a.caldav_password = secret("istota_caldav_password", "CalDAV password", a.caldav_password)
            if not a.caldav_password:
                out("  No CalDAV password; leaving [caldav] out.")
                a.caldav_url = a.caldav_username = ""

    # Modules.
    if flag("location", False):
        a.location_enabled = True
    elif interactive:
        a.location_enabled = _ask_yes_no(
            input_fn, "Enable GPS/location tracking?", a.location_enabled, out=out,
        )
    _collect_modules(a, args, interactive=interactive and not kept_user, input_fn=input_fn, out=out)
    if flag("developer", False):
        a.developer_enabled = True
    elif interactive:
        a.developer_enabled = _ask_yes_no(
            input_fn, "Enable the developer skill?", a.developer_enabled, out=out,
        )
    if a.developer_enabled:
        a.gitlab_token = secret("istota_developer_gitlab_token", "GitLab token", a.gitlab_token)
        a.github_token = secret("istota_developer_github_token", "GitHub token", a.github_token)

    # Compose profiles.
    profiles = list(flag("profile", []) or [])
    if not profiles and interactive:
        kept = set(a.profiles)
        if _ask_yes_no(
            input_fn, "Run the browser container?",
            "browser" in kept if prior is not None else True, out=out,
        ):
            profiles.append("browser")
        if a.uses_nextcloud and a.talk_enabled and _ask_yes_no(
            input_fn, "Run the Talk signaling server?", "signaling" in kept, out=out,
        ):
            profiles.append("signaling")
        if _ask_yes_no(
            input_fn, "Run the WhatsApp (Baileys) sidecar?", "whatsapp-baileys" in kept, out=out,
        ):
            profiles.append("whatsapp-baileys")
    elif not profiles:
        profiles = list(a.profiles)
    a.profiles = tuple(profiles)
    _refuse_on(a)
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

    prior = None
    try:
        prior = prior_answers(data_dir, vm_dir)
    except PriorConfigUnreadable as exc:
        out(f"  {exc}; starting from first-run defaults.")
    a = collect_container_answers(
        args, input_fn=input_fn, out=out, getpass_fn=getpass_fn,
        current_storage=current_storage(vm_dir), prior=prior,
    )
    write_container_setup(a, data_dir=data_dir, vm_dir=vm_dir, out=out, backup=RERUN_CONFIG_BACKUPS)

    out("")
    out("Setup complete.")
    out(f"  Config:  {config_path} (yours to edit; nothing regenerates it)")
    out(f"  Admins:  {config_path.parent / 'admins'}")
    if vm_dir is not None:
        out(f"  Stack:   {vm_dir / '.env'}, {vm_dir / 'host.env'}, {secrets_dir}/")
    out(f"  First admin: {a.user_id}")
    return 0


def _stage_rclone_conf(a: ContainerAnswers, vm_dir: Path, secrets_dir: Path, out) -> None:
    """``rclone.conf`` for the VM's Nextcloud mount, staged for ``istota-stack``
    to install. The password is this run's answer, else the one a previous run
    left in the secrets file, which the loop above kept.

    ``istota-stack`` copies the installed file into the staging directory, and
    one that already says the same thing is kept byte for byte: the obscured
    password is salted, and ``provision.sh --digest`` hashes the file (#720)."""
    password = a.nextcloud_app_password or _read_secret_file(secrets_dir / "istota_nextcloud_app_password")
    if not password:
        out("  No Nextcloud app password, so no rclone.conf was written; the mount needs one.")
        return
    path = vm_dir / "rclone.conf"
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing = None
    try:
        text = webdav_remote_for(
            existing, RCLONE_REMOTE, a.nextcloud_url, a.nextcloud_username, password,
            dav_prefix=a.nextcloud_dav_prefix,
        )
    except ValueError as exc:
        raise SetupError(f"The Nextcloud answers cannot go into rclone.conf: {exc}.") from None
    _write_private(path, text)


def write_container_setup(
    a: ContainerAnswers, *, data_dir: Path, vm_dir: Path | None, out, backup: int | None = None,
) -> None:
    """Write everything a set of answers decides, for either front end.

    ``config.toml``, ``admins``, ``managed.json`` and the master key under ``data_dir``; with a
    ``vm_dir``, the credential files and the staged ``.env`` and ``host.env``,
    plus ``rclone.conf`` when the workspace is the Nextcloud mount.
    Validation is the caller's: this writes what it is given.

    ``backup`` set, a config.toml already there is kept as
    ``config.toml.bak.<stamp>`` first, the newest ``backup`` of them kept.
    """
    config_path = data_dir / "config" / "config.toml"
    secrets_dir = vm_dir / "secrets" if vm_dir is not None else None
    _carry_forward_session_secret(a, config_path, secrets_dir)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    inline = secrets_dir is None
    from istota import apply

    text = render_container_config(a, inline_credentials=inline)
    if backup and config_path.exists():
        apply.write_config_with_backup(config_path, text, backup)
    else:
        _write_private(config_path, text)
    _ensure_admins_file(config_path.parent / "admins", a.user_id, out=out)
    # Setup hands every resource to whoever edits next; a management plan
    # claims its own back on its next run.

    apply.write_managed(config_path.parent, {name: (False, "setup") for name in apply.MANAGED_RESOURCES})
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
        if a.workspace_is_mount:
            _stage_rclone_conf(a, vm_dir, secrets_dir, out)
    elif a.brain_kind != "native" and (a.claude_code_oauth_token or a.anthropic_api_key):
        out(
            "  The Claude credential has no config key; without --vm-dir it was not "
            "written. Put it in the stack's secrets/ directory."
        )
