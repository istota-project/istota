"""Tests for the local single-user self-update (`istota update`).

The update logic (`istota.updater`) orchestrates git + uv + migrations with all
external effects injected, so these run without a real git checkout, uv, or npm.
"""

import json
import subprocess
from pathlib import Path

import pytest

from istota import updater
from istota.config import (
    Config,
    NextcloudConfig,
    SecurityConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _standalone_config(tmp_path):
    """A config where ``is_standalone`` is True (blank NC url + web.auth none)."""
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(url=""),
        users={"stefan": UserConfig(display_name="Stefan")},
        talk=TalkConfig(enabled=False),
        security=SecurityConfig(sandbox_enabled=False),
        web=WebConfig(enabled=True, port=8799, auth="none"),
        bot_name="Istota",
    )


def _server_config(tmp_path):
    """A config where ``is_standalone`` is False (NC url set)."""
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(url="https://cloud.example.com"),
        users={"stefan": UserConfig(display_name="Stefan")},
        web=WebConfig(auth="nextcloud"),
        bot_name="Istota",
    )


def _make_source(tmp_path) -> Path:
    """A directory that looks like a git checkout."""
    src = tmp_path / "checkout"
    (src / ".git").mkdir(parents=True)
    return src


def _write_record(tmp_path, source: Path, *, method="checkout",
                  extras="local,money,location", ref="main", channel=None) -> Path:
    path = tmp_path / "install.json"
    rec = {
        "method": method,
        "source": str(source),
        "extras": extras,
        "ref": ref,
    }
    if channel is not None:
        rec["channel"] = channel
    path.write_text(json.dumps(rec))
    return path


class FakeRun:
    """Records subprocess invocations and returns canned results.

    Distinguishes the two ``git rev-parse`` calls by whether the ref token is
    ``HEAD`` (local) or something else (the remote-tracking ref).
    """

    def __init__(self, *, head="oldsha", remote="newsha", dirty="", install_rc=0,
                 fetch_rc=0, reset_rc=0, status_rc=0, tags="v0.32.0\n"):
        self.calls: list[tuple[list[str], Path | None]] = []
        self.head = head
        self.remote = remote
        self.dirty = dirty
        self.install_rc = install_rc
        self.fetch_rc = fetch_rc
        self.reset_rc = reset_rc
        self.status_rc = status_rc
        self.tags = tags

    def __call__(self, cmd, *, cwd=None):
        self.calls.append((list(cmd), cwd))
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, self.status_rc, self.dirty, "")
        if "tag" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self.tags, "")
        if "rev-parse" in cmd:
            # bare HEAD → local sha; FETCH_HEAD or a tag ref → the remote (target) sha.
            val = self.head if cmd[-1] == "HEAD" else self.remote
            return subprocess.CompletedProcess(cmd, 0, val + "\n", "")
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, self.fetch_rc, "", "")
        if "reset" in cmd:
            return subprocess.CompletedProcess(cmd, self.reset_rc, "", "")
        if "install" in cmd:
            return subprocess.CompletedProcess(cmd, self.install_rc, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ran(self, token) -> bool:
        return any(token in cmd for cmd, _ in self.calls)

    def reset_targets(self) -> list[str]:
        """Last token of every `git reset --hard <target>` invocation."""
        return [cmd[-1] for cmd, _ in self.calls if "reset" in cmd]


def _run_kwargs(**over):
    """Common injected callables for run_update; overridable per test."""
    migrated: list[Path] = []
    built: list[Path] = []
    kwargs = dict(
        build_web=lambda src: built.append(src),
        migrate=lambda db_path: migrated.append(db_path),
        daemon_running=lambda: False,
    )
    kwargs.update(over)
    return kwargs, migrated, built


# ---------------------------------------------------------------------------
# install record
# ---------------------------------------------------------------------------


class TestInstallRecord:
    def test_record_path_sibling_of_config(self, tmp_path):
        cfg = tmp_path / "sub" / "config.toml"
        assert updater.install_record_path(cfg) == tmp_path / "sub" / "install.json"

    def test_record_path_default_xdg(self):
        assert updater.install_record_path(None) == (
            Path.home() / ".config" / "istota" / "install.json"
        )

    def test_load_record_reads_json(self, tmp_path):
        src = _make_source(tmp_path)
        path = _write_record(tmp_path, src, extras="local", ref="main")
        rec = updater.load_install_record(path)
        assert rec["source"] == str(src)
        assert rec["extras"] == "local"
        assert rec["ref"] == "main"
        assert rec["method"] == "checkout"

    def test_load_record_missing_raises(self, tmp_path):
        with pytest.raises(updater.UpdateError) as exc:
            updater.load_install_record(tmp_path / "nope.json")
        assert "install.sh" in str(exc.value)

    def test_load_record_malformed_raises(self, tmp_path):
        path = tmp_path / "install.json"
        path.write_text("{not json")
        with pytest.raises(updater.UpdateError):
            updater.load_install_record(path)

    def test_load_record_non_dict_raises(self, tmp_path):
        path = tmp_path / "install.json"
        path.write_text("[1, 2, 3]")  # valid JSON, wrong shape
        with pytest.raises(updater.UpdateError) as exc:
            updater.load_install_record(path)
        assert "object" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# run_update guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_refuses_on_server_shape(self, tmp_path):
        cfg = _server_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "standalone" in str(exc.value).lower()
        assert not run.calls  # never touched git/uv

    def test_missing_source_dir_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        rec = _write_record(tmp_path, tmp_path / "gone")
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "source" in str(exc.value).lower()

    def test_dir_without_git_raises(self, tmp_path):
        # Directory exists but is not a git checkout (no .git).
        cfg = _standalone_config(tmp_path)
        src = tmp_path / "plain"
        src.mkdir()
        rec = _write_record(tmp_path, src)
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "checkout" in str(exc.value).lower()
        assert not run.calls

    def test_unknown_method_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, method="tarball")
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError):
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert not run.calls

    def test_pypi_method_not_supported_yet(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, method="pypi")
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "pypi" in str(exc.value).lower()
        assert not run.calls


