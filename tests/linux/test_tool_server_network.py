"""The CONNECT bridge still reaches a Bash child, one process further away.

`build_bwrap_cmd`'s network wrapper is a shell that starts the TCP-to-Unix
bridge and then `exec env HTTPS_PROXY=… HTTP_PROXY=… NO_PROXY= "$@"`. Under the
per-call sandbox the thing it `exec`ed *was* the Bash command, so the variables
landed directly on the process that made the request. Now it is the tool
server, and the command is that server's grandchild with an environment built
in the daemon — which has no bridge and no port.

`tool_server.merge_proxy_env` is what closes that gap, and this file is where
the closing is measured rather than asserted about a dict. Without it every
network-using build inside a native task fails with an error pointing nowhere
near the change: the allowlist is still enforced, the proxy is still running,
and the command simply cannot find it.

Three claims:

- an allowlisted host is reachable from a Bash child;
- an unlisted one is refused, and refused *by the proxy* rather than by DNS —
  which is the assertion that distinguishes a working allowlist from a broken
  network;
- and `NO_PROXY` arrives as the **empty string** rather than being dropped,
  because the wrapper sets it empty deliberately, to blank an inherited
  exemption list. A merge that tested truthiness would drop it and re-open
  whatever the daemon's own `NO_PROXY` exempts.

Run with `scripts/test-linux.sh`. Carries the `linux` marker. It also needs a
bwrap that can bring up loopback in a fresh network namespace, which is a
separate capability from creating one — see `_can_unshare_net`.
"""

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd
from istota.network_proxy import NetworkProxy, write_bridge_script
from istota.session.tools import hello_payload, start_tool_server

from .test_sandbox_real import _can_unshare_net, _unavailable

pytestmark = pytest.mark.linux


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")
    if not _can_unshare_net():
        _unavailable("needs CAP_NET_ADMIN in the namespace to bring up loopback")


def _q(path):
    return shlex.quote(str(path))


# --------------------------------------------------------------------------- #
# Shell probes. `tests/test_linux_probe_scripts.py` runs each under /bin/sh
# against a present and an absent target — see the note in
# `test_tool_server_real.py` for the defect class that guard exists for.
# --------------------------------------------------------------------------- #


def env_probe(names) -> str:
    """`NAME=[value]` per variable, and `NAME=UNSET` when it is not set.

    The bracket matters: `NO_PROXY` is legitimately the empty string here, and
    `NO_PROXY=` alone would be indistinguishable from a variable that arrived
    with no value *because it was dropped and re-created by something else*.
    `${X+set}` is the only test that separates set-and-empty from unset.
    """
    parts = []
    for name in names:
        parts.append(
            f'if [ -n "${{{name}+set}}" ]; then echo "{name}=[${{{name}}}]"; '
            f'else echo "{name}=UNSET"; fi'
        )
    return "; ".join(parts)


def fetch_probe(url: str, label: str, max_time: int = 20) -> str:
    """`LABEL=OK` on a 2xx/3xx, `LABEL=REFUSED` otherwise, printed either way.

    `-s -o /dev/null` so nothing of the body reaches the transcript, and an
    explicit `--max-time` so a blackholed connection fails rather than hanging
    out the tool's own timeout — the two are different findings and only one of
    them is about the allowlist. `max_time` is a parameter so the guard in
    `tests/test_linux_probe_scripts.py` can exercise the refusal arm against a
    routes-nowhere address without waiting twenty seconds for it.
    """
    return (
        f'if curl -s -o /dev/null --max-time {int(max_time)} {shlex.quote(url)}; '
        f'then echo "{label}=OK"; else echo "{label}=REFUSED"; fi'
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def layout(tmp_path, make_config):
    db_dir = tmp_path / "app" / "data"
    db_dir.mkdir(parents=True)
    (db_dir / "istota.db").write_text("db")
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    return make_config(
        db_path=db_dir / "istota.db",
        module_data_dir=tmp_path / "app" / "moduledbs",
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True),
    )


@pytest.fixture
def user_temp(layout):
    d = layout.temp_dir / "alice"
    (d / ".developer").mkdir(parents=True, exist_ok=True)
    write_bridge_script(d / ".developer" / "net-bridge")
    return d


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


@pytest.fixture
def proxy(tmp_path):
    """A live CONNECT proxy with a one-host allowlist.

    `example.com:443` because it is the host `tests/linux/test_sandbox_real.py`
    already reaches for, and because this file needs a *pair* — one allowed and
    one not — to say anything at all.
    """
    sock = tmp_path / "net.sock"
    with NetworkProxy(sock, ["example.com:443"]):
        yield sock


