"""Experimental feature flags.

Operator-scoped, off by default. Operators flip features on per-instance
via the ``[experimental] features = [...]`` block in ``config.toml``
(deployed by Ansible). Not exposed in the web UI, not toggleable per
user, not advertised in onboarding.

This module owns three things:

1. ``KNOWN_FEATURES`` — the registry of every gateable flag with a
   one-line description. The single source of truth for what's
   gateable; ``load_config`` warns on unknown names so typos surface
   without breaking startup.
2. ``requires_feature`` — a Click decorator for gating CLI subcommands.
   Reads the active feature set from ``ISTOTA_EXPERIMENTAL_FEATURES``
   (CSV) which the executor propagates to every skill subprocess. On
   miss, prints the same ``{"status":"error","error":"..."}`` envelope
   the scheduler's command-task path already recognises and exits 1.
3. ``enabled_features_from_env`` — the env reader, exposed for skill
   subprocesses and tests.

Naming convention for flag names:

* ``module_<name>`` — module-level gate (e.g. ``module_foo``)
* ``skill_<name>`` — skill-level gate (e.g. ``skill_foo``)
* free-form — CLI subcommand gates (e.g. ``money_tax``)
"""

from __future__ import annotations

import json
import os
import sys
from functools import wraps
from typing import Callable


KNOWN_FEATURES: dict[str, str] = {
    "money_tax": "Money: tax-lot commands (lots)",
    "money_wash_sales": "Money: IRS wash-sale violation detector",
}


_ENV_VAR = "ISTOTA_EXPERIMENTAL_FEATURES"


def enabled_features_from_env() -> frozenset[str]:
    """Read the active feature set from the propagated env var.

    The executor sets ``ISTOTA_EXPERIMENTAL_FEATURES`` to a CSV of
    feature names on every skill subprocess and command-task. Returns
    an empty frozenset if the var is unset or empty.
    """
    raw = os.environ.get(_ENV_VAR, "")
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def requires_feature(name: str) -> Callable:
    """Click decorator: gate a CLI subcommand behind an experimental flag.

    Off path emits a ``{"status":"error","error":"..."}`` envelope on
    stdout and exits 1 so the scheduler's command-task error detection
    surfaces the failure cleanly. On path is a passthrough.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if name not in enabled_features_from_env():
                msg = (
                    f"feature {name!r} is experimental and not enabled on this instance"
                )
                print(json.dumps({"status": "error", "error": msg}))
                sys.exit(1)
            return func(*args, **kwargs)

        return wrapper

    return decorator
