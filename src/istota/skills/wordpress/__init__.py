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
    python -m istota.skills.wordpress terms create --taxonomy SLUG --name N [--parent ID] --confirmed
    python -m istota.skills.wordpress media list [--search Q] [--mime image]
    python -m istota.skills.wordpress media upload --file PATH [--title T] [--alt A] [--caption C]
    python -m istota.skills.wordpress media update --id N [--title T] [--alt A] [--caption C]
    python -m istota.skills.wordpress users list|get ...
    python -m istota.skills.wordpress users create --username U --email E --role R --confirmed
    python -m istota.skills.wordpress users update --id N [--role R] [--name N] ... --confirmed
    python -m istota.skills.wordpress settings get
    python -m istota.skills.wordpress settings update --set KEY=JSON ... --confirmed
    python -m istota.skills.wordpress plugins list
    python -m istota.skills.wordpress plugins activate|deactivate --plugin DIR/FILE [--network] --confirmed
    python -m istota.skills.wordpress plugins install --slug S [--activate [--network]] --confirmed
    python -m istota.skills.wordpress rest METHOD ROUTE [--query K=V ...] [--body-file F] [--confirmed]
    python -m istota.skills.wordpress abilities list [--category C]
    python -m istota.skills.wordpress abilities run NAME [--input-file F] [--confirmed]
    python -m istota.skills.wordpress options get --page SLUG
    python -m istota.skills.wordpress options update --page SLUG [--acf-file F] [--acf-set K=JSON] --confirmed
    python -m istota.skills.wordpress network sites
    python -m istota.skills.wordpress fields get (--id N | --page SLUG) [--path PATH] [--output OUT]
    python -m istota.skills.wordpress fields edit (--id N | --page SLUG) --token T \
        [--set PATH=JSON] [--set-file PATH=FILE] [--insert PATH=JSON] [--insert-file PATH=FILE] \
        [--remove PATH] [--move FROM=TO] [--ops-file F] [--confirmed]

``options``, ``network`` and ``fields`` need the istota-connector plugin on the
site (`connector.py`, `fields.py`).

Every verb but `sites` takes ``--site`` and, on a multisite network, ``--blog``.

**How a call finds its credential.** A site is the vault entry
``wordpress_<name>`` (`sites.py`); ``--site NAME`` names it, and with no
``--site`` the user's only such entry is used, found through the proxy's
uncharged ``vault_list``. That one entry is resolved whole, after the argv has
parsed and every host path has resolved and before the verb's handler runs
(`_site_verb`), through `_credref.resolve_entry`: one fetch from the task's
budget, over the private credential fd, yielding the application password, the
WordPress login and the site URL. The URL decides where requests go and the
entry's ``bound_hosts`` decide where they may go (`client.py`).

The user id is ``ISTOTA_USER_ID`` and the config is the daemon's own
(``ISTOTA_CONFIG_PATH``), read host-side for ``[wordpress] private_hosts``.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from istota.skills._cli import error_envelope, fail, parse_and_resolve, run_skill_cli
from istota.skills._credref import resolve_entry
from istota.skills._hostpath import EGRESS, REMOTE, WRITE, host_path

from . import admin, connector, content, discovery, fields, generic, media
from .cache import Cache
from .client import WordPressClient, WordPressError, fence, resolve_host
from .sites import (
    SiteError,
    SiteRecord,
    blog_base,
    check_blog,
    check_bound,
    normalize_site_url,
    same_site,
    select_site,
    site_for,
    site_names,
)

