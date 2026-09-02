"""What the tool server can reach, on a real kernel, inside a real namespace.

`tests/test_tool_server.py` runs the same server unsandboxed and asserts the
seam. This file asserts the containment, and the difference is the whole reason
the spec exists: mocking `_bwrap_available` and inspecting root lists proves
only that Python assembled a policy, which is the thing being replaced.

**The distinguishing assertion, and why every test here disables `ToolEnv`'s
own confinement.** Before this change the five file tools ran in the daemon and
a forbidden path was *refused* by a Python allowlist — "path is outside the
allowed workspace". That refusal is available to a broken sandbox too, so a
test that only checked for it would pass on a namespace that mounted nothing.
So `read_roots=None` / `write_roots=None` are sent in the `hello` frame here,
which switches the allowlist off entirely and leaves the namespace as the only
thing that can hide anything. A hidden path then answers "File not found",
which is an observation about the mount table rather than about a list in
Python. Where a test wants the allowlist, it says so and asserts the *other*
wording.

The positive control is in the same shape as Stage 1's: every absence is
paired with a path that must be readable through the same server, so "nothing
was found" cannot pass by nothing being mounted.

Run with `scripts/test-linux.sh`. Carries the `linux` marker.
"""

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import DeveloperConfig, SecurityConfig
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd
from istota.session.tools import hello_payload, start_tool_server

from .test_sandbox_real import _unavailable

pytestmark = pytest.mark.linux

ALICE_SENTINEL = "alice-workspace-sentinel"
BOB_SENTINEL = "bob-must-not-be-visible"
DB_SENTINEL = "framework-db-sentinel"
LOG_SENTINEL = "session-log-sentinel"


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def _q(path):
    return shlex.quote(str(path))


# --------------------------------------------------------------------------- #
# Shell probes, rendered by a function so they can be exercised standalone.
#
# `tests/test_linux_probe_scripts.py` runs each of these under /bin/sh against a
# present and an absent tree, in the default suite, on any platform. That guard
# exists because of a defect Stage 1's review caught: a probe built as
# `cat <missing> | tr -d "\n" | sed -e "s/^/L=/"` emits *nothing at all* when
# the file is absent — sed is line-oriented and zero input lines produce zero
# output lines — and the absent case was the entire assertion, so the marker
# never printed and the tier would have gone red on the first real host. A
# probe whose failure mode is silence has to be run against both arms before it
# is trusted, and on a tier nobody can execute here that has to happen off the
# tier.
# --------------------------------------------------------------------------- #


def presence_probe(targets: dict) -> str:
    """For each label, `LABEL=PRESENT` or `LABEL=ABSENT`, one line each.

    `echo` with the test inline rather than anything pipeline-shaped: the
    marker must be printed on both arms, and a pipeline over empty input
    prints neither.
    """
    lines = []
    for label, path in targets.items():
        lines.append(
            f'if [ -e {_q(path)} ]; then echo "{label}=PRESENT"; '
            f'else echo "{label}=ABSENT"; fi'
        )
    return "; ".join(lines)


def listing_probe(directory) -> str:
    """`ENTRIES=[…]` for a directory, or `ENTRIES=[MISSING]` if it is not there.

    Deliberately distinguishes "empty" from "not a directory": a tmpfs mask is
    present-and-empty and an unbound path is absent, and reporting both as an
    empty list would let one pass for the other.
    """
    return (
        f'if [ -d {_q(directory)} ]; then echo "ENTRIES=[$(ls -A {_q(directory)} 2>&1 | tr "\\n" " ")]"; '
        f'else echo "ENTRIES=[MISSING]"; fi'
    )