def _hello(user_temp):
    return hello_payload(
        cwd=user_temp,
        # No proxy variables here, deliberately: this is the daemon's view, and
        # the daemon has no bridge. If they appeared in this dict the test
        # would pass without `merge_proxy_env` existing at all.
        subprocess_env={"PATH": "/usr/bin:/bin", "HOME": str(user_temp)},
        read_roots=None,
        write_roots=None,
        write_denied_roots=(),
        deferred_dir=user_temp,
        bash_timeout_seconds=90,
        max_output_bytes=30_000,
        max_read_lines=2000,
        max_read_bytes=25_000_000,
        bash_spill_full_output=True,
    )


def _wrap_for(layout, task, user_temp, sock: Path):
    def _wrap(cmd):
        wrapped = build_bwrap_cmd(
            cmd, layout, task, False, [], user_temp,
            net_proxy_sock=sock, profile=SandboxProfile.NATIVE,
        )
        assert wrapped[0] == "bwrap", "sandbox unavailable — nothing below is meaningful"
        assert "--unshare-net" in wrapped, (
            "no network namespace, so an unlisted host would be reachable and "
            "the refusal below would prove nothing"
        )
        return wrapped

    return _wrap


def _text(result):
    return "".join(getattr(b, "text", "") for b in result.content)


def _run(layout, task, user_temp, sock, command):
    async def _go():
        server = await start_tool_server(
            _hello(user_temp), sandbox_wrap=_wrap_for(layout, task, user_temp, sock)
        )
        try:
            return _text(await server.call("Bash", "1", {"command": command}, None, None))
        finally:
            await server.aclose()

    return asyncio.run(_go())


class TestTheProxyVariablesReachABashChild:
    def test_all_three_arrive_and_no_proxy_is_set_and_empty(
        self, layout, task, user_temp, proxy,
    ):
        """The merge, observed where it has to be true: two processes below the
        shell that `env` set them on."""
        out = _run(
            layout, task, user_temp, proxy,
            env_probe(["HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"]),
        )
        assert "HTTPS_PROXY=[http://127.0.0.1:" in out, out
        assert "HTTP_PROXY=[http://127.0.0.1:" in out, out
        # Set and empty. `NO_PROXY=UNSET` is the failure a truthiness test in
        # `merge_proxy_env` produces, and it silently re-opens whatever the
        # daemon's own exemption list covers.
        assert "NO_PROXY=[]" in out, out

    def test_the_bridge_value_wins_a_collision(
        self, layout, task, user_temp, proxy, monkeypatch,
    ):
        """The daemon's own value is a *host* address with no route out of
        `--unshare-net`, so the bridge's wins. Measured here rather than
        argued: this is the one place both values exist at once."""
        async def _go():
            hello = _hello(user_temp)
            hello["subprocess_env"]["HTTPS_PROXY"] = "http://192.0.2.1:9"
            server = await start_tool_server(
                hello, sandbox_wrap=_wrap_for(layout, task, user_temp, proxy)
            )
            try:
                return _text(await server.call(
                    "Bash", "1", {"command": env_probe(["HTTPS_PROXY", "HTTP_PROXY"])},
                    None, None,
                ))
            finally:
                await server.aclose()

        out = asyncio.run(_go())
        assert "HTTPS_PROXY=[http://127.0.0.1:" in out, out
        assert "192.0.2.1" not in out, out
        # And the one the daemon did *not* name is there too, which is what
        # makes this a merge rather than a choice between two whole
        # environments.
        assert "HTTP_PROXY=[http://127.0.0.1:" in out, out


class TestTheAllowlistStillDecides:
    def test_an_allowlisted_host_is_reachable(self, layout, task, user_temp, proxy):
        """The positive control for the refusal below, and the assertion that
        actually proves the merge: without the variables this fails too, for a
        different reason and with the same output."""
        out = _run(
            layout, task, user_temp, proxy,
            fetch_probe("https://example.com/", "ALLOWED"),
        )
        assert "ALLOWED=OK" in out, out

    def test_an_unlisted_host_is_refused(self, layout, task, user_temp, proxy):
        out = _run(
            layout, task, user_temp, proxy,
            fetch_probe("https://example.org/", "UNLISTED"),
        )
        assert "UNLISTED=REFUSED" in out, out

    def test_the_refusal_comes_from_the_proxy_rather_than_from_no_network(
        self, layout, task, user_temp, proxy,
    ):
        """Both halves in one command, one server, one namespace.

        Run apart, "allowed works" and "unlisted fails" can both be true of a
        namespace where nothing works and one where everything does — the pair
        in a single session is what makes each one mean something.
        """
        out = _run(
            layout, task, user_temp, proxy,
            fetch_probe("https://example.com/", "ALLOWED")
            + "; "
            + fetch_probe("https://example.org/", "UNLISTED"),
        )
        assert "ALLOWED=OK" in out, out
        assert "UNLISTED=REFUSED" in out, out
