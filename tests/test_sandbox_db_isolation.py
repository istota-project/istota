"""The framework and per-module SQLite files are unreachable from the sandbox.

The invariant these tests pin down: no database the daemon owns is visible
inside bwrap, for admin or non-admin, no matter what ``sandbox_ro_paths``
contains. Every read and write goes through a skill CLI, which the proxy runs
host-side and scopes by ``ISTOTA_USER_ID``.

This replaces an earlier posture where the framework DB was ``--ro-bind``ed for
admins and the *reference deployment* additionally exposed every user's module
DB, because ``module_data_dir`` defaults under ``istota_home`` and
``sandbox_ro_paths`` defaulted to the ``/srv/app`` that contains it.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SecurityConfig
from istota.executor import (
    _validate_workspace_dir,
    build_bwrap_cmd,
    native_fs_roots,
)


@pytest.fixture
def iso_config(tmp_path):
    """Config whose DB dir and module-DB root sit under a common parent.

    Mirrors the reference deployment: ``{istota_home}/data/istota.db`` and
    ``{istota_home}/data/modules/{user}/{module}.db``, with ``istota_home``
    itself inside the tree an operator lists in ``sandbox_ro_paths``.
    """
    srv = tmp_path / "srv" / "app"
    home = srv / "istota"
    data = home / "data"
    (data / "modules" / "alice").mkdir(parents=True)
    (data / "modules" / "bob").mkdir(parents=True)
    db_file = data / "istota.db"
    db_file.touch()
    (data / "modules" / "alice" / "health.db").touch()
    (data / "modules" / "bob" / "money.db").touch()

    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)

    return Config(
        db_path=db_file,
        module_data_dir=data / "modules",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(sandbox_enabled=True),
    )


@pytest.fixture
def iso_task():
    return db.Task(
        id=1, prompt="test", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


def _bwrap(config, task, is_admin):
    user_temp = config.temp_dir / task.user_id
    user_temp.mkdir(parents=True, exist_ok=True)
    with patch("istota.executor._bwrap_available", return_value=True):
        return build_bwrap_cmd(
            ["claude", "-p", "test"], config, task, is_admin, [], user_temp,
        )


def _bind_sources(argv, bind_type):
    """Every source path bound with ``bind_type``."""
    return [
        argv[i + 1] for i in range(len(argv) - 1)
        if argv[i] == bind_type
    ]


def _tmpfs_paths(argv):
    return [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--tmpfs"]


def _mask_paths(argv):
    """The database masks alone, without the namespace's own tmpfs mounts.

    `build_bwrap_cmd` emits `--tmpfs /tmp` early, with `--proc` and `--dev`,
    and the database masks last — after every bind, which is the ordering the
    masks depend on. So "after the last bind" is exactly the mask set.

    The distinction only shows on Linux: `tmp_path` lives under `/tmp` there
    and under `/private/var/folders` on darwin, so a test asking "does any
    tmpfs shadow the workspace" gets a false yes from the namespace's `/tmp`
    in the Linux runner and never on the developer host. It is a false yes
    because the workspace is bound *after* that tmpfs and is therefore present
    in the namespace regardless.
    """
    binds = [i for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    after = max(binds) if binds else -1
    return [argv[i + 1] for i in range(after + 1, len(argv) - 1) if argv[i] == "--tmpfs"]


def _last_index(argv, value):
    for i in range(len(argv) - 1, -1, -1):
        if argv[i] == value:
            return i
    return -1


class TestFrameworkDbUnreachable:
    """The framework DB file is bound nowhere, for anyone."""

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_db_file_never_bound(self, iso_config, iso_task, is_admin):
        argv = _bwrap(iso_config, iso_task, is_admin)
        db_str = str(iso_config.db_path.resolve())
        for bind_type in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try"):
            assert db_str not in _bind_sources(argv, bind_type), (
                f"{db_str} bound with {bind_type}"
            )

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_wal_and_shm_never_bound(self, iso_config, iso_task, is_admin):
        argv = _bwrap(iso_config, iso_task, is_admin)
        joined = " ".join(argv)
        for suffix in ("-wal", "-shm"):
            sidecar = str(iso_config.db_path) + suffix
            assert sidecar not in joined

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_db_directory_is_masked(self, iso_config, iso_task, is_admin):
        """An empty tmpfs covers the DB directory, so nothing under it reads."""
        argv = _bwrap(iso_config, iso_task, is_admin)
        assert str(iso_config.db_path.parent.resolve()) in _tmpfs_paths(argv)


class TestModuleDbsUnreachable:
    """Other users' per-module DBs are the wider half of the old exposure."""

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_module_root_is_masked(self, iso_config, iso_task, is_admin):
        argv = _bwrap(iso_config, iso_task, is_admin)
        masked = _tmpfs_paths(argv)
        root = str(iso_config.module_db_root().resolve())
        db_dir = str(iso_config.db_path.parent.resolve())
        # In the default layout the module root sits under the DB directory, so
        # masking that covers it. Assert the covering mask is really in argv —
        # an `or root.startswith(db_dir)` disjunct would be a statement about
        # two strings and would hold with the mask block deleted entirely.
        if root.startswith(db_dir + "/"):
            assert db_dir in masked
        else:
            assert root in masked

    def test_module_root_masked_when_relocated(self, iso_config, iso_task, tmp_path):
        """A module_data_dir outside the DB directory gets its own mask."""
        relocated = tmp_path / "elsewhere" / "modules"
        (relocated / "bob").mkdir(parents=True)
        iso_config.module_data_dir = relocated
        argv = _bwrap(iso_config, iso_task, True)
        assert str(relocated.resolve()) in _tmpfs_paths(argv)


