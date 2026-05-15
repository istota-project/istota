"""End-to-end probe for the Monarch programmatic-login flow.

Run this against the live api.monarch.com to verify two things:

1. Does ``MonarchClient.login_with_credentials`` actually work from this
   host, or is Cloudflare in the way? (Server-side IPs frequently get
   challenged; the cookie-paste workflow is the always-works fallback.)
2. If login succeeds, do the captured cookies actually authenticate against
   /graphql? (They should — the production code path uses them the same way.)

Usage::

    MM_EMAIL=stefan@example.com MM_PASSWORD='hunter2' \
        uv run python scripts/probe_monarch_login.py

If your account has TOTP MFA enabled, supply the *current* 6-digit code via
``MM_MFA_TOTP``. We don't take the secret — generate the code yourself
(authenticator app or ``oathtool --totp -b 'YOURSECRET'``).

The script prints structured output and exits non-zero on any failure so it
slots cleanly into a heartbeat or smoke test if we ever want to schedule it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from istota.money._vendor.monarch_client import (
    MonarchAuthError,
    MonarchClient,
    MonarchCloudflareBlocked,
    MonarchCookieAuth,
    MonarchMFARequired,
)


def _emit(label: str, payload: dict) -> None:
    print(f"--- {label} ---")
    print(json.dumps(payload, indent=2, sort_keys=True))


async def _probe() -> int:
    email = os.environ.get("MM_EMAIL", "").strip()
    password = os.environ.get("MM_PASSWORD", "")
    mfa_totp = os.environ.get("MM_MFA_TOTP", "").strip() or None

    if not (email and password):
        print(
            "Set MM_EMAIL and MM_PASSWORD (and MM_MFA_TOTP if MFA-enabled).",
            file=sys.stderr,
        )
        return 2

    _emit("input", {
        "email": email,
        "password_present": True,
        "mfa_totp_present": bool(mfa_totp),
    })

    # Phase 1 — login
    try:
        auth: MonarchCookieAuth = await MonarchClient.login_with_credentials(
            email=email, password=password, mfa_totp=mfa_totp,
        )
    except MonarchCloudflareBlocked as exc:
        _emit("login_result", {
            "ok": False, "kind": "cloudflare_blocked", "error": str(exc),
        })
        return 3
    except MonarchMFARequired as exc:
        _emit("login_result", {
            "ok": False, "kind": "mfa_required", "error": str(exc),
            "hint": "Re-run with MM_MFA_TOTP set to the current 6-digit code.",
        })
        return 4
    except MonarchAuthError as exc:
        _emit("login_result", {
            "ok": False, "kind": "auth_error", "error": str(exc),
        })
        return 5

    # Print *truncated* cookie values — operators want to confirm something
    # came back without leaking the full credential.
    _emit("login_result", {
        "ok": True,
        "session_id_prefix": auth.session_id[:8] + "…",
        "csrftoken_prefix": auth.csrftoken[:8] + "…",
        "session_id_len": len(auth.session_id),
        "csrftoken_len": len(auth.csrftoken),
    })

    # Phase 2 — verify cookies authenticate /graphql
    client = MonarchClient(auth)
    try:
        me = await client.whoami()
    except Exception as exc:  # noqa: BLE001
        _emit("verify_result", {
            "ok": False, "error": f"{type(exc).__name__}: {exc}",
        })
        return 6

    _emit("verify_result", {"ok": True, "me": me})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_probe()))
