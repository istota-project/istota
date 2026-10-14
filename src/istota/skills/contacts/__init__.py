"""Contact lookup and edits across the user's available backends."""

import argparse
from dataclasses import asdict
import logging
import os
import time

from istota.lib.untrusted import frame_untrusted
from istota.skills._cli import error_envelope, parse_and_resolve, run_skill_cli
from . import carddav, google
from .model import ContactField, ContactInput, match_score, one_line

BACKENDS = (carddav, google)
logger = logging.getLogger(__name__)


def _error(exc):
    if isinstance(exc, (carddav.CardDAVError, google.GoogleError)):
        extra = {"reason": exc.reason}
        if exc.status_code is not None:
            extra["status_code"] = exc.status_code
        if getattr(exc, "needs", None):
            extra["needs"] = exc.needs
        message = {
            "google_token_invalid": "Reconnect Google to refresh your contacts access.",
            "google_api_disabled": "The operator must enable the Google People API.",
            "google_scope_insufficient": "Reconnect Google with the required contacts access.",
            "changed_since_read": "Show the contact again and retry once.",
            "lookup_limit": "Other contacts lookup reached its page limit.",
        }.get(exc.reason, exc.reason)
        return error_envelope(message, **extra)
    if isinstance(exc, ValueError) and str(exc) in {"nothing_to_change", "name_required", "invalid_vcard", "invalid_birthday"}:
        return error_envelope(str(exc), reason=str(exc))
    return error_envelope("Contact operation failed", reason="internal_error")


def _sources(backend):
    return ("nextcloud",) if backend is carddav else ("google", "google-other")


def _available(args):
    env = dict(os.environ)
    if not env.get("ISTOTA_USER_ID", "").strip():
        raise carddav.CardDAVError("no_user")
    found = []
    for backend in BACKENDS:
        ctx = backend.BackendContext(env=env)
        if backend.available(ctx):
            found.append((backend, ctx))
    if not found:
        raise carddav.CardDAVError("no_contact_source")
    args.used = [_sources(backend)[0] for backend, _ in found]
    return found


def _public(value, field=""):
    if isinstance(value, dict):
        return {key: _public(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item, field) for item in value]
    if isinstance(value, str):
        if field != "note":
            value = one_line(value)
        if field == "note" or len(value) > 200:
            value = frame_untrusted(value, "contact")
    return value


def _hint(ctx):
    username = one_line(ctx.env.get("CARDDAV_USERNAME", ""))
    return f"Share an address book with the bot account {username} from the Nextcloud Contacts app."


def _collect(args, *, query=None):
    found = _available(args)
    result = []
    errors = []
    answered = 0
    extra = {}
    for backend, ctx in found:
        if getattr(args, "source", None) and args.source not in _sources(backend):
            continue
        failures = []
        try:
            if query is None:
                items = backend.books(ctx)
            elif backend is google:
                items = backend.search(ctx, query, limit=100, source=getattr(args, "source", None))
            else:
                items = backend.search(ctx, query, limit=100)
            if backend is carddav and not items and (query is None or not backend.books(ctx)):
                extra["hint"] = _hint(ctx)
            answered += 1
            result.extend(item for item in items if not getattr(args, "source", None) or item.source == args.source)
            if getattr(ctx, "skipped_cards", 0):
                extra["skipped_cards"] = extra.get("skipped_cards", 0) + ctx.skipped_cards
            if getattr(ctx, "truncated_books", []):
                extra.setdefault("truncated_books", []).extend(ctx.truncated_books)
        except Exception as exc:
            if not getattr(ctx, "errors", []):
                failures.append((getattr(args, "source", None) or _sources(backend)[0], exc))
        failures.extend(getattr(ctx, "errors", []))
        for source, exc in failures:
            detail = _error(exc)
            errors.append({"source": source,
                           **{key: detail[key] for key in ("reason", "status_code", "needs", "error") if key in detail}})
    if not answered:
        if errors:
            detail = dict(errors[0])
            detail.pop("source")
            return {"status": "error", **detail, "errors": errors}
        return error_envelope("no_contact_source", reason="no_contact_source", errors=errors)
    if query is not None:
        result.sort(key=lambda c: (-match_score(c, query), c.name.casefold(), c.id))
        result = result[:args.limit]
    args.result_count = len(result)
    return {"status": "ok", "contacts" if query is not None else "books": [_public(asdict(item)) for item in result],
            "partial": bool(errors), "errors": errors, **extra}


def cmd_books(args):
    return _collect(args)


def cmd_search(args):
    if not args.query.strip():
        return error_envelope("A search query is required", reason="empty_query")
    if not 1 <= args.limit <= 100:
        return error_envelope("Limit must be between 1 and 100", reason="invalid_limit")
    logger.debug("contacts query=%r", args.query)
    return _collect(args, query=args.query)


