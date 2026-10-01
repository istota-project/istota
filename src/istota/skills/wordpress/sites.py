"""Which WordPress site a call is about, and where the request may go.

Three things, each a boundary the rest of the skill leans on:

- **The site record.** `config/WORDPRESS.md` holds one ``[[sites]]`` table per
  install in a TOML fence: ``name`` (the ``--site`` value), ``credential`` (the
  vault entry; ``wordpress_<name>`` by default), ``multisite`` and
  ``default``. Nothing secret, and since ISSUE-583 nothing about *where* either:
  the site's URL and login come from the vault entry itself, read whole in one
  fetch. The file is user-written and model-editable, which is acceptable only
  because of the next point.
- **The bound-host check.** A request's authority must be one of the vault
  entry's ``bound_hosts``, computed by the credential broker's own
  `credential_host` so the two cannot disagree about what an authority is. An
  entry with no binding sends nothing at all.
- **``--blog``.** A multisite network is one record and one credential; a slug
  addresses a subdirectory site under the record's URL and a host addresses a
  subdomain one, which then has to be bound as well.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from urllib.parse import urlsplit

from istota.credential_broker.bindings import credential_host
from istota.secrets_vault import VAULT_NAME_RE
from istota.toml_fence import find_toml_block

SITES_FILE = "WORDPRESS.md"

#: A site name is the ``--site`` value and becomes part of the default
#: credential name, so it is held to the vault's name rule with room for the
#: ``wordpress_`` prefix.
NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,53}\Z")

#: A subdirectory blog slug, as WordPress allows them in a network path.
BLOG_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,62}\Z")

_KNOWN_KEYS = frozenset({"name", "credential", "multisite", "default"})
_SITES_HEADER_RE = re.compile(r"^[ \t]*\[\[[ \t]*sites[ \t]*\]\]", re.MULTILINE)
_TOML_LINE_RE = re.compile(r"at line (\d+)")


class SiteError(Exception):
    """A refusal about the site or its address, with the reason code it maps to."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SiteRecord:
    name: str
    credential: str
    multisite: bool = False
    default: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "credential": self.credential,
            "multisite": self.multisite,
            "default": self.default,
        }


def _block_line_offset(text: str, start: int) -> int:
    """How many lines precede the TOML body, so a body line becomes a file line."""
    return text.count("\n", 0, start)


def parse_sites(text: str) -> tuple[list[SiteRecord], list[str]]:
    """The records in a WORDPRESS.md, and every problem found, with file lines.

    A record with a problem is left out and named rather than guessed at, and
    the rest still load, so one typo does not take every site down with it.
    An empty or missing file is no records and no errors.
    """
    if not text or not text.strip():
        return [], []
    span = find_toml_block(text)
    if span is None:
        return [], [f"{SITES_FILE} has no closed ```toml block"]
    start, end = span
    body = text[start:end]
    offset = _block_line_offset(text, start)
    try:
        data = tomllib.loads(body)
    except tomllib.TOMLDecodeError as exc:
        message = str(exc)
        match = _TOML_LINE_RE.search(message)
        if match:
            line = int(match.group(1)) + offset
            message = _TOML_LINE_RE.sub(f"at line {line}", message, count=1)
        return [], [f"{SITES_FILE}: {message}"]

    unknown_top = sorted(set(data) - {"sites"})
    errors = [f"{SITES_FILE}: unknown top-level key {key!r}" for key in unknown_top]
    tables = data.get("sites", [])
    if not isinstance(tables, list):
        return [], errors + [f"{SITES_FILE}: `sites` must be [[sites]] tables"]

    header_lines = [
        body.count("\n", 0, m.start()) + 1 + offset
        for m in _SITES_HEADER_RE.finditer(body)
    ]
    records: list[SiteRecord] = []
    seen: set[str] = set()
    for index, table in enumerate(tables):
        line = header_lines[index] if index < len(header_lines) else None
        where = f"{SITES_FILE} line {line}" if line else f"{SITES_FILE} site #{index + 1}"
        record, problem = _record(table, seen)
        if problem:
            errors.append(f"{where}: {problem}")
            continue
        seen.add(record.name)
        records.append(record)

    defaults = [r.name for r in records if r.default]
    if len(defaults) > 1:
        errors.append(
            f"{SITES_FILE}: more than one site has default = true ({', '.join(defaults)}); "
            f"none is used as the default"
        )
        records = [
            SiteRecord(r.name, r.credential, r.multisite, False) for r in records
        ]
    return records, errors