class TestRoPathsCannotReExpose:
    """The mask is the backstop: it must outlive an operator's RO path."""

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_srv_app_ro_path_does_not_expose_dbs(self, iso_config, iso_task, is_admin):
        """The historical default. The RO bind lands, the mask lands after it."""
        srv_app = iso_config.db_path.parents[2]  # …/srv/app
        iso_config.security.sandbox_ro_paths = [str(srv_app)]
        argv = _bwrap(iso_config, iso_task, is_admin)

        assert str(srv_app.resolve()) in _bind_sources(argv, "--ro-bind"), (
            "the configured RO path should still be bound"
        )
        db_dir = str(iso_config.db_path.parent.resolve())
        assert db_dir in _tmpfs_paths(argv)
        # bwrap applies operations in argv order, so the mask only wins if it
        # comes after the bind that would otherwise expose it.
        assert _last_index(argv, db_dir) > _last_index(argv, str(srv_app.resolve()))

    def test_ro_paths_default_is_empty(self):
        """Nothing is exposed by default; /srv/app was for a service now gone."""
        assert Config().security.sandbox_ro_paths == []

    def test_symlinked_root_is_masked_at_the_name_the_bind_used(
        self, iso_config, iso_task, tmp_path,
    ):
        """A resolved-only mask misses the path the model would actually use.

        `_ro_bind` keeps the *unresolved* string as its sandbox destination, so
        with `/srv -> /realstore` the bind lands at `/srv/app` while a resolved
        mask lands at `/realstore/app/...` — a path not in the namespace, and
        the databases stay readable under the symlinked name.
        """
        real = tmp_path / "realstore"
        real.mkdir()
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        data = real / "data"
        data.mkdir()
        db_file = data / "istota.db"
        db_file.touch()

        # db_path as written goes through the symlink.
        iso_config.db_path = tmp_path / "link" / "data" / "istota.db"
        iso_config.module_data_dir = tmp_path / "link" / "data" / "modules"
        iso_config.security.sandbox_ro_paths = [str(tmp_path / "link")]

        argv = _bwrap(iso_config, iso_task, True)
        masked = _tmpfs_paths(argv)
        assert str(tmp_path / "link" / "data") in masked, (
            "the symlinked name — the one the RO bind exposes — must be masked"
        )
        assert str(data.resolve()) in masked


