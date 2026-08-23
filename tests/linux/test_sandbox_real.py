"""What is true *inside* the bubblewrap namespace, on a real Linux kernel.

`tests/test_sandbox.py` asserts that `build_bwrap_cmd` puts `--tmpfs` in argv.
That is the only thing it can assert on darwin, where `_bwrap_available` is
patched and bwrap has never run. These tests build the same argv and then
execute it around a `/bin/sh` probe, so the assertion is about the namespace
rather than about the command line that was meant to produce it.

Run them with `scripts/test-linux.sh`. They carry the `linux` marker, which
pyproject's addopts deselects, so `uv run pytest` on a host without Docker or
bubblewrap is unaffected — that is deliberate, not an oversight.

**An assertion that a nested user namespace cannot lift a mask must probe for
`--disable-userns` rather than assume it.** `build_bwrap_cmd` passes the flag
where bwrap supports it. In container mode bwrap does not: the flag needs
`/proc/sys/user/max_user_namespaces`, which is read-only in a container, so
`_bwrap_supports_disable_userns()` probes false, the flag is omitted, and a
nested `unshare -Urm` *can* unmount a mask there. That is the container's
limitation rather than the product's behaviour, and an unconditional assertion
would be asserting the wrong system. Native mode (ISSUE-314) runs on a real
host where the flag is supported and the assertion holds — so the guard is the
same probe the product uses, not the driver's mode. What these tests assert
today is the mask's own properties — present, empty, unwritable — which hold
either way.
"""

import functools
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import _bwrap_available, build_bwrap_cmd

pytestmark = pytest.mark.linux


