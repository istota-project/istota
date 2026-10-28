"""The CLI harness's WebFetch as its own MCP server (#862, Stage 6).

``python -I -m istota.brain._web_fetch_mcp --config PATH``, run as a real
subprocess the way the ``claude`` CLI starts it from the harness's second
``mcpServers`` entry. It runs outside the sandbox, in the daemon's network
namespace, as native WebFetch does, so what is held here is: the environment
the CLI hands it is gone before anything is served, the tool list is WebFetch
alone, the policy in the config file is the one a call is judged by, a call is
held until the harness releases calls, and a bad config starts nothing.

No call reaches the internet: every call below is refused by the policy or the
address rule before or instead of a fetch, which is what proves the policy
crossed the file intact. Fetching itself is `tests/test_native_web_fetch.py`'s.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from istota.brain import _web_fetch_mcp as web_mcp
from istota.session.tools import WebFetchPolicy
from istota.session.tools.dispatch import HARNESS_TEXT_CAP_CHARS

_TIMEOUT = 20.0
_QUIET = 0.6
SENTINEL_NAME = "ISTOTA_TEST_CLI_SENTINEL"
SENTINEL_VALUE = "cli-credential-sentinel-6c1f"


def _write_config(tmp_path, policy=None, *, url_corpus=None, gate=None, name="web-fetch.json"):
    document = web_mcp.config_document(
        policy or WebFetchPolicy(), cwd=tmp_path, url_corpus=url_corpus, gate=gate,
    )
    path = tmp_path / name
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    return path


def _argv(config_path):
    return [sys.executable, "-I", "-m", "istota.brain._web_fetch_mcp", "--config", str(config_path)]


class _Mcp:
    def __init__(self, config_path):
        self.proc = subprocess.Popen(
            _argv(config_path),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, SENTINEL_NAME: SENTINEL_VALUE},
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def reply(self, timeout=_TIMEOUT):
        line = self._lines.get(timeout=timeout)
        assert line is not None, "the server closed its stdout"
        return json.loads(line)

    def no_reply_within(self, seconds) -> bool:
        try:
            self._lines.get(timeout=seconds)
        except queue.Empty:
            return True
        return False

    def request(self, request_id, method, params=None):
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return self.reply()

    def call(self, request_id, url):
        return self.request(request_id, "tools/call", {"name": "WebFetch", "arguments": {"url": url}})

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=_TIMEOUT)


@pytest.fixture
def server():
    started = []

    def _start(config_path):
        mcp = _Mcp(config_path)
        started.append(mcp)
        return mcp

    yield _start
    for mcp in started:
        mcp.kill()


def _text(reply):
    return "".join(b.get("text", "") for b in reply["result"]["content"] if b.get("type") == "text")


class TestTheConfigDocument:
    def test_a_policy_survives_the_file(self, tmp_path):
        policy = WebFetchPolicy(
            timeout_seconds=7.5, max_bytes=1234, max_content_chars=999, max_redirects=2,
            allow_http=True, allowed_ports=(443, 8443), user_agent="TestAgent/2",
            allow_hosts=("example.org",), block_hosts=("bad.example",),
            extra_blocked_cidrs=("8.8.8.0/24",), require_url_provenance=True,
        )
        path = _write_config(tmp_path, policy, url_corpus=frozenset({"https://example.org/a"}))
        document = web_mcp.read_config(str(path))
        assert web_mcp.policy_from_document(document) == policy
        assert web_mcp.corpus_from_document(document) == frozenset({"https://example.org/a"})

    def test_it_names_every_policy_field(self, tmp_path):
        document = web_mcp.config_document(WebFetchPolicy(), cwd=tmp_path, url_corpus=None, gate=None)
        assert set(document["policy"]) == {f.name for f in fields(WebFetchPolicy)}
        assert document["url_corpus"] is None

    @pytest.mark.parametrize("change", [
        {"policy": {"surprise": 1}},
        {"policy": {"allowed_ports": "443"}},
        {"policy": {"allow_http": "yes"}},
        {"policy": {"timeout_seconds": True}},
        {"url_corpus": "https://example.org/"},
        {"policy": None},
    ])
    def test_a_field_of_the_wrong_shape_is_refused(self, tmp_path, change):
        document = web_mcp.config_document(WebFetchPolicy(), cwd=tmp_path, url_corpus=None, gate=None)
        for key, value in change.items():
            if isinstance(value, dict) and isinstance(document.get(key), dict):
                document[key].update(value)
            else:
                document[key] = value
        with pytest.raises(web_mcp.ConfigError):
            web_mcp.policy_from_document(document)
            web_mcp.corpus_from_document(document)


class TestTheServer:
    def test_it_offers_webfetch_alone(self, server, tmp_path):
        mcp = server(_write_config(tmp_path))
        init = mcp.request(0, "initialize", {"protocolVersion": "2025-11-25"})
        assert init["result"]["capabilities"] == {"tools": {}}
        listed = mcp.request(1, "tools/list")["result"]["tools"]
        assert [tool["name"] for tool in listed] == ["WebFetch"]
        assert listed[0]["annotations"]["readOnlyHint"] is True
        assert "url" in listed[0]["inputSchema"]["properties"]

    def test_a_call_is_judged_by_the_policy_in_the_file(self, server, tmp_path):
        mcp = server(_write_config(tmp_path, WebFetchPolicy(block_hosts=("blocked.example",))))
        reply = mcp.call(1, "https://blocked.example/page")
        assert reply["result"]["isError"] is True
        assert "host blocked.example is blocked" in _text(reply)

    def test_the_url_corpus_crosses_too(self, server, tmp_path):
        policy = WebFetchPolicy(require_url_provenance=True, block_hosts=("seen.example",))
        mcp = server(_write_config(tmp_path, policy, url_corpus=frozenset({"https://seen.example/a"})))
        unseen = mcp.call(1, "https://unseen.example/b")
        assert "require_url_provenance is on" in _text(unseen)
        # The corpus URL gets past provenance and stops at the next rule.
        seen = mcp.call(2, "https://seen.example/a")
        assert "host seen.example is blocked" in _text(seen)

    def test_the_address_rule_still_refuses_loopback(self, server, tmp_path):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"loopback content")

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            policy = WebFetchPolicy(allow_http=True, allowed_ports=(port,))
            mcp = server(_write_config(tmp_path, policy))
            reply = mcp.call(1, f"http://127.0.0.1:{port}/secret")
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert reply["result"]["isError"] is True
        assert "private or reserved address" in _text(reply)
        assert hits == []

    def test_an_unknown_tool_is_refused(self, server, tmp_path):
        mcp = server(_write_config(tmp_path))
        reply = mcp.request(1, "tools/call", {"name": "Bash", "arguments": {"command": "id"}})
        assert reply["result"]["isError"] is True
        assert "Unknown tool: Bash" in _text(reply)

    def test_the_first_call_waits_for_the_gate(self, server, tmp_path):
        gate = tmp_path / "web-gate.fifo"
        os.mkfifo(gate, 0o600)
        mcp = server(_write_config(tmp_path, WebFetchPolicy(block_hosts=("b.example",)), gate=str(gate)))
        mcp.request(0, "initialize", {"protocolVersion": "2025-11-25"})
        assert mcp.request(1, "tools/list")["result"]["tools"]
        mcp.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "WebFetch", "arguments": {"url": "https://b.example/"}}})
        assert mcp.no_reply_within(_QUIET), "a call ran before the gate opened"
        deadline = time.monotonic() + _TIMEOUT
        while True:
            try:
                fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:
                assert time.monotonic() < deadline, "the server never opened the gate"
                time.sleep(0.02)
        os.write(fd, b"\x01")
        os.close(fd)
        assert "host b.example is blocked" in _text(mcp.reply())
        assert "host b.example is blocked" in _text(mcp.call(3, "https://b.example/again"))

    def test_stdin_eof_ends_it(self, server, tmp_path):
        mcp = server(_write_config(tmp_path))
        mcp.proc.stdin.close()
        assert mcp.proc.wait(timeout=_TIMEOUT) == 0


class TestTheEnvironment:
    def test_the_clis_environment_is_gone_before_anything_is_served(self, tmp_path):
        """The CLI hands its MCP servers its whole environment, credential
        included; this records what the server held when it began serving."""
        probe = (
            "import json, os, sys\n"
            "from istota.brain import _web_fetch_mcp as m\n"
            "def serve(dispatcher, gate):\n"
            "    print(json.dumps({'env': dict(os.environ), 'offered': list(dispatcher.offered)}))\n"
            "    return 0\n"
            "m.serve_mcp = serve\n"
            "sys.exit(m.main(sys.argv[1:]))\n"
        )
        path = _write_config(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-I", "-c", probe, "--config", str(path)],
            capture_output=True, env={**os.environ, SENTINEL_NAME: SENTINEL_VALUE},
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        seen = json.loads(proc.stdout)
        assert seen == {"env": {}, "offered": ["WebFetch"]}

    def test_the_text_cap_is_the_harness_cap(self, tmp_path):
        path = _write_config(tmp_path)
        dispatcher = web_mcp.build_dispatcher(web_mcp.read_config(str(path)))
        assert dispatcher.offered == ("WebFetch",)
        assert dispatcher._text_cap == HARNESS_TEXT_CAP_CHARS


class TestARefusedStart:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-I", "-m", "istota.brain._web_fetch_mcp", *args],
            capture_output=True, env={**os.environ, SENTINEL_NAME: SENTINEL_VALUE},
            stdin=subprocess.DEVNULL, timeout=60,
        )

    def test_a_symlinked_config_is_refused(self, tmp_path):
        real = _write_config(tmp_path)
        link = tmp_path / "link.json"
        link.symlink_to(real)
        proc = self._run("--config", str(link))
        assert proc.returncode == 2
        assert proc.stdout == b""

    def test_a_malformed_config_is_refused(self, tmp_path):
        path = tmp_path / "web-fetch.json"
        path.write_text(json.dumps({"policy": {"block_hosts": "x"}, "cwd": str(tmp_path)}))
        proc = self._run("--config", str(path))
        assert proc.returncode == 2
        assert proc.stdout == b""
        assert SENTINEL_VALUE.encode() not in proc.stderr

    @pytest.mark.parametrize("argv", [[], ["--config"], ["--other", "x"], ["--config", "a", "b"]])
    def test_any_other_argv_is_refused(self, argv):
        proc = self._run(*argv)
        assert proc.returncode == 2
        assert proc.stdout == b""
