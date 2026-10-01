"""Forge consumers use inert placeholders before the TLS boundary."""
import json
import os
import subprocess

import pytest

from istota import forge_cli
from tests.test_developer_shims import _make_config, _run_hook


@pytest.mark.parametrize("forge", ["gitlab", "github"])
@pytest.mark.parametrize("skill_proxy", [True, False])
def test_helpers_and_wrapper_use_placeholders(tmp_path, forge, skill_proxy, monkeypatch):
    config = _make_config(tmp_path, devbox=False)
    config.security.credential_broker.enabled = True
    config.security.skill_proxy_enabled = skill_proxy
    setattr(config.developer, forge + "_token", "fixture-forge-password")
    env, temp = _run_hook(config, tmp_path)
    helper = temp / ".developer" / ("git-credential-helper" + ("-github" if forge == "github" else ""))
    result = subprocess.run([str(helper), "get"], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], forge.upper() + "_TOKEN": "ambient-password"})
    assert result.returncode == 0
    placeholder = "{{cred:forge." + forge + "}}"
    assert "password=" + placeholder in result.stdout
    assert "ambient-password" not in result.stdout
    policy = forge_cli.load_policy(str(temp / ".developer/forge-policy.json"), forge)
    assert policy["credential_broker"] is True
    assert policy["direct_token"] is False
    def no_socket(*args):
        pytest.fail("broker consumer fetched a real credential")
    monkeypatch.setattr(forge_cli, "_sock_roundtrip", no_socket)
    assert forge_cli.fetch_forge_credentials(forge, {"ISTOTA_SKILL_PROXY_SOCK": "/unused"}, policy) == (placeholder, "")
    _, _, child = forge_cli.build_invocation(forge, ["api", "user"],
        {"GIT_SSL_CAINFO": "/trust/bundle.pem", "GITLAB_TOKEN": "ambient-password"},
        placeholder, "/bin/unused", "/config", "https://git.example.com")
    assert child["GIT_SSL_CAINFO"] == "/trust/bundle.pem"
    assert "ambient-password" not in child.values()
    assert placeholder in child.values()


def test_broker_wrapper_still_checks_deny_policy(tmp_path, monkeypatch):
    config = _make_config(tmp_path, devbox=False)
    config.security.credential_broker.enabled = True
    config.developer.gitlab_token = "fixture-forge-password"
    _, temp = _run_hook(config, tmp_path)
    monkeypatch.setattr(forge_cli, "fetch_forge_credentials", lambda *args: pytest.fail("denied command requested credentials"))
    assert forge_cli.main([str(temp / ".developer/glab"), "repo", "delete"]) == forge_cli.EXIT_DENIED


def test_disabled_broker_policy_keeps_legacy_mode(tmp_path):
    config = _make_config(tmp_path, devbox=False)
    config.developer.gitlab_token = "fixture-forge-password"
    _, temp = _run_hook(config, tmp_path)
    policy = json.loads((temp / ".developer/forge-policy.json").read_text())
    assert not policy["gitlab"].get("credential_broker")
    assert " env GITLAB_TOKEN" in (temp / ".developer/git-credential-helper").read_text()


