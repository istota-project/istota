"""`project_fs_roots`: the rules a plain walk over the plan does not give you.

`native_fs_roots` used to derive its three lists by hand, beside the function
that derived the binds, and ISSUE-319 and ISSUE-320 were each one copy
disagreeing with the other. It projects the plan now. Four things about that
projection are decisions rather than consequences, and each is here because a
future simplification would look correct and be wrong:

- ``.developer`` is a deny root whether or not the directory exists, and the
  argv is unchanged in every state the path can be in, including a symlink
  loop, where a check after `resolve()` raises instead;
- a read-only entry nested in an earlier read-write one is a *write-deny* root
  rather than a read root, which is what bwrap's ordering already does;
- the derived package cache is not a write root of its own;
- a REPL workspace the blocklist refuses costs the workspace and nothing else.

The full two-directions parity assertion is
`tests/test_sandbox_plan_parity.py`. This file covers the rules that parity
test cannot state, because they are exactly where the projection is allowed to
differ from a mechanical walk.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, DeveloperConfig, SecurityConfig
from istota.executor import build_bwrap_cmd, native_fs_roots
from istota.sandbox_plan import SandboxProfile


@pytest.fixture
def config(tmp_path):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Channels" / "room123").mkdir(parents=True)
    db_file = tmp_path / "data" / "istota.db"
    db_file.parent.mkdir(parents=True)
    db_file.touch()
    return Config(
        db_path=db_file,
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(sandbox_enabled=True),
    )


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="t", user_id="alice", source_type="talk",
        status="running", conversation_token="room123",
    )


@pytest.fixture
def user_temp(config):
    path = config.temp_dir / "alice"
    path.mkdir(parents=True)
    return path


def _argv(config, task, user_temp, **kwargs):
    with patch("istota.executor._bwrap_available", return_value=True):
        return build_bwrap_cmd(
            ["cmd"], config, task, False, [], user_temp,
            profile=SandboxProfile.NATIVE, **kwargs,
        )


def _binds(argv, flag):
    return [
        (argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag
    ]


class TestTheDeveloperCarveOut:
    """The plan carries the entry unconditionally so the projection can see it.

    `build_bwrap_cmd` re-reads the filesystem on every invocation while the
    roots are built once per task, so an existence gate here leaves a window in
    which a `.developer` created mid-run is read-only for the Bash tool and
    writable for the file tools. Carrying the entry closes that; `require_dir`
    is what keeps the *argv* the same in the two states where the old
    `is_dir()` gate refused the bind.
    """

    def _denied(self, config, task, user_temp):
        return native_fs_roots(config, task, False, [], user_temp)[2]

    def test_denied_when_the_directory_is_absent(self, config, task, user_temp):
        assert not (user_temp / ".developer").exists()
        assert self._denied(config, task, user_temp) == [
            user_temp.resolve() / ".developer"
        ]

    def test_denied_when_the_path_is_a_regular_file(self, config, task, user_temp):
        (user_temp / ".developer").write_text("planted by an earlier task")
        assert self._denied(config, task, user_temp) == [
            user_temp.resolve() / ".developer"
        ]

    def test_denied_when_the_directory_exists(self, config, task, user_temp):
        (user_temp / ".developer").mkdir()
        assert self._denied(config, task, user_temp) == [
            user_temp.resolve() / ".developer"
        ]

    @pytest.mark.parametrize("shape", ["absent", "file", "symlink_loop"])
    def test_the_argv_binds_nothing_unless_it_is_a_directory(
        self, config, task, user_temp, shape,
    ):
        """The control for the entry being emitted unconditionally.

        `user_temp_dir` is per user rather than per task and is bound
        read-write into every sandbox of that user, so a model in one task can
        leave any of these three at this name for the next one. The render
        skips a *missing* source on its own; only `require_dir` skips the other
        two — and `symlink_loop` is why it is applied before `resolve()`, which
        raises `RuntimeError` on one. A raise here fails every later sandbox
        build for that user, which is worse than the wrong bind it was added to
        prevent.
        """
        dev = user_temp / ".developer"
        if shape == "file":
            dev.write_text("not a directory")
        elif shape == "symlink_loop":
            dev.symlink_to(user_temp / "loop")
            (user_temp / "loop").symlink_to(dev)

        argv = _argv(config, task, user_temp)

        assert str(user_temp.resolve() / ".developer") not in argv

    def test_a_symlink_loop_does_not_break_the_roots_either(
        self, config, task, user_temp,
    ):
        """The projection's own half of the same input. It reaches the
        `always_deny` branch, which returns before resolving, so it never had
        the render's problem — asserted rather than assumed."""
        dev = user_temp / ".developer"
        dev.symlink_to(user_temp / "loop")
        (user_temp / "loop").symlink_to(dev)

        assert self._denied(config, task, user_temp) == [
            user_temp.resolve() / ".developer"
        ]

    def test_a_real_directory_is_still_bound_read_only(
        self, config, task, user_temp,
    ):
        """The other half of the control: `require_dir` must not have turned
        the bind off for the case it exists to serve."""
        (user_temp / ".developer").mkdir()

        argv = _argv(config, task, user_temp)

        assert any(
            src == str(user_temp.resolve() / ".developer")
            for src, _ in _binds(argv, "--ro-bind")
        )

    def test_the_deny_root_is_the_path_as_written(self, config, task, user_temp, tmp_path):
        """Carried unresolved, matching the bind and matching what this
        function has always returned.

        Behaviour preservation, and deliberately not a claim that the denial is
        symlink-proof: `ToolEnv` realpaths every deny root before comparing, so
        a symlink planted at this name still relocates the denial downstream.
        Closing that is a decision about the enforcer, not about the
        projection. What this pins is that the projection did not start
        resolving a value it never used to resolve.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (user_temp / ".developer").symlink_to(elsewhere)

        assert self._denied(config, task, user_temp) == [
            user_temp.resolve() / ".developer"
        ]


class TestReadOnlyNestedInsideReadWrite:
    """bwrap's ordering, expressed where there are no mounts.

    A later `--ro-bind` under an earlier `--bind` takes the write away, and the
    only thing that can say so in a list of roots is containment. Before the
    projection the hand-written derivation put such an entry on `read_only`,
    which left it writable through the root that contained it — the file tools
    disagreeing with the namespace about the same path.
    """

    def _resource(self, path):
        return db.UserResource(
            id=1, display_name="ref", user_id="alice", resource_type="folder",
            resource_path=path, permissions="read",
        )

    def test_a_read_only_resource_inside_the_channel_bind_is_write_denied(
        self, config, task, user_temp,
    ):
        nested = config.nextcloud_mount_path / "Channels" / "room123" / "ref"
        nested.mkdir()

        read, write, denied = native_fs_roots(
            config, task, False, [self._resource("Channels/room123/ref")], user_temp,
        )

        assert nested.resolve() in denied
        assert nested.resolve() not in write
        # Still reachable for reading, through the channel root that contains
        # it — a deny root takes the write away and nothing else.
        assert any(nested.resolve().is_relative_to(root) for root in read)

    def test_the_argv_agrees(self, config, task, user_temp):
        """The claim is that the projection now matches the binds, so the
        assertion has to name the binds."""
        nested = config.nextcloud_mount_path / "Channels" / "room123" / "ref"
        nested.mkdir()
        channel = (config.nextcloud_mount_path / "Channels" / "room123").resolve()

        with patch("istota.executor._bwrap_available", return_value=True):
            argv = build_bwrap_cmd(
                ["cmd"], config, task, False,
                [self._resource("Channels/room123/ref")], user_temp,
                profile=SandboxProfile.NATIVE,
            )

        rw_at = argv.index(str(channel))
        ro_at = argv.index(str(nested.resolve()))
        assert argv[rw_at - 1] == "--bind"
        assert argv[ro_at - 1] == "--ro-bind"
        assert ro_at > rw_at, "the read-only re-bind has to come after the bind"

    def test_an_unnested_read_only_resource_is_still_a_read_root(
        self, config, task, user_temp,
    ):
        """The control. Containment must not swallow every read-only entry."""
        outside = config.nextcloud_mount_path / "Reference"
        outside.mkdir()

        read, _write, denied = native_fs_roots(
            config, task, False, [self._resource("Reference")], user_temp,
        )

        assert outside.resolve() in read
        assert outside.resolve() not in denied


class TestARejectedWorkspaceCostsOnlyTheWorkspace:
    """`build_mount_plan` propagates the `ValueError`; this one logs it.

    The asymmetry is deliberate — a workspace the blocklist refuses must fail a
    task rather than silently run without the directory it named — but the
    roots are an error-message layer over the same plan, so here every other
    root still stands.
    """

    def test_the_other_roots_survive(self, config, task, user_temp, caplog):
        forbidden = config.db_path.parent

        with caplog.at_level("WARNING"):
            read, write, denied = native_fs_roots(
                config, task, False, [], user_temp, workspace_dir=forbidden,
            )

        assert any("rejected by blocklist" in r.message for r in caplog.records)
        assert user_temp.resolve() in write
        assert (config.nextcloud_mount_path / "Users" / "alice").resolve() in write
        assert forbidden.resolve() not in write
        assert forbidden.resolve() not in read
        assert denied == [user_temp.resolve() / ".developer"]


class TestTheDerivedCacheIsNotAWriteRoot:
    """ISSUE-320, restated against the projection rather than the old body.

    The plan carries the answer as `user_data=False` on the derived branch, so
    the projection has no branch of its own — which is the property worth
    pinning, since a later reader adding one back would reopen the symlink
    window with no test naming it.
    """

    def _setup(self, config, tmp_path, admin):
        repos = tmp_path / "repos"
        (repos / "alice").mkdir(parents=True)
        config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))
        config.admin_users = {"alice"} if admin else set()
        return repos

    def test_derived(self, config, task, user_temp, tmp_path):
        repos = self._setup(config, tmp_path, admin=True)

        _read, write, _denied = native_fs_roots(config, task, True, [], user_temp)

        cache = (repos / "alice" / ".package-caches").resolve()
        roots = [Path(p).resolve() for p in write]
        assert cache not in roots
        assert any(cache.is_relative_to(root) for root in roots)

    def test_fallback(self, config, task, user_temp, tmp_path):
        """The control: without it the assertion above passes on a build that
        dropped the cache write root altogether."""
        fallback = tmp_path / "caches"
        fallback.mkdir()
        config.security.sandbox_cache_dir = str(fallback)

        _read, write, _denied = native_fs_roots(config, task, False, [], user_temp)

        assert (fallback / "alice").resolve() in [Path(p).resolve() for p in write]
