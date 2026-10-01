"""WordPress skill: read and administer WordPress sites over the core REST API.

Usage:
    python -m istota.skills.wordpress sites
    python -m istota.skills.wordpress describe [--type SLUG] [--refresh] [--output OUT]
    python -m istota.skills.wordpress list --type SLUG [--status S] [--search Q] ...
    python -m istota.skills.wordpress get --id N [--type SLUG] [--fields F] [--output OUT]
    python -m istota.skills.wordpress create --type SLUG --title T [--content-file F] ...
    python -m istota.skills.wordpress update --id N [--type SLUG] [--status S] ... [--confirmed]
    python -m istota.skills.wordpress delete --id N [--type SLUG] [--force --confirmed]
    python -m istota.skills.wordpress publish --id N [--type SLUG] [--date D] --confirmed
    python -m istota.skills.wordpress terms list --taxonomy SLUG [--search Q]
    python -m istota.skills.wordpress media list [--search Q] [--mime image]
    python -m istota.skills.wordpress users list|get ...
    python -m istota.skills.wordpress settings get
    python -m istota.skills.wordpress plugins list
    python -m istota.skills.wordpress rest GET ROUTE [--query K=V ...]
    python -m istota.skills.wordpress abilities list [--category C]

Every verb but `sites` takes ``--site`` and, on a multisite record, ``--blog``.

**How a call finds its credential.** ``--site`` names a record in the user's
``config/WORDPRESS.md`` (`sites.py`), and the record names a vault entry. That
one entry is resolved whole, after the argv has parsed and every host path has
resolved and before the verb's handler runs (`_site_verb`), through
`_credref.resolve_entry`: one fetch from the task's budget, over the private
credential fd, yielding the application password, the WordPress login and the
site URL. The URL decides where requests go and the entry's ``bound_hosts``
decide where they may go, so a record the model edited cannot move the password
(`client.py`). The site is chosen before the fetch, so a typo in ``--site``
spends nothing.

The user id is ``ISTOTA_USER_ID`` and the config is the daemon's own
(``ISTOTA_CONFIG_PATH``), read host-side for ``[wordpress] private_hosts`` and
the user's workspace.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from istota.skills._cli import error_envelope, fail, parse_and_resolve, run_skill_cli
from istota.skills._credref import resolve_entry
from istota.skills._hostpath import EGRESS, WRITE, host_path

from . import admin, content, discovery, generic, media
from .cache import Cache
from .client import WordPressClient, WordPressError, fence, resolve_host
from .sites import (
    SITES_FILE,
    SiteError,
    SiteRecord,
    blog_base,
    check_blog,
    check_bound,
    normalize_site_url,
    parse_sites,
    same_site,
    select_site,
)

log = logging.getLogger(__name__)

FEATURE = "skill_wordpress"


@dataclass
class SiteContext:
    """What a handler needs about the site it is talking to."""

    record: SiteRecord
    client: WordPressClient
    base: str
    blog: str | None
    cache: Cache

    def envelope(self) -> dict:
        return {"site": self.record.name, "blog": self.blog}


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _site_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--site", help="site name from WORDPRESS.md (default: the default site)")
    parser.add_argument("--blog", help="multisite network site: a slug, or a host on a subdomain network")


def _paging(parser: argparse.ArgumentParser, *, limit_help: str) -> None:
    parser.add_argument("--limit", type=int, help=limit_help)
    parser.add_argument("--page", type=int, default=1, help="result page, from 1")


def _confirmed(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirmed", action="store_true",
                        help="the user agreed to exactly what the refusal described")


def _write_args(parser: argparse.ArgumentParser) -> None:
    """The fields `create` and `update` share."""
    body = parser.add_mutually_exclusive_group()
    host_path(body, "--content-file", mode=EGRESS,
              help="read the post content (block markup) from a file in your own workspace")
    body.add_argument("--content", help="the post content")
    parser.add_argument("--excerpt", help="the excerpt")
    parser.add_argument("--slug", help="the post slug")
    parser.add_argument("--date", help="ISO 8601; naive is site time, an offset is converted to UTC")
    parser.add_argument("--password", help="a post password; empty to clear it")
    parser.add_argument("--terms", action="append",
                        help="TAXONOMY=name1,name2 (names or ids); repeatable")
    parser.add_argument("--create-terms", action="store_true",
                        help="create --terms names that do not exist (needs --confirmed)")
    parser.add_argument("--featured-media-id", type=int, help="attachment id, 0 for none")
    host_path(parser, "--meta-file", mode=EGRESS,
              help="a JSON object of registered post meta, from your own workspace")
    _confirmed(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="istota-skill wordpress",
                                     description="WordPress sites over the REST API")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sites", help="list the configured sites (no network, no vault read)")

    p = sub.add_parser("describe", help="discover types, taxonomies, account and APIs")
    _site_args(p)
    p.add_argument("--type", help="also report this post type's ACF field schema")
    p.add_argument("--refresh", action="store_true", help="ignore the hour-long discovery cache")
    host_path(p, "--output", mode=WRITE,
              help="with --type: write the full ACF JSON Schema here")

    p = sub.add_parser("list", help="list posts of one type")
    _site_args(p)
    p.add_argument("--type", required=True, help="post type slug")
    p.add_argument("--status", default="any", choices=content.LIST_STATUSES)
    p.add_argument("--search", help="search text")
    p.add_argument("--slug", help="exact slug")
    p.add_argument("--category", help="category names or ids, comma-separated")
    p.add_argument("--tag", help="tag names or ids, comma-separated")
    _paging(p, limit_help=f"items per page, 1-{content.MAX_LIMIT} (default 20)")

    p = sub.add_parser("get", help="one post, with raw content and ACF values")
    _site_args(p)
    p.add_argument("--id", type=int, required=True, help="post id")
    p.add_argument("--type", default="post", help="post type slug (default: post)")
    p.add_argument("--fields", help="comma-separated top-level fields to return")
    host_path(p, "--output", mode=WRITE,
              help="write the full item as JSON here and return a summary")

    p = sub.add_parser("create", help="create a draft or pending post (never publishes)")
    _site_args(p)
    p.add_argument("--type", required=True, help="post type slug")
    p.add_argument("--title", required=True, help="the post title")
    p.add_argument("--status", default="draft", choices=content.CREATE_STATUSES)
    p.add_argument("--if-absent", action="store_true",
                   help="with --slug: return the existing post of that slug instead of a second one")
    _write_args(p)

    p = sub.add_parser("update", help="change fields of an existing post")
    _site_args(p)
    p.add_argument("--id", type=int, required=True, help="post id")
    p.add_argument("--type", default="post", help="post type slug (default: post)")
    p.add_argument("--title", help="the post title")
    p.add_argument("--status", choices=content.UPDATE_STATUSES,
                   help="publish, future and private need --confirmed")
    _write_args(p)

    p = sub.add_parser("delete", help="move a post to the trash, or delete it for good")
    _site_args(p)
    p.add_argument("--id", type=int, required=True, help="post id")
    p.add_argument("--type", default="post", help="post type slug (default: post)")
    p.add_argument("--force", action="store_true",
                   help="skip the trash and delete for good (needs --confirmed)")
    _confirmed(p)

    p = sub.add_parser("publish", help="publish a post (needs --confirmed)")
    _site_args(p)
    p.add_argument("--id", type=int, required=True, help="post id")
    p.add_argument("--type", default="post", help="post type slug (default: post)")
    p.add_argument("--date", help="ISO 8601; a future date schedules the post")
    _confirmed(p)

    p = sub.add_parser("terms", help="taxonomy terms")
    terms = p.add_subparsers(dest="terms_command", required=True)
    p = terms.add_parser("list", help="list a taxonomy's terms")
    _site_args(p)
    p.add_argument("--taxonomy", required=True, help="taxonomy slug, e.g. category")
    p.add_argument("--search", help="search text")
    _paging(p, limit_help=f"terms per page, 1-{content.MAX_LIMIT} (default 100)")

    p = sub.add_parser("media", help="the media library")
    media_sub = p.add_subparsers(dest="media_command", required=True)
    p = media_sub.add_parser("list", help="list media items")
    _site_args(p)
    p.add_argument("--search", help="search text")
    p.add_argument("--mime", help="image, video, audio, text, application, or a full MIME type")
    _paging(p, limit_help=f"items per page, 1-{content.MAX_LIMIT} (default 20)")

    p = sub.add_parser("users", help="site users")
    users = p.add_subparsers(dest="users_command", required=True)
    p = users.add_parser("list", help="list users")
    _site_args(p)
    p.add_argument("--role", help="only users with this role")
    p.add_argument("--search", help="search text")
    _paging(p, limit_help=f"users per page, 1-{content.MAX_LIMIT} (default 20)")
    p = users.add_parser("get", help="one user, with capabilities")
    _site_args(p)
    p.add_argument("--id", required=True, help="user id, or `me`")

    p = sub.add_parser("settings", help="site settings")
    settings = p.add_subparsers(dest="settings_command", required=True)
    p = settings.add_parser("get", help="read /wp/v2/settings")
    _site_args(p)

    p = sub.add_parser("plugins", help="installed plugins")
    plugins = p.add_subparsers(dest="plugins_command", required=True)
    p = plugins.add_parser("list", help="list plugins and their status")
    _site_args(p)

    p = sub.add_parser("rest", help="call a REST route the other verbs do not cover")
    _site_args(p)
    p.add_argument("method", choices=["GET"], help="HTTP method (GET only for now)")
    p.add_argument("route", help="route under the site's /wp-json/, e.g. wp/v2/menus")
    p.add_argument("--query", action="append", help="query parameter K=V; repeatable")

    p = sub.add_parser("abilities", help="the Abilities API (WordPress 6.9+)")
    abilities = p.add_subparsers(dest="abilities_command", required=True)
    p = abilities.add_parser("list", help="list registered abilities")
    _site_args(p)
    p.add_argument("--category", help="only this ability category")

    return parser


# --------------------------------------------------------------------------- #
# The site, the credential and the client
# --------------------------------------------------------------------------- #


def _load_config():
    from istota.config import load_config

    path = os.environ.get("ISTOTA_CONFIG_PATH", "")
    try:
        return load_config(Path(path) if path else None)
    except Exception as exc:  # noqa: BLE001 — becomes the envelope's reason
        raise SiteError(f"Could not load the istota config: {type(exc).__name__}.",
                        "unknown_site") from None


def _transport():
    """The httpx transport; None in production. Tests replace this."""
    return None


def _resolver():
    """The address resolver; the system one in production. Tests replace this."""
    return resolve_host


def _read_records(config, user_id: str) -> tuple[list[SiteRecord], list[str]]:
    from istota.storage import read_user_config_file

    text = read_user_config_file(config, user_id, SITES_FILE)
    if text is None:
        raise SiteError(
            f"config/{SITES_FILE} could not be read: there is no workspace on this "
            f"deployment, or the file is not a regular file.",
            "unknown_site",
        )
    return parse_sites(text)


def _user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if not user_id:
        raise SiteError("No user id in the environment (ISTOTA_USER_ID); this CLI "
                        "runs under a task.", "unknown_site")
    return user_id


def _entry_text(entry, field: str) -> str:
    value = getattr(entry, field)
    return value.reveal().strip() if value is not None else ""


def _require_enabled(config) -> None:
    """Refuse unless the operator enabled the skill.

    The proxy runs every `cli: true` skill whatever the experimental gate
    says (that gate decides selection and the menu), so a CLI that reaches
    the vault and the network carries its own (`skills.md`).
    """
    if not config.experimental.is_enabled(FEATURE):
        raise SiteError(
            f"The wordpress skill is not enabled on this deployment; the operator "
            f"adds {FEATURE!r} to [experimental] features.",
            "skill_disabled",
        )


def _open_site(args) -> SiteContext:
    """The record, the entry and a client, in the order that spends least first."""
    user_id = _user_id()
    config = _load_config()
    _require_enabled(config)
    records, errors = _read_records(config, user_id)
    try:
        record = select_site(records, args.site)
    except SiteError as exc:
        if errors:
            raise SiteError(f"{exc} Problems in {SITES_FILE}: {'; '.join(errors)}",
                            exc.reason) from None
        raise
    blog = check_blog(args.blog, record.multisite)

    entry, refusal = resolve_entry(record.credential, f"wordpress --site {record.name}")
    if refusal is not None:
        fail(refusal, reason="vault_credential_refused")
    url = _entry_text(entry, "url")
    if not url:
        raise SiteError(
            f"Vault entry {record.credential!r} has no URL field; put the site's "
            f"address there.",
            "credential_unbound",
        )
    if not _entry_text(entry, "username") or entry.password is None:
        raise SiteError(
            f"Vault entry {record.credential!r} needs both a username (the "
            f"WordPress login) and a password (the application password).",
            "credential_incomplete",
        )
    site_url = normalize_site_url(url)
    check_bound(site_url, entry.bound_hosts)
    base = blog_base(site_url, blog)

    client = WordPressClient(
        site_url=site_url,
        username=entry.username,
        password=entry.password,
        bound_hosts=entry.bound_hosts,
        private_hosts=config.wordpress.private_hosts,
        transport=_transport(),
        resolve=_resolver(),
    )
    cache = Cache(config.db_path, user_id, site=record.name, blog=blog,
                  scope_key=f"{record.credential}|{base}")
    ctx = SiteContext(record=record, client=client, base=base, blog=blog, cache=cache)
    if blog is not None:
        try:
            _check_blog_exists(ctx, refresh=getattr(args, "refresh", False))
        except BaseException:
            client.close()
            raise
    return ctx


def _check_blog_exists(ctx: SiteContext, *, refresh: bool) -> None:
    """Refuse a ``--blog`` the network does not have, before calling it.

    WordPress answers a mistyped subdirectory with the main site (or a redirect
    to its signup page), so without this a typo would act on the wrong site.
    The site's own index has to report the URL that was built.
    """
    if not refresh and ctx.cache.get("blog_ok") is True:
        return
    try:
        index, _ = ctx.client.get("", base=ctx.base)
    except WordPressError as exc:
        if exc.reason in ("not_found", "unknown_route", "bad_response") or exc.extra.get("redirect"):
            raise WordPressError(
                f"--blog {ctx.blog} is not a site on this network ({exc.reason}).",
                "unknown_blog",
            ) from None
        raise
    index = index if isinstance(index, dict) else {}
    if not (same_site(index.get("url"), ctx.base) or same_site(index.get("home"), ctx.base)):
        raise WordPressError(
            f"--blog {ctx.blog} is not a site on this network: the index there "
            f"reports {fence(index.get('url')) or '(no url)'}.",
            "unknown_blog",
        )
    ctx.cache.put("blog_ok", True)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def cmd_sites(args) -> dict:
    """The records in WORDPRESS.md. No network and no vault fetch."""
    config = _load_config()
    _require_enabled(config)
    records, errors = _read_records(config, _user_id())
    return {
        "status": "ok",
        "file": f"config/{SITES_FILE}",
        "sites": [r.as_dict() for r in records],
        "errors": errors,
    }


def _site_verb(handler, precheck=None):
    """`handler` with the site opened first and the client closed after.

    Opening the site is the vault fetch, so it happens only once the argv has
    parsed, every host path has resolved and `precheck` (the verb's own local
    checks) has passed, and never for `sites`.
    """

    def run(args):
        if precheck is not None:
            precheck(args)
        args.wp = _open_site(args)
        try:
            return handler(args)
        finally:
            args.wp.client.close()

    run.__name__ = handler.__name__
    return run


#: A write refused for one of these may have been refused on stale discovery
#: (a field group just switched to REST, a route just registered), so the
#: site's cache is dropped and the next call rediscovers (spec §9.3).
_STALE_REASONS = frozenset({"acf_not_in_rest", "unknown_route", "validation_error"})


def _write_verb(handler, precheck=None):
    """`_site_verb`, dropping the discovery cache when the site refuses as stale."""

    def run(args):
        try:
            return handler(args)
        except WordPressError as exc:
            if exc.reason in _STALE_REASONS:
                args.wp.cache.drop()
            raise

    run.__name__ = handler.__name__
    return _site_verb(run, precheck)


COMMANDS = {
    "sites": cmd_sites,
    "describe": _site_verb(discovery.cmd_describe),
    "list": _site_verb(content.cmd_list, content.check_paging),
    "get": _site_verb(content.cmd_get, content.check_get),
    "create": _write_verb(content.cmd_create, content.check_create),
    "update": _write_verb(content.cmd_update, content.check_update),
    "delete": _write_verb(content.cmd_delete),
    "publish": _write_verb(content.cmd_publish, content.check_publish),
    "terms list": _site_verb(content.cmd_terms_list, content.check_paging),
    "media list": _site_verb(media.cmd_media_list, content.check_paging),
    "users list": _site_verb(admin.cmd_users_list, content.check_paging),
    "users get": _site_verb(admin.cmd_users_get, admin.check_user_id),
    "settings get": _site_verb(admin.cmd_settings_get),
    "plugins list": _site_verb(admin.cmd_plugins_list),
    "rest": _site_verb(generic.cmd_rest, generic.prepare_rest),
    "abilities list": _site_verb(generic.cmd_abilities_list),
}


def command_key(args) -> str:
    """``terms list`` for a nested verb, ``list`` for a top-level one."""
    nested = getattr(args, f"{args.command}_command", None)
    return f"{args.command} {nested}" if nested else args.command


def _on_exception(exc: BaseException) -> dict:
    if isinstance(exc, (WordPressError, SiteError)):
        extra = getattr(exc, "extra", {}) or {}
        return error_envelope(str(exc), reason=exc.reason, **extra)
    log.warning("wordpress: unexpected %s", type(exc).__name__)
    return error_envelope(f"{type(exc).__name__}: {exc}")


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    run_skill_cli(COMMANDS, args, command=command_key(args), on_exception=_on_exception,
                  error_ensure_ascii=False)