@pytest.fixture
def broker_responder(tmp_path):
    """Authenticated smart HTTP from git itself, plus minimal forge API replies."""
    import base64
    import shutil
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    repository = tmp_path / "upstream.git"
    subprocess.run([git, "init", "--bare", str(repository)], check=True, capture_output=True)
    # An actual commit makes clone verify pack transfer, not just discovery.
    env = {**os.environ, "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    tree = subprocess.run([git, "--git-dir", str(repository), "mktree"], input=b"", capture_output=True, check=True).stdout.strip()
    commit = subprocess.run([git, "--git-dir", str(repository), "commit-tree", tree.decode(), "-m", "fixture"], env=env, capture_output=True, check=True).stdout.strip()
    subprocess.run([git, "--git-dir", str(repository), "update-ref", "HEAD", commit.decode()], check=True, capture_output=True)
    def respond(request, body):
        headers = dict(request.headers)
        authorization = headers.get(b"authorization", b"")
        authenticated = (authorization in (b"Bearer fixture-forge-password", b"token fixture-forge-password")
                         or headers.get(b"private-token") == b"fixture-forge-password")
        if authorization.startswith(b"Basic "):
            authenticated = base64.b64decode(authorization[6:]) == b"oauth2:fixture-forge-password"
        if not authenticated:
            return 401, [(b"www-authenticate", b'Basic realm="fixture"')], b"unauthorized"
        target = request.target.decode()
        if not target.startswith("/upstream.git/"):
            return 200, [(b"content-type", b"application/json")], b'{"login":"alice","username":"alice"}'
        path, _, query = target.partition("?")
        result = subprocess.run([git, "http-backend"], input=body, capture_output=True, check=True, env={
            **os.environ, "GIT_PROJECT_ROOT": str(tmp_path), "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": request.method.decode(),
            "CONTENT_TYPE": headers.get(b"content-type", b"").decode(), "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "alice",
        })
        header_block, payload = result.stdout.split(b"\r\n\r\n", 1)
        response_headers = []
        status = 200
        for line in header_block.split(b"\r\n"):
            name, value = line.split(b":", 1)
            if name.lower() == b"status":
                status = int(value.strip().split()[0])
            else:
                response_headers.append((name, value.strip()))
        return status, response_headers, payload
    return respond


from tests.test_credential_broker_intercept import broker as tls_broker  # noqa: E402
from tests.test_credential_broker_intercept import broker_scheme  # noqa: E402, F401 -- shared fixture

broker = tls_broker


@pytest.fixture
def forge_clients(broker, tmp_path):
    import shutil
    import socket
    import sys
    import time
    from istota import db
    from istota.credential_broker import ca, grants
    from istota.credential_broker.bindings import sync_forge_bindings
    from istota.network_proxy import write_bridge_script
    from tests.test_developer_shims import _Ctx
    from istota.skills.developer import setup_env
    config, _, authority, _, proxy, host, received = broker
    config.developer.enabled = True
    config.developer.repos_dir = str(tmp_path / "repos")
    config.devbox.enabled = False
    config.developer.gitlab_username = "oauth2"
    for forge, binary in (("gitlab", "glab"), ("github", "gh")):
        setattr(config.developer, forge + "_url", "https://" + host)
        setattr(config.developer, forge + "_token", "fixture-forge-password")
        setattr(config.developer, binary + "_bin_path", shutil.which(binary) or "/missing")
    with db.get_db(config.db_path) as conn:
        sync_forge_bindings(conn, "alice", config.developer)
        for forge in ("gitlab", "github"):
            grants.put_grant(conn, "alice", "forge." + forge)
        task_id = db.create_task(conn, user_id="alice", prompt="test", source_type="talk", conversation_token="room-a")
    with db.get_db(config.db_path) as conn:
        grants.ensure_credential_grants(conn, task_id, "alice")
    proxy.broker.task_id = task_id
    temp = tmp_path / "client"
    temp.mkdir()
    env = setup_env(_Ctx(config, temp))
    env.pop("ISTOTA_PATH_PREPEND", None)
    env.update(PATH=os.environ["PATH"], HOME=str(temp), GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(ca.write_trust_bundle(authority, tmp_path / "trust"))
    bridge = tmp_path / "bridge"
    write_bridge_script(bridge)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(bridge), str(proxy.socket_path), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                assert process.poll() is None, "CONNECT bridge exited"
                assert time.monotonic() < deadline, "CONNECT bridge did not listen"
                time.sleep(.02)
        env["HTTPS_PROXY"] = env["https_proxy"] = f"http://127.0.0.1:{port}"
        yield env, temp, host, received
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()


@pytest.mark.parametrize("broker", ["forge.example.test"], indirect=True)
def test_real_git_clone_through_broker(forge_clients):
    env, temp, host, received = forge_clients
    result = subprocess.run(["git", "clone", "https://" + host + "/upstream.git", str(temp / "clone")],
                            env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    assert (temp / "clone/.git/HEAD").is_file()
    assert any(request.target.endswith(b"/git-upload-pack") for request, _ in received)
    assert b"fixture-forge-password" not in result.stdout + result.stderr


@pytest.mark.parametrize("broker", ["forge.example.test"], indirect=True)
@pytest.mark.parametrize("binary", ["gh", "glab"])
def test_real_forge_cli_through_broker(forge_clients, binary):
    import shutil
    import sys
    if not shutil.which(binary):
        pytest.skip(binary + " is not installed")
    if sys.platform != "linux":
        pytest.skip("Go SSL_CERT_FILE trust is supported on Linux; do not modify host trust")
    env, temp, host, received = forge_clients
    result = subprocess.run([str(temp / ".developer" / binary), "api", "user"],
                            env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["username"] == "alice"
    assert received
    assert b"fixture-forge-password" not in result.stdout + result.stderr


@pytest.mark.parametrize("broker", ["forge.example.test"], indirect=True)
def test_real_git_refuses_without_substitution(forge_clients, monkeypatch):
    from istota.credential_broker import intercept
    original = intercept._headers
    def no_substitution(state, request, host):
        _, replacements, names = original(state, request, host)
        return list(request.headers), replacements, names
    monkeypatch.setattr(intercept, "_headers", no_substitution)
    env, temp, host, received = forge_clients
    result = subprocess.run(["git", "clone", "https://" + host + "/upstream.git", str(temp / "clone")],
                            env=env, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b"Authentication failed" in result.stderr
    assert any(b"authorization" in dict(request.headers) for request, _ in received)
    assert not any(request.target.endswith(b"/git-upload-pack") for request, _ in received)
