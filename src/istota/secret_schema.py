"""Schema registry for the encrypted ``secrets`` table.

A "service" groups one or more (key, value) credentials a user has
configured. The web settings UI and the ``istota secret`` CLI both
validate writes against this registry — anything not declared here is
rejected so a typo doesn't silently land an orphan row in the DB.

There are two flavors:

* **Connected services** — cross-cutting per-user credentials a skill
  consumes (Karakeep, Google Workspace). They appear on the main
  ``/istota/settings`` page, unless flagged ``cli_only`` (e.g.
  ``native_brain``), in which case they stay operator-provisioned via the
  ``istota secret`` CLI and never render in the web UI.
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
    "garmin": {
        "label": "Garmin Connect",
        # Cross-module: the health module syncs daily summaries and the
        # location module imports GPS tracks, both off one shared token
        # blob. Auth routes live at /istota/api/garmin/* (module-agnostic).
        "used_by": ("health", "location"),
        # custom_ui: not a writable-fields service and not a simple OAuth
        # redirect either — it is an interactive email/password → MFA flow.
        # The settings page renders the bespoke GarminCard instead of the
        # generic ServiceCard. No operator-writable keys (the blob is
        # machine-managed), so fields is empty and CLI validation yields an
        # empty key set.
        "custom_ui": True,
        "fields": [],
    },
    "ntfy": {
        "label": "ntfy push",
        # ntfy is consumed by `notifications._send_ntfy`, which is called
        # from heartbeat alerts and scheduled-job output (`output_target=ntfy`).
        # Neither is a skill, but `used_by` shape is shared with skill-backed
        # services, so we list the dispatch surfaces for the UI hint.
        "used_by": ("heartbeat", "scheduler"),
        # One-way push channel to the user's own ntfy account/server.
        # ``topic`` is the only required field; auth is optional and only
        # needed for protected ntfy instances. Priority is hardcoded to the
        # ntfy default (3) — per-call overrides flow through the API.
        "fields": [
            # server_url defaults to https://ntfy.sh when unset — only
            # operators of self-hosted servers need to fill it in. Marking
            # it optional means topic-only users see "configured" rather
            # than "partial" in the settings UI.
            {"key": "server_url", "label": "Server URL",   "type": "url",      "optional": True},
            {"key": "topic",      "label": "Default topic", "type": "text"},
            {"key": "token",      "label": "Access token (optional)", "type": "password"},
            {"key": "username",   "label": "Username (optional)",     "type": "text"},
            {"key": "password",   "label": "Password (optional)",     "type": "password"},
        ],
    },
    "native_brain": {
        "label": "Native brain provider",
        # Consumed by the native brain (brain.kind = "native"), not a skill.
        # When set, this per-user key overlays the instance-wide
        # `[brain.native] api_key` / `ISTOTA_BRAIN_NATIVE_API_KEY`.
        #
        # cli_only: not surfaced in the web UI. The web knob overrode only the
        # key (not provider/model/base_url), so it did less than it appeared to
        # — a per-user *billing* override, not a "bring your own brain" one.
        # Rather than grow it into a full per-user brain config (which opens an
        # SSRF surface, since NativeBrain runs in-process on the daemon), we
        # keep it operator-provisioned via `istota secret ensure -s native_brain`
        # and the runtime overlay in `executor._native_with_user_key`. Still in
        # the schema so CLI validation accepts it; just hidden from the UI.
        "cli_only": True,
        "used_by": (),
        "fields": [
            {"key": "api_key", "label": "Provider API key", "type": "password"},
        ],
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
            # Monarch's API enforces Django CSRF on /graphql; the cookie pair
            # is the only credential we store. Email/password login is a
            # transient input flow handled by /api/money/monarch/login — those
            # values never persist.
            "fields": [
                {"key": "session_id", "label": "session_id cookie", "type": "password"},
                {"key": "csrftoken",  "label": "csrftoken cookie",  "type": "password"},
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
