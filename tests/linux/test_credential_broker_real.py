"""Real TLS substitution through the production bwrap and CONNECT bridge."""
import os
import shlex
import subprocess
import sys

import pytest

from istota import db
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd
from istota.sandbox.network_proxy import write_bridge_script
from istota.credential_broker import ca
from tests.test_credential_broker_intercept import broker as tls_broker, broker_responder, broker_scheme, VALUE, PLACEHOLDER  # noqa: F401 - shared fixture
from .test_sandbox_real import _can_unshare_net, _unavailable

broker = tls_broker
pytestmark = pytest.mark.linux


@pytest.fixture(autouse=True)
def real_bwrap():
    if sys.platform != "linux" or not _bwrap_available() or not _can_unshare_net():
        _unavailable("requires Linux bubblewrap with a network namespace")


class SubstitutionOracleFailure(AssertionError):
    """Only the upstream credential assertion may satisfy the negative control."""


def containment_probe(broker, tmp_path):
    config, task_id, authority, _, proxy, host, received = broker
    config.temp_dir = tmp_path / "tasks"
    config.workspace_path = tmp_path / "workspace"
    config.module_data_dir = tmp_path / "modules"
    (config.workspace_path / "Users" / "alice").mkdir(parents=True)
    user_temp = config.temp_dir / "alice"
    (user_temp / ".developer").mkdir(parents=True)
    write_bridge_script(user_temp / ".developer" / "net-bridge")
    control = config.temp_dir / ".control" / "alice" / f"task_{task_id}"
    trust = ca.write_trust_bundle(authority, control / "trust")
    task = db.Task(id=task_id, prompt="containment probe", user_id="alice", source_type="talk",
                   status="running", conversation_token="room-a")
    command = (
        "env; for f in /proc/[0-9]*/environ; do cat \"$f\" 2>/dev/null; done; "
        f"find {shlex.quote(str(user_temp))} -type f -exec cat {{}} \";\"; "
        "curl --retry 3 --retry-connrefused --retry-delay 1 --max-time 15 -v "
        f"-H 'Authorization: {PLACEHOLDER.decode()}' https://{host}/"
    )
    argv = build_bwrap_cmd(["/bin/sh", "-c", command], config, task, False, [], user_temp,
                           net_proxy_sock=proxy.socket_path, profile=SandboxProfile.NATIVE,
                           extra_ro_binds=[control], sandbox_env=trust)
    assert argv[0] == "bwrap"
    assert "--unshare-net" in argv
    result = subprocess.run(argv, capture_output=True, timeout=40,
                            env={"PATH": os.environ["PATH"], "HOME": str(user_temp)})
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    output = result.stdout + result.stderr
    assert received, "stub received no request"
    if dict(received[0][0].headers)[b"authorization"] != VALUE:
        raise SubstitutionOracleFailure("upstream substitution oracle")
    assert VALUE not in output, "credential entered sandbox output"
    assert PLACEHOLDER in output


def test_sandbox_contains_value_and_authenticates(broker, tmp_path):
    containment_probe(broker, tmp_path)


@pytest.mark.xfail(strict=True, raises=SubstitutionOracleFailure, reason="substitution-disabled upstream oracle must fail")
def test_substitution_disabled_negative_control(broker, tmp_path):
    # Preserve both TLS legs and request handling; only suppress replacement.
    from istota.credential_broker import intercept
    original = intercept._headers
    def no_substitution(state, request, host):
        _, replacements, names = original(state, request, host)
        return list(request.headers), replacements, names
    from unittest.mock import patch
    with patch.object(intercept, "_headers", no_substitution):
        containment_probe(broker, tmp_path)
