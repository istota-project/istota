"""The shared-credential fetch path: the proxy branches, the cap, the shim.

Three layers, and the file is arranged around what each of them can be wrong
about.

The **proxy** owns every rule: which names exist, how many fetches an attempt
may make, and what a refusal says. Those tests drive a real ``SkillProxy`` over
a real ``AF_UNIX`` socket with hand-built JSON, because the wire is what a
hand-rolled five-line client speaks and the shim is a convenience sitting on
top of it.

The **shim** owns only ergonomics, and the one property it has to keep is that
``run`` puts the value in front of the child and nowhere else. Its tests drive a
real child process against a live proxy and read back what the child could see —
its environment, its own ``/proc/self/cmdline`` where there is one, and the
shim's own stdout.

``build_task_runtime`` owns the placement: the namespace reaches the proxy, no
value reaches either environment, and the shim's directory is on the model's
PATH and on nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from istota import credential_shim, executor, secrets_store, secrets_vault, task_env
from istota.config import Config, DevboxConfig, SecurityConfig
from istota.skill_proxy import SkillProxy

#: Distinct enough that a sweep over two whole environment dicts means
#: something. Every one of these is a *value*; the names beside them are
#: deliberately ordinary.
VAULT = {
    "github_pat": "vaultvalue-ghp-aaaaaaaaaaaa",
    "home_assistant_token": "vaultvalue-hass-bbbbbbbbbbbb",
    "home_assistant_url": "https://hass.example.test",
}


@pytest.fixture
def sock_path():
    """Short socket path that fits the AF_UNIX limit (~104 chars on macOS)."""
    d = tempfile.mkdtemp(prefix="vc_", dir="/tmp")
    p = Path(d) / "s.sock"
    yield p
    p.unlink(missing_ok=True)
    Path(d).rmdir()


def request(sock_path, payload):
    """One JSON line to the proxy, one back. The wire, not the shim."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(sock_path))
    sock.sendall((json.dumps(payload) + "\n").encode())
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    sock.close()
    return json.loads(b"".join(chunks).decode().strip())


def proxy(sock_path, **kwargs):
    kwargs.setdefault("vault_credentials", dict(VAULT))
    return SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}, task_id=7, **kwargs)


# ---------------------------------------------------------------------------
# The proxy branches
# ---------------------------------------------------------------------------


class TestVaultList:
    def test_names_are_returned_sorted_and_values_are_not(self, sock_path):
        with proxy(sock_path):
            reply = request(sock_path, {"type": "vault_list"})
        assert reply == {"names": sorted(VAULT)}
        blob = json.dumps(reply)
        for value in VAULT.values():
            assert value not in blob

    def test_an_empty_namespace_lists_nothing(self, sock_path):
        with proxy(sock_path, vault_credentials={}):
            reply = request(sock_path, {"type": "vault_list"})
        assert reply == {"names": []}


