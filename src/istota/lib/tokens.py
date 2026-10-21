"""Bearer tokens that are stored only as a hash: the one shape and the one hash.

Shared by `location.ingest_tokens` and `webui.device_tokens`, so the two kinds of
device credential cannot drift in how they are minted or stored. A token is
`secrets.token_urlsafe(32)`, 256 bits of randomness, so an unsalted SHA-256 is
enough and lets a presented token be looked up by its hash. `token_tail` is the
last four characters, kept in the clear so a list can tell rows apart.
`bearer_token` reads one out of an `Authorization` header.
"""

import hashlib
import secrets

TAIL_CHARS = 4


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str, str]:
    """A fresh `(plaintext, token_hash, token_tail)`. The plaintext is never stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token), token[-TAIL_CHARS:]


def bearer_token(authorization: str) -> str | None:
    """The credential in an `Authorization: Bearer` value, or None for any other scheme.

    A Bearer header with nothing after it is `""`, not None: the request
    chose the bearer path, and an empty credential has to be refused there
    rather than fall back to whatever else the request carries.
    """
    scheme, _, credential = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credential.strip()
