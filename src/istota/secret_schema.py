"""Schema registry for the encrypted ``secrets`` table.

A "service" groups one or more (key, value) credentials a user has
configured. The web settings UI and the ``istota secret`` CLI both
validate writes against this registry — anything not declared here is
rejected so a typo doesn't silently land an orphan row in the DB.

There are two flavors:

* **Connected services** — cross-cutting per-user credentials a skill
  consumes (Karakeep, Google Workspace). They appear on the main
  ``/istota/settings`` page.
* **Module services** — credentials owned by a specific module (Monarch
  for ``money``, Tumblr for ``feeds``, Overland for ``location``). They
  appear on the matching per-module settings page and are gated by
  ``Config.is_module_enabled``.

The dicts below carry UI-only metadata (``label``, ``fields[].type``)
alongside structural info (``fields[].key``, ``used_by``). Pure-data
consumers (CLI validation, secrets-store import) only care about the
structural bits.
"""

from __future__ import annotations


# Cross-cutting per-user credentials. Each ``fields`` entry's ``key`` is
# the secret key under that service.
CONNECTED_SERVICE_SCHEMA: dict[str, dict] = {
    "karakeep": {
        "label": "Karakeep",
        "used_by": ("bookmarks",),
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "url"},
            {"key": "api_key",  "label": "API key",  "type": "password"},
        ],
    },
    "google_workspace": {
        "label": "Google Workspace",
        "used_by": ("google_workspace",),
        # OAuth flow lives at /istota/google/connect — the UI shows a
        # Connect button instead of writable fields when this is set.
        "oauth": True,
        "fields": [],
    },
}

# Module-owned credential blocks. Outer dict key = module name (must be in
# ``istota.modules.MODULE_NAMES``); inner dict mirrors
# ``CONNECTED_SERVICE_SCHEMA``.
MODULE_SERVICE_SCHEMA: dict[str, dict[str, dict]] = {
    "feeds": {
        "feeds": {
            "label": "Feeds (Tumblr)",
            "used_by": ("feeds",),
            "fields": [
                {"key": "tumblr_api_key", "label": "Tumblr API key (optional)",
                 "type": "password"},
            ],
        },
    },
    "money": {
        "monarch": {
            "label": "Monarch Money",
            "used_by": ("money",),
            "fields": [
                {"key": "email",         "label": "Email",                     "type": "email"},
                {"key": "password",      "label": "Password",                  "type": "password"},
                {"key": "session_token", "label": "Session token (optional)",  "type": "password"},
            ],
        },
    },
    "location": {
        "overland": {
            "label": "Overland GPS",
            "used_by": ("location",),
            "fields": [
                {"key": "ingest_token", "label": "Ingest token", "type": "password"},
            ],
        },
    },
}


def all_known_services() -> dict[str, dict]:
    """Union of connected + module-owned service schemas.

    Module-service schemas have no resource_types: they're gated by
    ``Config.is_module_enabled`` at request time, not by the schema.
    """
    out: dict[str, dict] = dict(CONNECTED_SERVICE_SCHEMA)
    for mod_services in MODULE_SERVICE_SCHEMA.values():
        for service, schema in mod_services.items():
            out[service] = schema
    return out


def known_service_keys() -> dict[str, frozenset[str]]:
    """Flat ``{service: {key, ...}}`` mapping for fast validation.

    The CLI uses this to reject typos before they reach the DB. Empty
    values (``"fields": []``) — e.g. OAuth-only services — yield an empty
    frozenset, which signals "no operator-writable keys".
    """
    return {
        service: frozenset(f["key"] for f in schema.get("fields", []))
        for service, schema in all_known_services().items()
    }
