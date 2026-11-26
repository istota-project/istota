"""Schema registry for the encrypted ``secrets`` table.

A "service" groups one or more (key, value) credentials a user has
configured. The web settings UI and the ``istota secret`` CLI both
validate writes against this registry — anything not declared here is
rejected so a typo doesn't silently land an orphan row in the DB.

There are two flavors:

* **Connected services** — cross-cutting per-user credentials a skill
  consumes (Karakeep, Google Workspace). They appear on the main
  ``/istota/settings`` page, unless flagged ``cli_only`` (e.g.
  ``provider_keys``), in which case they stay operator-provisioned via the
  ``istota secret`` CLI and never render in the web UI.
* **Module services** — credentials owned by a specific module (Monarch
  for ``money``, Tumblr for ``feeds``, Overland for ``location``). They
  appear on the matching per-module settings page and are gated by
  ``Config.is_module_enabled``.

A skill plugin's ``plugin_*`` services are a third, dynamic set: a caller
holding a skill index passes it to `all_known_services`, `known_service_keys`
or `service_accepts_key` and they are added (`plugin_service_keys`). The
``istota secret`` CLI and the settings plan do; the web settings routes do
not, so a plugin's service has no settings card in v1.

A field marked ``plain: True`` is a setting stored beside the credentials
rather than a credential (ntfy's server URL and topic). The settings UI
returns and shows its stored value; every other field stays write-only.

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
        # custom_ui: the card outgrew the generic OAuth branch (ISSUE-240).
        # It renders the granted scopes and a per-service read-only/full
        # picker bounded by the operator's ceiling, fed by its own
        # /api/google/status endpoint — the GarminCard shape.
        "custom_ui": True,
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
            # than "partial" in the settings UI. ``plain``: a setting, not a
            # secret, so the settings card shows the stored value.
            {"key": "server_url", "label": "Server URL",   "type": "url",      "optional": True, "plain": True},
            {"key": "topic",      "label": "Default topic", "type": "text",    "plain": True},
            {"key": "token",      "label": "Access token (optional)", "type": "password"},
            {"key": "username",   "label": "Username (optional)",     "type": "text"},
            {"key": "password",   "label": "Password (optional)",     "type": "password"},
        ],
    },
    "provider_keys": {
        "label": "Model provider keys",
        # A user's own key for a model provider, keyed by provider id
        # (`istota secret ensure -s provider_keys -k openrouter`). In that
        # user's tasks a request to that provider carries it in place of the
        # deployment's key (`llm.providers.with_user_key`); internal operator
        # calls never do. Replaced `native_brain`/`api_key`, which `istota
        # init` moves here once.
        #
        # cli_only: not surfaced in the web UI. Opening a per-user endpoint
        # would be an SSRF surface, so a user can hold a key for a deployment
        # provider but not add one.
        "cli_only": True,
        "used_by": (),
        # Any provider id is a key (`key_rule`); the built-ins are listed so a
        # settings plan, whose keys are a fixed set, can provision them.
        "key_rule": "provider_id",
        "fields": [
            {"key": "anthropic", "label": "Anthropic API key", "type": "password"},
            {"key": "openrouter", "label": "OpenRouter API key", "type": "password"},
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
        "carto": {
            "label": "CARTO basemap",
            "used_by": ("location",),
            # Its own service rather than another field on `overland`: it is a
            # different credential with a different lifecycle, and a second
            # field there would flip that card to "Partial" for every user who
            # has an ingest token and no basemap key.
            #
            # The only credential in the schema that is *meant* to reach the
            # browser. MapLibre puts it in the tile URL, so it ships to every
            # client that loads a map and appears in every request they make —
            # CARTO issues these keys free, for exactly that use. It is stored
            # here for the encryption at rest, the per-user scoping and the
            # input UI, not because it is confidential. Nothing reads it back
            # as a value: `/api/map/basemap` returns it only already embedded
            # in the tile URL the browser is about to fetch, so the store's
            # write-only-to-the-browser property is kept where it means
            # something (ISSUE-334).
            "hint": (
                "Free from carto.com/basemaps/apikey. Setting a key selects "
                "CARTO as your basemap; leave it empty to use the deployment "
                "default. The key travels in the map tile URL, so treat it as "
                "public."
            ),
            "fields": [
                {"key": "api_key", "label": "API key", "type": "password"},
            ],
        },
    },
}


def plugin_service_keys(skill_index: dict) -> dict[str, tuple[str, ...]]:
    """The ``(service, keys)`` pairs a skill plugin's ``from: secret`` specs name.

    Only entries ``is_plugin`` holds for, and only services matching
    ``PLUGIN_SERVICE_RE``. The filter is here rather than left to the loader's
    manifest check because an operator entry over a plugin is a plugin entry
    too and its ``env:`` is not put through that check: without it a
    ``config/skills/<plugin>/skill.md`` naming ``mail_account`` would make
    that service writable (spec: external-skill-packages).

    The imports are function-scoped: a caller with an index has already
    loaded the skills package, and the callers with none (the web settings
    routes, the config-load path) must not.
    """
    from istota.lib.skill_module import PLUGIN_SERVICE_RE
    from istota.skills._types import is_plugin

    pairs: dict[str, set[str]] = {}
    for meta in skill_index.values():
        if not is_plugin(meta):
            continue
        for spec in meta.env_specs:
            if spec.source != "secret" or not spec.key:
                continue
            if PLUGIN_SERVICE_RE.fullmatch(spec.service) is None:
                continue
            pairs.setdefault(spec.service, set()).add(spec.key)
    return {service: tuple(sorted(keys)) for service, keys in sorted(pairs.items())}


def _plugin_service_schemas(skill_index: dict) -> dict[str, dict]:
    """A minimal registry entry per plugin service, in the static entries' shape.

    ``cli_only``: v1 has no settings card for a plugin, and the web routes
    never pass an index anyway. No ``key_rule``: the declared keys are all.
    """
    used_by: dict[str, set[str]] = {}
    for name, meta in skill_index.items():
        for spec in meta.env_specs:
            if spec.source == "secret":
                used_by.setdefault(spec.service, set()).add(name)
    return {
        service: {
            "label": service,
            "used_by": tuple(sorted(used_by.get(service, ()))),
            "cli_only": True,
            "fields": [{"key": key, "label": key, "type": "password"} for key in keys],
        }
        for service, keys in plugin_service_keys(skill_index).items()
    }


def all_known_services(skill_index: dict | None = None) -> dict[str, dict]:
    """Union of connected + module-owned service schemas.

    Module-service schemas have no resource_types: they're gated by
    ``Config.is_module_enabled`` at request time, not by the schema.

    With ``skill_index``, each skill plugin's secret services are added
    (`plugin_service_keys`). With none, the static registry alone, which is
    what the web settings routes ask for.
    """
    out: dict[str, dict] = dict(CONNECTED_SERVICE_SCHEMA)
    for mod_services in MODULE_SERVICE_SCHEMA.values():
        for service, schema in mod_services.items():
            out[service] = schema
    if skill_index is not None:
        for service, schema in _plugin_service_schemas(skill_index).items():
            out.setdefault(service, schema)
    return out


def service_accepts_key(service: str, key: str, skill_index: dict | None = None) -> bool:
    """Whether ``key`` may be written under ``service``.

    A listed field, or, for a service with ``key_rule = "provider_id"``, any
    provider id (`llm.providers.is_provider_id`). ``skill_index`` as for
    `all_known_services`.
    """
    schema = all_known_services(skill_index).get(service)
    if not schema:
        return False
    if key in {f["key"] for f in schema.get("fields", [])}:
        return True
    if schema.get("key_rule") == "provider_id":
        from istota.llm.providers import is_provider_id

        return is_provider_id(key)
    return False


def known_service_keys(skill_index: dict | None = None) -> dict[str, frozenset[str]]:
    """Flat ``{service: {key, ...}}`` mapping for fast validation.

    The CLI uses this to reject typos before they reach the DB. Empty
    values (``"fields": []``) — e.g. OAuth-only services — yield an empty
    frozenset, which signals "no operator-writable keys". ``skill_index`` as
    for `all_known_services`.
    """
    return {
        service: frozenset(f["key"] for f in schema.get("fields", []))
        for service, schema in all_known_services(skill_index).items()
    }
