"""A URL safe to print, with any user name or password taken out.

stdlib-only leaf, so a skill CLI can use it without importing `istota.doctor`.
"""

from urllib.parse import urlsplit, urlunsplit

REDACTED = "[redacted]"


def redact_userinfo(url: str, *, remove: bool = False) -> str:
    """A URL safe to print: userinfo replaced, everything else intact.

    A configured URL is not supposed to carry credentials, but
    `https://user:token@host` parses fine and this string goes straight into a
    report that reaches the admin dashboard and the log.

    **Replaced, not removed.** Deleting the userinfo renders
    `https://bot:token@host` as `https://host`, and an operator reading that
    cannot tell the configured value carried a credential at all — which is the
    most useful thing the report could have told them, and the thing they need
    in order to know something wants rotating.

    ``remove=True`` deletes it instead, for a surface that shows the address
    and says separately that the value is wrong (the admin Connections card).

    A password holding an unencoded ``#``, ``?`` or ``/`` ends the netloc
    early, so the credential lands in the path, query or fragment. An ``@``
    anywhere past the netloc is treated as that case: nothing of the URL is
    returned.
    """
    unparseable = "" if remove else "(unparseable)"
    try:
        parts = urlsplit(url)
    except ValueError:
        return unparseable
    if "@" in (parts.path + parts.query + parts.fragment):
        return unparseable
    if "@" not in (parts.netloc or ""):
        return url
    _, _, hostport = parts.netloc.rpartition("@")
    netloc = hostport if remove else f"{REDACTED}@{hostport}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