class TestMasksAreReadOnly:
    """A writable mask lets a probe leave a zero-byte database behind.

    `sqlite3 {db_dir}/istota.db "select …"` on a writable tmpfs *creates* the
    file and then reports `no such table`, which reads as a missing schema or a
    corrupt database rather than as "the file is not in this namespace". The
    stray file also outlives the probe for the rest of the task. Remounting each
    mask read-only turns both into one honest `unable to open database file`.
    """

    @pytest.fixture(autouse=True)
    def _supported(self):
        """The probe shells out to a real bwrap, which the dev host has not."""
        with patch("istota.executor._bwrap_supports_remount_ro", return_value=True):
            yield

    def _remount_ro_paths(self, argv):
        return [
            argv[i + 1] for i in range(len(argv) - 1)
            if argv[i] == "--remount-ro"
        ]

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_every_db_mask_is_remounted_read_only(
        self, iso_config, iso_task, is_admin,
    ):
        argv = _bwrap(iso_config, iso_task, is_admin)
        db_dir = str(iso_config.db_path.parent.resolve())
        assert db_dir in self._remount_ro_paths(argv)

    def test_relocated_module_root_is_remounted_read_only(
        self, iso_config, iso_task, tmp_path,
    ):
        """The second mask gets the same treatment as the first."""
        relocated = tmp_path / "elsewhere" / "modules"
        (relocated / "bob").mkdir(parents=True)
        iso_config.module_data_dir = relocated
        argv = _bwrap(iso_config, iso_task, True)
        assert str(relocated.resolve()) in self._remount_ro_paths(argv)

    def test_symlinked_name_is_remounted_too(self, iso_config, iso_task, tmp_path):
        """Both names a mask answers to have to be read-only, not just one."""
        real = tmp_path / "realstore"
        real.mkdir()
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        (real / "data").mkdir()
        iso_config.db_path = tmp_path / "link" / "data" / "istota.db"
        iso_config.module_data_dir = tmp_path / "link" / "data" / "modules"

        argv = _bwrap(iso_config, iso_task, True)
        remounted = self._remount_ro_paths(argv)
        assert str(tmp_path / "link" / "data") in remounted
        assert str((real / "data").resolve()) in remounted

    def test_remount_follows_the_tmpfs_it_applies_to(self, iso_config, iso_task):
        """`--remount-ro` acts on whatever is mounted at that path *now*."""
        argv = _bwrap(iso_config, iso_task, True)
        db_dir = str(iso_config.db_path.parent.resolve())
        tmpfs_at = [
            i for i in range(len(argv) - 1)
            if argv[i] == "--tmpfs" and argv[i + 1] == db_dir
        ]
        remount_at = [
            i for i in range(len(argv) - 1)
            if argv[i] == "--remount-ro" and argv[i + 1] == db_dir
        ]
        assert tmpfs_at and remount_at
        assert min(remount_at) > min(tmpfs_at)

    @pytest.mark.parametrize("relocated_modules", [False, True])
    def test_every_remount_has_a_tmpfs_before_it(
        self, iso_config, iso_task, tmp_path, relocated_modules,
    ):
        """The pairing rule, over the whole argv rather than one path.

        A `--remount-ro` on a path that is not a mount point is fatal — bwrap
        exits with "Can't remount readonly … Unable to find … in mount table"
        before running anything, so getting this wrong fails every task rather
        than weakening one directory.
        """
        if relocated_modules:
            relocated = tmp_path / "elsewhere" / "modules"
            (relocated / "bob").mkdir(parents=True)
            iso_config.module_data_dir = relocated
        argv = _bwrap(iso_config, iso_task, True)

        remounts = [i for i, tok in enumerate(argv) if tok == "--remount-ro"]
        assert remounts, "nothing to check — the masks lost their remount"
        for i in remounts:
            path = argv[i + 1]
            assert any(
                argv[j] == "--tmpfs" and argv[j + 1] == path
                for j in range(i)
            ), f"--remount-ro {path} has no --tmpfs at that path before it"

    def test_only_the_db_masks_are_remounted(self, iso_config, iso_task):
        """/tmp, ~/.claude and the rest stay writable — tasks need them."""
        argv = _bwrap(iso_config, iso_task, True)
        allowed = {
            str(iso_config.db_path.parent),
            str(iso_config.db_path.parent.resolve()),
            str(iso_config.module_db_root()),
            str(iso_config.module_db_root().resolve()),
        }
        assert set(self._remount_ro_paths(argv)) <= allowed

    def test_a_refused_mask_is_not_remounted(self, iso_config, iso_task, tmp_path):
        """No tmpfs was mounted there, so remounting would hit the real dir."""
        workspace = iso_config.nextcloud_mount_path
        iso_config.db_path = workspace / "istota.db"
        iso_config.module_data_dir = tmp_path / "modules"
        (tmp_path / "modules").mkdir(parents=True, exist_ok=True)
        argv = _bwrap(iso_config, iso_task, True)
        assert str(workspace.resolve()) not in self._remount_ro_paths(argv)

    def test_omitted_when_bwrap_does_not_support_it(self, iso_config, iso_task):
        """An unknown flag makes bwrap exit non-zero — that fails every task."""
        with patch("istota.executor._bwrap_supports_remount_ro", return_value=False):
            argv = _bwrap(iso_config, iso_task, True)
        assert "--remount-ro" not in argv
        # The mask itself must survive the missing flag.
        assert str(iso_config.db_path.parent.resolve()) in _tmpfs_paths(argv)

    def test_no_mask_is_nested_inside_another(self, iso_config, iso_task):
        """Read-only turns a nested mask from redundant into fatal.

        bwrap has to `mkdir` the second mountpoint on the first mask's tmpfs
        and gets EROFS: "Can't mkdir …: Read-only file system", exit 1, before
        the task runs. The default layout puts the module root under the DB
        directory, so this is the shipped configuration and not a corner.
        """
        argv = _bwrap(iso_config, iso_task, True)
        masked = [Path(p) for p in _tmpfs_paths(argv)]
        db_masks = [
            p for p in masked
            if p.is_relative_to(iso_config.db_path.parent.resolve())
        ]
        for i, inner in enumerate(db_masks):
            for outer in db_masks[:i]:
                assert not inner.is_relative_to(outer), (
                    f"{inner} is masked inside {outer}; bwrap cannot mkdir it"
                )

    def test_module_root_masked_when_its_parent_mask_was_refused(
        self, iso_config, iso_task, tmp_path,
    ):
        """A cover that was never mounted covers nothing.

        The old caller-side "already under db_dir?" test skipped the module
        root whenever it was nested, including when the db_dir mask had been
        refused for shadowing a path the task needs — so the module DBs stayed
        visible for want of a cover that was never mounted.
        """
        # db_dir is the temp root, which holds the task's own temp dir.
        iso_config.db_path = iso_config.temp_dir / "istota.db"
        iso_config.module_data_dir = iso_config.temp_dir / "modules"
        (iso_config.temp_dir / "modules").mkdir(parents=True, exist_ok=True)

        argv = _bwrap(iso_config, iso_task, True)
        masked = _tmpfs_paths(argv)
        assert str(iso_config.temp_dir.resolve()) not in masked, (
            "masking the temp root would hide the task's own scratch dir"
        )
        assert str((iso_config.temp_dir / "modules").resolve()) in masked