def _record(table: object, seen: set[str]) -> tuple[SiteRecord | None, str | None]:
    if not isinstance(table, dict):
        return None, "not a table"
    unknown = sorted(set(table) - _KNOWN_KEYS)
    if unknown:
        hint = ""
        if {"url", "username"} & set(unknown):
            hint = " (the site's URL and login come from its vault entry)"
        return None, f"unknown key(s) {', '.join(unknown)}{hint}"
    name = table.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None, "name must be lowercase letters, digits and underscores, starting with a letter"
    if name in seen:
        return None, f"duplicate site name {name!r}"
    credential = table.get("credential", f"wordpress_{name}")
    if not isinstance(credential, str) or not VAULT_NAME_RE.fullmatch(credential):
        return None, f"credential {credential!r} is not a vault entry name"
    multisite = table.get("multisite", False)
    default = table.get("default", False)
    if not isinstance(multisite, bool) or not isinstance(default, bool):
        return None, "multisite and default must be true or false"
    return SiteRecord(name, credential, multisite, default), None


def select_site(records: list[SiteRecord], name: str | None) -> SiteRecord:
    """The record ``--site`` names, or the default, or the only one."""
    if not records:
        raise SiteError(
            f"No WordPress sites are configured. Add a [[sites]] table to "
            f"config/{SITES_FILE}.",
            "unknown_site",
        )
    if name:
        for record in records:
            if record.name == name:
                return record
        known = ", ".join(r.name for r in records)
        raise SiteError(f"No site named {name!r} in {SITES_FILE} (known: {known}).",
                        "unknown_site")
    defaults = [r for r in records if r.default]
    if defaults:
        return defaults[0]
    if len(records) == 1:
        return records[0]
    raise SiteError(
        f"Several sites are configured and none is the default; pass --site "
        f"({', '.join(r.name for r in records)}).",
        "unknown_site",
    )


def normalize_site_url(url: str) -> str:
    """The vault entry's URL as a base: HTTPS, no credentials, query or fragment.

    The trailing slash goes so a path can be appended; a WordPress in a
    subdirectory keeps its path.
    """
    url = url.strip()
    # The broker reads a bare authority in the URL field as https (it binds
    # one that way), so the site URL does too.
    if "://" not in url and not any(c in url for c in "/?#@"):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SiteError(
            "The site URL must be HTTPS. WordPress itself disables application "
            "passwords over plain HTTP outside a local environment.",
            "host_refused",
        )
    if not parts.hostname or parts.username or parts.password:
        raise SiteError("The site URL has no host, or carries user information.",
                        "host_refused")
    if parts.query or parts.fragment:
        raise SiteError("The site URL may not carry a query or fragment.", "host_refused")
    return f"https://{parts.netloc}{parts.path.rstrip('/')}"


def check_bound(url: str, bound_hosts) -> str:
    """The request's authority, if the credential is bound to it. Raises otherwise.

    Uses the broker's `credential_host`, the function that produced the
    binding, so an authority means the same thing on both sides (an explicit
    default port collapses, a non-default one is kept, a name is lowercased).
    HTTP is refused outright here; see `normalize_site_url`.
    """
    hosts = tuple(bound_hosts or ())
    if not hosts:
        raise SiteError(
            "The vault entry is bound to no host, so its password goes nowhere. "
            "Give the entry the site's URL in its URL field.",
            "credential_unbound",
        )
    if urlsplit(url).scheme != "https":
        raise SiteError("Requests go over HTTPS only.", "host_refused")
    try:
        authority = credential_host(url)
    except ValueError as exc:
        raise SiteError(f"Not a usable site address: {exc}", "host_refused") from None
    if authority not in hosts:
        raise SiteError(
            f"{authority} is not a host the vault entry is bound to "
            f"({', '.join(hosts)}). Nothing was sent.",
            "credential_host_mismatch",
        )
    return authority


def check_blog(blog: str | None, multisite: bool) -> str | None:
    """``--blog`` normalised and checked for shape, before any vault fetch is spent.

    A value with a dot is a subdomain network's host, anything else a
    subdirectory network's slug. A host still has to pass the bound-host check
    on every request; this only refuses what cannot be one.
    """
    if blog is None:
        return None
    blog = blog.strip().lower()
    if not multisite:
        raise SiteError(
            "--blog applies only to a site with multisite = true in its record.",
            "unknown_blog",
        )
    if "." in blog:
        if blog.startswith(".") or any(c in blog for c in "/:@?#\\ "):
            raise SiteError(f"--blog {blog!r} is not a host name.", "unknown_blog")
        return blog
    if not BLOG_SLUG_RE.fullmatch(blog):
        raise SiteError(f"--blog {blog!r} is not a site slug.", "unknown_blog")
    return blog


def blog_base(site_url: str, blog: str | None) -> str:
    """The base URL for one site of a network: ``{url}/{slug}`` or ``https://{host}``.

    `blog` is what `check_blog` returned.
    """
    if blog is None:
        return site_url
    if "." in blog:
        return f"https://{blog}"
    return f"{site_url}/{blog}"


def same_site(reported: object, base: str) -> bool:
    """Whether the URL a site's index reports is the base the client built."""
    if not isinstance(reported, str) or not reported:
        return False

    def norm(value: str) -> tuple[str, str, str]:
        parts = urlsplit(value.strip())
        return parts.scheme.lower(), (parts.netloc or "").lower(), parts.path.rstrip("/")

    return norm(reported) == norm(base)
