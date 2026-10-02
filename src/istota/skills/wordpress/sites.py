"""Which WordPress site a call is about, and where the request may go.

Three things, each a boundary the rest of the skill leans on:

- **The site.** A site is a vault entry named ``wordpress_<name>``, and
  ``--site NAME`` names it. There is no site file: the entry holds the URL, the
  login and the application password, and is read whole in one fetch. With no
  ``--site``, the user's only ``wordpress_*`` entry is used; the list comes
  from the proxy's ``vault_list``, which returns names and no values and is not
  charged to the fetch budget.
- **The bound-host check.** A request's authority must be one of the vault
  entry's ``bound_hosts``, computed by the credential broker's own
  `credential_host` so the two cannot disagree about what an authority is. An
  entry with no binding sends nothing at all.
- **``--blog``.** A multisite network is one entry; a slug addresses a
  subdirectory site under the entry's URL and a host addresses a subdomain one,
  which then has to be bound as well. Whether the install is a network is not
  recorded anywhere: a ``--blog`` the site does not have is refused by the
  index check before anything acts on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from istota.credential_broker.bindings import credential_host

#: A site's vault entry is this prefix plus the site name.
ENTRY_PREFIX = "wordpress_"

#: A site name is the ``--site`` value and the tail of its entry name, so it is
#: held to the vault's name rule with room for the prefix.
NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,53}\Z")

#: A subdirectory blog slug, as WordPress allows them in a network path.
BLOG_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,62}\Z")

_ENTRY_HINT = (
    "Add a vault entry named wordpress_<name> in the istota group, with the "
    "site's address in its URL field, the WordPress login as its username and "
    "an application password as its password."
)


class SiteError(Exception):
    """A refusal about the site or its address, with the reason code it maps to."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SiteRecord:
    name: str
    credential: str

    def as_dict(self) -> dict:
        return {"name": self.name, "credential": self.credential}


def site_names(entries) -> list[str]:
    """The site names the vault holds: every ``wordpress_<name>`` entry, sorted."""
    names = set()
    for entry in entries or ():
        if isinstance(entry, str) and entry.startswith(ENTRY_PREFIX):
            name = entry[len(ENTRY_PREFIX):]
            if NAME_RE.fullmatch(name):
                names.add(name)
    return sorted(names)


def site_for(name: str) -> SiteRecord:
    """The site ``--site NAME`` names. Checks the shape only; no vault read."""
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise SiteError(
            f"--site {name!r} is not a site name: lowercase letters, digits and "
            f"underscores, starting with a letter.",
            "unknown_site",
        )
    return SiteRecord(name, ENTRY_PREFIX + name)


def select_site(entries, name: str | None) -> SiteRecord:
    """The site ``--site`` names, or the only one the vault holds.

    `entries` is the vault's entry names; it is consulted only when ``--site``
    is absent. A named site with no entry behind it is refused by the entry
    fetch itself.
    """
    if name:
        return site_for(name)
    names = site_names(entries)
    if len(names) == 1:
        return site_for(names[0])
    if not names:
        raise SiteError(f"No WordPress site is set up. {_ENTRY_HINT}", "unknown_site")
    raise SiteError(
        f"Several WordPress sites are set up; pass --site ({', '.join(names)}).",
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


def check_blog(blog: str | None) -> str | None:
    """``--blog`` normalised and checked for shape, before any vault fetch is spent.

    A value with a dot is a subdomain network's host, anything else a
    subdirectory network's slug. A host still has to pass the bound-host check
    on every request; this only refuses what cannot be one.
    """
    if blog is None:
        return None
    blog = blog.strip().lower()
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