def _unavailable(reason):
    """Skip — unless we are inside the runner, where a skip is the bug.

    `scripts/test-linux.sh` sets ISTOTA_LINUX_TIER=1. That run exists to make
    the sandbox path execute, and it checks bwrap with its own probes before
    starting pytest; if these tests then quietly skipped anyway — because the
    probes and `_bwrap_available()` ask slightly different questions — the
    driver would exit 0 having run none of them. That is precisely the silent
    non-execution the whole tier was built to end, so in there it fails.

    Outside the runner (a bare `pytest -m linux` on a laptop) a skip is the
    right answer: nothing claimed the environment could do this.
    """
    if os.environ.get("ISTOTA_LINUX_TIER") == "1":
        pytest.fail(f"running under scripts/test-linux.sh, where this must not skip: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    """Skip unless this host can actually build the namespace.

    A fixture rather than `pytest.mark.skipif`, because a skipif condition is
    evaluated at *collection* — so a probe written that way spawns a bwrap
    subprocess on every `uv run pytest`, in every xdist worker, on any Linux
    developer machine, for tests the `linux` marker has already deselected. A
    fixture runs only when a test does.
    """
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


@functools.lru_cache(maxsize=1)
def _can_unshare_net():
    """Whether `--unshare-net` can bring up its loopback here.

    A separate question from `_bwrap_available()`, which probes bwrap without
    any `--unshare-net`. Bringing up `lo` in a fresh network namespace needs
    CAP_NET_ADMIN in that namespace: unprivileged bwrap gets it from the user
    namespace it creates, but bwrap running as real root — which is what
    happens inside `scripts/test-linux.sh` — does not create one, so the
    capability has to be granted by the container. The driver checks this too
    and fails rather than skipping; the guard here is for the other case, a
    bare `pytest -m linux` somewhere that has bwrap but not the capability.

    Cached, and reached only from a fixture, so it costs one subprocess per
    session and nothing at all on a run that does not select these tests.
    """
    if sys.platform != "linux" or not _bwrap_available():
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--", "true"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@pytest.fixture
def layout(tmp_path, make_config):
    """A config whose databases sit *inside* a `sandbox_ro_paths` entry.

    That composition is the one the mask exists for. ISSUE-156's shape was a
    single RO bind of `/srv/app` — a path that mentions no database — exposing
    the framework DB, its live -wal/-shm and every user's module DB. Putting
    the DBs under a bound directory is what makes the mask assertions
    non-vacuous: without the mask the probe would find the files, because the
    bind puts them there.
    """
    app = tmp_path / "app"
    db_dir = app / "data"
    module_dir = app / "moduledbs"
    db_dir.mkdir(parents=True)
    module_dir.mkdir(parents=True)

    (db_dir / "istota.db").write_text("framework-db-contents")
    (db_dir / "istota.db-wal").write_text("wal-contents")
    (module_dir / "alice").mkdir()
    (module_dir / "alice" / "health.db").write_text("module-db-contents")
    # A sibling of the database directories, under the same RO bind. It proves
    # the bind itself happened, so "the DB is not visible" cannot pass because
    # nothing was mounted at all.
    (app / "README").write_text("readable-from-the-sandbox")

    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Users" / "bob").mkdir(parents=True)
    (mount / "Users" / "alice" / "mine.txt").write_text("alice's file")
    (mount / "Users" / "bob" / "secret.txt").write_text("bob's file")

    config = make_config(
        db_path=db_dir / "istota.db",
        module_data_dir=module_dir,
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True, sandbox_ro_paths=[str(app)]),
    )
    return config


@pytest.fixture
def user_temp(layout):
    d = layout.temp_dir / "alice"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


def _q(path):
    """Shell-quote a path for the `sh -c` probes.

    `tmp_path` is space-free today because it is built from the test name, so
    nothing here is currently broken — but a parametrised id or a `TMPDIR` with
    a space would split one argument into two and the probe would report on a
    path nobody asked about.
    """
    return shlex.quote(str(path))


def run_probe(script, config, task, user_temp, *, is_admin=False, **kwargs):
    """Run `sh -c script` inside the real sandbox and return the result.

    Fails the test rather than returning if bwrap declined to build a command:
    `build_bwrap_cmd` returns *cmd unchanged* when the sandbox is unavailable,
    so a probe that silently ran on the host would pass every assertion below
    that expects something to be missing.
    """
    cmd = build_bwrap_cmd(
        ["/bin/sh", "-c", script], config, task, is_admin, [], user_temp, **kwargs,
    )
    assert cmd[0] == "bwrap", "sandbox unavailable — probe would have run unsandboxed"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


class TestDatabaseMasks:
    def test_framework_db_dir_is_present_empty_and_unwritable(self, layout, task, user_temp):
        db_dir = Path(layout.db_path).parent
        result = run_probe(
            f'cd {_q(db_dir)} 2>/dev/null && echo PRESENT || echo ABSENT; '
            f'echo "entries=[$(ls -A {_q(db_dir)} 2>&1)]"; '
            f'cat {_q(layout.db_path)} 2>/dev/null && echo READ_OK || echo READ_FAIL; '
            f'touch {_q(f"{db_dir}/probe")} 2>/dev/null && echo WRITE_OK || echo WRITE_FAIL',
            layout, task, user_temp,
        )
        out = result.stdout
        # Present, so a lookup fails at the boundary rather than at a path that
        # does not exist — the mask is a dead end, not a missing mount.
        assert "PRESENT" in out, result.stderr
        assert "entries=[]" in out, out
        assert "READ_FAIL" in out, out
        assert "framework-db-contents" not in out, out
        # Read-only, not merely empty: on a writable mask `sqlite3 istota.db`
        # creates a zero-byte file and answers "no such table", which reads as
        # corruption rather than as a boundary.
        assert "WRITE_FAIL" in out, out

    def test_module_db_root_is_present_empty_and_unwritable(self, layout, task, user_temp):
        module_root = layout.module_db_root()
        result = run_probe(
            f'echo "entries=[$(ls -A {_q(module_root)} 2>&1)]"; '
            f'touch {_q(f"{module_root}/probe")} 2>/dev/null && echo WRITE_OK || echo WRITE_FAIL',
            layout, task, user_temp,
        )
        out = result.stdout
        assert "entries=[]" in out, out
        assert "WRITE_FAIL" in out, out

    def test_the_masks_survive_the_ro_bind_that_would_expose_them(
        self, layout, task, user_temp,
    ):
        """The mask operations come last, and that ordering is load-bearing.

        `sandbox_ro_paths` binds the directory holding both database roots. If
        a refactor moved the masks earlier in argv, the bind would land on top
        of them and the databases would be readable again — with argv still
        containing every `--tmpfs` the old tests look for.
        """
        app = Path(layout.security.sandbox_ro_paths[0])
        result = run_probe(
            f'cat {_q(f"{app}/README")}; '
            f'cat {_q(layout.db_path)} 2>/dev/null || echo DB_UNREADABLE',
            layout, task, user_temp,
        )
        assert "readable-from-the-sandbox" in result.stdout, result.stderr
        assert "DB_UNREADABLE" in result.stdout, result.stdout


class TestReadOnlyPaths:
    def test_sandbox_ro_path_is_readable_and_not_writable(self, layout, task, user_temp):
        app = Path(layout.security.sandbox_ro_paths[0])
        result = run_probe(
            f'cat {_q(f"{app}/README")}; '
            f'echo tampered > {_q(f"{app}/README")} 2>/dev/null && echo WRITE_OK || echo WRITE_FAIL; '
            f'touch {_q(f"{app}/new")} 2>/dev/null && echo CREATE_OK || echo CREATE_FAIL',
            layout, task, user_temp,
        )
        out = result.stdout
        assert "readable-from-the-sandbox" in out, result.stderr
        assert "WRITE_FAIL" in out, out
        assert "CREATE_FAIL" in out, out
        # And the host file is untouched, which the in-sandbox exit status alone
        # would not prove if the write had landed somewhere unexpected.
        assert (app / "README").read_text() == "readable-from-the-sandbox"


class TestMountScoping:
    def test_another_users_directory_is_not_in_the_namespace(self, layout, task, user_temp):
        mount = layout.nextcloud_mount_path
        users = _q(f"{mount}/Users")
        result = run_probe(
            f'cat {_q(f"{mount}/Users/alice/mine.txt")}; '
            f'cat {_q(f"{mount}/Users/bob/secret.txt")} 2>/dev/null || echo BOB_UNREADABLE; '
            f'echo "users=[$(ls -A {users} 2>&1)]"',
            layout, task, user_temp,
        )
        out = result.stdout
        assert "alice's file" in out, result.stderr
        assert "BOB_UNREADABLE" in out, out
        assert "bob's file" not in out, out
        # `Users/` itself *is* listable — bwrap creates it as the mount point
        # for the one bind below it. What must not be there is the sibling. An
        # earlier draft probed for the directory being unlistable and never
        # asserted the result, which would have passed either way.
        assert "users=[alice]" in out, out


class TestCustomSystemPrompt:
    def test_bound_read_only_when_configured(self, layout, tmp_path, task, user_temp):
        prompt = tmp_path / "system-prompt.md"
        prompt.write_text("operator system prompt")
        layout.custom_system_prompt = True

        result = run_probe(
            f'cat {_q(prompt)}; '
            f'echo tampered > {_q(prompt)} 2>/dev/null && echo WRITE_OK || echo WRITE_FAIL',
            layout, task, user_temp,
        )
        out = result.stdout
        assert "operator system prompt" in out, result.stderr
        assert "WRITE_FAIL" in out, out

    def test_absent_when_not_configured(self, layout, tmp_path, task, user_temp):
        prompt = tmp_path / "system-prompt.md"
        prompt.write_text("operator system prompt")
        layout.custom_system_prompt = False

        result = run_probe(
            f'cat {_q(prompt)} 2>/dev/null || echo PROMPT_ABSENT', layout, task, user_temp,
        )
        assert "PROMPT_ABSENT" in result.stdout, result.stdout
        assert "operator system prompt" not in result.stdout


class TestNetworkIsolation:
    """`--unshare-net` is asserted against a listener, not against the internet.

    The probe target is a socket this test opened on the container's loopback.
    Inside a fresh network namespace the sandbox has its own loopback, so the
    connection fails for a structural reason rather than because the runner
    happens to be offline — and the same probe without the proxy socket, which
    is what omits `--unshare-net`, connects. That pair is the assertion.
    """

    @pytest.fixture(autouse=True)
    def _requires_netns(self):
        if not _can_unshare_net():
            _unavailable("bwrap cannot bring up a network namespace here")

    @staticmethod
    def _write_probe(dest, port, unix_sock):
        """A Python probe on disk rather than a `python3 -c` string.

        The command is already wrapped in `sh -c` by `build_bwrap_cmd` when a
        proxy socket is present, so an inline script would be quoted twice.
        `dest` is under the user's temp dir, which the sandbox binds RW, so the
        file is at the same path inside and out.
        """
        dest.write_text(
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=3).close()\n"
            "    print('TCP_OK')\n"
            "except OSError as exc:\n"
            "    print('TCP_FAIL', type(exc).__name__)\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "try:\n"
            f"    s.connect({str(unix_sock)!r})\n"
            "    print('UNIX_OK')\n"
            "except OSError as exc:\n"
            "    print('UNIX_FAIL', type(exc).__name__)\n"
        )
        return f"python3 {_q(dest)}"

    def test_unshare_net_blocks_tcp_while_the_proxy_socket_stays_reachable(
        self, layout, task, user_temp,
    ):
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        port = tcp.getsockname()[1]

        net_sock_path = user_temp / "net-proxy.sock"
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.bind(str(net_sock_path))
        unix.listen(1)

        # build_bwrap_cmd wraps the command in a shell that backgrounds this
        # bridge script before exec'ing the probe. A missing file would still
        # let the probe run, but it would put a Python traceback on stderr for
        # every network-isolated test; a no-op keeps the output about the test.
        dev_dir = user_temp / ".developer"
        dev_dir.mkdir(exist_ok=True)
        (dev_dir / "net-bridge").write_text("import sys\nsys.exit(0)\n")

        script = self._write_probe(user_temp / "net_probe.py", port, net_sock_path)

        try:
            isolated = run_probe(
                script, layout, task, user_temp, net_proxy_sock=net_sock_path,
            )
            shared = run_probe(script, layout, task, user_temp)
        finally:
            tcp.close()
            unix.close()

        # The control: without --unshare-net the same listener is reachable, so
        # TCP_FAIL below is isolation rather than a listener that was never up.
        assert "TCP_OK" in shared.stdout, (shared.stdout, shared.stderr)

        assert "TCP_FAIL" in isolated.stdout, (isolated.stdout, isolated.stderr)
        # Unix sockets cross a network namespace, which is exactly why the
        # CONNECT proxy is reached over one.
        assert "UNIX_OK" in isolated.stdout, (isolated.stdout, isolated.stderr)


class TestSystemBinaries:
    """Debian ships much of `/usr/bin` as symlinks into `/etc/alternatives`.

    `/usr` is bound whole, so the links are all present — but each one points
    at an absolute path in a directory the selective `/etc` binds have to name
    explicitly. Miss it and every alternatives-managed command in the namespace
    is a dangling link: `awk`, `cc`, `vi`, `editor`, `pager`, `which`, `nc`.
    The failure reads as `No such file or directory` for a binary that `ls`
    shows sitting right there, and only inside the sandbox.
    """

    def test_an_alternatives_managed_binary_runs(self, layout, task, user_temp):
        # `pytest.skip`, not `_unavailable`: this asks about the host's
        # packaging rather than about bwrap, and the driver's probes check
        # neither, so escalating it to a failure inside the tier would fail
        # on a question nobody validated. A distribution that does not route
        # awk through alternatives has nothing here to assert.
        #
        # normpath rather than a prefix test on readlink(): the target is
        # absolute on Debian but may be written relative, and `resolve()`
        # cannot stand in because it would follow the link the whole way to
        # /usr/bin/mawk and lose the alternatives step.
        link = Path("/usr/bin/awk")
        if not link.is_symlink():
            pytest.skip("no /usr/bin/awk on this host")
        target = Path(os.path.normpath(link.parent / link.readlink()))
        if target.parent != Path("/etc/alternatives"):
            pytest.skip(f"awk is not managed through /etc/alternatives here: {target}")

        result = run_probe(
            "awk 'BEGIN { print \"AWK_OK\" }' || echo AWK_FAIL",
            layout, task, user_temp,
        )
        assert "AWK_OK" in result.stdout, (result.stdout, result.stderr)