class TestMaskPathsHelper:
    """The helper the test below is built on, checked against a live argv.

    `_mask_paths` is defined as "every `--tmpfs` after the last bind", which is
    exactly the property `build_bwrap_cmd` promises and exactly the property a
    refactor could break. If the masks ever moved ahead of the binds — or were
    dropped — the helper would return an empty list and every loop over it
    would pass by never running. So the helper gets its own control.
    """

    def test_it_finds_the_database_masks_for_an_ordinary_config(
        self, iso_config, iso_task,
    ):
        argv = _bwrap(iso_config, iso_task, True)
        masks = _mask_paths(argv)

        assert str(iso_config.db_path.parent.resolve()) in masks
        # The default layout derives the module root under the framework DB's
        # directory, so it gets no mask of its own — `_mask_dir` skips a
        # candidate an earlier mask already covers. Covered is what matters.
        module_root = iso_config.module_db_root()
        assert any(module_root.is_relative_to(Path(m)) for m in masks)

    def test_it_finds_a_module_root_masked_in_its_own_right(
        self, iso_config, iso_task, tmp_path,
    ):
        """Two masks when the two roots are siblings rather than nested."""
        module_root = tmp_path / "srv" / "app" / "istota" / "moduledbs"
        module_root.mkdir(parents=True)
        iso_config.module_data_dir = module_root

        masks = _mask_paths(_bwrap(iso_config, iso_task, True))

        assert str(iso_config.db_path.parent.resolve()) in masks
        assert str(module_root.resolve()) in masks

    def test_it_excludes_the_namespace_tmpfs_mounts(self, iso_config, iso_task):
        """`/tmp` is mounted with `--proc` and `--dev`, long before any bind."""
        argv = _bwrap(iso_config, iso_task, True)

        assert "/tmp" in _tmpfs_paths(argv), "precondition: the namespace mounts /tmp"
        assert "/tmp" not in _mask_paths(argv)


class TestMaskDoesNotShadowNeededPaths:
    """A mask above the workspace would be an outage, not a hardening."""

    def test_db_dir_containing_the_workspace_is_refused(
        self, iso_config, iso_task, tmp_path, caplog,
    ):
        """The standalone layout puts db_path beside the workspace root."""
        workspace = tmp_path / "standalone"
        (workspace / "Users" / "alice").mkdir(parents=True)
        iso_config.nextcloud_mount_path = workspace
        iso_config.db_path = workspace / "istota.db"
        iso_config.module_data_dir = workspace / "modules"

        with caplog.at_level("ERROR"):
            argv = _bwrap(iso_config, iso_task, True)

        assert str(workspace.resolve()) not in _tmpfs_paths(argv), (
            "masking the mount root would hide the user's own workspace"
        )
        assert any("Not masking" in r.message for r in caplog.records), (
            "refusing to mask leaves databases exposed; it must be loud"
        )

    def test_user_temp_dir_is_never_masked(self, iso_config, iso_task, tmp_path):
        """The prompt and result files live here; masking it breaks the task."""
        iso_config.db_path = iso_config.temp_dir / "istota.db"
        iso_config.module_data_dir = iso_config.temp_dir / "modules"
        argv = _bwrap(iso_config, iso_task, True)
        user_temp = (iso_config.temp_dir / "alice").resolve()
        masks = _mask_paths(argv)
        # A floor before the loop. `_mask_paths` returns [] if the masks ever
        # stopped being the last mount operations, and a loop over [] passes
        # without asserting anything — vacuously green under exactly the
        # ordering regression this file exists to catch.
        assert masks, "no masks after the last bind — has the mask ordering changed?"
        for masked in masks:
            assert not user_temp.is_relative_to(masked), (
                f"{masked} shadows the user temp dir"
            )


