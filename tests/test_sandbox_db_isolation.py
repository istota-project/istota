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
        for masked in _tmpfs_paths(argv):
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
        read_roots, write_roots = native_fs_roots(
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
