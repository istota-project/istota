"""Servarr skill: request media and check downloads through the arr APIs.

Usage:
    python -m istota.skills.servarr apps
    python -m istota.skills.servarr status [--app A]
    python -m istota.skills.servarr describe [--app A]
    python -m istota.skills.servarr search QUERY --kind K | --kind K (--tmdb N | --tvdb N | --mbid ID)
        | --kind album --artist NAME [QUERY]
    python -m istota.skills.servarr add --kind K (--tmdb N | --tvdb N | --mbid ID) [--profile P]
        [--root R] [--metadata-profile M] [--monitor MODE] [--no-search] [--confirmed]
    python -m istota.skills.servarr queue [--app A] [--mine] [--problems] [--limit N]
    python -m istota.skills.servarr why --kind K (--library-id N | --tmdb N | --tvdb N | --mbid ID)
        [--releases] [--limit N]
    python -m istota.skills.servarr wanted [--app A] [--cutoff] [--limit N]
    python -m istota.skills.servarr search-now --kind K (--library-id N | --tmdb N | --tvdb N | --mbid ID)
    python -m istota.skills.servarr requests [--user U] [--kind K] [--limit N]
    python -m istota.skills.servarr indexers [--test] [--limit N]

The connection is the operator's, one per app, read host-side from the
daemon's own config (``ISTOTA_CONFIG_PATH``, ``[servarr.apps.<app>]``). The
keys never enter the sandbox, a vault, the argv or the output, and no argv
value ever becomes a URL or a host. Who may call what is `access.py`'s table,
checked after argv parses and before any app is touched.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from istota.skills._cli import error_envelope, parse_and_resolve, run_skill_cli
from istota.skills._hostpath import REMOTE, host_path

from . import access, activity, library
from .apps import APP_ORDER, KIND_APP, display_url, ignored
from .client import ServarrError, envelope_for, resolve_host
from .library import SERIES_MONITOR_MODES
from .run import Run, run_deadline

log = logging.getLogger(__name__)

KINDS = tuple(KIND_APP)


def _transport():
    """The httpx transport; None in production. Tests replace this."""
    return None


def _resolver():
    """The address resolver; the system one in production. Tests replace this."""
    return resolve_host


def _clock() -> float:
    """The run's clock. Tests replace this."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """The run's wait between polls. Tests replace this."""
    time.sleep(seconds)


def _now() -> datetime:
    """Wall-clock time, for the dates the apps report. Tests replace this."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _external_id(parser: argparse.ArgumentParser, *, library: bool) -> None:
    ids = parser.add_mutually_exclusive_group()
    if library:
        ids.add_argument("--library-id", help="the item's id in its arr")
    ids.add_argument("--tmdb", help="TMDB id (films)")
    ids.add_argument("--tvdb", help="TVDB id (shows)")
    ids.add_argument("--mbid", help="MusicBrainz id (artists and albums)")