class TestMisconfiguredModuleRoot:
    """A bad module_data_dir must not take every task down with it."""

    def test_sandbox_still_builds(self, iso_config, iso_task, caplog):
        """module_db_root() raises for a root under the mount; masking is not
        the place that failure should surface — it would turn one broken module
        into "no task runs at all"."""
        iso_config.module_data_dir = iso_config.nextcloud_mount_path / "modules"
        with caplog.at_level("WARNING"):
            argv = _bwrap(iso_config, iso_task, True)
        assert argv[0] == "bwrap"
        # The framework DB is still masked even though the module root isn't.
        assert str(iso_config.db_path.parent.resolve()) in _tmpfs_paths(argv)
        assert any("module_data_dir" in r.message for r in caplog.records)

    def test_module_resolution_still_raises(self, iso_config):
        """The misconfiguration keeps failing loudly where it matters."""
        iso_config.module_data_dir = iso_config.nextcloud_mount_path / "modules"
        with pytest.raises(ValueError, match="local disk"):
            iso_config.module_db_path("alice", "health")


class TestDisableUserns:
    """A tmpfs can be unmounted from a nested user namespace."""

    def test_flag_passed_when_bwrap_supports_it(self, iso_config, iso_task):
        with patch("istota.executor._bwrap_supports_disable_userns", return_value=True):
            argv = _bwrap(iso_config, iso_task, True)
        assert "--disable-userns" in argv
        assert argv.index("--disable-userns") < argv.index("--")

    def test_flag_omitted_when_unsupported(self, iso_config, iso_task):
        """Passing an unknown flag makes bwrap exit non-zero — that would fail
        every task on a host with bwrap older than 0.8."""
        with patch("istota.executor._bwrap_supports_disable_userns", return_value=False):
            argv = _bwrap(iso_config, iso_task, True)
        assert "--disable-userns" not in argv

    def test_unshare_user_travels_with_it(self, iso_config, iso_task):
        """bwrap: "--disable-userns requires --unshare-user", exit 1.

        Without the companion flag bwrap refuses the argv outright, so the two
        ship together or not at all. Verified against bubblewrap 0.11.0.
        """
        with patch("istota.executor._bwrap_supports_disable_userns", return_value=True):
            argv = _bwrap(iso_config, iso_task, True)
        assert "--unshare-user" in argv
        assert argv.index("--unshare-user") < argv.index("--disable-userns")

    def test_unshare_user_omitted_with_it(self, iso_config, iso_task):
        """It is only there to satisfy the other flag; alone it buys nothing."""
        with patch("istota.executor._bwrap_supports_disable_userns", return_value=False):
            argv = _bwrap(iso_config, iso_task, True)
        assert "--unshare-user" not in argv

    def test_probe_argv_carries_the_companion_flag(self):
        """The gap that kept this flag out of every sandbox it ever built.

        The probe used to run `bwrap --ro-bind / / --disable-userns -- true`,
        which bwrap rejects for the same reason the real argv would have been
        rejected — so the answer was "unsupported" on every host, and nobody
        noticed because the failure mode was silence.
        """
        from istota import executor

        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            return SimpleNamespace(returncode=0, stderr=b"")

        with patch.dict(executor._bwrap_flag_support, {}, clear=True), \
                patch("istota.executor._bwrap_available", return_value=True), \
                patch("istota.executor.subprocess.run", fake_run):
            assert executor._bwrap_supports_disable_userns() is True

        cmd = recorded["cmd"]
        assert "--unshare-user" in cmd
        assert cmd.index("--unshare-user") < cmd.index("--disable-userns")


