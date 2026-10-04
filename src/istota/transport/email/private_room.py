"""The user's private email room: mail between the user and the bot alone.

Email owns one room per user the way SMS and WhatsApp do (email on rooms,
section 7). Its binding is ``surface='email'`` with ``surface_ref`` the
per-user token below, which is `sms_conversation_token`'s form. A thread
room is bound under its root Message-ID instead, so the two are told apart by
the ref: an email binding is the private room exactly when its ref is the
room creator's own token (`is_private_email_ref`), the rule
`routing.phone_room` applies to a phone binding.

A leaf with no package imports, so `db`, `rooms.scopes` and `routing` can ask
it without importing the email transport.
"""

from __future__ import annotations

import hashlib


def email_conversation_token(user_id: str) -> str:
    """The ``surface_ref`` of ``user_id``'s private email room."""
    digest = hashlib.sha256(f"istota-email-v1\0{user_id}".encode()).hexdigest()[:24]
    return "email-" + digest


def is_private_email_ref(surface_ref: object, owner_user_id: object) -> bool:
    """Whether an email binding's ref is its room creator's private room."""
    return (
        isinstance(surface_ref, str) and isinstance(owner_user_id, str)
        and bool(owner_user_id)
        and surface_ref == email_conversation_token(owner_user_id)
    )


__all__ = ["email_conversation_token", "is_private_email_ref"]