class TestVaultCredential:
    def test_a_present_name_returns_its_value(self, sock_path):
        with proxy(sock_path):
            reply = request(
                sock_path,
                {"type": "vault_credential", "name": "github_pat", "mode": "inject"},
            )
        assert reply == {"value": VAULT["github_pat"]}

    def test_an_absent_name_is_refused(self, sock_path):
        with proxy(sock_path):
            reply = request(
                sock_path, {"type": "vault_credential", "name": "nope"},
            )
        assert reply["reason"] == "vault_credential_not_present"
        assert "value" not in reply

    def test_the_passphrase_is_not_in_the_namespace(self, sock_path, tmp_path):
        """`vault/passphrase` and `vault_entries/*` are separate services, and
        that separation is what makes the task-side read unable to return the
        passphrase — a rule nobody has to remember rather than an exemption.

        Driven through the real store and the real resolution rather than a
        hand-built dict, because the claim is about which service
        `_vault_credentials` asks for.
        """
        db_path = tmp_path / "framework.db"
        from istota import db as framework_db

        framework_db.init_db(db_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
            secrets_store.upsert_secret(
                db_path, "alice", "vault", "passphrase", "the-passphrase",
            )
            config = Config(db_path=db_path)
            resolved = task_env._vault_credentials(config, "alice")

        assert resolved == {}
        with proxy(sock_path, vault_credentials=resolved):
            reply = request(
                sock_path, {"type": "vault_credential", "name": "passphrase"},
            )
        assert reply["reason"] == "vault_credential_not_present"

    def test_the_two_namespaces_do_not_read_each_other(self, sock_path):
        """The manifest allowlist and the user's vault are separate name spaces
        and one branch may not answer from the other's dict."""
        with SkillProxy(
            sock_path,
            {"KARAKEEP_API_KEY": "manifest-secret"},
            {"PATH": "/usr/bin"},
            vault_credentials=dict(VAULT),
        ):
            as_vault = request(
                sock_path,
                {"type": "vault_credential", "name": "KARAKEEP_API_KEY"},
            )
            as_manifest = request(
                sock_path, {"type": "credential", "name": "github_pat"},
            )
        assert as_vault["reason"] == "vault_credential_not_present"
        assert as_manifest["reason"] == "credential_not_present"

    def test_a_vault_value_is_never_merged_into_a_skill_subprocess_env(
        self, sock_path, monkeypatch,
    ):
        """The skill dispatch merges `credential_env` wholesale when no
        per-skill map is given. A vault value living in that dict would be
        handed to every skill CLI; they are separate dicts so it cannot."""
        captured = {}

        class _Result:
            stdout, stderr, returncode = "{}", "", 0

        def _fake_run(cmd, env=None, **kwargs):
            captured.update(env or {})
            return _Result()

        monkeypatch.setattr("istota.skill_proxy.subprocess.run", _fake_run)
        with proxy(sock_path, allowed_skills=frozenset({"email"})):
            request(sock_path, {"skill": "email", "args": ["send"]})

        blob = "\n".join(f"{k}={v}" for k, v in captured.items())
        for name, value in VAULT.items():
            assert value not in blob, name


class TestTheFetchLog:
    def _levels(self, caplog, marker):
        return [r.levelno for r in caplog.records if marker in r.getMessage()]

    def test_an_injection_logs_at_info_and_a_read_at_warning(
        self, sock_path, caplog,
    ):
        with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
            with proxy(sock_path):
                request(sock_path, {
                    "type": "vault_credential",
                    "name": "github_pat",
                    "mode": "inject",
                })
                request(sock_path, {
                    "type": "vault_credential",
                    "name": "home_assistant_token",
                    "mode": "read",
                })
        assert self._levels(caplog, "name=github_pat") == [logging.INFO]
        assert self._levels(caplog, "name=home_assistant_token") == [
            logging.WARNING
        ]

    def test_an_unrecognised_mode_is_recorded_as_read(self, sock_path, caplog):
        """The direction that does not under-report: `mode` is a claim by
        whoever holds the socket, so an absent or invented one must not be able
        to buy the quieter level."""
        with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
            with proxy(sock_path):
                request(sock_path, {
                    "type": "vault_credential",
                    "name": "github_pat",
                    "mode": "something-else",
                })
                request(sock_path, {
                    "type": "vault_credential", "name": "home_assistant_url",
                })
        assert self._levels(caplog, "name=github_pat") == [logging.WARNING]
        assert self._levels(caplog, "name=home_assistant_url") == [
            logging.WARNING
        ]
        assert all(
            "mode=read" in r.getMessage()
            for r in caplog.records
            if "vault_credential task_id" in r.getMessage()
        )

    def test_no_value_reaches_any_log_record(self, sock_path, caplog):
        with caplog.at_level(logging.DEBUG, logger="istota.skill_proxy"):
            with proxy(sock_path):
                for name in VAULT:
                    request(sock_path, {
                        "type": "vault_credential", "name": name,
                        "mode": "inject",
                    })
                request(sock_path, {"type": "vault_list"})
                request(sock_path, {
                    "type": "vault_credential", "name": "absent",
                })
        blob = "\n".join(r.getMessage() for r in caplog.records)
        for value in VAULT.values():
            assert value not in blob

    def test_a_name_off_the_socket_is_bounded_and_flattened(
        self, sock_path, caplog,
    ):
        """The name is attacker-chosen outright — it comes off a socket any
        process in the sandbox can speak to — so an unflattened one could forge
        a record in the daemon's own log."""
        hostile = "a\nproxy_rejected task_id=0 forged=yes " + "b" * 200
        with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
            with proxy(sock_path):
                reply = request(
                    sock_path,
                    {"type": "vault_credential", "name": hostile},
                )
        assert caplog.records
        for record in caplog.records:
            message = record.getMessage()
            # The forged text survives as *text*; what it cannot do is start a
            # second record, which is the whole of what the flatten buys.
            assert "\n" not in message
            # And the bound is what stops one request filling the log.
            assert len(message) < 300
        assert "\n" not in reply["name"]
        assert len(reply["name"]) <= secrets_vault._LABEL_MAX_CHARS + 1


class TestTheFetchCap:
    def test_the_eleventh_fetch_is_refused_at_a_limit_of_ten(self, sock_path):
        with proxy(sock_path, vault_fetch_limit=10):
            for _ in range(10):
                reply = request(sock_path, {
                    "type": "vault_credential", "name": "github_pat",
                })
                assert reply == {"value": VAULT["github_pat"]}
            refused = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
        assert refused["reason"] == "vault_credential_limit"

    def test_the_refusal_names_no_credential(self, sock_path):
        """A refusal that named one would make the cap an enumeration oracle:
        a present name and an absent one would answer differently past it."""
        with proxy(sock_path, vault_fetch_limit=1):
            request(sock_path, {"type": "vault_credential", "name": "github_pat"})
            present = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
            absent = request(sock_path, {
                "type": "vault_credential", "name": "definitely_not_there",
            })
        assert present == absent
        assert present["reason"] == "vault_credential_limit"
        assert "github_pat" not in json.dumps(present)

    def test_absent_names_spend_the_budget(self, sock_path):
        """Counting only successful lookups makes probing for absent names free,
        which is the enumeration the cap exists to bound. The whole budget goes
        on names that do not exist, and a name that does is then refused."""
        with proxy(sock_path, vault_fetch_limit=3):
            for index in range(3):
                reply = request(sock_path, {
                    "type": "vault_credential", "name": f"absent_{index}",
                })
                assert reply["reason"] == "vault_credential_not_present"
            after = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
        assert after["reason"] == "vault_credential_limit"

    def test_repeating_one_name_spends_the_budget(self, sock_path):
        """Counted per request, not per distinct name: a loop over one name is
        the case the cap cannot distinguish from a legitimate one."""
        with proxy(sock_path, vault_fetch_limit=2):
            request(sock_path, {"type": "vault_credential", "name": "github_pat"})
            request(sock_path, {"type": "vault_credential", "name": "github_pat"})
            third = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
        assert third["reason"] == "vault_credential_limit"

    def test_listing_does_not_count(self, sock_path):
        """Interleaved, so the assertion is about the listing rather than about
        the order of the two kinds of request."""
        with proxy(sock_path, vault_fetch_limit=2):
            request(sock_path, {"type": "vault_list"})
            request(sock_path, {"type": "vault_credential", "name": "github_pat"})
            request(sock_path, {"type": "vault_list"})
            second = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
            request(sock_path, {"type": "vault_list"})
        assert second == {"value": VAULT["github_pat"]}

    def test_zero_is_unlimited(self, sock_path):
        with proxy(sock_path, vault_fetch_limit=0):
            for _ in range(25):
                reply = request(sock_path, {
                    "type": "vault_credential", "name": "github_pat",
                })
        assert reply == {"value": VAULT["github_pat"]}

    def test_a_second_proxy_starts_at_zero(self, sock_path):
        """Per task attempt, held in memory, and that is the whole lifetime:
        `build_task_runtime` constructs one proxy per attempt, so a retry gets
        a fresh budget."""
        with proxy(sock_path, vault_fetch_limit=1):
            request(sock_path, {"type": "vault_credential", "name": "github_pat"})
            assert request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })["reason"] == "vault_credential_limit"
        with proxy(sock_path, vault_fetch_limit=1):
            reply = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
        assert reply == {"value": VAULT["github_pat"]}

    def test_the_cap_holds_under_concurrent_connections(self, sock_path):
        """`unix_server` runs one thread per connection, so the counter is
        locked. Without the lock two threads can read the same value and both
        pass a budget of one."""
        results: list[dict] = []
        lock = threading.Lock()

        def _fetch():
            reply = request(sock_path, {
                "type": "vault_credential", "name": "github_pat",
            })
            with lock:
                results.append(reply)

        with proxy(sock_path, vault_fetch_limit=4):
            threads = [threading.Thread(target=_fetch) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        served = [r for r in results if "value" in r]
        assert len(results) == 16
        assert len(served) == 4

    def test_the_cap_logs_the_count_and_the_limit_and_no_name(
        self, sock_path, caplog,
    ):
        with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
            with proxy(sock_path, vault_fetch_limit=1):
                request(sock_path, {
                    "type": "vault_credential", "name": "github_pat",
                })
                request(sock_path, {
                    "type": "vault_credential", "name": "github_pat",
                })
        refusals = [
            r for r in caplog.records
            if "vault_credential_limit" in r.getMessage()
        ]
        assert len(refusals) == 1
        assert refusals[0].levelno == logging.WARNING
        assert "count=2" in refusals[0].getMessage()
        assert "limit=1" in refusals[0].getMessage()
        assert "github_pat" not in refusals[0].getMessage()


# ---------------------------------------------------------------------------
# The shim
# ---------------------------------------------------------------------------


SHIM = Path(credential_shim.__file__)


def run_shim(sock_path, args, *, env=None, **kwargs):
    environ = dict(os.environ)
    environ["ISTOTA_SKILL_PROXY_SOCK"] = str(sock_path)
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, str(SHIM), *args],
        env=environ, capture_output=True, text=True, timeout=60, **kwargs,
    )


