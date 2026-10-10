"""`istota apply -f plan`: the one declarative verb, `[config]` only for now.

The contract a config manager builds on is the exit code, after
`terraform plan -detailed-exitcode`: 0 nothing changed, 2 changed (or would
change, under `--dry-run`), 1 an error with nothing written. Ansible's
`changed_when` and `--check` read exactly that, so each code is asserted along
with what it promises about the file on disk.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from istota import apply, cli

PLAN_CONFIG = {
    "bot_name": "Istota",
    "db_path": "/data/db/istota.db",
    "site": {"hostname": "bot.example.com"},
    "scheduler": {"poll_interval": 7},
    "users": {"alice": {"display_name": "Alice", "timezone": "UTC"}},
}


def _write_plan(path: Path, config: dict | None = None, **sections) -> Path:
    from istota.lib import toml_write

    document = {"config": PLAN_CONFIG if config is None else config, **sections}
    path.write_text(toml_write.dumps(document))
    return path


def _invoke(monkeypatch, capsys, config_path: Path, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config_path), "apply", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def config_path(tmp_path) -> Path:
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir()
    return path


class TestTheExitCodes:
    def test_a_new_config_is_written_and_answers_2(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "plan.toml")
        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == apply.EXIT_CHANGED == 2
        assert tomllib.loads(config_path.read_text()) == PLAN_CONFIG
        assert "+[site]" in out

    def test_the_same_plan_again_answers_0_and_leaves_the_file(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "plan.toml")
        _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        before = config_path.read_bytes()
        mtime = config_path.stat().st_mtime_ns

        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))

        assert code == apply.EXIT_UNCHANGED == 0
        assert config_path.read_bytes() == before
        assert config_path.stat().st_mtime_ns == mtime
        assert "+" not in "".join(line[:1] for line in out.splitlines())

    def test_a_hand_formatted_file_with_the_same_values_is_unchanged(
        self, monkeypatch, capsys, tmp_path, config_path,
    ):
        config_path.write_text(
            "# the operator's comments survive a no-op apply\n"
            'bot_name = "Istota"\ndb_path = "/data/db/istota.db"\n'
            "[scheduler]\npoll_interval = 7\n"
            '[site]\nhostname = "bot.example.com"\n'
            '[users.alice]\ntimezone = "UTC"\ndisplay_name = "Alice"\n'
        )
        before = config_path.read_bytes()
        plan = _write_plan(tmp_path / "plan.toml")
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == 0
        assert config_path.read_bytes() == before

    def test_dry_run_answers_2_and_writes_nothing(self, monkeypatch, capsys, tmp_path, config_path):
        _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "plan.toml")))
        before = config_path.read_bytes()
        changed = dict(PLAN_CONFIG, scheduler={"poll_interval": 9})
        plan = _write_plan(tmp_path / "plan2.toml", changed)

        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--dry-run")

        assert code == 2
        assert config_path.read_bytes() == before
        assert "-poll_interval = 7" in out
        assert "+poll_interval = 9" in out

    def test_dry_run_with_no_change_answers_0(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "plan.toml")
        _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--dry-run")
        assert code == 0

    def test_a_json_plan_is_the_same_plan(self, monkeypatch, capsys, tmp_path, config_path):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"config": PLAN_CONFIG}))
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == 2
        assert tomllib.loads(config_path.read_text()) == PLAN_CONFIG


class TestRefusalsWriteNothing:
    @pytest.fixture
    def existing(self, config_path) -> bytes:
        config_path.write_text('bot_name = "Before"\n')
        return config_path.read_bytes()

    def _refused(self, monkeypatch, capsys, config_path, plan, existing) -> str:
        code, out, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == apply.EXIT_ERROR == 1, out + err
        assert config_path.read_bytes() == existing
        return err

    def test_a_bad_value_is_refused(self, monkeypatch, capsys, tmp_path, config_path, existing):
        bad = dict(PLAN_CONFIG, scheduler={"poll_interval": "fast"})
        err = self._refused(monkeypatch, capsys, config_path, _write_plan(tmp_path / "p.toml", bad), existing)
        assert "scheduler.poll_interval" in err

    def test_a_value_the_loader_raises_on_is_refused(self, monkeypatch, capsys, tmp_path, config_path, existing):
        bad = dict(PLAN_CONFIG, email={"confirm_sender_match": "verify"})
        self._refused(monkeypatch, capsys, config_path, _write_plan(tmp_path / "p.toml", bad), existing)

    def test_a_section_this_version_does_not_reconcile_is_refused(
        self, monkeypatch, capsys, tmp_path, config_path, existing,
    ):
        plan = _write_plan(tmp_path / "p.toml", users={"alice": {"timezone": "UTC"}})
        err = self._refused(monkeypatch, capsys, config_path, plan, existing)
        assert "[users]" in err

    def test_a_newer_plan_version_is_refused(self, monkeypatch, capsys, tmp_path, config_path, existing):
        plan = _write_plan(tmp_path / "p.toml", meta={"version": apply.PLAN_VERSION + 1})
        self._refused(monkeypatch, capsys, config_path, plan, existing)

    def test_an_unparsable_plan_is_refused(self, monkeypatch, capsys, tmp_path, config_path, existing):
        plan = tmp_path / "p.toml"
        plan.write_text("[config\n")
        self._refused(monkeypatch, capsys, config_path, plan, existing)

    def test_a_null_in_a_json_plan_is_refused(self, monkeypatch, capsys, tmp_path, config_path, existing):
        plan = tmp_path / "p.json"
        plan.write_text(json.dumps({"config": dict(PLAN_CONFIG, bot_name=None)}))
        self._refused(monkeypatch, capsys, config_path, plan, existing)


class TestWhatItSays:
    def test_an_unknown_key_warns_and_is_still_applied(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "p.toml", dict(PLAN_CONFIG, scheduler={"poll_intervall": 3}))
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == 2
        assert "scheduler.poll_intervall" in err
        assert tomllib.loads(config_path.read_text())["scheduler"] == {"poll_intervall": 3}

    def test_a_credential_in_the_config_is_not_printed(self, monkeypatch, capsys, tmp_path, config_path):
        secret = "app-password-that-must-not-be-printed"
        config = dict(PLAN_CONFIG, nextcloud={"url": "https://cloud.example.com", "app_password": secret})
        code, out, err = _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml", config)))
        assert code == 2
        assert secret not in out + err
        assert "app_password" in out
        assert tomllib.loads(config_path.read_text())["nextcloud"]["app_password"] == secret

    def test_a_plan_without_a_config_section_changes_nothing(self, monkeypatch, capsys, tmp_path, config_path):
        plan = tmp_path / "p.toml"
        plan.write_text(f"[meta]\nversion = {apply.PLAN_VERSION}\n")
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == 0
        assert not config_path.exists()

    def test_the_written_file_is_private(self, monkeypatch, capsys, tmp_path, config_path):
        _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml")))
        assert config_path.stat().st_mode & 0o777 == 0o600