class TestBwrapAvailabilityProbe:
    """Whether the sandbox runs at all, and what argv the answer commits to.

    The probe used to be one command — `bwrap --ro-bind / / -- true` — and that
    command is answered by whether bwrap decided to unshare the user namespace
    on its own. It does that when it is neither setuid nor uid 0, which is
    every bare-metal deployment and no container: the shipped image runs as
    root without CAP_SYS_ADMIN, so the probe failed at
    `unshare(CLONE_NEWNS)`, the daemon logged one warning and ran **every task
    unsandboxed**. Measured inside the image: plain exits 1, the same command
    with `--unshare-user` exits 0.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from istota import executor

        saved = (executor._bwrap_checked, executor._bwrap_needs_unshare_user)
        executor._bwrap_checked = None
        executor._bwrap_needs_unshare_user = False
        with patch.dict(executor._bwrap_flag_support, {}, clear=True):
            yield
        executor._bwrap_checked, executor._bwrap_needs_unshare_user = saved

    def _run_probes(self, *results):
        """Patch `subprocess.run` to answer each probe in turn, recording argv."""
        from istota import executor

        recorded: list[list[str]] = []
        answers = list(results)

        def fake_run(cmd, **kwargs):
            recorded.append(list(cmd))
            return answers.pop(0)

        ctx = patch("istota.executor.subprocess.run", fake_run)
        return recorded, ctx, executor

    def test_a_host_where_the_plain_probe_works_is_left_alone(self):
        """No second probe, no flag, nothing about that host changes."""
        recorded, ctx, executor = self._run_probes(
            SimpleNamespace(returncode=0, stderr=b""),
        )
        with patch("istota.executor.sys.platform", "linux"), \
                patch("shutil.which", return_value="/usr/bin/bwrap"), ctx:
            assert executor._bwrap_available() is True
            assert executor._bwrap_requires_unshare_user() is False

        assert len(recorded) == 1, recorded
        assert "--unshare-user" not in recorded[0]

    def test_a_plain_failure_is_retried_with_unshare_user(self, caplog):
        recorded, ctx, executor = self._run_probes(
            SimpleNamespace(
                returncode=1,
                stderr=b"bwrap: Creating new namespace failed: Operation not permitted",
            ),
            SimpleNamespace(returncode=0, stderr=b""),
        )
        with caplog.at_level("INFO"), \
                patch("istota.executor.sys.platform", "linux"), \
                patch("shutil.which", return_value="/usr/bin/bwrap"), ctx:
            assert executor._bwrap_available() is True
            assert executor._bwrap_requires_unshare_user() is True

        assert len(recorded) == 2, recorded
        assert "--unshare-user" in recorded[1]
        # The reason the first probe gave, so a reader of the log can tell this
        # host apart from one where bwrap simply works.
        assert any(
            "Operation not permitted" in record.getMessage()
            for record in caplog.records
        )

    def test_both_probes_failing_is_still_no_sandbox(self, caplog):
        recorded, ctx, executor = self._run_probes(
            SimpleNamespace(returncode=1, stderr=b"first reason"),
            SimpleNamespace(returncode=1, stderr=b"second reason"),
        )
        with caplog.at_level("WARNING"), \
                patch("istota.executor.sys.platform", "linux"), \
                patch("shutil.which", return_value="/usr/bin/bwrap"), ctx:
            assert executor._bwrap_available() is False
            assert executor._bwrap_requires_unshare_user() is False

        assert len(recorded) == 2, recorded
        message = " ".join(record.getMessage() for record in caplog.records)
        # Both reasons, because "it failed" is the same sentence for a kernel
        # with user namespaces switched off and a container blocking the call.
        assert "first reason" in message and "second reason" in message

    def test_the_flag_probes_carry_it_too(self):
        """Otherwise a supported flag reports unsupported on this host.

        This is not cosmetic. `--remount-ro` is what makes the database mask
        read-only, and its probe carries no `--unshare-user` of its own — so on
        a host needing the flag the probe fails at namespace creation, the mask
        stays writable, and a `sqlite3` probe against it creates a zero-byte
        file and answers "no such table", which reads as a corrupt database
        rather than as a boundary.
        """
        from istota import executor

        recorded: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            recorded.append(list(cmd))
            return SimpleNamespace(returncode=0, stderr=b"")

        executor._bwrap_needs_unshare_user = True
        with patch("istota.executor._bwrap_available", return_value=True), \
                patch("istota.executor.subprocess.run", fake_run):
            assert executor._bwrap_supports("--remount-ro", ["--remount-ro"]) is True

        assert recorded[0][:2] == ["bwrap", "--unshare-user"], recorded

    def test_it_is_not_added_twice(self):
        """`--disable-userns`'s probe already names it."""
        from istota import executor

        recorded: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            recorded.append(list(cmd))
            return SimpleNamespace(returncode=0, stderr=b"")

        executor._bwrap_needs_unshare_user = True
        with patch("istota.executor._bwrap_available", return_value=True), \
                patch("istota.executor.subprocess.run", fake_run):
            executor._bwrap_supports_disable_userns()

        assert recorded[0].count("--unshare-user") == 1, recorded[0]

    def test_no_probe_at_all_off_linux(self):
        recorded, ctx, executor = self._run_probes()
        with patch("istota.executor.sys.platform", "darwin"), ctx:
            assert executor._bwrap_available() is False
        assert recorded == []