#: A child that reports everything it can see about how it was invoked. Reads
#: its own `/proc/self/cmdline` where there is one, because the claim "the value
#: is not in the argv" has to be made from inside the process whose argv it is.
REPORTER = r"""
import json, os, sys
try:
    with open("/proc/self/cmdline", "rb") as fh:
        raw = fh.read().decode("utf-8", "replace")
except OSError:
    raw = "\x00".join(sys.argv)
print(json.dumps({
    "env": dict(os.environ),
    "argv": sys.argv,
    "cmdline": raw,
    "stdin": sys.stdin.read(),
}))
"""


class TestTheShimListVerb:
    def test_it_prints_the_names_one_per_line(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, ["list"])
        assert result.returncode == 0
        assert result.stdout.split() == sorted(VAULT)
        for value in VAULT.values():
            assert value not in result.stdout

    def test_an_empty_namespace_prints_nothing_and_exits_zero(self, sock_path):
        with proxy(sock_path, vault_credentials={}):
            result = run_shim(sock_path, ["list"])
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "Traceback" not in result.stderr


class TestTheShimRunVerb:
    def _drive(self, sock_path, args, **kwargs):
        with proxy(sock_path, **kwargs):
            return run_shim(
                sock_path,
                ["run", *args, "--", sys.executable, "-c", REPORTER],
                input="",
            )

    def test_the_child_sees_the_variable(self, sock_path):
        result = self._drive(sock_path, ["TOKEN=github_pat"])
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["env"]["TOKEN"] == VAULT["github_pat"]

    def test_no_part_of_the_value_is_in_the_shims_own_stdout(self, sock_path):
        result = self._drive(sock_path, ["TOKEN=github_pat"])
        payload = json.loads(result.stdout)
        # Everything the shim itself wrote, which is the child's output and
        # nothing else. The value is inside the JSON because the *child* was
        # asked to report its own environment.
        del payload["env"]["TOKEN"]
        assert VAULT["github_pat"] not in json.dumps(payload)
        assert VAULT["github_pat"] not in result.stderr

    def test_the_value_is_not_in_the_childs_argv(self, sock_path):
        result = self._drive(sock_path, ["TOKEN=github_pat"])
        payload = json.loads(result.stdout)
        assert VAULT["github_pat"] not in payload["cmdline"]
        assert VAULT["github_pat"] not in " ".join(payload["argv"])

    def test_several_variables_at_once(self, sock_path):
        result = self._drive(
            sock_path,
            ["TOKEN=home_assistant_token", "URL=home_assistant_url"],
        )
        env = json.loads(result.stdout)["env"]
        assert env["TOKEN"] == VAULT["home_assistant_token"]
        assert env["URL"] == VAULT["home_assistant_url"]

    def test_the_childs_other_variables_are_inherited(self, sock_path):
        with proxy(sock_path):
            result = run_shim(
                sock_path,
                ["run", "TOKEN=github_pat", "--", sys.executable, "-c", REPORTER],
                env={"CARRIED_THROUGH": "yes"},
                input="",
            )
        assert json.loads(result.stdout)["env"]["CARRIED_THROUGH"] == "yes"

    def test_the_childs_exit_status_is_the_shims(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", "TOKEN=github_pat", "--", sys.executable, "-c",
                "import sys; sys.exit(37)",
            ])
        assert result.returncode == 37

    def test_a_child_killed_by_a_signal_is_reported_as_a_shell_would(
        self, sock_path,
    ):
        """`execvpe` replaced the shim's own process, so the signal is the
        child's and nothing invents a number for it."""
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", "TOKEN=github_pat", "--", sys.executable, "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ])
        assert result.returncode == -15

    def test_stdin_survives_a_closed_fd_zero(self, sock_path):
        """With stdin closed, `os.pipe()` may hand back fd 0 as the read end —
        `dup2(0, 0)` is then a no-op and closing the source would give the child
        no stdin at all rather than the value."""
        with proxy(sock_path):
            result = run_shim(
                sock_path,
                ["run", "--stdin", "github_pat", "--",
                 sys.executable, "-c", REPORTER],
                preexec_fn=lambda: os.close(0),
            )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["stdin"] == VAULT["github_pat"]

    def test_stdin_delivers_the_value_and_sets_no_variable(self, sock_path):
        result = self._drive(sock_path, ["--stdin", "github_pat"])
        payload = json.loads(result.stdout)
        assert payload["stdin"] == VAULT["github_pat"]
        assert not [k for k in payload["env"] if VAULT["github_pat"] in k]
        assert VAULT["github_pat"] not in json.dumps(payload["env"])
        assert VAULT["github_pat"] not in payload["cmdline"]

    def test_an_absent_name_executes_nothing(self, sock_path):
        """Running the command without the variable would be worse: a `curl`
        with an empty bearer token gets a 401 the model debugs in the wrong
        place."""
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", "TOKEN=not_there", "--", sys.executable, "-c",
                "print('THE CHILD RAN')",
            ])
        assert result.returncode == 1
        assert "THE CHILD RAN" not in result.stdout

    def test_a_later_absent_name_executes_nothing_either(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", "A=github_pat", "B=not_there", "--",
                sys.executable, "-c", "print('THE CHILD RAN')",
            ])
        assert result.returncode == 1
        assert "THE CHILD RAN" not in result.stdout

    @pytest.mark.parametrize("var", ["PATH", "LD_PRELOAD", "ISTOTA_SKILL_PROXY_SOCK"])
    def test_a_reserved_variable_is_refused(self, sock_path, var):
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", f"{var}=github_pat", "--", sys.executable, "-c",
                "print('THE CHILD RAN')",
            ])
        assert result.returncode == 1
        assert "THE CHILD RAN" not in result.stdout

    @pytest.mark.parametrize("token", ["1TOKEN=x", "TO-KEN=x", "=x", "TOKEN=", "TOKEN"])
    def test_a_malformed_assignment_is_refused(self, sock_path, token):
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", token, "--", sys.executable, "-c",
                "print('THE CHILD RAN')",
            ])
        assert result.returncode == 1
        assert "THE CHILD RAN" not in result.stdout

    def test_a_missing_separator_is_refused(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", "TOKEN=github_pat", sys.executable, "-c",
                "print('THE CHILD RAN')",
            ])
        assert result.returncode == 1
        assert "THE CHILD RAN" not in result.stdout
        assert "Usage" in result.stderr

    def test_nothing_after_the_separator_is_refused(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, ["run", "TOKEN=github_pat", "--"])
        assert result.returncode == 1
        assert "Usage" in result.stderr

    def test_a_command_that_does_not_exist_reports_127(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, [
                "run", "TOKEN=github_pat", "--", "no-such-program-anywhere",
            ])
        assert result.returncode == 127

    def test_the_refusals_reach_the_proxy_before_anything_is_executed(
        self, sock_path,
    ):
        """A malformed variable is refused without spending a fetch, because
        parsing happens before any request goes out."""
        with proxy(sock_path, vault_fetch_limit=1) as live:
            run_shim(sock_path, [
                "run", "1BAD=github_pat", "--", sys.executable, "-c", "pass",
            ])
            assert live._vault_fetches == 0