def _for_id(args):
    prefix = args.id.split(":", 1)[0]
    source = {"nc": "nextcloud", "google": "google", "google-other": "google-other"}.get(prefix)
    for backend, ctx in _available(args):
        if source in _sources(backend):
            return backend, ctx
    raise carddav.CardDAVError("not_found")


def _contact_result(args, contact):
    if contact is None:
        raise carddav.CardDAVError("not_found")
    args.result_count = 1
    return {"status": "ok", "contact": _public(asdict(contact))}


def cmd_show(args):
    backend, ctx = _for_id(args)
    return _contact_result(args, backend.get(ctx, args.id))


def _field(value):
    text, separator, kind = value.rpartition(":")
    return ContactField(text, kind.lower() or None) if separator else ContactField(value)


def _input(args):
    fields = ContactInput()
    for name in ("name", "given", "family", "nickname", "org", "title", "birthday", "note"):
        value = getattr(args, name, None)
        if getattr(args, "clear_" + name, False):
            value = ""
        setattr(fields, name, value)
    for arg, dest in (("email", "emails"), ("phone", "phones"), ("add_email", "add_emails"), ("add_phone", "add_phones")):
        setattr(fields, dest, [_field(value) for value in getattr(args, arg, [])])
    fields.remove_emails = getattr(args, "remove_email", [])
    fields.remove_phones = getattr(args, "remove_phone", [])
    return fields


def cmd_create(args):
    fields = _input(args)
    if not fields.name or not fields.name.strip():
        raise ValueError("name_required")
    source = args.book if args.book in {"google", "google-other"} else "nextcloud"
    for backend, ctx in _available(args):
        if source in _sources(backend):
            return _contact_result(args, backend.create(ctx, args.book, fields))
    raise carddav.CardDAVError("not_found")


def cmd_update(args):
    fields = _input(args)
    if all(value is None or value == [] for value in asdict(fields).values()):
        raise ValueError("nothing_to_change")
    backend, ctx = _for_id(args)
    return _contact_result(args, backend.update(ctx, args.id, fields, etag=args.etag))


def cmd_delete(args):
    backend, ctx = _for_id(args)
    backend.delete(ctx, args.id, etag=args.etag)
    return {"status": "ok"}


def build_parser():
    parser = argparse.ArgumentParser(prog="istota-skill contacts", description="Look up and edit contacts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("books", help="List available address books")
    search = sub.add_parser("search", help="Search contacts")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--source", choices=("nextcloud", "google", "google-other"))
    sub.add_parser("show", help="Show a contact").add_argument("id")
    create = sub.add_parser("create", help="Create a contact")
    create.add_argument("--book", required=True)
    update = sub.add_parser("update", help="Update a contact")
    update.add_argument("id")
    update.add_argument("--etag")
    delete = sub.add_parser("delete", help="Delete a contact")
    delete.add_argument("id")
    delete.add_argument("--etag")
    for target in (create, update):
        for name in ("name", "given", "family", "nickname", "org", "title", "birthday", "note"):
            group = target.add_mutually_exclusive_group(required=target is create and name == "name")
            group.add_argument("--" + name)
            if target is update and name in ("org", "title", "birthday", "note"):
                group.add_argument("--clear-" + name, action="store_true")
        for name in (("email", "phone") if target is create else ("add-email", "remove-email", "add-phone", "remove-phone")):
            target.add_argument("--" + name, action="append", default=[])
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    started = time.monotonic()
    try:
        run_skill_cli({"books": cmd_books, "search": cmd_search, "show": cmd_show,
                       "create": cmd_create, "update": cmd_update, "delete": cmd_delete},
                      args, on_exception=_error)
    finally:
        logger.info("contacts verb=%s backends=%s count=%s duration=%.3f", args.command,
                    ",".join(getattr(args, "used", [])), getattr(args, "result_count", 0), time.monotonic() - started)


def setup_env(ctx) -> dict[str, str]:
    from istota import db
    from istota.credentials.google_scopes import levels_from_scopes
    from istota.credentials.google_tokens import access_token_for

    if not ctx.config.google_workspace.enabled:
        return {}
    with db.get_db(ctx.config.db_path) as conn:
        scopes = db.get_google_scopes(conn, ctx.task.user_id) or []
        if "contacts" not in levels_from_scopes(scopes):
            return {}
        token = access_token_for(conn, ctx.config, ctx.task.user_id)
    if not token:
        return {}
    return {"GOOGLE_CONTACTS_TOKEN": token, "GOOGLE_CONTACTS_SCOPES": " ".join(scopes)}
