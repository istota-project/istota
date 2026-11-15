"""A URL safe to print, with any user name or password taken out.

stdlib-only leaf, so a skill CLI can use it without importing `istota.doctor`,
and `istota-stack logs` can run it with the VM host's python3 (#968).
"""

import re
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[redacted]"
UNPARSEABLE = "(unparseable)"

# A web address in running text runs to the next whitespace: stopping at a
# quote or a bracket would end a password holding one early and print the rest.
# No word boundary before the scheme, so `label_https://` still matches, and the
# scheme is bounded, so a long run of scheme characters costs linear time.
_WEB_ADDRESS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31})://\S+")
_WORD = re.compile(r"\S+")
# What may stand before the `/` that starts a request target in a word.
_TARGET_OPENERS = "\"'([=:,"


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
    unparseable = "" if remove else UNPARSEABLE
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


def _redact_address(match: re.Match) -> str:
    url = match.group(0)
    bare = f"{match.group(1)}://{REDACTED}"
    # Asked of the whole address first: an `@` past the netloc is a password
    # that ended it early, which the address without its query no longer shows.
    if redact_userinfo(url) == UNPARSEABLE:
        return bare
    # Split before the userinfo goes: `[redacted]@host` does not parse again.
    parts = urlsplit(url)
    head = redact_userinfo(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
    query = f"?{REDACTED}" if parts.query else ""
    fragment = f"#{REDACTED}" if parts.fragment else ""
    return f"{head}{query}{fragment}"


def _redact_target(match: re.Match) -> str:
    """A word holding a request target (`/path?query`, at the word's start or
    after one of `_TARGET_OPENERS`) with its query or fragment replaced. One
    scan of the word, where a regex would rescan it from every `/`."""
    word = match.group(0)
    cut = min((i for i in (word.find("?"), word.find("#")) if i >= 0), default=-1)
    if cut < 0:
        return word
    for i in range(cut):
        if word[i] == "/" and (i == 0 or word[i - 1] in _TARGET_OPENERS):
            return f"{word[:cut + 1]}{REDACTED}"
    return word


def redact_web_addresses(text: str) -> str:
    """`text` with every web address in it safe to hand to someone.

    Userinfo goes through `redact_userinfo`, and an address it cannot vouch for
    is reduced to its scheme. The query and the fragment are replaced whatever
    they hold, since that is where a link carries its one-use token (the setup
    code, a set-password or enrolment link), and so is the query or fragment
    of a request target an access log writes without a scheme.
    """
    text = _WEB_ADDRESS.sub(_redact_address, text)
    return _WORD.sub(_redact_target, text)
