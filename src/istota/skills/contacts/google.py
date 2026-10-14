"""Google Contacts via People API. No contact cache or in-task token refresh."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import re
from time import sleep

import httpx

from .model import AddressBook, ContactInput, contact_from_person, person_from_input, match_score

BASE_URL = "https://people.googleapis.com/v1/"
SCOPE = "https://www.googleapis.com/auth/contacts"
READ_FIELDS = "metadata,names,nicknames,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies"
OTHER_FIELDS = "names,emailAddresses,phoneNumbers"
MAX_OTHER_PAGES = 10


@dataclass
class BackendContext:
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    client: httpx.Client | None = field(default=None, repr=False)
    errors: list[tuple[str, "GoogleError"]] = field(default_factory=list)


class GoogleError(Exception):
    def __init__(self, reason, status_code=None, *, needs=None):
        self.reason = reason
        self.status_code = status_code
        self.needs = needs
        super().__init__(reason)


def _scopes(ctx):
    return set(ctx.env.get("GOOGLE_CONTACTS_SCOPES", "").split())


def available(ctx):
    return bool(ctx.env.get("GOOGLE_CONTACTS_TOKEN") and
                _scopes(ctx) & {SCOPE, SCOPE + ".readonly", SCOPE + ".other.readonly"})


def books(ctx):
    grants = _scopes(ctx)
    result = []
    if grants & {SCOPE, SCOPE + ".readonly"}:
        result.append(AddressBook("google", "google", "Google Contacts", SCOPE in grants))
    if SCOPE + ".other.readonly" in grants:
        result.append(AddressBook("google-other", "google-other", "Other contacts", False))
    return result


def _request(ctx, method, path, *, params=None, body=None):
    if not ctx.env.get("ISTOTA_USER_ID", "").strip():
        raise GoogleError("no_user")
    if not ctx.env.get("GOOGLE_CONTACTS_TOKEN"):
        raise GoogleError("no_contact_source")
    headers = {"Authorization": "Bearer " + ctx.env["GOOGLE_CONTACTS_TOKEN"]}
    try:
        if ctx.client is not None:
            response = ctx.client.request(method, BASE_URL + path, params=params, json=body, headers=headers, follow_redirects=False)
        else:
            with httpx.Client(timeout=20.0, follow_redirects=False, trust_env=False) as client:
                response = client.request(method, BASE_URL + path, params=params, json=body, headers=headers)
    except httpx.HTTPError:
        raise GoogleError("unreachable") from None
    status = response.status_code
    if not 200 <= status < 300:
        try:
            error = response.json().get("error", {})
            reasons = {error.get("status")}
            for item in error.get("details", []) + error.get("errors", []):
                reasons.add(item.get("reason"))
        except (ValueError, AttributeError, TypeError):
            reasons = set()
        reason = "request_failed"
        needs = None
        if status == 401:
            reason = "google_token_invalid"
        elif status == 403 and reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}:
            reason = "google_scope_insufficient"
            needs = "contacts read" if method == "GET" else "contacts read-write"
        elif status == 403 and reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
            reason = "google_api_disabled"
        elif status == 403:
            reason = "read_only"
        elif status == 404:
            reason = "not_found"
        elif status == 412 or (status == 400 and reasons & {"FAILED_PRECONDITION", "failedPrecondition"}):
            reason = "changed_since_read"
        elif status == 429:
            reason = "rate_limited"
        elif status >= 500:
            reason = "server_error"
        raise GoogleError(reason, status, needs=needs)
    if method == "DELETE":
        return {}
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise GoogleError("invalid_response", status) from None


def search(ctx, query, *, limit, source=None):
    if not query.strip():
        raise GoogleError("empty_query")
    ctx.errors.clear()
    selected = [book for book in books(ctx) if source is None or book.source == source]
    if not selected:
        raise GoogleError("no_contact_source")
    ready = []
    for book in selected:
        other = book.source == "google-other"
        path = "otherContacts:search" if other else "people:searchContacts"
        params = {"readMask": OTHER_FIELDS if other else READ_FIELDS, "pageSize": 30, "query": ""}
        try:
            _request(ctx, "GET", path, params=params)
            ready.append((book, path, params))
        except GoogleError as exc:
            ctx.errors.append((book.source, exc))
    # Google's search cache refreshes lazily after the warm-up request.
    if ready:
        sleep(5)
    result = []
    answered = 0
    for book, path, params in ready:
        params["query"] = query
        try:
            data = _request(ctx, "GET", path, params=params)
            for item in data.get("results", []):
                result.append(contact_from_person(item["person"], source=book.source, writable=book.writable))
            answered += 1
        except GoogleError as exc:
            ctx.errors.append((book.source, exc))
    if not answered and ctx.errors:
        raise ctx.errors[0][1]
    result.sort(key=lambda c: (-match_score(c, query), c.name.casefold(), c.id))
    return result[:limit]


def _resource(contact_id):
    match = re.fullmatch(r"(google|google-other):(people|otherContacts)/([A-Za-z0-9_-]+)", contact_id)
    if not match or (match[1] == "google") != (match[2] == "people"):
        raise GoogleError("not_found")
    return match[1], match[2] + "/" + match[3]


def _person(ctx, contact_id):
    source, resource = _resource(contact_id)
    if source not in {book.source for book in books(ctx)}:
        raise GoogleError("google_scope_insufficient", needs="contacts read")
    if source == "google":
        try:
            return _request(ctx, "GET", resource, params={"personFields": READ_FIELDS, "sources": "READ_SOURCE_TYPE_CONTACT"})
        except GoogleError as exc:
            if exc.reason == "not_found":
                return None
            raise
    # Other contacts have no single-resource GET; keep only the requested row.
    params = {"readMask": OTHER_FIELDS, "pageSize": 1000}
    seen = set()
    for _ in range(MAX_OTHER_PAGES):
        data = _request(ctx, "GET", "otherContacts", params=params)
        for person in data.get("otherContacts", []):
            if person.get("resourceName") == resource:
                return person
        token = data.get("nextPageToken")
        if not token:
            return None
        if token in seen:
            break
        seen.add(token)
        params["pageToken"] = token
    raise GoogleError("lookup_limit")


def get(ctx, contact_id):
    source, _ = _resource(contact_id)
    person = _person(ctx, contact_id)
    return contact_from_person(person, source=source, writable=SCOPE in _scopes(ctx)) if person else None


def _require_write(ctx, source):
    if source == "google-other" or SCOPE not in _scopes(ctx):
        raise GoogleError("read_only")


def create(ctx, book_key, fields: ContactInput):
    if book_key not in {"google", "google-other"}:
        raise GoogleError("not_found")
    _require_write(ctx, book_key)
    if not fields.name or not fields.name.strip():
        raise ValueError("name_required")
    person = _request(ctx, "POST", "people:createContact",
                      params={"personFields": READ_FIELDS}, body=person_from_input(fields))
    return contact_from_person(person, source="google", writable=True)


def _before_write(ctx, contact_id, etag):
    source, resource = _resource(contact_id)
    _require_write(ctx, source)
    person = _person(ctx, contact_id)
    if person is None:
        raise GoogleError("not_found")
    sources = [item for item in person.get("metadata", {}).get("sources", []) if item.get("type") == "CONTACT"]
    if not sources or not sources[0].get("etag"):
        raise GoogleError("read_only")
    if etag is not None and etag != sources[0]["etag"]:
        raise GoogleError("changed_since_read")
    return resource, person, sources


def update(ctx, contact_id, changes: ContactInput, *, etag=None):
    resource, person, sources = _before_write(ctx, contact_id, etag)
    body = person_from_input(changes, person)
    mask = ",".join(body)
    body.update(resourceName=resource, etag=person.get("etag"), metadata={"sources": sources})
    result = _request(ctx, "PATCH", resource + ":updateContact",
                      params={"updatePersonFields": mask, "personFields": READ_FIELDS}, body=body)
    return contact_from_person(result, source="google", writable=True)


def delete(ctx, contact_id, *, etag=None):
    resource, _, _ = _before_write(ctx, contact_id, etag)
    # People API has no conditional delete; this preflight cannot close the race.
    _request(ctx, "DELETE", resource + ":deleteContact")