log = logging.getLogger(__name__)


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
    parser.add_argument("--site", help="site name: the vault entry wordpress_<name> (default: the only one)")
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
    featured = parser.add_mutually_exclusive_group()
    featured.add_argument("--featured-media-id", type=int, help="attachment id, 0 for none")
    host_path(featured, "--featured-image", mode=EGRESS,
              help="upload this file from your own workspace and make it the featured image")
    host_path(parser, "--meta-file", mode=EGRESS,
              help="a JSON object of registered post meta, from your own workspace")
    host_path(parser, "--acf-file", mode=EGRESS,
              help="a JSON object of ACF fields to write whole, from your own workspace; "
                   '{"$upload": PATH} anywhere in a value uploads PATH and puts its id there')
    parser.add_argument("--acf-set", action="append",
                        help="FIELD=JSON, one ACF field written whole; repeatable")
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
    p = terms.add_parser("create", help="create a term (needs --confirmed)")
    _site_args(p)
    p.add_argument("--taxonomy", required=True, help="taxonomy slug, e.g. category")
    p.add_argument("--name", required=True, help="the term name")
    p.add_argument("--parent", type=int, help="parent term id (hierarchical taxonomies)")
    p.add_argument("--slug", help="the term slug")
    _confirmed(p)

    p = sub.add_parser("media", help="the media library")
    media_sub = p.add_subparsers(dest="media_command", required=True)
    p = media_sub.add_parser("list", help="list media items")
    _site_args(p)
    p.add_argument("--search", help="search text")
    p.add_argument("--mime", help="image, video, audio, text, application, or a full MIME type")
    _paging(p, limit_help=f"items per page, 1-{content.MAX_LIMIT} (default 20)")
    p = media_sub.add_parser("upload", help="upload a file to the media library")
    _site_args(p)
    host_path(p, "--file", mode=EGRESS, required=True,
              help="the file to upload, from your own workspace")
    p.add_argument("--title", help="the attachment title")
    p.add_argument("--alt", help="alt text")
    p.add_argument("--caption", help="the caption")
    p = media_sub.add_parser("update", help="change an attachment's title, alt text or caption")
    _site_args(p)
    p.add_argument("--id", type=int, required=True, help="attachment id")
    p.add_argument("--title", help="the attachment title")
    p.add_argument("--alt", help="alt text")
    p.add_argument("--caption", help="the caption")

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
    p = users.add_parser("create", help="create a user with a role (needs --confirmed)")
    _site_args(p)
    p.add_argument("--username", required=True, help="the login")
    p.add_argument("--email", required=True, help="the user's email address")
    p.add_argument("--role", required=True, help="role slug, e.g. editor")
    p.add_argument("--name", help="display name")
    _confirmed(p)
    p = users.add_parser("update", help="change a user's role or profile (needs --confirmed)")
    _site_args(p)
    p.add_argument("--id", required=True, help="user id, or `me`")
    p.add_argument("--role", help="role slug; replaces the user's roles")
    p.add_argument("--name", help="display name")
    p.add_argument("--email", help="email address")
    p.add_argument("--first-name", help="first name")
    p.add_argument("--last-name", help="last name")
    _confirmed(p)

    p = sub.add_parser("settings", help="site settings")
    settings = p.add_subparsers(dest="settings_command", required=True)
    p = settings.add_parser("get", help="read /wp/v2/settings")
    _site_args(p)
    p = settings.add_parser("update", help="change site settings (needs --confirmed)")
    _site_args(p)
    p.add_argument("--set", action="append", required=True,
                   help="KEY=JSON, one setting; repeatable (a string is quoted: title='\"x\"')")
    _confirmed(p)

    p = sub.add_parser("plugins", help="installed plugins")
    plugins = p.add_subparsers(dest="plugins_command", required=True)
    p = plugins.add_parser("list", help="list plugins and their status")
    _site_args(p)
    for verb in ("activate", "deactivate"):
        p = plugins.add_parser(verb, help=f"{verb} a plugin (needs --confirmed)")
        _site_args(p)
        host_path(p, "--plugin", mode=REMOTE, required=True,
                  note="a plugin's dir/file on the WordPress host, checked by "
                       "admin.plugin_id and resolved by WordPress",
                  help="dir/file, as `plugins list` names it")
        p.add_argument("--network", action="store_true",
                       help="network-wide, on a multisite network (needs a super admin)")
        _confirmed(p)
    p = plugins.add_parser("install", help="install from WordPress.org (needs --confirmed)")
    _site_args(p)
    p.add_argument("--slug", required=True, help="the WordPress.org plugin slug")
    p.add_argument("--activate", action="store_true", help="activate it once installed")
    p.add_argument("--network", action="store_true",
                   help="with --activate: network-wide, on a multisite network")
    _confirmed(p)

    p = sub.add_parser("rest", help="call a REST route the other verbs do not cover")
    _site_args(p)
    p.add_argument("method", choices=generic.REST_METHODS,
                   help="HTTP method; anything but GET needs --confirmed")
    p.add_argument("route", help="route under the site's /wp-json/, e.g. wp/v2/menus")
    p.add_argument("--query", action="append", help="query parameter K=V; repeatable")
    host_path(p, "--body-file", mode=EGRESS,
              help="a JSON request body, from your own workspace (not with GET)")
    _confirmed(p)

    p = sub.add_parser("abilities", help="the Abilities API (WordPress 6.9+)")
    abilities = p.add_subparsers(dest="abilities_command", required=True)
    p = abilities.add_parser("list", help="list registered abilities")
    _site_args(p)
    p.add_argument("--category", help="only this ability category")
    p = abilities.add_parser("run", help="run an ability (needs --confirmed unless readonly)")
    _site_args(p)
    p.add_argument("name", help="the ability, namespace/name")
    host_path(p, "--input-file", mode=EGRESS,
              help="the ability's input as JSON, from your own workspace")
    _confirmed(p)

    p = sub.add_parser("options", help="ACF options pages (needs the istota-connector plugin)")
    options = p.add_subparsers(dest="options_command", required=True)
    p = options.add_parser("get", help="read an options page's REST-visible fields")
    _site_args(p)
    p.add_argument("--page", required=True, help="the options page slug, e.g. acf-options")
    p = options.add_parser("update", help="write options page fields whole (needs --confirmed)")
    _site_args(p)
    p.add_argument("--page", required=True, help="the options page slug, e.g. acf-options")
    host_path(p, "--acf-file", mode=EGRESS,
              help="a JSON object of fields to write whole, from your own workspace; "
                   '{"$upload": PATH} anywhere in a value uploads PATH and puts its id there')
    p.add_argument("--acf-set", action="append",
                   help="FIELD=JSON, one field written whole; repeatable")
    _confirmed(p)

    p = sub.add_parser("network", help="a multisite network (needs the istota-connector plugin)")
    network = p.add_subparsers(dest="network_command", required=True)
    p = network.add_parser("sites", help="list the network's sites (needs a super admin)")
    _site_args(p)

    p = sub.add_parser("fields", help="ACF values edited by path (needs the istota-connector plugin)")
    fields_sub = p.add_subparsers(dest="fields_command", required=True)
    p = fields_sub.add_parser("get", help="list a post's or options page's fields, or read one path")
    _site_args(p)
    _field_target(p)
    p.add_argument("--path", help="a field path such as blocks/0/items; without it, the "
                                  "fields and their tokens are listed")
    host_path(p, "--output", mode=WRITE,
              help="with --path: write the value and its definition as JSON here")
    p = fields_sub.add_parser("edit", help="set, insert, remove or move inside one field")
    _site_args(p)
    _field_target(p)
    p.add_argument("--token", required=True, help="the field's token from `fields get`")
    fields.add_op_arguments(p)
    host_path(p, "--ops-file", mode=EGRESS,
              help="a JSON array of op objects from your own workspace, applied before the flags")
    _confirmed(p)

    return parser


