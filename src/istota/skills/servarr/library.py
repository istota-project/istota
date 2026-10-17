"""The library half of the skill: what an add will use, and later search and add.

Stage 1 holds the add-defaults resolution, which `describe` reports and `add`
will apply. For a quality profile, a root folder and (lidarr) a metadata
profile, the order is:

1. the flag (`--profile`, `--root`, `--metadata-profile`; admin tier only),
   matched against the app's own list: profiles by exact name, then
   case-insensitively; root folders by exact path. A miss is
   ``unknown_profile`` or ``unknown_root``, naming what exists;
2. ``[servarr.defaults.<app>]``, matched the same way. A name the app does not
   have is ``stale_default``, never a quiet fall-through, because that would
   hide drift between the stack and the config;
3. the app's only option, when it has exactly one;
4. otherwise ``choice_required``, listing the options.

``minimum_availability`` (radarr) and ``monitor`` are not looked up in the app:
they come from config or their built-in default.
"""

from __future__ import annotations

from .apps import DEFAULT_FIELDS, MEDIA_APPS
from .client import ArrClient, ServarrError, as_rows, number, selector
from .run import Run, walk_apps

#: Fields resolved against a list the app returns: (option key, flag, miss reason).
LISTED_FIELDS = {
    "quality_profile": ("name", "--profile", "unknown_profile"),
    "root_folder": ("path", "--root", "unknown_root"),
    "metadata_profile": ("name", "--metadata-profile", "unknown_profile"),
}
#: Fields with a built-in default and no list to check against.
BUILTIN_DEFAULTS = {"minimum_availability": "released", "monitor": "all"}


def _match(options: list[dict], key: str, wanted: str, *, fold: bool) -> dict | None:
    for option in options:
        if option.get(key) == wanted:
            return option
    if fold:
        folded = wanted.casefold()
        for option in options:
            value = option.get(key)
            if isinstance(value, str) and value.casefold() == folded:
                return option
    return None


def _found(option: dict, key: str, source: str) -> dict:
    result = {"value": option.get(key)}
    if key == "name" and "id" in option:
        result["id"] = option["id"]
    result["source"] = source
    return result


def resolve_field(
    app: str, field: str, options: list[dict], *, flag: str | None = None,
    configured: str | None = None,
) -> dict:
    """``{"value", ["id"], "source"}`` for one listed field, or a refusal.

    The value is the server's own spelling, which is what an add sends. Names
    in a refusal's ``available`` are passed through `selector`, so a hostile
    one arrives fenced.
    """
    key, flag_name, miss = LISTED_FIELDS[field]
    fold = key == "name"
    available = [selector(option.get(key)) for option in options]

    if flag is not None:
        option = _match(options, key, flag, fold=fold)
        if option is None:
            raise ServarrError(
                f"{app} has no {field.replace('_', ' ')} matching {flag_name} {flag!r}.",
                miss, app=app, field=field, available=available,
            )
        return _found(option, key, "flag")

    if configured:
        option = _match(options, key, configured, fold=fold)
        if option is None:
            config_key = f"servarr.defaults.{app}.{field}"
            raise ServarrError(
                f"The configured default {config_key} = {configured!r} is not one {app} "
                f"has; the operator needs to correct it.",
                "stale_default", app=app, field=field, config_key=config_key,
                available=available,
            )
        return _found(option, key, "config")

    if len(options) == 1:
        return _found(options[0], key, "only_option")

    raise ServarrError(
        f"{app} has {len(options)} {field.replace('_', ' ')} options and no default is "
        f"configured, so one has to be chosen.",
        "choice_required", app=app, field=field, available=available,
    )


def describe_defaults(app: str, fields: tuple[str, ...], configured: dict,
                      catalog: dict[str, list[dict]]) -> dict:
    """What an add with no flags would use, field by field; never raises.

    A field that cannot be resolved reports its refusal (``error``, plus
    ``config_key`` and ``available`` where they apply) in place of a value, so
    `describe` shows every problem at once rather than the first.
    """
    configured = configured if isinstance(configured, dict) else {}
    out: dict[str, dict] = {}
    for field in fields:
        value = configured.get(field)
        value = value.strip() if isinstance(value, str) and value.strip() else None
        if field in BUILTIN_DEFAULTS:
            out[field] = ({"value": value, "source": "config"} if value
                          else {"value": BUILTIN_DEFAULTS[field], "source": "default"})
            continue
        try:
            out[field] = resolve_field(app, field, catalog.get(field, []), configured=value)
        except ServarrError as exc:
            entry = {"error": exc.reason}
            for name in ("config_key", "available"):
                if name in exc.extra:
                    entry[name] = exc.extra[name]
            out[field] = entry
    return out


def _named(rows: list[dict]) -> list[dict]:
    return [{"id": number(row.get("id")), "name": selector(row.get("name"))} for row in rows]


def _describe(client: ArrClient, defaults: dict) -> dict:
    app = client.app
    profiles = as_rows(client.request("GET", "qualityprofile"), app, "qualityprofile")
    roots = as_rows(client.request("GET", "rootfolder"), app, "rootfolder")
    tags = as_rows(client.request("GET", "tag"), app, "tag")
    catalog = {"quality_profile": profiles, "root_folder": roots}
    out = {
        "quality_profiles": _named(profiles),
        # `freeSpace` is reported as given: on a network mount it may mean little.
        "root_folders": [
            {"path": selector(row.get("path")), "free_space": number(row.get("freeSpace")),
             "accessible": row.get("accessible") if isinstance(row.get("accessible"), bool)
             else None}
            for row in roots
        ],
        "tags": [{"id": number(row.get("id")), "label": selector(row.get("label"))}
                 for row in tags],
    }
    if "metadata_profile" in DEFAULT_FIELDS[app]:
        metadata = as_rows(client.request("GET", "metadataprofile"), app, "metadataprofile")
        catalog["metadata_profile"] = metadata
        out["metadata_profiles"] = _named(metadata)
    out["defaults"] = describe_defaults(app, DEFAULT_FIELDS[app], defaults.get(app, {}), catalog)
    return out


def cmd_describe(run: Run, args) -> dict:
    """Profiles, root folders, tags and the add defaults of each media app."""
    defaults = run.config.servarr.defaults
    if args.app:
        if args.app not in MEDIA_APPS:
            raise ServarrError(f"{args.app} serves no media, so it has nothing to describe; "
                               f"use status for it.", "not_a_media_app", app=args.app)
        names = [args.app]
    else:
        names = [name for name in MEDIA_APPS if name in run.config.servarr.apps]
    return walk_apps(run, names, lambda client: _describe(client, defaults),
                     named=bool(args.app))