# ---------------------------------------------------------------------------
# checkout flow
# ---------------------------------------------------------------------------


class TestCheckoutFlow:
    def test_already_up_to_date_skips_reinstall(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(head="same", remote="same")
        kwargs, migrated, built = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert rc == 0
        assert run.ran("fetch")
        assert not run.ran("reset")     # nothing to reset to
        assert not run.ran("install")   # no reinstall
        assert migrated == []           # no migration on a no-op
        assert built == []

    def test_happy_path_updates(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, extras="local,money", ref="main")
        run = FakeRun(head="old", remote="new")
        kwargs, migrated, built = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert rc == 0
        assert run.ran("fetch")
        assert run.ran("reset")
        assert run.ran("install")
        # reinstall targets the recorded source + extras
        install_cmd = next(c for c, _ in run.calls if "install" in c)
        assert any(f"{src}[local,money]" in tok for tok in install_cmd)
        assert migrated == [cfg.db_path]
        assert built == [src]

    def test_dirty_checkout_without_force_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(dirty=" M src/istota/cli.py\n")
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "uncommitted" in str(exc.value).lower() or "dirty" in str(exc.value).lower()
        assert not run.ran("reset")
        assert not run.ran("install")

    def test_dirty_checkout_with_force_proceeds(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(dirty=" M x\n", head="old", remote="new")
        kwargs, migrated, _ = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, force=True, **kwargs)
        assert rc == 0
        assert run.ran("reset")
        assert run.ran("install")
        assert migrated == [cfg.db_path]

    def test_uv_install_failure_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(head="old", remote="new", install_rc=1)
        kwargs, migrated, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "uv" in str(exc.value).lower() or "install" in str(exc.value).lower()
        assert migrated == []  # migrations only run after a successful reinstall

    def test_fetch_failure_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(fetch_rc=1)
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "fetch" in str(exc.value).lower()
        assert not run.ran("install")

    def test_status_failure_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(status_rc=1)
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError):
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert not run.ran("fetch")

    def test_reset_failure_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(head="old", remote="new", reset_rc=1)
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError):
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert not run.ran("install")

    def test_install_failure_rolls_back_checkout(self, tmp_path):
        # After the forward reset to the new commit, a failed reinstall must roll
        # the checkout back to the old HEAD so the next run re-detects the update
        # instead of falsely reporting "already up to date".
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(head="oldsha", remote="new", install_rc=1)
        kwargs, migrated, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError):
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        # forward reset to FETCH_HEAD, then rollback reset to the old sha.
        assert run.reset_targets() == ["FETCH_HEAD", "oldsha"]
        assert migrated == []

    def test_default_migrate_runs_fresh_istota_init(self, tmp_path):
        # With migrate NOT injected, migrations must shell out to the reinstalled
        # `istota init` (fresh code) rather than the stale in-process db.init_db.
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(head="old", remote="new")
        rc = updater.run_update(
            cfg, record_path=rec, config_path=tmp_path / "config.toml",
            run=run, build_web=lambda s: None, daemon_running=lambda: False,
        )
        assert rc == 0
        init_cmds = [c for c, _ in run.calls if "init" in c]
        assert init_cmds, "expected a fresh `istota init` subprocess"
        assert init_cmds[0][0] == "istota"
        assert "-c" in init_cmds[0]  # config path threaded through

    def test_daemon_running_prints_restart_nudge(self, tmp_path, capsys):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)
        run = FakeRun(head="old", remote="new")
        kwargs, _, _ = _run_kwargs(daemon_running=lambda: True)
        rc = updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert rc == 0
        assert "restart" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# release channel