def _field_target(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", type=int, help="a post id, of any type")
    target.add_argument("--page", help="an ACF options page slug, e.g. acf-options")


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


def _list_entries() -> list[str]:
    """The task's vault entry names, through the proxy. Tests replace this."""
    from istota.sandbox.credential_shim import ProxyError, list_entries

    try:
        return list_entries()
    except ProxyError as exc:
        raise SiteError(f"Could not list the vault's entries: {exc}",
                        "vault_credential_refused") from None


def _user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if not user_id:
        raise SiteError("No user id in the environment (ISTOTA_USER_ID); this CLI "
                        "runs under a task.", "unknown_site")
    return user_id


def _entry_text(entry, field: str) -> str:
    value = getattr(entry, field)
    return value.reveal().strip() if value is not None else ""


def _open_site(args) -> SiteContext:
    """The site, the entry and a client, in the order that spends least first."""
    user_id = _user_id()
    config = getattr(args, "config", None) or _load_config()
    if args.site:
        record = site_for(args.site)
    else:
        record = select_site(_list_entries(), None)
    blog = check_blog(args.blog)

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
    """The sites the vault holds: its ``wordpress_*`` entries. No network, no fetch."""
    names = site_names(_list_entries())
    return {
        "status": "ok",
        "sites": [site_for(name).as_dict() for name in names],
    }


def _site_verb(handler, precheck=None):
    """`handler` with the site opened first and the client closed after.

    Opening the site is the vault fetch, so it happens only once the argv has
    parsed, every host path has resolved and `precheck` (the verb's own local
    checks, with ``args.config`` loaded) has passed, and never for `sites`.
    """

    def run(args):
        # A precheck may hold files open for the handler (`media.open_upload`);
        # whatever it registers here is closed on every path.
        args.closers = []
        try:
            args.config = _load_config()
            if precheck is not None:
                precheck(args)
            args.wp = _open_site(args)
            try:
                return handler(args)
            except WordPressError as exc:
                # Which site a refusal or an ambiguous write was about.
                exc.extra.setdefault("site", args.wp.record.name)
                exc.extra.setdefault("blog", args.wp.blog)
                raise
            finally:
                args.wp.client.close()
        finally:
            for close in reversed(args.closers):
                close()

    run.__name__ = handler.__name__
    return run


#: A write refused for one of these may have been refused on stale discovery
#: (a field group just switched to REST, a route just registered), so the
#: site's cache is dropped and the next call rediscovers (spec §9.3).
_STALE_REASONS = frozenset({"acf_not_in_rest", "unknown_route", "validation_error",
                            "stale_value", "unknown_path"})


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
    "terms create": _write_verb(content.cmd_terms_create, content.check_terms_create),
    "media list": _site_verb(media.cmd_media_list, content.check_paging),
    "media upload": _write_verb(media.cmd_media_upload, media.check_upload),
    "media update": _write_verb(media.cmd_media_update, media.check_media_update),
    "users list": _site_verb(admin.cmd_users_list, content.check_paging),
    "users get": _site_verb(admin.cmd_users_get, admin.check_user_id),
    "users create": _site_verb(admin.cmd_users_create, admin.check_users_create),
    "users update": _site_verb(admin.cmd_users_update, admin.check_users_update),
    "settings get": _site_verb(admin.cmd_settings_get),
    "settings update": _site_verb(admin.cmd_settings_update, admin.check_settings_update),
    "plugins list": _site_verb(admin.cmd_plugins_list),
    "plugins activate": _site_verb(admin.cmd_plugins_activate, admin.check_plugin_status),
    "plugins deactivate": _site_verb(admin.cmd_plugins_deactivate, admin.check_plugin_status),
    "plugins install": _site_verb(admin.cmd_plugins_install, admin.check_plugin_install),
    "rest": _site_verb(generic.cmd_rest, generic.prepare_rest),
    "abilities list": _site_verb(generic.cmd_abilities_list),
    "abilities run": _site_verb(generic.cmd_abilities_run, generic.check_ability),
    "options get": _site_verb(connector.cmd_options_get, connector.check_page),
    "options update": _write_verb(connector.cmd_options_update,
                                  connector.check_options_update),
    "network sites": _site_verb(connector.cmd_network_sites),
    "fields get": _site_verb(fields.cmd_fields_get, fields.check_fields_get),
    "fields edit": _write_verb(fields.cmd_fields_edit, fields.check_fields_edit),
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