def parent_env_probe(name: str) -> str:
    """Whether the *server process* holds ``name``, read from inside a Bash call.

    ISSUE-390's second carrier. A Bash child is handed an explicit environment
    and so inherits nothing, but it runs at the same uid in the same PID
    namespace as its parent — the tool server — and ``/proc/<ppid>/environ`` is
    readable to it. Stripping the ``hello`` frame alone leaves the token there.

    ``PARENT_CMD`` is reported alongside and is not decoration: if ``$PPID`` is
    ever something other than the server, ``PARENT_ENV=ABSENT`` is true of the
    wrong process and the assertion passes for no reason. The test asserts on
    both.
    """
    return (
        f'if tr "\\0" "\\n" < /proc/$PPID/environ 2>/dev/null '
        f'| grep -q "^{name}="; then echo "PARENT_ENV=PRESENT"; '
        f'else echo "PARENT_ENV=ABSENT"; fi; '
        f'echo "PARENT_CMD=[$(tr "\\0" " " < /proc/$PPID/cmdline 2>/dev/null)]"'
    )


def write_probe(path) -> str:
    """`WRITE=OK` or `WRITE=FAIL`, printed either way."""
    return (
        f'if touch {_q(path)} 2>/dev/null; then echo "WRITE=OK"; '
        f'else echo "WRITE=FAIL"; fi'
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def layout(tmp_path, make_config):
    """Two users, a database directory, a session-log directory, a config dir.

    The database directory holds a sentinel because the masks are asserted from
    inside the server here for the first time — `tests/smoke` asserts the same
    property for the claude_code path, and this is the native one.
    """
    app = tmp_path / "app"
    db_dir = app / "data"
    db_dir.mkdir(parents=True)
    (db_dir / "istota.db").write_text(DB_SENTINEL)
    (db_dir / "logs").mkdir()
    (db_dir / "logs" / "alice").mkdir()
    (db_dir / "logs" / "alice" / "run.jsonl").write_text(LOG_SENTINEL)
    (app / "moduledbs").mkdir()
    (app / "moduledbs" / "alice").mkdir()
    (app / "moduledbs" / "alice" / "health.db").write_text("module-db-sentinel")

    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Users" / "bob").mkdir(parents=True)
    (mount / "Users" / "alice" / "mine.txt").write_text(ALICE_SENTINEL + "\n")
    (mount / "Users" / "bob" / "secret.txt").write_text(BOB_SENTINEL + "\n")

    temp = tmp_path / "temp"
    (temp / "alice").mkdir(parents=True)
    (temp / "bob").mkdir(parents=True)
    (temp / "bob" / "other.txt").write_text(BOB_SENTINEL + "\n")

    return make_config(
        db_path=db_dir / "istota.db",
        module_data_dir=app / "moduledbs",
        nextcloud_mount_path=mount,
        temp_dir=temp,
        security=SecurityConfig(sandbox_enabled=True),
    )


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


def _wrap_for(layout, task, user_temp, *, is_admin=False, user_resources=()):
    def _wrap(cmd):
        wrapped = build_bwrap_cmd(
            cmd, layout, task, is_admin, list(user_resources), user_temp,
            profile=SandboxProfile.NATIVE,
        )
        assert wrapped[0] == "bwrap", (
            "sandbox unavailable — the server would have run on the host, and "
            "every absence below would pass for the wrong reason"
        )
        return wrapped

    return _wrap


def _hello(user_temp, **kw):
    args = dict(
        cwd=user_temp,
        subprocess_env={"PATH": "/usr/bin:/bin", "HOME": str(user_temp)},
        # Off, deliberately: see the module docstring. The namespace is what is
        # under test, so nothing in Python may be able to answer first.
        read_roots=None,
        write_roots=None,
        write_denied_roots=(),
        deferred_dir=user_temp,
        bash_timeout_seconds=60,
        max_output_bytes=30_000,
        max_read_lines=2000,
        max_read_bytes=25_000_000,
        bash_spill_full_output=True,
    )
    args.update(kw)
    return hello_payload(**args)


class _Session:
    def __init__(self, layout, task, user_temp, **kw):
        self._kw = kw
        self._layout, self._task, self._user_temp = layout, task, user_temp
        self.server = None

    async def __aenter__(self):
        hello_kw = self._kw.pop("hello", {})
        spawn_env = self._kw.pop("env", None)
        self.server = await start_tool_server(
            _hello(self._user_temp, **hello_kw),
            sandbox_wrap=_wrap_for(
                self._layout, self._task, self._user_temp, **self._kw
            ),
            env=spawn_env,
        )
        return self.server

    async def __aexit__(self, *exc):
        if self.server is not None:
            await self.server.aclose()
        return False


def _text(result):
    return "".join(getattr(b, "text", "") for b in result.content)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The six tools, and what is not there for them to reach
# --------------------------------------------------------------------------- #


class TestTheSixToolsReachThePermittedFiles:
    """The positive control for everything below.

    Without it "bob's file is not found" passes on a namespace that mounted
    nothing at all, which is exactly the shape that has passed vacuously four
    times in this repo.
    """

    def test_all_six_work_inside_the_namespace(self, layout, task, user_temp):
        async def _go():
            async with _Session(layout, task, user_temp) as server:
                out = {}
                out["write"] = await server.call(
                    "Write", "1",
                    {"file_path": str(user_temp / "made.txt"), "content": "in-sandbox\n"},
                    None, None,
                )
                out["read"] = await server.call(
                    "Read", "2", {"file_path": str(user_temp / "made.txt")}, None, None
                )
                out["edit"] = await server.call(
                    "Edit", "3",
                    {"file_path": str(user_temp / "made.txt"),
                     "old_string": "in-sandbox", "new_string": "edited"},
                    None, None,
                )
                out["glob"] = await server.call(
                    "Glob", "4", {"pattern": "*.txt", "path": str(user_temp)}, None, None
                )
                out["grep"] = await server.call(
                    "Grep", "5",
                    {"pattern": "edited", "path": str(user_temp), "output_mode": "content"},
                    None, None,
                )
                out["bash"] = await server.call(
                    "Bash", "6", {"command": "echo alive-in-the-namespace"}, None, None
                )
                return {k: _text(v) for k, v in out.items()}

        text = _run(_go())
        assert "Created" in text["write"]
        assert "in-sandbox" in text["read"]
        assert "1 block(s) replaced" in text["edit"]
        assert "made.txt" in text["glob"]
        assert "edited" in text["grep"]
        assert "alive-in-the-namespace" in text["bash"]
        # And the write landed on the host, through the bind — a tmpfs would
        # satisfy every assertion above and lose the file.
        assert (user_temp / "made.txt").read_text() == "edited\n"


class TestAnotherUsersDataIsAbsentRatherThanRefused:
    """The assertion `native_fs_roots` structurally cannot make.

    A Python allowlist can only ever say *no*. It cannot say the path is not
    there, and "not there" is what survives a bug in the allowlist, a symlink
    planted mid-run, and a tool that forgot to call `resolve`.
    """

    def test_bobs_workspace_is_not_in_the_namespace(self, layout, task, user_temp):
        bob = layout.temp_dir / "bob" / "other.txt"

        async def _go():
            async with _Session(layout, task, user_temp) as server:
                read = await server.call("Read", "1", {"file_path": str(bob)}, None, None)
                listed = await server.call(
                    "Bash", "2", {"command": listing_probe(layout.temp_dir / "bob")},
                    None, None,
                )
                mine = await server.call(
                    "Read", "3", {"file_path": str(user_temp / "mine-marker.txt")},
                    None, None,
                )
                return _text(read), _text(listed), _text(mine)

        (user_temp / "mine-marker.txt").write_text(ALICE_SENTINEL + "\n")
        read, listed, mine = _run(_go())

        assert BOB_SENTINEL not in read
        # *Not found*, not *refused*: the second wording would mean a Python
        # list answered and the namespace was never consulted.
        assert "File not found" in read, read
        assert "outside the allowed workspace" not in read
        assert "ENTRIES=[MISSING]" in listed, listed
        # The control, through the same server in the same session.
        assert ALICE_SENTINEL in mine

    def test_another_admins_repos_subtree_is_not_in_the_namespace(
        self, layout, task, user_temp, tmp_path, make_config,
    ):
        """`developer.repos_dir` is a root of per-user subtrees and
        `build_bwrap_cmd` binds `{repos_dir}/{user_id}`, never the root. The
        smoke tier asserts this for the claude_code path; this is the native
        one, and it is the same bind under a different profile."""
        repos = tmp_path / "repos"
        (repos / "alice" / "ns").mkdir(parents=True)
        (repos / "alice" / "ns" / "mine.txt").write_text(ALICE_SENTINEL + "\n")
        (repos / "bob" / "ns").mkdir(parents=True)
        (repos / "bob" / "ns" / "theirs.txt").write_text(BOB_SENTINEL + "\n")
        cfg = make_config(
            db_path=layout.db_path,
            module_data_dir=layout.module_data_dir,
            nextcloud_mount_path=layout.nextcloud_mount_path,
            temp_dir=layout.temp_dir,
            security=SecurityConfig(sandbox_enabled=True),
            developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
        )

        async def _go():
            async with _Session(cfg, task, user_temp, is_admin=True) as server:
                probe = await server.call(
                    "Bash", "1",
                    {"command": presence_probe({
                        "MINE": repos / "alice" / "ns" / "mine.txt",
                        "THEIRS": repos / "bob" / "ns" / "theirs.txt",
                        "ROOT": repos,
                    })},
                    None, None,
                )
                return _text(probe)

        out = _run(_go())
        assert "MINE=PRESENT" in out, out  # the control
        assert "THEIRS=ABSENT" in out, out


class TestTheDataStoresAreAbsent:
    def test_databases_logs_and_the_config_directory(self, layout, task, user_temp):
        async def _go():
            async with _Session(layout, task, user_temp) as server:
                db_dir = Path(layout.db_path).parent
                listed = await server.call(
                    "Bash", "1", {"command": listing_probe(db_dir)}, None, None
                )
                read_db = await server.call(
                    "Read", "2", {"file_path": str(layout.db_path)}, None, None
                )
                read_log = await server.call(
                    "Read", "3",
                    {"file_path": str(db_dir / "logs" / "alice" / "run.jsonl")},
                    None, None,
                )
                grepped = await server.call(
                    "Grep", "4",
                    {"pattern": DB_SENTINEL, "path": str(db_dir), "output_mode": "content"},
                    None, None,
                )
                return _text(listed), _text(read_db), _text(read_log), _text(grepped)

        listed, read_db, read_log, grepped = _run(_go())
        # Present and empty, not missing: a mask is a dead end rather than an
        # absent mount, and `_mask_dir` is what makes it so.
        assert "ENTRIES=[ ]" in listed or "ENTRIES=[]" in listed, listed
        assert DB_SENTINEL not in read_db
        assert LOG_SENTINEL not in read_log
        # Grep is the one that used to walk the *daemon's* filesystem view and
        # filter the results afterwards, so it is the tool for which "the file
        # is not in the namespace" is a genuinely new property.
        assert DB_SENTINEL not in grepped

    def test_the_module_database_root_is_masked_too(self, layout, task, user_temp):
        async def _go():
            async with _Session(layout, task, user_temp) as server:
                listed = await server.call(
                    "Bash", "1", {"command": listing_probe(layout.module_db_root())},
                    None, None,
                )
                write = await server.call(
                    "Bash", "2",
                    {"command": write_probe(layout.module_db_root() / "probe")},
                    None, None,
                )
                return _text(listed), _text(write)

        listed, write = _run(_go())
        assert "ENTRIES=[ ]" in listed or "ENTRIES=[]" in listed, listed
        assert "WRITE=FAIL" in write, write


class TestReadOnlyPathsRejectWrites:
    def test_the_developer_carve_out_is_read_only_in_the_namespace(
        self, layout, task, user_temp,
    ):
        """`.developer` holds the credential helpers, and bwrap makes it
        read-only by re-binding it after its parent's read-write bind — a hole
        inside a root that a containment list cannot express. With
        `write_roots=None` here, the namespace is the only thing that can
        refuse."""
        dev = user_temp / ".developer"
        dev.mkdir()
        (dev / "credential-fetch").write_text("original\n")

        async def _go():
            async with _Session(layout, task, user_temp) as server:
                blocked = await server.call(
                    "Write", "1",
                    {"file_path": str(dev / "credential-fetch"), "content": "replaced"},
                    None, None,
                )
                allowed = await server.call(
                    "Write", "2",
                    {"file_path": str(user_temp / "ok.txt"), "content": "fine"},
                    None, None,
                )
                return _text(blocked), _text(allowed)

        blocked, allowed = _run(_go())
        assert (dev / "credential-fetch").read_text() == "original\n"
        # The *kernel's* refusal, not the allowlist's. With `write_roots=None`
        # the Python check is off, so the write reaches `open()` and comes back
        # EROFS — which is what the re-bind buys and what a containment list
        # could only imitate.
        assert "Read-only file system" in blocked, blocked
        assert "Created" in allowed  # the control


class TestTheAncestorSwapRace:
    """The case `ToolEnv.resolve` cannot win and a namespace does not have.

    `resolve` realpaths a path and then the tool opens it, which are two
    syscalls with a window between them: a component swapped in that window is
    followed by the open. The mitigation was to operate on the resolved path,
    which narrows it and does not close it.

    Inside the namespace there is nothing to win. The host path the swap aims
    at is not mounted, so following the new symlink lands on nothing — and this
    test replaces the link *after* the server started, so the mount table is
    already fixed and cannot be renegotiated.
    """

    def test_a_symlink_swapped_mid_session_reaches_nothing(self, layout, task, user_temp):
        link = user_temp / "link"
        inside = user_temp / "inside.txt"
        inside.write_text(ALICE_SENTINEL + "\n")
        link.symlink_to(inside)
        bob = layout.temp_dir / "bob" / "other.txt"

        async def _go():
            async with _Session(layout, task, user_temp) as server:
                before = await server.call("Read", "1", {"file_path": str(link)}, None, None)
                # The swap, with the namespace already built.
                link.unlink()
                link.symlink_to(bob)
                after = await server.call("Read", "2", {"file_path": str(link)}, None, None)
                return _text(before), _text(after)

        before, after = _run(_go())
        assert ALICE_SENTINEL in before, before  # the control: the link worked
        assert BOB_SENTINEL not in after, after
        assert "File not found" in after, after

    def test_swapping_an_ancestor_directory_reaches_nothing_either(
        self, layout, task, user_temp,
    ):
        """The harder half: the swapped component is a *directory* in the
        middle of the path, which is the shape a realpath-then-open check is
        weakest against."""
        holder = user_temp / "holder"
        real = user_temp / "real"
        real.mkdir()
        (real / "file.txt").write_text(ALICE_SENTINEL + "\n")
        holder.symlink_to(real)
        elsewhere = layout.temp_dir / "bob"
        (elsewhere / "file.txt").write_text(BOB_SENTINEL + "\n")

        async def _go():
            async with _Session(layout, task, user_temp) as server:
                before = await server.call(
                    "Read", "1", {"file_path": str(holder / "file.txt")}, None, None
                )
                holder.unlink()
                holder.symlink_to(elsewhere)
                after = await server.call(
                    "Read", "2", {"file_path": str(holder / "file.txt")}, None, None
                )
                return _text(before), _text(after)

        before, after = _run(_go())
        assert ALICE_SENTINEL in before, before
        assert BOB_SENTINEL not in after, after


class TestTheControlSocketIsNotReachableFromABashChild:
    def test_no_descendant_can_see_the_control_descriptor(self, layout, task, user_temp):
        """A command that could reach the socket could answer its own tool
        calls. `close_fds` is the mechanism; this is the measurement, inside
        the namespace where bwrap's own fork and exec sit between the two."""
        captured = {}

        def _wrap(cmd):
            captured["fd"] = cmd[cmd.index("--fd") + 1]
            return _wrap_for(layout, task, user_temp)(cmd)

        async def _go():
            server = await start_tool_server(_hello(user_temp), sandbox_wrap=_wrap)
            try:
                fd = captured["fd"]
                return _text(await server.call(
                    "Bash", "1",
                    {"command": presence_probe({"FD": Path(f"/proc/self/fd/{fd}")})},
                    None, None,
                ))
            finally:
                await server.aclose()

        out = _run(_go())
        assert "FD=ABSENT" in out, out


class TestTheClaudeRuntimeBlockIsStillAbsentHere:
    """Stage 1's property, re-asserted at the site it now protects.

    `tests/linux/test_sandbox_profiles_real.py` proves the NATIVE argv builds a
    namespace without the credential. This proves the tool server is built with
    that argv — a regression that spawned it under the CLAUDE profile would
    leave that file green and hand the model the token again.
    """

    def test_the_credential_is_not_in_the_tool_servers_namespace(
        self, layout, task, user_temp, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        creds = home / ".claude" / ".credentials.json"
        creds.write_text('{"token": "sentinel-not-a-real-token"}\n')
        monkeypatch.setenv("HOME", str(home))

        async def _go():
            async with _Session(layout, task, user_temp) as server:
                read = await server.call("Read", "1", {"file_path": str(creds)}, None, None)
                probe = await server.call(
                    "Bash", "2", {"command": presence_probe({"CREDS": creds})}, None, None
                )
                return _text(read), _text(probe)

        read, probe = _run(_go())
        assert "sentinel-not-a-real-token" not in read
        assert "CREDS=ABSENT" in probe, probe


class TestTheClaudeTokenIsNotInTheServersOwnEnvironment:
    """ISSUE-390's second carrier, which only a real namespace can settle.

    The daemon-side tests assert what is *passed*. This asserts what a Bash
    call can actually *read* out of its parent, which is the thing that makes
    the spawn-env strip load-bearing rather than tidy.
    """

    _TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"

    def _spawn_env(self, user_temp, **extra):
        """A minimal spawn env that can still start the server.

        Nothing is carried across from the harness, and that is now the point
        rather than a detail (ISSUE-398). This used to copy `PYTHONPATH`
        through, because the tier's image installed the dependencies without the
        project and `istota` was importable from nowhere else — a workaround
        that existed in this one file, which is why the tier looked like it
        covered the tool server while 47 tests elsewhere never started one. The
        image now installs the project, so a spawn env holding two variables
        must start a server; if it does not, the install regressed, and this
        class is where that surfaces before the handshake failure gets mistaken
        for the boundary holding.
        """
        return {"PATH": "/usr/bin:/bin", "HOME": str(user_temp), **extra}

    def _ask(self, layout, task, user_temp, spawn_env):
        async def _go():
            async with _Session(layout, task, user_temp, env=spawn_env) as server:
                return await server.call(
                    "Bash", "c1", {"command": parent_env_probe(self._TOKEN)}, None, None
                )
        return _text(_run(_go()))

    def test_a_bash_child_cannot_read_it_from_the_servers_environ(
        self, layout, task, user_temp
    ):
        out = self._ask(layout, task, user_temp, self._spawn_env(user_temp))
        assert "PARENT_ENV=ABSENT" in out
        # Without this the absence above is a fact about some other process.
        assert "istota.tool_server" in out

    def test_the_positive_control_finds_it_when_the_spawn_carries_it(
        self, layout, task, user_temp
    ):
        """Without this the test above passes on a probe that reads nothing.

        Spawning the server with the token present must make the same probe say
        PRESENT. If it does not, the probe is broken, not the boundary.
        """
        out = self._ask(
            layout, task, user_temp,
            self._spawn_env(user_temp, **{self._TOKEN: "sk-ant-oat-fake-for-tests"}),
        )
        assert "PARENT_ENV=PRESENT" in out
        assert "istota.tool_server" in out