# ---------------------------------------------------------------------------


class TestChannel:
    def test_legacy_record_without_channel_tracks_main(self, tmp_path):
        # A record predating the channel field keeps the old branch-tracking
        # behavior — never a silent reset backwards onto an older release tag.
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, ref="main")  # no channel key
        run = FakeRun(head="old", remote="new")
        kwargs, _, _ = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert rc == 0
        assert run.ran("fetch")
        # main channel resets to FETCH_HEAD (branch tip), never to a tag
        assert run.reset_targets() == ["FETCH_HEAD"]
        assert not run.ran("tag")

    def test_stable_channel_updates_to_latest_release_tag(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, channel="stable")
        run = FakeRun(head="old", remote="new", tags="v0.32.0\nv0.31.1\n")
        kwargs, migrated, _ = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert rc == 0
        # fetched tags, listed them, reset to the highest release tag
        assert run.ran("tag")
        assert run.reset_targets() == ["v0.32.0"]
        assert run.ran("install")
        assert migrated == [cfg.db_path]

    def test_stable_channel_already_on_latest_tag_skips_reinstall(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, channel="stable")
        run = FakeRun(head="same", remote="same")  # HEAD == tag commit
        kwargs, migrated, _ = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert rc == 0
        assert not run.ran("reset")
        assert not run.ran("install")
        assert migrated == []

    def test_stable_channel_no_release_tags_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, channel="stable")
        run = FakeRun(head="old", remote="new", tags="")  # no v* tags
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "release" in str(exc.value).lower()
        assert "--channel main" in str(exc.value)
        assert not run.ran("install")

    def test_unknown_channel_in_record_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, channel="nightly")
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, **kwargs)
        assert "channel" in str(exc.value).lower()
        assert not run.ran("install")

    def test_channel_override_persisted_to_record(self, tmp_path):
        # Passing --channel main against a stable record switches AND persists,
        # so future runs stay on main without repeating the flag.
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, channel="stable")
        run = FakeRun(head="old", remote="new")
        kwargs, _, _ = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, channel="main", **kwargs)
        assert rc == 0
        # tracked main (FETCH_HEAD), not the release tag
        assert run.reset_targets() == ["FETCH_HEAD"]
        persisted = json.loads(rec.read_text())
        assert persisted["channel"] == "main"
        # other fields preserved
        assert persisted["source"] == str(src)
        assert persisted["extras"] == "local,money,location"

    def test_channel_override_to_stable_persisted(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src)  # legacy, no channel
        run = FakeRun(head="old", remote="new", tags="v0.32.0\n")
        kwargs, _, _ = _run_kwargs()
        rc = updater.run_update(cfg, record_path=rec, run=run, channel="stable", **kwargs)
        assert rc == 0
        assert run.reset_targets() == ["v0.32.0"]
        assert json.loads(rec.read_text())["channel"] == "stable"

    def test_invalid_channel_override_raises(self, tmp_path):
        cfg = _standalone_config(tmp_path)
        src = _make_source(tmp_path)
        rec = _write_record(tmp_path, src, channel="stable")
        run = FakeRun()
        kwargs, _, _ = _run_kwargs()
        with pytest.raises(updater.UpdateError) as exc:
            updater.run_update(cfg, record_path=rec, run=run, channel="bogus", **kwargs)
        assert "channel" in str(exc.value).lower()
