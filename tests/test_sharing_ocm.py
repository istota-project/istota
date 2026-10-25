"""Receiving an Open Cloud Mesh folder share: `sharing/ocm.py` and its routes.

`POST /ocm/shares` is the web app's first fully unauthenticated public route,
so the properties held here are the refusal ones: every refusal writes
nothing (read with a watermark on `shared_sources`, `secrets` and
`notifications` plus a column filter), the steps run in a fixed order (over a
rate limit, no outbound fetch at all), and an unknown user answers exactly as
an unknown host does, and only once the sender is verified.

The sender is a stdlib server on loopback, named in `[sharing] private_hosts`,
serving a Nextcloud-shaped `/ocm-provider/` document. The offer bodies are the
OCM 1.0 shape Nextcloud 30 and 31 were observed to send (Stage 1's
`stage-1-captures.md`), with example names; the signatures are made here with
a key generated per test, as Nextcloud 31 makes them (draft-cavage, rsa-sha512
over `(request-target) content-length date digest host`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from email.utils import formatdate
from pathlib import Path
from unittest import mock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from istota import db
from istota.config import Config, SharingConfig, SharingOcmConfig, SiteConfig, UserConfig
from istota.credentials import store as secrets_store
from istota.sharing import ocm, sources
from tests.test_sharing_ics import _Server

SITE = "bot.example.com"
SECRET = "synthetic-share-secret-7f3a"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def secret_key_env():
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


@pytest.fixture(autouse=True)
def fresh_limits():
    ocm.reset_limits()
    yield
    ocm.reset_limits()


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


class Sender:
    """A Nextcloud-shaped OCM sender on loopback."""

    def __init__(self) -> None:
        self.server = _Server()
        self.port = self.server.port
        self.host = f"127.0.0.1:{self.port}"
        self.public_key_pem: str | None = None
        self.document: dict | None = None
        self.publish()

    def discovery(self) -> dict:
        document = {
            "enabled": True,
            "apiVersion": "1.0-proposal1",
            "version": "1.1.0",
            "endPoint": f"http://{self.host}/ocm",
            "resourceTypes": [
                {"name": "file", "shareTypes": ["user", "group"],
                 "protocols": {"webdav": "/public.php/webdav/"}},
            ],
        }
        if self.public_key_pem:
            document["publicKey"] = {
                "keyId": f"https://{self.host}/ocm#signature",
                "publicKeyPem": self.public_key_pem,
            }
        return document

    def publish(self, document: dict | None = None, status: int = 200) -> None:
        self.document = document if document is not None else self.discovery()
        payload = json.dumps(self.document).encode()
        self.server.routes["/ocm-provider/"] = (status, {"Content-Type": "application/json"}, payload)

    @property
    def fetches(self) -> int:
        return sum(1 for r in self.server.received if r["path"] == "/ocm-provider/")

    def offer(self, **overrides) -> dict:
        """The OCM 1.0 offer body Nextcloud sends, with this sender as owner."""
        body = {
            "shareWith": f"alice@https://{SITE}",
            "shareType": "user",
            "name": "Taxes 2026",
            "resourceType": "file",
            "description": "",
            "providerId": 5,
            "owner": f"carol@http://{self.host}",
            "ownerDisplayName": "carol",
            "sharedBy": f"carol@http://{self.host}",
            "sharedByDisplayName": "carol",
            "protocol": {
                "name": "webdav",
                "options": {
                    "sharedSecret": SECRET,
                    "permissions": "{http://open-cloud-mesh.org/ns}share-permissions",
                },
            },
        }
        body.update(overrides)
        return body

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def sender():
    s = Sender()
    yield s
    s.close()


@pytest.fixture
def config(db_path):
    return Config(
        db_path=db_path,
        site=SiteConfig(hostname=SITE),
        users={"alice": UserConfig(display_name="Alice Example")},
        sharing=SharingConfig(private_hosts=["127.0.0.1"], ocm=SharingOcmConfig(enabled=True)),
    )


def _headers(body: bytes) -> dict:
    return {"host": SITE, "content-type": "application/json", "content-length": str(len(body))}


def _sign(key, body: bytes, *, key_id: str, path: str = "/ocm/shares",
          algorithm: str = "rsa-sha512", date: str | None = None, digest: str | None = None,
          signed: str = "(request-target) content-length date digest host") -> dict:
    """Headers for ``body`` signed as Nextcloud 31 signs an offer."""
    sha = hashlib.sha512 if algorithm == "rsa-sha512" else hashlib.sha256
    label = "SHA-512" if algorithm == "rsa-sha512" else "SHA-256"
    headers = _headers(body)
    headers["date"] = date or formatdate(usegmt=True)
    headers["digest"] = digest or f"{label}=" + base64.b64encode(sha(body).digest()).decode()
    lines = []
    for name in signed.split():
        if name == "(request-target)":
            lines.append(f"(request-target): post {path}")
        else:
            lines.append(f"{name}: {headers[name]}")
    hash_algo = hashes.SHA512() if algorithm == "rsa-sha512" else hashes.SHA256()
    signature = key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hash_algo)
    headers["signature"] = (
        f'keyId="{key_id}",algorithm="{algorithm}",headers="{signed}",'
        f'signature="{base64.b64encode(signature).decode()}"'
    )
    return headers


def _receive(config, body, headers=None, **kwargs):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    with db.get_db(config.db_path) as conn:
        return ocm.receive_share(conn, config, raw, headers or _headers(raw), **kwargs)


WATCHED = {
    "shared_sources": "kind = 'ocm_share'",
    "secrets": "service = 'shared_source'",
    "notifications": "source = 'shared_source'",
}


def _watermark(config) -> dict:
    with db.get_db(config.db_path) as conn:
        return {
            table: conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            for table in WATCHED
        }


def _written_since(config, mark) -> dict:
    with db.get_db(config.db_path) as conn:
        return {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE id > ? AND {where}", (mark[table],),
            ).fetchone()[0]
            for table, where in WATCHED.items()
        }


NOTHING = {"shared_sources": 0, "secrets": 0, "notifications": 0}


def _offers(config, user="alice"):
    with db.get_db(config.db_path) as conn:
        return sources.sources_for_user(conn, user, kinds=("ocm_share",), states=sources.STATES)


def _read_secret(config, user, key):
    with db.get_db(config.db_path) as conn:
        return secrets_store.get_secret(None, user, sources.SECRET_SERVICE, key,
                                        connection=conn, touch=False)


# ------------------------------------------------------------------ discovery

class TestDiscovery:
    def test_the_document_nextcloud_reads(self, config):
        document = ocm.discovery_document(config)
        assert document["enabled"] is True
        assert document["apiVersion"] == "1.1.0"
        assert document["endPoint"] == f"https://{SITE}/ocm"
        assert document["provider"]
        assert document["resourceTypes"] == [
            {"name": "file", "shareTypes": ["user"], "protocols": {"webdav": "/webdav/"}},
        ]
        assert "publicKey" not in document


class TestParseCloudId:
    @pytest.mark.parametrize("raw, user, scheme, host, path", [
        ("alice@https://cloud.example.org", "alice", "https", "cloud.example.org", ""),
        ("carol@http://nc30.example.org", "carol", "http", "nc30.example.org", ""),
        ("alice@share.example.com:8443", "alice", None, "share.example.com:8443", ""),
        ("a.b@c@https://Cloud.Example.org/nextcloud", "a.b@c", "https", "cloud.example.org", "/nextcloud"),
        ("alice@https://cloud.example.org/", "alice", "https", "cloud.example.org", ""),
    ])
    def test_split_at_the_last_at_with_the_scheme_kept(self, raw, user, scheme, host, path):
        cloud = ocm.parse_cloud_id(raw)
        assert (cloud.user, cloud.scheme, cloud.host, cloud.path) == (user, scheme, host, path)

    @pytest.mark.parametrize("raw", [
        "", "alice", "@cloud.example.org", "alice@", "alice@https://", "alice @x.example",
        "alice@ftp://cloud.example.org", "alice@https://user:pw@cloud.example.org",
        "alice@cloud.example.org:notaport", 7, None, "alice@cloud.example.org?x=1",
    ])
    def test_malformed(self, raw):
        assert ocm.parse_cloud_id(raw) is None


# ---------------------------------------------------------------- the routes

def _ocm_client(config):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from istota.sharing import ocm_routes

    app = FastAPI()
    app.include_router(ocm_routes.router)
    app.dependency_overrides[ocm_routes.current_config] = lambda: config
    app.dependency_overrides[ocm_routes.client_address] = lambda: None
    return TestClient(app, base_url=f"https://{SITE}")


class TestRoutes:
    @pytest.mark.parametrize("path", ["/ocm-provider", "/ocm-provider/", "/.well-known/ocm"])
    def test_discovery_served_without_a_redirect(self, config, path):
        with _ocm_client(config) as client:
            response = client.get(path, follow_redirects=False)
        assert response.status_code == 200
        body = response.json()
        assert body["apiVersion"] and body["endPoint"] == f"https://{SITE}/ocm"
        assert "publicKey" not in body

    @pytest.mark.parametrize("method, path", [
        ("GET", "/ocm-provider"), ("GET", "/ocm-provider/"), ("GET", "/.well-known/ocm"),
        ("POST", "/ocm/shares"), ("POST", "/ocm/notifications"),
    ])
    def test_every_ocm_path_is_404_when_off(self, config, sender, method, path):
        config.sharing.ocm.enabled = False
        mark = _watermark(config)
        with _ocm_client(config) as client:
            response = client.request(method, path, json=sender.offer())
        assert response.status_code == 404
        assert sender.fetches == 0
        assert _written_since(config, mark) == NOTHING

    def test_an_offer_through_the_route(self, config, sender):
        with _ocm_client(config) as client:
            response = client.post("/ocm/shares", json=sender.offer())
        assert response.status_code == 201, response.text
        assert response.json() == {"recipientDisplayName": "Alice Example"}
        assert response.headers["cache-control"] == "no-store"
        assert [o.state for o in _offers(config)] == ["pending"]

    def test_an_oversize_body_is_refused_before_it_is_read_whole(self, config, sender):
        mark = _watermark(config)
        body = json.dumps(sender.offer(description="x" * (ocm.OFFER_MAX_BYTES + 1))).encode()
        with _ocm_client(config) as client:
            response = client.post("/ocm/shares", content=body,
                                   headers={"Content-Type": "application/json"})
        assert response.status_code in (400, 413)
        assert sender.fetches == 0
        assert _written_since(config, mark) == NOTHING

    async def test_the_web_app_serves_them_at_the_root(self, tmp_path):
        """The router is in the real app, ahead of the static mount, and gated per request."""
        from httpx import ASGITransport, AsyncClient

        from tests.test_web_app import _make_config, _patch_app

        cfg = _make_config(tmp_path)
        app = _patch_app(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as c:
            assert (await c.get("/ocm-provider/", follow_redirects=False)).status_code == 404
            cfg.sharing.ocm.enabled = True
            answer = await c.get("/ocm-provider/", follow_redirects=False)
        assert answer.status_code == 200
        assert answer.json()["endPoint"] == "https://example.com/ocm"


# ---------------------------------------------------------- receiving: happy

class TestTheCapturedOffer:
    def test_the_nextcloud_30_body_unsigned(self, config, sender):
        answer = _receive(config, sender.offer())
        assert (answer.status, answer.body) == (201, {"recipientDisplayName": "Alice Example"})
        [offer] = _offers(config)
        assert offer.state == "pending"
        assert offer.access == "read"
        assert offer.remote_host == sender.host
        assert offer.remote_key == f"{sender.host}|5"
        assert offer.extras["webdav"] == f"http://{sender.host}/public.php/webdav/"
        assert offer.extras["name"] == "Taxes 2026"
        assert SECRET not in json.dumps(offer.extras)
        assert _read_secret(config, "alice", str(offer.id)) == SECRET
        assert sender.fetches == 1

    def test_a_string_provider_id_is_the_same_share(self, config, sender):
        _receive(config, sender.offer(providerId=5))
        _receive(config, sender.offer(providerId="5"))
        assert [o.remote_key for o in _offers(config)] == [f"{sender.host}|5"]

    def test_empty_and_absent_display_names_are_accepted(self, config, sender):
        body = sender.offer(ownerDisplayName="", sharedByDisplayName="")
        del body["description"]
        assert _receive(config, body).status == 201

    def test_the_user_part_matches_the_stored_spelling(self, config, sender):
        answer = _receive(config, sender.offer(shareWith=f"Alice@https://{SITE}"))
        assert answer.status == 201
        assert [o.user_id for o in _offers(config)] == ["alice"]


# ------------------------------------------------------- receiving: refusals

def _bad(sender, **changes):
    body = sender.offer()
    for path, value in changes.items():
        target = body
        parts = path.split("__")
        for part in parts[:-1]:
            target = target[part]
        if value is _DROP:
            del target[parts[-1]]
        else:
            target[parts[-1]] = value
    return body


_DROP = object()


class TestRefusalsWriteNothing:
    @pytest.mark.parametrize("change", [
        {"resourceType": "calendar"},
        {"shareType": "group"},
        {"protocol__name": "multi"},
        {"protocol__options__sharedSecret": _DROP},
        {"protocol__options__sharedSecret": ""},
        {"shareWith": _DROP},
        {"owner": _DROP},
        {"sharedBy": 7},
        {"providerId": _DROP},
        {"providerId": True},
        {"providerId": -1},
        {"name": "x" * 256},
        {"description": "x" * 256},
    ], ids=lambda c: next(iter(c)))
    def test_a_malformed_body_is_400(self, config, sender, change):
        mark = _watermark(config)
        answer = _receive(config, _bad(sender, **change))
        assert answer.status == 400
        assert set(answer.body) == {"message", "validationErrors"}
        assert sender.fetches == 0
        assert _written_since(config, mark) == NOTHING

    @pytest.mark.parametrize("raw", [b"not json", b"[]", b'"a string"', b"{" * 5000])
    def test_a_body_that_is_not_an_object_is_400(self, config, sender, raw):
        mark = _watermark(config)
        assert _receive(config, raw).status == 400
        assert _written_since(config, mark) == NOTHING

    def test_no_refusal_echoes_its_input(self, config, sender):
        marker = "echo-marker-c41"
        answer = _receive(config, sender.offer(resourceType=marker, name=marker * 30))
        assert marker not in json.dumps(answer.body)

    @pytest.mark.parametrize("variant", [
        "discovery_404", "not_json", "no_api_version", "no_endpoint",
        "endpoint_elsewhere", "shared_by_elsewhere", "unparsable_owner",
    ])
    def test_an_unverified_sender_is_403(self, config, sender, variant):
        body = sender.offer()
        if variant == "discovery_404":
            sender.publish(status=404)
        elif variant == "not_json":
            sender.server.routes["/ocm-provider/"] = (200, {}, b"<html>")
        elif variant == "no_api_version":
            sender.publish({**sender.discovery(), "apiVersion": ""})
        elif variant == "no_endpoint":
            sender.publish({**sender.discovery(), "endPoint": ""})
        elif variant == "endpoint_elsewhere":
            sender.publish({**sender.discovery(), "endPoint": "https://other.example.org/ocm"})
        elif variant == "shared_by_elsewhere":
            body["sharedBy"] = "carol@https://other.example.org"
        elif variant == "unparsable_owner":
            body["owner"] = "no-at-sign"
        mark = _watermark(config)
        answer = _receive(config, body)
        assert answer.status == 403
        assert answer.body["message"] == "sender_unverified"
        assert _written_since(config, mark) == NOTHING

    def test_a_sender_on_a_private_address_unlisted_is_403(self, config, sender):
        config.sharing.private_hosts = []
        mark = _watermark(config)
        answer = _receive(config, sender.offer())
        assert answer.status == 403
        assert sender.fetches == 0
        assert _written_since(config, mark) == NOTHING

    def test_an_absolute_uri_on_another_host_is_400(self, config, sender):
        body = sender.offer()
        body["protocol"]["options"]["uri"] = "https://elsewhere.example.org/public.php/webdav/"
        mark = _watermark(config)
        answer = _receive(config, body)
        assert answer.status == 400
        assert _written_since(config, mark) == NOTHING

    def test_a_relative_uri_is_a_path_under_the_base(self, config, sender):
        body = sender.offer()
        body["protocol"]["options"]["uri"] = "sub/dir"
        assert _receive(config, body).status == 201
        [offer] = _offers(config)
        assert offer.extras["webdav"] == f"http://{sender.host}/public.php/webdav/"
        assert offer.extras["share_path"] == "sub/dir"

    def test_a_uri_climbing_out_of_the_base_is_400(self, config, sender):
        body = sender.offer()
        body["protocol"]["options"]["uri"] = "../../remote.php/dav/"
        assert _receive(config, body).status == 400


class TestRecipient:
    def _unknown_user(self, sender):
        return sender.offer(shareWith=f"mallory@https://{SITE}")

    def _unknown_host(self, sender):
        return sender.offer(shareWith="alice@https://other.example.org")

    def test_unknown_user_and_unknown_host_answer_identically(self, config, sender):
        mark = _watermark(config)
        user = _receive(config, self._unknown_user(sender))
        host = _receive(config, self._unknown_host(sender))
        assert user.status == host.status == 404
        assert user.body == host.body
        assert _written_since(config, mark) == NOTHING

    def test_existence_is_not_answered_before_the_sender_is_verified(self, config, sender):
        sender.publish(status=404)
        known = _receive(config, sender.offer())
        unknown = _receive(config, self._unknown_user(sender))
        assert known.status == unknown.status == 403
        assert known.body == unknown.body

    @pytest.mark.parametrize("share_with", [
        f"alice@https://{SITE}:8443", f"alice@https://{SITE}/sub", "alice@https://example.com",
    ])
    def test_the_host_is_compared_exactly(self, config, sender, share_with):
        assert _receive(config, sender.offer(shareWith=share_with)).status == 404


class TestStepOrder:
    def test_over_the_per_host_limit_no_fetch_is_made(self, config, sender):
        for n in range(ocm.SENDER_MAX_PER_MINUTE):
            assert _receive(config, sender.offer(providerId=100 + n)).status in (201, 429)
        fetched = sender.fetches
        mark = _watermark(config)
        answer = _receive(config, sender.offer(providerId=999))
        assert answer.status == 429
        assert sender.fetches == fetched
        assert _written_since(config, mark) == NOTHING

    def test_over_the_per_address_limit_no_fetch_is_made(self, config, sender):
        for n in range(ocm.ADDRESS_MAX_PER_MINUTE):
            _receive(config, sender.offer(providerId=100 + n), client_address="198.51.100.7")
        fetched = sender.fetches
        answer = _receive(config, sender.offer(providerId=999), client_address="198.51.100.7")
        assert answer.status == 429
        assert sender.fetches == fetched

    def test_the_global_discovery_cap(self, config, sender):
        config.sharing.ocm.max_discovery_fetches_per_minute = 1
        other = Sender()
        try:
            assert _receive(config, sender.offer()).status == 201
            mark = _watermark(config)
            answer = _receive(config, other.offer(providerId=6))
            assert answer.status == 429
            assert other.fetches == 0
            assert _written_since(config, mark) == NOTHING
        finally:
            other.close()

    def test_a_malformed_body_is_refused_before_the_limits(self, config, sender):
        for _ in range(ocm.SENDER_MAX_PER_MINUTE + 5):
            assert _receive(config, {"resourceType": "file"}).status == 400
        assert _receive(config, sender.offer()).status == 201


# ---------------------------------------------------------------- signatures

class TestSignatures:
    def _keyed(self, sender, key):
        sender.public_key_pem = _pem(key)
        sender.publish()

    def _signed(self, sender, key, body=None, **kwargs):
        raw = json.dumps(body or sender.offer()).encode()
        kwargs.setdefault("key_id", f"https://{sender.host}/ocm#signature")
        return raw, _sign(key, raw, **kwargs)

    def test_rsa_sha512_verifies(self, config, sender, key):
        self._keyed(sender, key)
        raw, headers = self._signed(sender, key)
        assert _receive(config, raw, headers).status == 201

    def test_rsa_sha256_verifies(self, config, sender, key):
        self._keyed(sender, key)
        raw, headers = self._signed(sender, key, algorithm="rsa-sha256")
        assert _receive(config, raw, headers).status == 201

    @pytest.mark.parametrize("variant", [
        "wrong_digest", "stale_date", "key_on_another_host", "missing_digest_header",
        "missing_host_header", "unsigned", "another_key", "body_changed", "unknown_algorithm",
    ])
    def test_refused_with_nothing_written(self, config, sender, key, variant):
        self._keyed(sender, key)
        raw, headers = self._signed(sender, key)
        if variant == "wrong_digest":
            raw, headers = self._signed(
                sender, key, digest="SHA-512=" + base64.b64encode(b"x" * 64).decode())
        elif variant == "stale_date":
            raw, headers = self._signed(
                sender, key, date=formatdate(time.time() - 600, usegmt=True))
        elif variant == "key_on_another_host":
            raw, headers = self._signed(sender, key, key_id="https://other.example.org/ocm#signature")
        elif variant == "missing_digest_header":
            raw, headers = self._signed(sender, key, signed="(request-target) content-length date host")
        elif variant == "missing_host_header":
            raw, headers = self._signed(sender, key, signed="(request-target) content-length date digest")
        elif variant == "unsigned":
            headers = _headers(raw)
        elif variant == "another_key":
            other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            raw, headers = self._signed(sender, other)
        elif variant == "body_changed":
            raw = raw.replace(b"Taxes 2026", b"Taxes 2027")
        elif variant == "unknown_algorithm":
            headers["signature"] = headers["signature"].replace("rsa-sha512", "hmac-sha256")
        mark = _watermark(config)
        answer = _receive(config, raw, headers)
        assert answer.status == 403
        assert answer.body["message"] == "sender_unverified"
        assert _written_since(config, mark) == NOTHING

    def test_an_unsigned_offer_from_a_sender_with_no_key_proceeds(self, config, sender):
        assert "publicKey" not in sender.document
        assert _receive(config, sender.offer()).status == 201

    def test_the_verifier_names_its_refusal(self, sender, key):
        raw = json.dumps(sender.offer()).encode()
        now = time.time()
        headers = _sign(key, raw, key_id=f"https://{sender.host}/ocm#signature")
        assert ocm.verify_signature("POST", "/ocm/shares", headers, raw, _pem(key), sender.host, now) is None
        stale = _sign(key, raw, key_id=f"https://{sender.host}/ocm#signature",
                      date=formatdate(now - 600, usegmt=True))
        assert ocm.verify_signature("POST", "/ocm/shares", stale, raw, _pem(key), sender.host, now) == "date_stale"
        assert ocm.verify_signature("POST", "/ocm/shares", _headers(raw), raw, _pem(key), sender.host, now) \
            == "signature_missing"
        assert ocm.verify_signature("POST", "/ocm/shares", headers, raw + b" ", _pem(key), sender.host, now) \
            == "digest_mismatch"
        assert ocm.verify_signature("POST", "/ocm/shares", headers, raw, _pem(key), "other.example.org", now) \
            == "key_host_mismatch"
        assert ocm.verify_signature("POST", "/ocm/notifications", headers, raw, _pem(key), sender.host, now) \
            == "signature_invalid"
        assert ocm.verify_signature("POST", "/ocm/shares", headers, raw, "not a pem", sender.host, now) \
            == "key_unusable"


# ------------------------------------------------------------- repeat offers

class TestRepeatOffers:
    def _first(self, config, sender):
        assert _receive(config, sender.offer()).status == 201
        [offer] = _offers(config)
        return offer

    def _set(self, config, offer, state):
        with db.get_db(config.db_path) as conn:
            sources.set_state(conn, offer.id, "alice", state, reason="test_setup")
            conn.commit()

    def test_a_pending_repeat_replaces_the_secret(self, config, sender):
        offer = self._first(config, sender)
        body = sender.offer()
        body["protocol"]["options"]["sharedSecret"] = "second-secret"
        assert _receive(config, body).status == 201
        assert [(o.id, o.state) for o in _offers(config)] == [(offer.id, "pending")]
        assert _read_secret(config, "alice", str(offer.id)) == "second-secret"

    def test_an_active_repeat_holds_the_new_secret_apart(self, config, sender):
        offer = self._first(config, sender)
        with db.get_db(config.db_path) as conn:
            secrets_store.delete_secret(None, "alice", sources.SECRET_SERVICE, str(offer.id),
                                        connection=conn)
            sources.set_state(conn, offer.id, "alice", "active")
            conn.commit()
        body = sender.offer()
        body["protocol"]["options"]["sharedSecret"] = "forged-or-fresh"
        assert _receive(config, body).status == 201
        [after] = _offers(config)
        assert after.state == "active"
        assert after.extras.get("reoffer_pending") is True
        assert _read_secret(config, "alice", f"{offer.id}:reoffer") == "forged-or-fresh"
        assert _read_secret(config, "alice", str(offer.id)) is None

    def test_a_declined_repeat_is_answered_and_ignored(self, config, sender):
        offer = self._first(config, sender)
        self._set(config, offer, "declined")
        mark = _watermark(config)
        assert _receive(config, sender.offer()).status == 201
        assert [o.state for o in _offers(config)] == ["declined"]
        assert _written_since(config, mark) == NOTHING
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT occurrences FROM notifications WHERE source = 'shared_source'").fetchone()
        assert row["occurrences"] == 1

    def test_a_revoked_repeat_is_pending_again(self, config, sender):
        offer = self._first(config, sender)
        self._set(config, offer, "revoked")
        assert _read_secret(config, "alice", str(offer.id)) is None
        assert _receive(config, sender.offer()).status == 201
        assert [(o.id, o.state) for o in _offers(config)] == [(offer.id, "pending")]
        assert _read_secret(config, "alice", str(offer.id)) == SECRET

    def test_the_pending_cap(self, config, sender):
        config.sharing.ocm.max_pending_per_user = 1
        self._first(config, sender)
        mark = _watermark(config)
        answer = _receive(config, sender.offer(providerId=6))
        assert answer.status == 429
        assert _written_since(config, mark) == NOTHING

    def test_no_master_key_stores_nothing(self, config, sender, monkeypatch):
        monkeypatch.delenv("ISTOTA_SECRET_KEY")
        mark = _watermark(config)
        assert _receive(config, sender.offer()).status == 503
        assert _written_since(config, mark) == NOTHING


# ------------------------------------------------------------- config, ingress

class TestTheNextcloudHostRule:
    def _write(self, tmp_path, site, nextcloud_url):
        path = tmp_path / "config.toml"
        path.write_text(
            f'db_path = "{tmp_path / "istota.db"}"\n'
            f'workspace_path = "{tmp_path / "ws"}"\n'
            f'[site]\nhostname = "{site}"\n'
            f'[nextcloud]\nurl = "{nextcloud_url}"\n'
            '[sharing.ocm]\nenabled = true\n'
        )
        return path

    def test_load_config_refuses_ocm_on_the_nextcloud_host(self, tmp_path):
        from istota.config import load_config

        path = self._write(tmp_path, "cloud.example.org", "https://cloud.example.org")
        with pytest.raises(ValueError, match=r"\[sharing\.ocm\]"):
            load_config(path, apply_db_overlays=False)

    def test_a_port_does_not_make_it_another_host(self, tmp_path):
        from istota.config import load_config

        path = self._write(tmp_path, "cloud.example.org:8443", "https://Cloud.Example.org/nc")
        with pytest.raises(ValueError):
            load_config(path, apply_db_overlays=False)

    def test_another_host_loads(self, tmp_path):
        from istota.config import load_config

        path = self._write(tmp_path, "bot.example.org", "https://cloud.example.org")
        assert load_config(path, apply_db_overlays=False).sharing.ocm.enabled is True


def _locations(conf: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in
            re.finditer(r"^location\s+([^{]+?)\s*\{(.*?)^\}", conf, re.M | re.S)}


def test_nginx_proxies_the_four_ocm_locations_to_the_web_app():
    locations = _locations((REPO / "docker" / "nginx" / "istota.conf.template").read_text())
    for name in ("= /.well-known/ocm", "= /ocm-provider", "^~ /ocm-provider/", "^~ /ocm/"):
        assert name in locations, name
        assert "proxy_pass http://$upstream_web;" in locations[name], name
        assert "proxy_set_header Host $http_host;" in locations[name], name
