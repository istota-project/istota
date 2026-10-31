"""Real TLS through CONNECT, an independent upstream CA, and live grants."""
import base64
import os
import socket
import ssl
import threading
import tempfile
from pathlib import Path
from contextlib import contextmanager, nullcontext

import h11
import pytest
from cryptography.hazmat.primitives import serialization
from istota import db
from istota.credentials import store as secrets_store
from istota.config import Config
from istota.credentials.broker import ca, grants, intercept
from istota.credentials.broker.bindings import parse_binding
from istota.sandbox.network_proxy import NetworkProxy

VALUE = b"fixture-broker-password"
PLACEHOLDER = b"{{cred:portal}}"


@pytest.fixture
def broker_responder():
    return None


@pytest.fixture
def broker_scheme(request):
    return getattr(request, "param", "https")


@pytest.fixture
def broker(tmp_path, monkeypatch, request, broker_responder, broker_scheme):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    (tmp_path / "daemon").mkdir()
    config = Config(db_path=tmp_path / "daemon" / "data.db")
    config.security.credential_broker.enabled = True
    config.security.credential_broker.scan_max_bytes = 128
    db.init_db(config.db_path)
    authority = ca.load_or_create_ca(tmp_path / "daemon" / "broker")
    upstream_ca = ca.load_or_create_ca(tmp_path / "daemon" / "upstream")
    def trust_upstream():
        context = ssl.create_default_context(cadata=upstream_ca.certificate.public_bytes(serialization.Encoding.PEM).decode())
        context.set_alpn_protocols(["http/1.1"])
        return context
    monkeypatch.setattr(ca, "upstream_context", trust_upstream)
    received = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(.2)
    upstream_host = getattr(request, "param", "localhost")
    host = f"{upstream_host}:{listener.getsockname()[1]}"
    original_getaddrinfo = socket.getaddrinfo
    def resolve_local(name, *args, **kwargs):
        return original_getaddrinfo("127.0.0.1" if name == upstream_host else name, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", resolve_local)
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "portal", VALUE.decode(),
                               binding=parse_binding(broker_scheme + "://" + host, {}, []))
    with db.get_db(config.db_path) as conn:
        grants.put_grant(conn, "alice", "portal")
        task_id = db.create_task(conn, user_id="alice", prompt="test", source_type="talk", conversation_token="room-a")
    with db.get_db(config.db_path) as conn:
        grants.ensure_credential_grants(conn, task_id, "alice")
    stopped = threading.Event()
    def serve(conn):
        try:
            with (nullcontext(conn) if broker_scheme == "http" else
                  ca.server_context(upstream_ca, upstream_host).wrap_socket(conn, server_side=True)) as tls:
                tls.settimeout(3)
                parser = h11.Connection(h11.SERVER)
                body = b""
                while True:
                    event = parser.next_event()
                    if event is h11.NEED_DATA:
                        data = tls.recv(65536)
                        if not data:
                            return
                        parser.receive_data(data)
                    elif isinstance(event, h11.Request):
                        request = event
                    elif isinstance(event, h11.Data):
                        body += event.data
                    elif isinstance(event, h11.EndOfMessage):
                        received.append((request, body))
                        if request.target == b"/disconnect":
                            return
                        response = b'{"echo":"' + VALUE + b'"}'
                        if request.target == b"/large":
                            response += b"x" * 256
                        status = 204 if request.target == b"/empty" else 304 if request.target == b"/not-modified" else 200
                        response_headers = [(b"x-echo", VALUE)]
                        if request.target == b"/header-name":
                            response_headers.append((b"x-echo-" + VALUE, b"yes"))
                        if broker_responder is not None:
                            status, response_headers, response = broker_responder(request, body)
                        # A responder may name its own chunk boundaries with a list.
                        chunks = response if isinstance(response, list) else [response[:16], response[16:]]
                        if request.target != b"/chunked":
                            response_headers.append((b"content-length", str(sum(map(len, chunks))).encode()))
                        tls.sendall(parser.send(h11.Response(status_code=status, headers=response_headers)))
                        if request.method != b"HEAD" and status not in (204, 304):
                            for chunk in chunks:
                                tls.sendall(parser.send(h11.Data(data=chunk)))
                        tls.sendall(parser.send(h11.EndOfMessage()))
                        parser.start_next_cycle()
                        body = b""
                    else:
                        return
        except (OSError, h11.ProtocolError):
            pass
    def accept():
        while not stopped.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    from istota.credentials.broker.intercept import Broker
    socket_dir = tempfile.TemporaryDirectory(prefix="broker-", dir="/tmp")
    # The upstream is a loopback listener, which only a private_hosts grant reaches.
    proxy = NetworkProxy(Path(socket_dir.name) / "p.sock", {host}, trusted_roots=[os.getpid()],
                         broker=Broker(config, task_id, "alice", authority),
                         private_hosts=[upstream_host])
    with proxy:
        yield config, task_id, authority, upstream_ca, proxy, host, received
    stopped.set()
    listener.close()
    thread.join(2)
    socket_dir.cleanup()