class TestTheShimGetVerb:
    def test_it_prints_the_value_with_no_trailing_newline(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, ["get", "github_pat"])
        assert result.returncode == 0
        assert result.stdout == VAULT["github_pat"]

    def test_it_is_logged_at_warning(self, sock_path, caplog):
        with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
            with proxy(sock_path):
                run_shim(sock_path, ["get", "github_pat"])
        served = [
            r for r in caplog.records if "vault_credential task_id" in r.getMessage()
        ]
        assert [r.levelno for r in served] == [logging.WARNING]
        assert "mode=read" in served[0].getMessage()

    def test_an_absent_name_exits_one(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, ["get", "not_there"])
        assert result.returncode == 1
        assert result.stdout == ""


class TestTheShimEnvVerb:
    def test_it_serves_the_manifest_allowlist(self, sock_path):
        with SkillProxy(
            sock_path, {"GITLAB_TOKEN": "glpat-manifest"}, {"PATH": "/usr/bin"},
        ):
            result = run_shim(sock_path, ["env", "GITLAB_TOKEN"])
        assert result.returncode == 0
        assert result.stdout == "glpat-manifest"

    def test_it_does_not_count_against_the_vault_budget(self, sock_path):
        with SkillProxy(
            sock_path,
            {"GITLAB_TOKEN": "glpat-manifest"},
            {"PATH": "/usr/bin"},
            vault_credentials=dict(VAULT),
            vault_fetch_limit=1,
        ) as live:
            run_shim(sock_path, ["env", "GITLAB_TOKEN"])
            run_shim(sock_path, ["env", "GITLAB_TOKEN"])
            assert live._vault_fetches == 0
            result = run_shim(sock_path, ["get", "github_pat"])
        assert result.returncode == 0

    def test_it_cannot_reach_a_vault_name(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, ["env", "github_pat"])
        assert result.returncode == 1