def _limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, help="how many to return (default 10, at most 50)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="istota-skill servarr",
        description="Request films, shows and albums and check downloads",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("apps", help="the configured apps and what this user may do")

    p = sub.add_parser("status", help="version and health of each app")
    p.add_argument("--app", choices=APP_ORDER)

    p = sub.add_parser("describe", help="profiles, root folders, tags and add defaults")
    p.add_argument("--app", choices=APP_ORDER)

    p = sub.add_parser("search", help="look a title up")
    p.add_argument("query", nargs="?", help="the title to look up")
    p.add_argument("--kind", choices=KINDS, required=True)
    p.add_argument("--artist", help="albums: list this artist's albums (a title narrows them)")
    _external_id(p, library=False)
    _limit(p)

    p = sub.add_parser("add", help="request an item by its external id")
    p.add_argument("--kind", choices=KINDS, required=True)
    _external_id(p, library=False)
    p.add_argument("--profile", help="quality profile name (admin only)")
    host_path(p, "--root", mode=REMOTE,
              note="a root folder on the arr's own machine, matched against the app's list",
              help="root folder on the arr's machine (admin only)")
    p.add_argument("--metadata-profile", help="lidarr metadata profile name (admin only)")
    p.add_argument("--monitor", choices=SERIES_MONITOR_MODES, help="which episodes to monitor")
    p.add_argument("--no-search", action="store_true", help="add without searching now")
    p.add_argument("--confirmed", action="store_true",
                   help="the user agreed to exactly what the refusal described")

    p = sub.add_parser("queue", help="what is downloading or stuck")
    p.add_argument("--app", choices=APP_ORDER)
    p.add_argument("--mine", action="store_true", help="only this user's requests")
    p.add_argument("--problems", action="store_true", help="only items that need attention")
    _limit(p)

    p = sub.add_parser("why", help="explain one item's state")
    p.add_argument("--kind", choices=KINDS, required=True)
    _external_id(p, library=True)
    p.add_argument("--releases", action="store_true",
                   help="search every indexer and show why each release was rejected")
    _limit(p)

    p = sub.add_parser("wanted", help="monitored items still missing")
    p.add_argument("--app", choices=APP_ORDER)
    p.add_argument("--cutoff", action="store_true", help="items below their quality cutoff")
    _limit(p)

    p = sub.add_parser("search-now", help="ask an arr to search for one item now")
    p.add_argument("--kind", choices=KINDS, required=True)
    _external_id(p, library=True)

    p = sub.add_parser("requests", help="what a user asked for")
    p.add_argument("--user", help="whose requests (default: the caller)")
    p.add_argument("--kind", choices=KINDS)
    _limit(p)

    p = sub.add_parser("indexers", help="prowlarr's indexers and their state")
    p.add_argument("--test", action="store_true", help="test every indexer (slow)")
    _limit(p)

    return parser


# --------------------------------------------------------------------------- #
# Verbs
# --------------------------------------------------------------------------- #


def cmd_apps(run: Run, args) -> dict:
    """The connection as this caller sees it. No network."""
    config = run.config
    tier = run.tier.name
    return {
        "status": "ok",
        "tier": tier,
        "apps": [{"app": name, "url": display_url(config.servarr.apps[name].url)}
                 for name in APP_ORDER if name in config.servarr.apps],
        "missing": [name for name in APP_ORDER if name not in config.servarr.apps],
        "ignored": ignored(config),
        "verbs": access.allowed_verbs(tier),
        "refused_flags": access.refused_flags(tier),
        "confirmed_allowed": not run.tier.email_thread,
    }


HANDLERS = {
    "apps": cmd_apps,
    "status": activity.cmd_status,
    "describe": library.cmd_describe,
    "search": library.cmd_search,
    "add": library.cmd_add,
    "queue": activity.cmd_queue,
    "why": activity.cmd_why,
    "wanted": activity.cmd_wanted,
    "search-now": activity.cmd_search_now,
    "requests": library.cmd_requests,
    "indexers": activity.cmd_indexers,
}


def _verb(verb: str):
    """Resolve the tier, check the table, then run the handler with its clients."""
    handler = HANDLERS[verb]

    def run_verb(args):
        ctx = access.load_context()
        tier = access.resolve_tier(ctx)
        access.check(verb, args, tier)
        config = ctx["config"]
        clock = _clock
        run = Run(config=config, tier=tier, user_id=ctx["user_id"],
                  deadline=run_deadline(config, clock), clock=clock, sleep=_sleep, now=_now,
                  transport=_transport(), resolve=_resolver())
        try:
            return handler(run, args)
        finally:
            run.close()

    return run_verb


COMMANDS = {verb: _verb(verb) for verb in HANDLERS}


def _on_exception(exc: BaseException) -> dict:
    if isinstance(exc, ServarrError):
        return envelope_for(exc)
    log.warning("servarr: unexpected %s", type(exc).__name__)
    return error_envelope(f"{type(exc).__name__}: {exc}")


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    run_skill_cli(COMMANDS, args, on_exception=_on_exception, error_ensure_ascii=False)