@contextmanager
def connect(broker, *, sni="localhost"):
    _, _, authority, upstream_ca, proxy, host, _ = broker
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(3)
    conn.connect(str(proxy.socket_path))
    conn.sendall(f"CONNECT {host} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    assert conn.recv(4096).startswith(b"HTTP/1.1 200")
    roots = (authority.certificate.public_bytes(serialization.Encoding.PEM)
             + upstream_ca.certificate.public_bytes(serialization.Encoding.PEM)).decode()
    context = ssl.create_default_context(cadata=roots)
    with context.wrap_socket(conn, server_hostname=sni) as tls:
        yield tls


def exchange(tls, host, *, method=b"GET", header=b"authorization", value=PLACEHOLDER,
             body=b"", target=b"/", extra=()):
    parser = h11.Connection(h11.CLIENT)
    headers = [(b"host", host.encode()), (header, value), (b"content-length", str(len(body)).encode()), *extra]
    wire = parser.send(h11.Request(method=method, target=target, headers=headers))
    if body:
        wire += parser.send(h11.Data(data=body))
    wire += parser.send(h11.EndOfMessage())
    # A header-only refusal may close immediately; submit this small request
    # together so the helper does not race that refusal with a second write.
    tls.sendall(wire)
    response = None
    payload = b""
    while True:
        event = parser.next_event()
        if event is h11.NEED_DATA:
            parser.receive_data(tls.recv(65536))
        elif isinstance(event, h11.Response):
            response = event
        elif isinstance(event, h11.Data):
            payload += event.data
        elif isinstance(event, h11.EndOfMessage):
            return response, payload
        elif isinstance(event, h11.ConnectionClosed):
            raise AssertionError("proxy closed before response")


def test_substitution_scrub_and_keepalive(broker, caplog):
    caplog.set_level("INFO", logger="istota.credentials.broker")
    with connect(broker) as tls:
        for _ in range(2):
            response, body = exchange(tls, broker[5], value=b"Bearer " + PLACEHOLDER)
            assert response.status_code == 200
            assert dict(response.headers)[b"x-echo"] == PLACEHOLDER
            assert VALUE not in body
            assert PLACEHOLDER in body
    assert len(broker[6]) == 2
    assert dict(broker[6][0][0].headers)[b"authorization"] == b"Bearer " + VALUE
    assert VALUE.decode() not in caplog.text
    assert caplog.text.count("credential_substituted") == 2


def test_basic_decode_and_live_revocation(broker):
    basic = b"Basic " + base64.b64encode(b"user:" + PLACEHOLDER)
    with connect(broker) as tls:
        assert exchange(tls, broker[5], value=basic)[0].status_code == 200
        with db.get_db(broker[0].db_path) as conn:
            grants.put_grant(conn, "alice", "portal")
        response, _ = exchange(tls, broker[5], value=basic)
        assert dict(response.headers)[b"x-istota-refused"] == b"credential_changed"
    sent = dict(broker[6][0][0].headers)[b"authorization"]
    assert base64.b64decode(sent.split()[1]) == b"user:" + VALUE
    assert len(broker[6]) == 1


@pytest.mark.parametrize("options,reason", [
    ({"header": b"x-not-auth"}, b"credential_header_not_allowed"),
    ({"body": PLACEHOLDER}, b"credential_in_body"),
    ({"value": b"{{cred:missing}}"}, b"credential_not_granted"),
    ({"target": b"/?token={{cred:portal}}"}, b"credential_in_url"),
    ({"body": b"compressed", "extra": [(b"content-encoding", b"gzip")]}, b"unsupported_encoding"),
])
def test_refusal_never_forwards(broker, options, reason):
    with connect(broker) as tls:
        response, _ = exchange(tls, broker[5], **options)
        assert response.status_code == 403
        assert dict(response.headers)[b"x-istota-refused"] == reason
    assert broker[6] == []


@pytest.mark.parametrize("method", [b"GET", b"DELETE", b"OPTIONS", b"PROPFIND"])
@pytest.mark.parametrize("legacy", [False, True])
def test_grants_allow_every_method_on_the_bound_host(broker, method, legacy):
    if legacy:
        with db.get_db(broker[0].db_path) as conn:
            conn.execute("UPDATE credential_grants SET methods='[\"GET\"]'")
    with connect(broker) as tls:
        response, _ = exchange(tls, broker[5], method=method)
        assert response.status_code == 200
    assert broker[6][0][0].method == method
    assert dict(broker[6][0][0].headers)[b"authorization"] == VALUE


def test_body_after_cap_is_literal_and_large_response_is_scrubbed(broker):
    body = b"x" * 128 + PLACEHOLDER
    with connect(broker) as tls:
        response, payload = exchange(tls, broker[5], body=body, target=b"/large")
    assert broker[6][0][1] == body
    assert response.status_code == 200
    assert payload == b'{"echo":"' + PLACEHOLDER + b'"}' + b"x" * 256
    assert dict(response.headers)[b"x-echo"] == PLACEHOLDER


def _echo_past_the_cap(request, body):
    """Past the 128-byte test cap, an echo split at the byte the target names."""
    split = int(request.target.rsplit(b"/", 1)[1])
    return 200, [], [b"y" * 200, b"before-" + VALUE[:split], VALUE[split:] + b"-after", b"z" * 200]


@pytest.mark.parametrize("broker_responder", [_echo_past_the_cap])
def test_large_response_scrubs_an_echo_across_every_chunk_boundary(broker):
    expected = b"y" * 200 + b"before-" + PLACEHOLDER + b"-after" + b"z" * 200
    with connect(broker) as tls:
        for split in range(len(VALUE) + 1):
            response, payload = exchange(tls, broker[5], target=b"/split/%d" % split)
            assert response.status_code == 200
            assert payload == expected


def _echo_across_the_cap(request, body):
    """The buffered prefix ends part-way through the echo."""
    split = int(request.target.rsplit(b"/", 1)[1])
    return 200, [], [b"y" * 130 + VALUE[:split], VALUE[split:] + b"-after", b"z" * 200]


@pytest.mark.parametrize("broker_responder", [_echo_across_the_cap])
def test_large_response_scrubs_an_echo_across_the_end_of_the_prefix(broker):
    expected = b"y" * 130 + PLACEHOLDER + b"-after" + b"z" * 200
    with connect(broker) as tls:
        for split in range(1, len(VALUE)):
            response, payload = exchange(tls, broker[5], target=b"/split/%d" % split)
            assert response.status_code == 200
            assert payload == expected


def test_a_stream_that_goes_quiet_still_gets_what_is_decided():
    class Stalled:
        def next_event(self):
            raise socket.timeout

    downstream = h11.Connection(h11.SERVER)
    downstream.receive_data(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    downstream.next_event()
    downstream.next_event()
    client, proxy = socket.socketpair()
    with client, proxy:
        proxy.sendall(downstream.send(h11.Response(status_code=200, headers=[(b"transfer-encoding", b"chunked")])))
        with pytest.raises(socket.timeout):
            intercept._relay_scrubbed(Stalled(), None, downstream, proxy, b"secret" + b"p" * 2000, {b"secret": b"<s>"})
        received = client.recv(65536)
    assert b"<s>" + b"p" * 1995 in received
    assert b"secret" not in received


def _echo_named_encoding(request, body):
    forms = {
        b"/base64": base64.b64encode(VALUE),
        b"/urlsafe": base64.urlsafe_b64encode(VALUE).rstrip(b"="),
        b"/hex": VALUE.hex().encode(),
        b"/basic": base64.b64encode(b"user:" + VALUE),
    }
    return 200, [], [b"y" * 200, b"[", forms[request.target], b"]"]


@pytest.mark.parametrize("broker_responder", [_echo_named_encoding])
@pytest.mark.parametrize("target", [b"/base64", b"/urlsafe", b"/hex", b"/basic"])
def test_large_response_scrubs_an_encoded_echo(broker, target):
    basic = target == b"/basic"
    value = b"Basic " + base64.b64encode(b"user:" + PLACEHOLDER) if basic else PLACEHOLDER
    with connect(broker) as tls:
        response, payload = exchange(tls, broker[5], target=target, value=value)
    assert response.status_code == 200
    scrubbed = base64.b64encode(b"user:" + PLACEHOLDER) if basic else PLACEHOLDER
    assert payload == b"y" * 200 + b"[" + scrubbed + b"]"


def test_echo_forms_cover_the_spellings_a_service_uses():
    value = b'a/b c+"d!>>~~~/'
    forms = intercept._echo_forms(value)
    for form in (
        value,
        base64.b64encode(value),
        base64.b64encode(value).replace(b"/", b"\\/"),
        b"a%2Fb%20c%2B%22d%21%3E%3E~~~%2F",
        b"a%2fb%20c%2b%22d%21%3e%3e~~~%2f",
        b"a%2Fb%20c%2B%22d!%3E%3E~~~%2F",
        b"a%2Fb+c%2B%22d%21%3E%3E~~~%2F",
        value.hex().encode(),
        value.hex().upper().encode(),
        b'a/b c+\\"d!>>~~~/',
        b'a\\/b c+\\"d!>>~~~\\/',
        b"a/b c+&quot;d!&gt;&gt;~~~/",
    ):
        assert form in forms


def test_a_shorter_value_waits_for_the_longer_one_it_begins():
    replacements = {b"abc": b"<short>", b"abcdef": b"<long>"}
    pattern = intercept._scrub_pattern(replacements)
    hold = 5
    out, pending = intercept._scrub_decided(b"xabc", pattern, replacements, 4 - hold)
    assert (out, pending) == (b"", b"xabc")
    pending += b"defyyyyy"
    out, pending = intercept._scrub_decided(pending, pattern, replacements, len(pending) - hold)
    rest, left = intercept._scrub_decided(pending, pattern, replacements, len(pending))
    assert out + rest == b"x<long>yyyyy"
    assert left == b""


def test_large_response_with_no_credential_passes_unchanged(broker):
    with connect(broker) as tls:
        response, payload = exchange(tls, broker[5], target=b"/large", value=b"none")
    assert response.status_code == 200
    assert payload == b'{"echo":"' + VALUE + b'"}' + b"x" * 256


def test_host_mismatch_and_passthrough(broker):
    with connect(broker) as tls:
        response, _ = exchange(tls, "other.example")
        assert response.status_code == 403
    assert broker[6] == []
    broker[4].broker = None
    with connect(broker) as tls:
        expected = ca.mint_leaf(broker[3], "localhost").certificate.public_bytes(serialization.Encoding.DER)
        assert tls.getpeercert(binary_form=True) == expected
        exchange(tls, broker[5])
    assert dict(broker[6][0][0].headers)[b"authorization"] == PLACEHOLDER


def test_sni_mismatch_never_reaches_upstream(broker):
    with pytest.raises(ssl.SSLError):
        with connect(broker, sni="other.example"):
            pass
    assert broker[6] == []


def test_empty_snapshot_keeps_original_tls(broker):
    with db.get_db(broker[0].db_path) as conn:
        conn.execute("DELETE FROM credential_task_grants WHERE task_id=?", (broker[1],))
    with connect(broker) as tls:
        expected = ca.mint_leaf(broker[3], "localhost").certificate.public_bytes(serialization.Encoding.DER)
        assert tls.getpeercert(binary_form=True) == expected
        exchange(tls, broker[5])
    assert dict(broker[6][0][0].headers)[b"authorization"] == PLACEHOLDER


def test_host_header_must_be_authority_only(broker):
    with connect(broker) as tls:
        response, _ = exchange(tls, broker[5] + "/ignored")
    assert response.status_code == 403
    assert broker[6] == []


def test_chunked_placeholder_split_across_chunks_is_refused(broker):
    with connect(broker) as tls:
        tls.sendall(f"POST / HTTP/1.1\r\nHost: {broker[5]}\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
        for chunk in [b"{{cr", b"ed:portal}}"]:
            tls.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        tls.sendall(b"0\r\n\r\n")
        reply = tls.recv(65536)
        assert b"403 Refused" in reply
        assert b"credential_in_body" in reply
    assert broker[6] == []


def test_conflicting_lengths_are_refused(broker):
    with connect(broker) as tls:
        tls.sendall(f"POST / HTTP/1.1\r\nHost: {broker[5]}\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx".encode())
        assert b"broker_protocol_error" in tls.recv(65536)
    assert broker[6] == []


def test_live_unbinding_refuses_existing_connection(broker):
    with connect(broker) as tls:
        assert exchange(tls, broker[5])[0].status_code == 200
        secrets_store.upsert_secret(broker[0].db_path, "alice", "vault_entries", "portal", VALUE.decode(),
                                   binding=parse_binding("https://other.example", {}, []))
        response, _ = exchange(tls, broker[5])
        assert dict(response.headers)[b"x-istota-refused"] == b"credential_not_bound"
    assert len(broker[6]) == 1


@pytest.mark.parametrize("method,target,status", [
    (b"HEAD", b"/", 200), (b"GET", b"/empty", 204), (b"GET", b"/not-modified", 304),
])
def test_no_body_responses_preserve_keepalive(broker, method, target, status):
    with connect(broker) as tls:
        response, payload = exchange(tls, broker[5], method=method, target=target)
        assert response.status_code == status
        assert payload == b""
        assert exchange(tls, broker[5])[0].status_code == 200


def test_small_chunked_response_scrubs_across_chunks(broker):
    with connect(broker) as tls:
        response, payload = exchange(tls, broker[5], target=b"/chunked")
    assert response.status_code == 200
    assert VALUE not in payload
    assert PLACEHOLDER in payload


def test_expect_continue_does_not_deadlock_upload(broker):
    with connect(broker) as tls:
        tls.sendall(f"POST / HTTP/1.1\r\nHost: {broker[5]}\r\nAuthorization: {PLACEHOLDER.decode()}\r\nContent-Length: 4\r\nExpect: 100-continue\r\n\r\n".encode())
        assert b"100 " in tls.recv(65536)
        tls.sendall(b"body")
        assert b"200 " in tls.recv(65536)
    assert broker[6][0][1] == b"body"


def test_substitution_is_audited_when_upstream_disconnects(broker, caplog):
    caplog.set_level("INFO", logger="istota.credentials.broker")
    with connect(broker) as tls:
        response, _ = exchange(tls, broker[5], target=b"/disconnect")
    assert response.status_code == 502
    assert caplog.text.count("credential_substituted") == 1
    assert "status=502" in caplog.text
    assert VALUE.decode() not in caplog.text


@pytest.mark.parametrize("broker", ["127.0.0.1"], indirect=True)
def test_ip_literal_without_sni_still_authenticates(broker):
    with connect(broker, sni="127.0.0.1") as tls:
        response, _ = exchange(tls, broker[5])
    assert response.status_code == 200
    assert dict(broker[6][0][0].headers)[b"authorization"] == VALUE


def test_secret_in_response_header_name_is_refused(broker):
    with connect(broker) as tls:
        response, payload = exchange(tls, broker[5], target=b"/header-name")
    assert response.status_code == 502
    assert VALUE not in repr(response.headers).encode() + payload
    assert dict(response.headers)[b"x-istota-refused"] == b"credential_in_response_header_name"


@pytest.mark.parametrize("broker_scheme", ["http"], indirect=True)
def test_http_requires_override_and_scrubs_response(broker):
    config, task_id, _, _, proxy, host, received = broker

    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "portal_username", "alice",
        binding={**parse_binding("http://" + host, {}, []), "credential": "portal"})
    basic = b"Basic " + base64.b64encode(b"{{cred:portal_username}}:" + PLACEHOLDER)

    def request(target=None):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(str(proxy.socket_path))
            client.sendall(b"GET " + (target or f"http://{host}/".encode()) +
                           f" HTTP/1.1\r\nHost: {host}\r\nAuthorization: ".encode() +
                           basic + b"\r\nConnection: close\r\n\r\n")
            response = b""
            while chunk := client.recv(65536):
                response += chunk
            return response

    assert b"credential_https_required" in request()
    assert received == []
    with db.get_db(config.db_path) as conn:
        grants.put_grant(conn, "alice", "portal", allow_http=True)
        # A policy edit cannot expand an already admitted task.
    assert b"credential_changed" in request()
    with db.get_db(config.db_path) as conn:
        new_task = db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                                 conversation_token="room-a")
        grants.ensure_credential_grants(conn, new_task, "alice")
    proxy.broker.task_id = new_task
    response = request()
    assert b"200" in response.split(b"\r\n")[0]
    assert dict(received[0][0].headers)[b"authorization"] == b"Basic " + base64.b64encode(b"alice:" + VALUE)
    assert VALUE not in response
    assert PLACEHOLDER in response
    assert b"403" in request(b"http://other.example/").split(b"\r\n")[0]
    assert len(received) == 1


def test_bound_host_at_a_private_address_is_refused_without_a_grant(broker):
    """ISSUE-883: interception reaches only an address the raw path could."""
    _, _, _, _, proxy, host, received = broker
    proxy.private_hosts = frozenset()
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(3)
        conn.connect(str(proxy.socket_path))
        conn.sendall(f"CONNECT {host} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        assert conn.recv(4096).startswith(b"HTTP/1.1 403")
    assert received == []


@pytest.mark.parametrize("broker_scheme", ["http"], indirect=True)
def test_each_brokered_upstream_connect_checks_the_address(broker):
    """Absolute-form HTTP has no CONNECT to check; the request's own connect does."""
    config, _, _, _, proxy, host, received = broker
    with db.get_db(config.db_path) as conn:
        grants.put_grant(conn, "alice", "portal", allow_http=True)
        task_id = db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                                 conversation_token="room-a")
        grants.ensure_credential_grants(conn, task_id, "alice")
    proxy.broker.task_id = task_id
    proxy.private_hosts = frozenset()
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        client.connect(str(proxy.socket_path))
        client.sendall(f"GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\n".encode()
                       + b"Authorization: Bearer " + PLACEHOLDER + b"\r\nConnection: close\r\n\r\n")
        response = b""
        while chunk := client.recv(65536):
            response += chunk
    assert response.startswith(b"HTTP/1.1 403")
    assert b"host_refused" in response
    assert received == []


@pytest.mark.parametrize("broker_scheme", ["http"], indirect=True)
def test_curl_http_through_bridge_and_native_environment(broker, tmp_path):
    import shutil
    import subprocess
    import sys
    import time
    from istota.sandbox.network_proxy import write_bridge_script
    from istota.sandbox.tool_server import merge_proxy_env
    curl = shutil.which("curl")
    assert curl, "curl is required to verify the HTTP proxy environment"
    config, _, _, _, proxy, host, received = broker
    with db.get_db(config.db_path) as conn:
        grants.put_grant(conn, "alice", "portal", allow_http=True)
        task_id = db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                                 conversation_token="room-a")
        grants.ensure_credential_grants(conn, task_id, "alice")
    proxy.broker.task_id = task_id
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    script = tmp_path / "net-bridge"
    write_bridge_script(script)
    process = subprocess.Popen([sys.executable, str(script), str(proxy.socket_path), str(port)])
    try:
        deadline = time.monotonic() + 3
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError("bridge did not start")
                time.sleep(.01)
        env = merge_proxy_env({"PATH": os.environ["PATH"]}, {
            "HTTP_PROXY": f"http://127.0.0.1:{port}",
            "http_proxy": f"http://127.0.0.1:{port}", "NO_PROXY": "", "no_proxy": "",
        })
        result = subprocess.run([curl, "--silent", "--show-error", "--fail", "--max-time", "3",
                                 "-H", "Authorization: " + PLACEHOLDER.decode(), "http://" + host + "/"],
                                env=env, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert dict(received[0][0].headers)[b"authorization"] == VALUE
        assert VALUE not in result.stdout
        assert PLACEHOLDER in result.stdout
    finally:
        process.terminate()
        process.wait(timeout=3)
