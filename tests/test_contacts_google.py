"""People API requests and scope enforcement at the HTTP boundary."""
import json
from types import SimpleNamespace

import httpx
import pytest

from istota import db
from istota.config import Config
from istota.skills import contacts
from istota.skills.contacts import google
from istota.skills.contacts.model import ContactField, ContactInput
from istota.skills._loader import effective_disabled_skills, load_skill_index

BASE_SCOPE = "https://www.googleapis.com/auth/"
PERSON = {
    "resourceName": "people/c123", "etag": "person-etag",
    "metadata": {"sources": [{"type": "CONTACT", "id": "123", "etag": "source-etag"}]},
    "names": [{"displayName": "Alice Example", "givenName": "Alice", "familyName": "Example"}],
    "emailAddresses": [{"value": "alice@example.com", "type": "work"}],
    "organizations": [{"name": "Example Corp", "title": "Engineer", "department": "Research"}],
    "biographies": [{"value": "Untrusted note", "contentType": "TEXT_PLAIN"}],
    "birthdays": [{"date": {"month": 1, "day": 15}}],
}
OTHER = {"resourceName": "otherContacts/c456", "names": [{"displayName": "Alice Other"}]}


def context(handler, scopes=("contacts", "contacts.other.readonly")):
    return google.BackendContext(env={
        "ISTOTA_USER_ID": "alice", "GOOGLE_CONTACTS_TOKEN": "test-token",
        "GOOGLE_CONTACTS_SCOPES": " ".join(BASE_SCOPE + s for s in scopes),
    }, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_search_warms_each_source_and_normalizes(monkeypatch):
    calls = []
    def handler(req):
        calls.append(req)
        assert req.headers["authorization"] == "Bearer test-token"
        person = OTHER if req.url.path.endswith("otherContacts:search") else PERSON
        return httpx.Response(200, json={"results": [{"person": person}]})
    monkeypatch.setattr(google, "sleep", lambda seconds: None, raising=False)
    ctx = context(handler)
    assert [(b.key, b.writable) for b in google.books(ctx)] == [("google", True), ("google-other", False)]
    found = google.search(ctx, "Alice", limit=20)
    assert [(r.url.path, r.url.params["query"]) for r in calls] == [
        ("/v1/people:searchContacts", ""), ("/v1/otherContacts:search", ""),
        ("/v1/people:searchContacts", "Alice"), ("/v1/otherContacts:search", "Alice"),
    ]
    assert found[0].id == "google:people/c123" and found[0].etag == "source-etag"
    assert found[0].birthday == "--01-15" and found[0].emails == [ContactField("alice@example.com", "work")]
    assert found[1].source == "google-other" and not found[1].writable


@pytest.mark.parametrize("scopes,keys", [(('contacts.readonly',), ['google']), (('contacts.other.readonly',), ['google-other']), ((), [])])
def test_partial_grants_and_local_write_refusal(scopes, keys):
    calls = []
    ctx = context(lambda req: calls.append(req) or httpx.Response(200, json={}), scopes)
    assert [b.key for b in google.books(ctx)] == keys
    assert google.available(ctx) == bool(keys)
    for action in (lambda: google.create(ctx, "google", ContactInput(name="Alice")),
                   lambda: google.update(ctx, "google:people/c123", ContactInput(note="Hi"), etag=None),
                   lambda: google.delete(ctx, "google:people/c123", etag=None)):
        with pytest.raises(google.GoogleError, match="read_only"):
            action()
    assert not calls


def test_other_contacts_write_refused_even_with_full_scope():
    ctx = context(lambda req: pytest.fail("write must be refused before HTTP"))
    for action in (lambda: google.create(ctx, "google-other", ContactInput(name="Alice")),
                   lambda: google.update(ctx, "google-other:otherContacts/c456", ContactInput(note="Hi"), etag=None),
                   lambda: google.delete(ctx, "google-other:otherContacts/c456", etag=None)):
        with pytest.raises(google.GoogleError, match="read_only"):
            action()


def test_create_update_delete_requests():
    calls = []
    ctx = context(lambda req: calls.append(req) or httpx.Response(200, json=PERSON if req.method != "DELETE" else {}))
    google.create(ctx, "google", ContactInput(name="Alice Example", emails=[ContactField("alice@example.com", "work")]))
    assert calls[-1].method == "POST" and calls[-1].url.path == "/v1/people:createContact"
    assert json.loads(calls[-1].content)["names"] == [{"unstructuredName": "Alice Example"}]
    google.update(ctx, "google:people/c123", ContactInput(add_emails=[ContactField("other@example.com")], title="Manager", note=""), etag="source-etag")
    req = calls[-1]
    assert req.method == "PATCH"
    assert set(req.url.params["updatePersonFields"].split(",")) == {"emailAddresses", "organizations", "biographies"}
    body = json.loads(req.content)
    assert body["metadata"]["sources"] == PERSON["metadata"]["sources"]
    assert body["etag"] == "person-etag"
    assert body["organizations"] == [{"name": "Example Corp", "title": "Manager", "department": "Research"}]
    assert len(body["emailAddresses"]) == 2 and body["biographies"] == []
    assert "names" not in body
    google.delete(ctx, "google:people/c123", etag="source-etag")
    assert calls[-2].method == "GET" and calls[-1].method == "DELETE"
    assert not calls[-1].content and not calls[-1].url.query


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_stale_etag_refused_before_mutation(operation):
    calls = []
    ctx = context(lambda req: calls.append(req) or httpx.Response(200, json=PERSON))
    with pytest.raises(google.GoogleError, match="changed_since_read"):
        if operation == "update":
            google.update(ctx, "google:people/c123", ContactInput(note="Hi"), etag="stale")
        else:
            google.delete(ctx, "google:people/c123", etag="stale")
    assert [r.method for r in calls] == ["GET"]


def test_show_other_contact_pages_without_a_name_cache(monkeypatch):
    calls = []
    def handler(req):
        calls.append(req)
        assert req.url.path == "/v1/otherContacts"
        return httpx.Response(200, json={"otherContacts": [OTHER]} if "pageToken" in req.url.params else {"nextPageToken": "page-two"})
    ctx = context(handler)
    assert google.get(ctx, "google-other:otherContacts/c456").id == "google-other:otherContacts/c456"
    assert calls[-1].url.params["pageToken"] == "page-two"
    assert google.get(ctx, "google-other:otherContacts/missing") is None
    monkeypatch.setattr(google, "MAX_OTHER_PAGES", 1)
    with pytest.raises(google.GoogleError, match="lookup_limit"):
        google.get(ctx, "google-other:otherContacts/c456")


@pytest.mark.parametrize("ident", ["google:people/../me", "google:people/x?key=y", "google:https://example.com", "google-other:people/c123", "google:people/%2f"])
def test_invalid_id_never_sends_a_token(ident):
    ctx = context(lambda req: pytest.fail("invalid id reached HTTP"))
    with pytest.raises(google.GoogleError, match="not_found"):
        google.get(ctx, ident)


@pytest.mark.parametrize("status,body,reason", [
    (401, {}, "google_token_invalid"),
    (403, {"details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}, "google_scope_insufficient"),
    (403, {"errors": [{"reason": "insufficientPermissions"}]}, "google_scope_insufficient"),
    (403, {"details": [{"reason": "SERVICE_DISABLED"}]}, "google_api_disabled"),
    (400, {"status": "FAILED_PRECONDITION"}, "changed_since_read"),
    (400, {"errors": [{"reason": "failedPrecondition"}]}, "changed_since_read"),
    (429, {}, "rate_limited"), (503, {}, "server_error"), (404, {}, "not_found"),
])
def test_error_mapping(status, body, reason):
    body["message"] = "private server detail"
    ctx = context(lambda req: httpx.Response(status, json={"error": body}))
    with pytest.raises(google.GoogleError) as caught:
        google.create(ctx, "google", ContactInput(name="Alice"))
    envelope = contacts._error(caught.value)
    assert envelope["reason"] == reason and envelope["status_code"] == status
    assert "private server detail" not in json.dumps(envelope)
    if reason == "google_scope_insufficient":
        assert envelope["needs"] == "contacts read-write"


def test_network_failure_is_sanitized():
    def handler(req):
        raise httpx.ConnectError("private address", request=req)
    with pytest.raises(google.GoogleError, match="unreachable"):
        google.get(context(handler), "google:people/c123")


@pytest.mark.parametrize("grant,enabled", [('contacts.readonly', True), ('contacts.other.readonly', True), ('contacts', True), ('drive.readonly', False)])
def test_setup_and_availability_use_stored_grants(tmp_path, monkeypatch, grant, enabled):
    from istota.credentials import google_tokens
    from istota.skills import google_workspace
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    cfg = Config(db_path=tmp_path / "test.db")
    cfg.google_workspace.enabled = True
    db.init_db(cfg.db_path)
    with db.get_db(cfg.db_path) as conn:
        db.upsert_google_token(conn, "alice", "test-token", "test-refresh", "2099-01-01T00:00:00", json.dumps([BASE_SCOPE + grant]))
    calls = []
    monkeypatch.setattr(google_tokens, "access_token_for", lambda conn, config, user: calls.append(user) or "test-token")
    ctx = SimpleNamespace(config=cfg, task=SimpleNamespace(user_id="alice"), user_temp_dir=tmp_path)
    env = contacts.setup_env(ctx)
    assert bool(env) == enabled
    assert calls == (["alice"] if enabled else [])
    if enabled:
        assert env["GOOGLE_CONTACTS_SCOPES"] == BASE_SCOPE + grant
    assert google_workspace.setup_env(ctx)["GOOGLE_WORKSPACE_CLI_TOKEN"] == "test-token"
    index = load_skill_index(tmp_path)
    assert ("contacts" not in effective_disabled_skills(cfg, "alice", index)) == enabled
    assert "contacts" in effective_disabled_skills(cfg, "bob", index)
    assert "contacts" in effective_disabled_skills(cfg, "alice", index, withheld_scopes={"contacts"})
    cfg.google_workspace.enabled = False
    assert contacts.setup_env(ctx) == {}
    specs = {s.var: s for s in index["contacts"].env_specs}
    assert specs["GOOGLE_CONTACTS_TOKEN"].sensitive
    assert specs["GOOGLE_CONTACTS_SCOPES"].sensitive


def test_search_waits_after_warming_sources(monkeypatch):
    events = []
    def handler(req):
        events.append((req.url.path, req.url.params["query"]))
        return httpx.Response(200, json={"results": []})
    monkeypatch.setattr(google, "sleep", lambda seconds: events.append(("sleep", seconds)), raising=False)
    assert google.search(context(handler), "Alice", limit=20) == []
    assert events == [
        ("/v1/people:searchContacts", ""), ("/v1/otherContacts:search", ""),
        ("sleep", 5),
        ("/v1/people:searchContacts", "Alice"), ("/v1/otherContacts:search", "Alice"),
    ]