class TestTheShimWithNoSocket:
    def test_every_verb_exits_two(self, tmp_path):
        environ = dict(os.environ)
        environ.pop("ISTOTA_SKILL_PROXY_SOCK", None)
        for args in (["list"], ["get", "x"], ["env", "X"],
                     ["run", "A=x", "--", "true"]):
            result = subprocess.run(
                [sys.executable, str(SHIM), *args],
                env=environ, capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 2, args
            assert "Traceback" not in result.stderr

    def test_an_unknown_verb_is_a_usage_error(self, sock_path):
        with proxy(sock_path):
            result = run_shim(sock_path, ["frobnicate"])
        assert result.returncode == 1
        assert "Usage" in result.stderr


# ---------------------------------------------------------------------------
# Placement: build_task_runtime
# ---------------------------------------------------------------------------


def _config(tmp_path, **security):
    sec = {"sandbox_enabled": True, "skill_proxy_enabled": True}
    sec.update(security)
    (tmp_path / "db").mkdir(exist_ok=True)
    return Config(
        db_path=tmp_path / "db" / "test.db",
        temp_dir=tmp_path / "temp",
        workspace_path=tmp_path / "mount",
        security=SecurityConfig(**sec),
        devbox=DevboxConfig(enabled=False),
    )


@pytest.fixture
def runtime_inputs(tmp_path, make_task):
    user_temp_dir = tmp_path / "temp" / "testuser"
    user_temp_dir.mkdir(parents=True)
    control_dir = tmp_path / "temp" / ".control" / "testuser" / "task_1"
    control_dir.mkdir(parents=True)
    return {
        "task": make_task(id=1, user_id="testuser"),
        "user_temp_dir": user_temp_dir,
        "control_dir": control_dir,
        "task_attempt": 1,
        "selected_skills": [],
        "skill_index": {},
        "is_admin": True,
        "user_resources": [],
        "user_config": None,
        "discovered_calendars": [],
    }


@pytest.fixture
def seeded_runtime(tmp_path, runtime_inputs, monkeypatch):
    """A runtime built against a real database holding a real vault namespace."""
    monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = _config(tmp_path)
    from istota import db as framework_db

    framework_db.init_db(config.db_path)
    for key, value in VAULT.items():
        secrets_store.upsert_secret(
            config.db_path, "testuser", secrets_vault.VAULT_ENTRY_SERVICE,
            key, value,
        )
    return task_env.build_task_runtime(config, **runtime_inputs)


class TestThePlacement:
    def test_the_namespace_reaches_the_proxy(self, seeded_runtime):
        assert seeded_runtime.proxy_ctx.vault_credentials == VAULT

    def test_the_configured_limit_reaches_the_proxy(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        config.security.vault_fetch_limit_per_task = 3
        runtime = task_env.build_task_runtime(config, **runtime_inputs)
        assert runtime.proxy_ctx.vault_fetch_limit == 3

    def test_no_vault_value_is_in_either_environment(self, seeded_runtime):
        """Written as a sweep over both dicts rather than as a check on named
        keys: what the proxy exists to prevent is a value reaching an
        environment at all, by any name anybody later invents."""
        haystacks = {
            "model env": seeded_runtime.env,
            "proxy base env": seeded_runtime.proxy_ctx.base_env,
            "proxy credential env": seeded_runtime.proxy_ctx.credential_env,
        }
        for label, env in haystacks.items():
            blob = "\n".join(f"{k}={v}" for k, v in env.items())
            for name, value in VAULT.items():
                assert value not in blob, f"{name} leaked into the {label}"

    def test_a_user_with_no_vault_gets_an_empty_namespace(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path)
        from istota import db as framework_db

        framework_db.init_db(config.db_path)
        runtime = task_env.build_task_runtime(config, **runtime_inputs)
        assert runtime.proxy_ctx.vault_credentials == {}


class TestTheShimPlacement:
    def test_it_is_written_owner_only(self, seeded_runtime, runtime_inputs):
        shim = credential_shim.shim_path(runtime_inputs["user_temp_dir"])
        assert shim.exists()
        assert shim.stat().st_mode & 0o777 == 0o700
        assert shim.parent.stat().st_mode & 0o777 == 0o700

    def test_it_is_the_module_verbatim(self, seeded_runtime, runtime_inputs):
        shim = credential_shim.shim_path(runtime_inputs["user_temp_dir"])
        assert shim.read_text() == SHIM.read_text()

    def test_its_directory_is_on_the_models_path(
        self, seeded_runtime, runtime_inputs,
    ):
        shim_dir = str(
            credential_shim.shim_path(runtime_inputs["user_temp_dir"]).parent
        )
        assert shim_dir in seeded_runtime.env["PATH"].split(os.pathsep)

    def test_its_directory_is_not_on_the_proxys_path(
        self, seeded_runtime, runtime_inputs,
    ):
        """A task-writable directory on a host-side skill CLI's PATH is a
        code-execution path no bind contains — the rule `HOOK_PATH_PREPEND_KEY`
        already follows, with the comment at its application site."""
        shim_dir = str(
            credential_shim.shim_path(runtime_inputs["user_temp_dir"]).parent
        )
        proxy_path = seeded_runtime.proxy_ctx.base_env["PATH"]
        assert shim_dir not in proxy_path.split(os.pathsep)

    def test_the_developer_hook_entries_win_the_search(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        """`.developer` is re-bound read-only inside the sandbox and `.istota`
        is not, so a shim directory ahead of it would let the model shadow the
        read-only forge wrappers with a file of its own."""
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        prepend = str(runtime_inputs["user_temp_dir"] / ".developer")
        monkeypatch.setattr(
            "istota.skills._env.dispatch_setup_env_hooks",
            lambda selected, index, ctx: {
                executor.HOOK_PATH_PREPEND_KEY: prepend,
            },
        )
        runtime = task_env.build_task_runtime(_config(tmp_path), **runtime_inputs)
        entries = runtime.env["PATH"].split(os.pathsep)
        shim_dir = str(
            credential_shim.shim_path(runtime_inputs["user_temp_dir"]).parent
        )
        assert entries.index(prepend) < entries.index(shim_dir)

    def test_a_decode_failure_degrades_rather_than_failing_the_task(
        self, tmp_path, monkeypatch,
    ):
        """`build_task_runtime` is called from `execute_task` with no handler
        around it, so the write's never-raises contract has to cover
        `ValueError` as well as `OSError` — a non-UTF-8 locale makes the source
        read raise `UnicodeDecodeError`, which is the first and not the
        second."""
        monkeypatch.setattr(
            "istota.atomic_write.write_text_atomic",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad codec")),
        )
        user_temp = tmp_path / "temp" / "testuser"
        user_temp.mkdir(parents=True)
        assert task_env._write_credential_shim(user_temp) is None

    def test_nothing_is_written_when_the_proxy_is_off(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        config = _config(tmp_path, skill_proxy_enabled=False)
        runtime = task_env.build_task_runtime(config, **runtime_inputs)
        shim = credential_shim.shim_path(runtime_inputs["user_temp_dir"])
        assert not shim.exists()
        assert str(shim.parent) not in runtime.env["PATH"].split(os.pathsep)

    def test_it_is_written_for_a_user_with_no_vault(
        self, tmp_path, runtime_inputs, monkeypatch,
    ):
        """Writing it is cheaper than deciding not to, and its verbs answer
        honestly against an empty namespace."""
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        runtime = task_env.build_task_runtime(_config(tmp_path), **runtime_inputs)
        assert runtime.proxy_ctx.vault_credentials == {}
        assert credential_shim.shim_path(
            runtime_inputs["user_temp_dir"]
        ).exists()


class TestTheDeveloperGitHelper:
    def test_it_fetches_its_token_through_the_shim(self, tmp_path, sock_path):
        """The one consumer the `env` verb exists for, driven end to end.

        `setup_env` writes the helper naming a shim it cannot see yet — the
        hook runs before the proxy branch that writes it — so this is also what
        holds the two sides' idea of the path equal. A source assertion could
        not: both would be wrong together.
        """
        from istota.config import DeveloperConfig
        from istota.skills.developer import setup_env

        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        repos = tmp_path / "repos"
        repos.mkdir()
        config = _config(tmp_path)
        config.developer = DeveloperConfig(
            enabled=True,
            repos_dir=str(repos),
            gitlab_token="glpat-from-the-proxy",
            gitlab_username="istota-bot",
        )

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.config = config
        ctx.user_temp_dir = str(user_temp)
        ctx.is_admin = True
        ctx.task = type("T", (), {"user_id": "alice"})()
        setup_env(ctx)

        assert task_env._write_credential_shim(user_temp) is not None
        helper = user_temp / ".developer" / "git-credential-helper"

        with SkillProxy(
            sock_path,
            {"GITLAB_TOKEN": "glpat-from-the-proxy"},
            {"PATH": "/usr/bin"},
        ):
            result = subprocess.run(
                ["/bin/sh", str(helper), "get"],
                env={
                    **os.environ,
                    "ISTOTA_SKILL_PROXY_SOCK": str(sock_path),
                },
                capture_output=True, text=True, timeout=60,
            )

        assert result.returncode == 0, result.stderr
        assert "username=istota-bot" in result.stdout
        assert "password=glpat-from-the-proxy" in result.stdout


# ---------------------------------------------------------------------------
# The prompt gate
# ---------------------------------------------------------------------------


class TestThePromptGate:
    def _db(self, tmp_path, rows):
        from istota import db as framework_db

        db_path = tmp_path / "framework.db"
        framework_db.init_db(db_path)
        with framework_db.get_db(db_path) as conn:
            for service, key in rows:
                conn.execute(
                    "INSERT INTO secrets (user_id, service, key, encrypted_value) "
                    "VALUES (?, ?, ?, ?)",
                    ("alice", service, key, b"ciphertext"),
                )
            conn.commit()
        return db_path

    def test_presence_never_decrypts(self, tmp_path, monkeypatch):
        """One `list_user_services` read: key names and timestamps, no Fernet,
        no `last_accessed_at` bump. The gate runs on every task assembly, and
        the row's ciphertext here is not ciphertext at all — a gate that
        decrypted could not answer True against it."""
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        db_path = self._db(tmp_path, [("vault_entries", "github_pat")])
        assert secrets_vault.has_shared_credentials(db_path, "alice") is True

    def test_no_master_key_is_no_namespace(self, tmp_path, monkeypatch):
        """The serving read (`get_service_secrets`) returns `{}` outright with
        no key, so a gate that answered True here would put a line in the
        system half about credentials `istota-credential list` cannot return,
        with nothing anywhere saying why."""
        monkeypatch.delenv("ISTOTA_SECRET_KEY", raising=False)
        db_path = self._db(tmp_path, [("vault_entries", "github_pat")])
        assert secrets_vault.has_shared_credentials(db_path, "alice") is False

    def test_the_passphrase_alone_is_not_a_namespace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        db_path = self._db(tmp_path, [("vault", "passphrase")])
        assert secrets_vault.has_shared_credentials(db_path, "alice") is False

    def test_another_users_namespace_does_not_count(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        db_path = self._db(tmp_path, [("vault_entries", "github_pat")])
        assert secrets_vault.has_shared_credentials(db_path, "bob") is False

    def test_a_missing_database_is_false_rather_than_a_raise(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        assert secrets_vault.has_shared_credentials(None, "alice") is False
        assert secrets_vault.has_shared_credentials(
            tmp_path / "nope.db", "alice",
        ) is False
