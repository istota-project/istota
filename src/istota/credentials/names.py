"""Credential names, password generation and store isolation."""
from __future__ import annotations
import logging
import re
import secrets
import string
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from istota.credentials import store as secrets_store
logger = logging.getLogger(__name__)

VAULT_MAX_VALUE_BYTES = 8192


VAULT_NAME_MAX_CHARS = 64


VAULT_NAME_RE = re.compile(rf"\A[a-z][a-z0-9_]{{0,{VAULT_NAME_MAX_CHARS - 1}}}\Z")


_SLUG_DROP_RE = re.compile(r"[^a-z0-9]+")


_LABEL_MAX_CHARS = 64


VAULT_ENTRY_SERVICE = "vault_entries"


class VaultError(Exception):
    """Credential validation or file-read failure."""


class VaultWriteRefused(VaultError):
    """The requested credential name or password policy is unusable."""


@dataclass(frozen=True)
class PasswordPolicy:
    length: int = 24
    require_lower: bool = True
    require_upper: bool = True
    require_digits: bool = True
    require_symbols: bool = True
    allow_lower: bool = True
    allow_upper: bool = True
    allow_digits: bool = True
    allow_symbols: bool = True


def generate_password(policy: PasswordPolicy = PasswordPolicy()) -> str:
    """Generate a password with every required class, or refuse the policy."""
    classes = (
        (string.ascii_lowercase, policy.require_lower, policy.allow_lower),
        (string.ascii_uppercase, policy.require_upper, policy.allow_upper),
        (string.digits, policy.require_digits, policy.allow_digits),
        ("!@#$%^&*-_=+?", policy.require_symbols, policy.allow_symbols),
    )
    if policy.length < 1 or policy.length > VAULT_MAX_VALUE_BYTES:
        raise VaultWriteRefused("password length is outside the allowed range")
    required = []
    alphabet = ""
    for characters, need, allowed in classes:
        if need and not allowed:
            raise VaultWriteRefused("password policy requires a forbidden class")
        if allowed:
            alphabet += characters
        if need:
            required.append(secrets.choice(characters))
    if not alphabet or len(required) > policy.length:
        raise VaultWriteRefused("password policy cannot fit the requested length")
    result = required + [secrets.choice(alphabet) for _ in range(policy.length - len(required))]
    secrets.SystemRandom().shuffle(result)
    return "".join(result)


def generated_entry_names(slug: str) -> tuple[str | None, str | None, str | None]:
    """The password, username and URL names a generated credential for ``slug`` has."""
    return (
        slug_name(("generated", slug)),
        slug_name(("generated", slug, "username")),
        slug_name(("generated", slug, "url")),
    )


def slug_name(segments: Sequence[str]) -> str | None:
    """The one name these path segments produce, or ``None`` for none."""
    parts: list[str] = []
    for segment in segments:
        slug = _SLUG_DROP_RE.sub("_", str(segment).casefold()).strip("_")
        if not slug:
            return None
        parts.append(slug)
    if not parts:
        return None
    name = "_".join(parts)
    return name if VAULT_NAME_RE.fullmatch(name) else None


def _label(name: str, limit: int = _LABEL_MAX_CHARS) -> str:
    """A name out of the vault file, bounded and flattened, for a log line."""
    head = "".join(ch if ch.isprintable() else " " for ch in str(name)[:limit])
    return head + ("…" if len(str(name)) > limit else "")


def label_for_display(name: str) -> str:
    """One name out of the vault file, bounded and flattened for a human."""
    return _label(name)


VAULT_ISOLATION_REASON = (
    "vaults are disabled on a multi-user deployment without effective sandboxing; "
    "same-uid tasks can access another user's credentials. Enable a working "
    "sandbox, or have the operator accept this exposure with [security] "
    "allow_unsandboxed_multi_user_vaults = true and restart the services"
)


class VaultIsolationRequired(VaultError):
    """The operator has not accepted unsandboxed multi-user vault access."""


def vault_has_other_users(config, user_id: str = "") -> bool:
    """Count task users, including a not-yet-configured CLI recipient."""
    users = set(config.users)
    if user_id:
        users.add(user_id)
    return len(users) > 1


def vault_isolation_refusal(config, user_id: str) -> str | None:
    """Gate the whole credential store on deployment isolation."""
    if not vault_has_other_users(config, user_id):
        return None
    if config.security.allow_unsandboxed_multi_user_vaults is True:
        return None
    from istota.executor import effective_sandboxing

    if effective_sandboxing(config):
        return None
    return VAULT_ISOLATION_REASON


def has_shared_credentials(db_path, user_id: str) -> bool:
    """Whether this user has anything in the shared-credential namespace."""
    if not db_path or not secrets_store.secret_key_available():
        return False
    # Opening creates a missing file, and a dry run with a bare Config()
    # reaches here with the relative default path (ISSUE-571).
    if not Path(db_path).is_file():
        return False
    try:
        return bool(secrets_store.list_user_services(Path(db_path), user_id).get(VAULT_ENTRY_SERVICE))
    except Exception as exc:
        logger.debug("vault: could not count shared credentials: %s", exc)
        return False