class TestUnshareUserReachesTheRealArgv:
    """The probe and the command it gates have to agree.

    A probe answered with `--unshare-user` and a command built without it is
    the worst of the three outcomes: the daemon reports a working sandbox and
    then builds one that cannot start, so every task fails where before every
    task merely ran unconfined.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from istota import executor

        saved = executor._bwrap_needs_unshare_user
        yield
        executor._bwrap_needs_unshare_user = saved

    def test_the_flag_is_emitted_when_the_probe_needed_it(
        self, iso_config, iso_task
    ):
        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=False
        ), patch(
            "istota.executor._bwrap_requires_unshare_user", return_value=True
        ):
            argv = _bwrap(iso_config, iso_task, True)

        assert "--unshare-user" in argv
        assert argv.index("--unshare-user") < argv.index("--")
        # Alone: `--disable-userns` needs a writable /proc/sys, which no
        # container has, and passing an unsupported flag fails every task.
        assert "--disable-userns" not in argv

    def test_it_is_not_emitted_where_bwrap_unshares_on_its_own(
        self, iso_config, iso_task
    ):
        """The bare-metal deployment, which this change must not touch."""
        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=False
        ), patch(
            "istota.executor._bwrap_requires_unshare_user", return_value=False
        ):
            argv = _bwrap(iso_config, iso_task, True)

        assert "--unshare-user" not in argv

    def test_the_hardening_branch_still_wins(self, iso_config, iso_task):
        """One `--unshare-user`, not two, when both reasons apply."""
        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=True
        ), patch(
            "istota.executor._bwrap_requires_unshare_user", return_value=True
        ):
            argv = _bwrap(iso_config, iso_task, True)

        assert argv.count("--unshare-user") == 1
        assert "--disable-userns" in argv


class TestBwrapFlagProbe:
    """The probe is the only gate on whether a hardening flag is applied."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from istota import executor

        with patch.dict(executor._bwrap_flag_support, {}, clear=True):
            yield

    def _probe(self, **run_kwargs):
        from istota import executor

        with patch("istota.executor._bwrap_available", return_value=True), \
                patch("istota.executor.subprocess.run", **run_kwargs) as run:
            return executor._bwrap_supports("--flag", ["--flag"]), run

    def test_exit_zero_is_supported(self):
        supported, run = self._probe(
            return_value=SimpleNamespace(returncode=0, stderr=b""),
        )
        assert supported is True
        assert run.call_count == 1

    def test_non_zero_exit_is_unsupported(self, caplog):
        with caplog.at_level("INFO"):
            supported, _ = self._probe(
                return_value=SimpleNamespace(
                    returncode=1, stderr=b"bwrap: Unknown option --flag",
                ),
            )
        assert supported is False
        assert any(
            "Unknown option --flag" in r.getMessage() for r in caplog.records
        ), "the reason bwrap gave must be logged"

    def test_a_probe_that_cannot_run_warns(self, caplog):
        """Distinct from a rejection: hardening was lost for an unrelated
        reason, and the answer is cached for the process either way."""
        with caplog.at_level("WARNING"):
            supported, _ = self._probe(side_effect=OSError("no bwrap"))
        assert supported is False
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_timeout_is_unsupported(self):
        import subprocess as _sp

        supported, _ = self._probe(side_effect=_sp.TimeoutExpired("bwrap", 5))
        assert supported is False

    def test_answer_is_cached(self):
        from istota import executor

        with patch("istota.executor._bwrap_available", return_value=True), \
                patch(
                    "istota.executor.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stderr=b""),
                ) as run:
            assert executor._bwrap_supports("--flag", ["--flag"]) is True
            assert executor._bwrap_supports("--flag", ["--flag"]) is True
        assert run.call_count == 1

    def test_no_probe_without_bwrap(self):
        from istota import executor

        with patch("istota.executor._bwrap_available", return_value=False), \
                patch("istota.executor.subprocess.run") as run:
            assert executor._bwrap_supports("--flag", ["--flag"]) is False
        run.assert_not_called()

    def test_no_advice_logged_where_there_is_no_sandbox(self, caplog):
        """"masks can be lifted" is nonsense on a host that mounts none."""
        from istota import executor

        with caplog.at_level("INFO"), \
                patch("istota.executor._bwrap_available", return_value=False):
            assert executor._bwrap_supports_disable_userns() is False
            assert executor._bwrap_supports_remount_ro() is False
        assert not any("sandbox_ro_paths" in r.message for r in caplog.records)


class TestMasksComeLast:
    """The whole design rests on argv ordering."""

    def test_no_mount_operation_follows_the_masks(self, iso_config, iso_task):
        iso_config.security.sandbox_ro_paths = [str(iso_config.db_path.parents[2])]
        argv = _bwrap(iso_config, iso_task, True)

        mount_ops = {
            "--bind", "--ro-bind", "--bind-try", "--ro-bind-try",
            "--dev-bind", "--symlink", "--tmpfs", "--proc", "--dev",
            "--overlay", "--ro-overlay",
        }
        last_mask = max(
            i for i, tok in enumerate(argv)
            if tok == "--tmpfs" and argv[i + 1] in (
                str(iso_config.db_path.parent.resolve()),
                str(iso_config.module_db_root()),
            )
        )
        trailing = [tok for tok in argv[last_mask + 2:] if tok in mount_ops]
        assert not trailing, (
            f"mount operations after the DB masks would undo them: {trailing}"
        )


class TestSandboxRoPathsValidation:
    """The key went from inert to live, so a malformed value now has teeth."""

    def _load(self, tmp_path, body):
        from istota.config import load_config

        cfg = tmp_path / "config.toml"
        cfg.write_text(f"[security]\n{body}\n", encoding="utf-8")
        return load_config(cfg)

    def test_bare_string_is_rejected(self, tmp_path, caplog):
        """`for p in "/srv/app"` iterates characters and ro-binds `/`."""
        with caplog.at_level("ERROR"):
            config = self._load(tmp_path, 'sandbox_ro_paths = "/srv/app"')
        assert config.security.sandbox_ro_paths == []
        assert any("must be a list" in r.message for r in caplog.records)

    def test_host_root_is_rejected(self, tmp_path, caplog):
        with caplog.at_level("ERROR"):
            config = self._load(tmp_path, 'sandbox_ro_paths = ["/", "/opt/svc"]')
        assert config.security.sandbox_ro_paths == ["/opt/svc"]
        assert any("host root" in r.message for r in caplog.records)

    def test_non_string_entries_dropped(self, tmp_path):
        config = self._load(tmp_path, 'sandbox_ro_paths = ["/opt/svc", 42, ""]')
        assert config.security.sandbox_ro_paths == ["/opt/svc"]

    def test_valid_list_passes_through(self, tmp_path):
        config = self._load(tmp_path, 'sandbox_ro_paths = ["/opt/svc"]')
        assert config.security.sandbox_ro_paths == ["/opt/svc"]


class TestSandboxWithoutProxyWarns:
    """A combination that leaves every skill CLI unable to reach a database."""

    def test_warns(self, tmp_path, caplog):
        from istota.config import load_config

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[security]\nsandbox_enabled = true\nskill_proxy_enabled = false\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            load_config(cfg)
        assert any(
            "skill_proxy_enabled = false" in r.message for r in caplog.records
        )

    def test_quiet_when_both_off(self, tmp_path, caplog):
        """The standalone install's trusted single-user posture."""
        from istota.config import load_config

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[security]\nsandbox_enabled = false\nskill_proxy_enabled = false\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            load_config(cfg)
        assert not any(
            "skill_proxy_enabled = false" in r.message for r in caplog.records
        )


class TestNativeFsRootsExcludeDb:
    """NativeBrain's in-process file tools get the same boundary."""

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_db_not_in_read_roots(self, iso_config, iso_task, is_admin):
        user_temp = iso_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)
        read_roots, write_roots, _ = native_fs_roots(
            iso_config, iso_task, is_admin, [], user_temp,
        )
        assert iso_config.db_path.resolve() not in read_roots
        assert iso_config.db_path.resolve() not in write_roots
        assert iso_config.db_path.parent.resolve() not in read_roots


class TestWorkspaceBlocklist:
    """`istota repl --workspace <db dir>` must not RW-bind the DBs back in."""

    def test_rejects_db_directory(self, iso_config):
        with pytest.raises(ValueError):
            _validate_workspace_dir(iso_config, iso_config.db_path.parent)

    def test_rejects_module_db_root(self, iso_config, tmp_path):
        relocated = tmp_path / "elsewhere" / "modules"
        (relocated / "bob").mkdir(parents=True)
        iso_config.module_data_dir = relocated
        with pytest.raises(ValueError):
            _validate_workspace_dir(iso_config, relocated)

    def test_rejects_directory_inside_module_root(self, iso_config):
        inside = iso_config.module_db_root() / "bob"
        with pytest.raises(ValueError):
            _validate_workspace_dir(iso_config, inside)

    def test_allows_unrelated_directory(self, iso_config, tmp_path):
        ok = tmp_path / "work"
        ok.mkdir()
        assert _validate_workspace_dir(iso_config, ok) == ok.resolve()


class TestModuleDbRoot:
    """`module_db_root()` is the single place the root is derived."""

    def test_defaults_alongside_framework_db(self, tmp_path):
        config = Config(db_path=tmp_path / "data" / "istota.db")
        assert config.module_db_root() == (tmp_path / "data" / "modules").resolve()

    def test_honours_explicit_module_data_dir(self, tmp_path):
        config = Config(
            db_path=tmp_path / "data" / "istota.db",
            module_data_dir=tmp_path / "mods",
        )
        assert config.module_db_root() == (tmp_path / "mods").resolve()

    def test_module_db_path_builds_on_it(self, tmp_path):
        config = Config(db_path=tmp_path / "data" / "istota.db")
        assert config.module_db_path("alice", "health") == (
            config.module_db_root() / "alice" / "health.db"
        )

    def test_refuses_module_root_under_the_mount(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "data" / "istota.db",
            module_data_dir=mount / "modules",
            nextcloud_mount_path=mount,
        )
        with pytest.raises(ValueError, match="local disk"):
            config.module_db_root()


class TestSandboxAdminDbWriteRetired:
    """The knob is gone; a stale config value must not silently loosen anything."""

    def test_config_has_no_such_field(self):
        assert not hasattr(SecurityConfig(), "sandbox_admin_db_write")

    def test_stale_toml_key_warns_and_is_ignored(self, tmp_path, caplog):
        from istota.config import load_config

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            "[security]\nsandbox_admin_db_write = true\n", encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            config = load_config(cfg_file)
        assert not hasattr(config.security, "sandbox_admin_db_write")
        assert any(
            "sandbox_admin_db_write" in r.message for r in caplog.records
        ), "removal of a security knob should be announced, not silent"
