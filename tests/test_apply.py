"""`istota apply -f plan`: the one declarative verb, `[config]` only for now.

The contract a config manager builds on is the exit code, after
`terraform plan -detailed-exitcode`: 0 nothing changed, 2 changed (or would
change, under `--dry-run`), 1 an error with nothing written. Ansible's
`changed_when` and `--check` read exactly that, so each code is asserted along
with what it promises about the file on disk.
"""

from __future__ import annotations

import io
import json
import os
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
        config = dict(
            PLAN_CONFIG, workspace_path="/mnt/shared",
            nextcloud={"url": "https://cloud.example.com", "app_password": secret},
        )
        code, out, err = _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml", config)))
        assert code == 2
        assert secret not in out + err
        assert "app_password" in out
        assert tomllib.loads(config_path.read_text())["nextcloud"]["app_password"] == secret

    def test_the_diff_is_of_redacted_documents(self, monkeypatch, capsys, tmp_path, config_path):
        """A header value under `extra_headers` has a harmless name, which the
        old line-by-line redaction let through. Every secret leaf is replaced
        before rendering; a changed one shows as changed, an unchanged one not
        at all."""
        config_path.write_text(
            'bot_name = "Before"\n'
            "[brain.native]\n"
            'api_key = "old-native-key"\n'
            "[brain.native.extra_headers]\n"
            'x-tenant = "old-header-value"\n'
            'x-region = "kept-header-value"\n'
        )
        config = {
            "bot_name": "After",
            "brain": {"native": {
                "api_key": "new-native-key",
                "extra_headers": {"x-tenant": "new-header-value", "x-region": "kept-header-value"},
            }},
        }
        code, out, err = _invoke(
            monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml", config)), "--dry-run",
        )
        assert code == 2, out + err
        for value in ("old-native-key", "new-native-key", "old-header-value", "new-header-value", "kept-header-value"):
            assert value not in out + err
        assert out == (
            "--- a/config.toml\n"
            "+++ b/config.toml\n"
            "@@ -1,8 +1,8 @@\n"
            '-bot_name = "Before"\n'
            '+bot_name = "After"\n'
            " \n"
            " [brain.native]\n"
            '-api_key = "<redacted>"\n'
            '+api_key = "<changed>"\n'
            " \n"
            " [brain.native.extra_headers]\n"
            '-x-tenant = "<redacted>"\n'
            '+x-tenant = "<changed>"\n'
            ' x-region = "<redacted>"\n'
            "Would change: config (--dry-run, nothing written).\n"
        )

    def test_a_plan_without_a_config_section_changes_nothing(self, monkeypatch, capsys, tmp_path, config_path):
        plan = tmp_path / "p.toml"
        plan.write_text(f"[meta]\nversion = {apply.PLAN_VERSION}\n")
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == 0
        assert not config_path.exists()

    def test_the_written_file_is_private(self, monkeypatch, capsys, tmp_path, config_path):
        _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml")))
        assert config_path.stat().st_mode & 0o777 == 0o600


class TestTypedProblems:
    """`istota apply` decides on the kind of problem the loader reports, not on
    its wording. A value the loader would silently replace is refused, which
    is what the module always promised and what these three did not get."""

    @pytest.mark.parametrize("config", [
        dict(PLAN_CONFIG, web={"token_storage": "plain"}),
        dict(PLAN_CONFIG, scheduler={"talk_poll_timeout": 0}),
        dict(PLAN_CONFIG, web={"auth": ["email", "carrier-pigeon"]}),
    ], ids=["token_storage", "talk_poll_timeout", "web_auth"])
    def test_a_value_the_loader_would_replace_is_refused(self, monkeypatch, capsys, tmp_path, config_path, config):
        config_path.write_text('bot_name = "Before"\n')
        before = config_path.read_bytes()
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml", config)))
        assert code == apply.EXIT_ERROR, err
        assert config_path.read_bytes() == before

    def test_collect_reports_kinds_and_paths(self):
        result = apply.collect(dict(PLAN_CONFIG, scheduler={"talk_poll_timeout": 0, "poll_intervall": 3}))
        assert result.error is None
        kinds = {(problem["kind"], tuple(problem["path"])) for problem in result.problems}
        assert ("discarded", ("scheduler", "talk_poll_timeout")) in kinds
        assert ("unknown", ("scheduler", "poll_intervall")) in kinds

    def test_collect_reports_a_loader_exception_as_an_error(self):
        result = apply.collect(dict(PLAN_CONFIG, email={"confirm_sender_match": "verify"}))
        assert result.error


class TestCheck:
    """`--check` validates a plan and nothing else: no current file is read and
    nothing is written. With `--json` it prints one object the web parses."""

    def _check(self, monkeypatch, capsys, plan_text: str, *argv: str) -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(plan_text))
        monkeypatch.delenv("ISTOTA_CONFIG_PATH", raising=False)
        monkeypatch.setattr("sys.argv", ["istota", "apply", "--check", "-f", "-", *argv])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code or 0
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_a_good_plan_answers_0_with_no_problems(self, monkeypatch, capsys):
        code, out, _ = self._check(monkeypatch, capsys, json.dumps({"config": PLAN_CONFIG}), "--json")
        assert code == 0
        assert json.loads(out) == {"problems": [], "error": None}

    def test_a_discard_is_a_problem_and_answers_1(self, monkeypatch, capsys):
        plan = {"config": dict(PLAN_CONFIG, scheduler={"talk_poll_timeout": 0})}
        code, out, _ = self._check(monkeypatch, capsys, json.dumps(plan), "--json")
        assert code == 1
        reply = json.loads(out)
        assert reply["error"] is None
        assert {"kind": "discarded", "path": ["scheduler", "talk_poll_timeout"]} in [
            {"kind": p["kind"], "path": p["path"]} for p in reply["problems"]]

    def test_an_unknown_key_is_reported_and_answers_0(self, monkeypatch, capsys):
        plan = {"config": dict(PLAN_CONFIG, scheduler={"poll_intervall": 3})}
        code, out, _ = self._check(monkeypatch, capsys, json.dumps(plan), "--json")
        assert code == 0
        assert [p["kind"] for p in json.loads(out)["problems"]] == ["unknown"]

    def test_an_unparsable_plan_is_an_error_in_the_json(self, monkeypatch, capsys):
        code, out, _ = self._check(monkeypatch, capsys, "[config\n", "--json")
        assert code == 1
        reply = json.loads(out)
        assert reply["problems"] == [] and reply["error"]

    def test_a_toml_plan_on_stdin_is_read_too(self, monkeypatch, capsys):
        from istota.lib import toml_write

        code, out, _ = self._check(monkeypatch, capsys, toml_write.dumps({"config": PLAN_CONFIG}), "--json")
        assert code == 0, out
        assert json.loads(out)["error"] is None

    def test_without_json_a_refusal_answers_1(self, monkeypatch, capsys):
        plan = {"config": dict(PLAN_CONFIG, scheduler={"talk_poll_timeout": 0})}
        code, _, err = self._check(monkeypatch, capsys, json.dumps(plan))
        assert code == 1
        assert "scheduler.talk_poll_timeout" in err

    def test_check_writes_nothing_even_with_a_config_named(self, monkeypatch, capsys, config_path):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"config": PLAN_CONFIG, "meta": {"managed": True}})))
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "--check", "-f", "-")
        assert code == 0
        assert list(config_path.parent.iterdir()) == []

    def test_json_without_check_is_refused(self, monkeypatch, capsys, tmp_path, config_path):
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml")), "--json")
        assert code == 1
        assert "--json" in err
        assert not config_path.exists()

    def test_a_plan_on_stdin_applies_like_a_file(self, monkeypatch, capsys, config_path):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"config": PLAN_CONFIG})))
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", "-")
        assert code == 2
        assert tomllib.loads(config_path.read_text()) == PLAN_CONFIG


class TestTheManagementMarker:
    """`config/managed.json` says who owns the config. Apply writes its
    `config` entry from the plan's `[meta]`, and a change to the entry alone is
    not a config change: exit 0, so `istota-stack apply` does not restart."""

    def _marker(self, config_path: Path) -> dict:
        return json.loads((config_path.parent / "managed.json").read_text())

    def test_a_plan_without_meta_records_an_unmanaged_plan(self, monkeypatch, capsys, tmp_path, config_path):
        _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml")))
        entry = self._marker(config_path)["config"]
        assert (entry["managed"], entry["source"]) == (False, "plan")
        assert entry["at"]
        assert (config_path.parent / "managed.json").stat().st_mode & 0o777 == 0o600

    def test_a_meta_only_change_is_written_and_answers_0(self, monkeypatch, capsys, tmp_path, config_path):
        _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml")))
        before = config_path.read_bytes()
        plan = _write_plan(tmp_path / "p2.toml", meta={"managed": True, "source": "ansible", "request": 4})

        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))

        assert code == apply.EXIT_UNCHANGED
        assert "Updated: management marker" in out
        assert config_path.read_bytes() == before
        entry = self._marker(config_path)["config"]
        assert (entry["managed"], entry["source"]) == (True, "ansible")

    def test_the_same_meta_again_rewrites_nothing(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "p.toml", meta={"managed": True, "source": "ansible"})
        _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        marker = config_path.parent / "managed.json"
        before = (marker.read_bytes(), marker.stat().st_mtime_ns)
        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == 0
        assert "management marker" not in out
        assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before

    def test_the_other_resources_are_kept(self, monkeypatch, capsys, tmp_path, config_path):
        secrets = {"managed": True, "source": "ansible", "at": "2026-10-01T00:00:00Z"}
        (config_path.parent / "managed.json").write_text(json.dumps({"secrets": secrets}))
        _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml")))
        assert self._marker(config_path)["secrets"] == secrets

    def test_dry_run_writes_no_marker(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "p.toml", meta={"managed": True})
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--dry-run")
        assert code == 2
        assert not (config_path.parent / "managed.json").exists()

    def test_a_plan_with_no_config_section_writes_no_marker(self, monkeypatch, capsys, tmp_path, config_path):
        plan = tmp_path / "p.toml"
        plan.write_text("[meta]\nmanaged = true\n")
        _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert not (config_path.parent / "managed.json").exists()

    @pytest.mark.parametrize("meta", [
        {"managed": "yes"}, {"source": 7}, {"source": "x" * 65}, {"request": "4"}, {"request": True},
    ])
    def test_a_bad_meta_is_refused(self, monkeypatch, capsys, tmp_path, config_path, meta):
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(_write_plan(tmp_path / "p.toml", meta=meta)))
        assert code == apply.EXIT_ERROR, err
        assert not config_path.exists()
        assert not (config_path.parent / "managed.json").exists()


class TestBackups:
    def _apply(self, monkeypatch, capsys, tmp_path, config_path, poll: int, *argv: str) -> int:
        plan = _write_plan(tmp_path / f"p{poll}.toml", dict(PLAN_CONFIG, scheduler={"poll_interval": poll}))
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), *argv)
        return code

    def _backups(self, config_path: Path) -> list[Path]:
        return sorted(config_path.parent.glob("config.toml.bak.*"))

    def test_the_previous_file_is_kept_private_and_only_the_newest_n(self, monkeypatch, capsys, tmp_path, config_path):
        for poll in (1, 2, 3, 4):
            assert self._apply(monkeypatch, capsys, tmp_path, config_path, poll, "--backup", "2") == 2

        backups = self._backups(config_path)
        assert len(backups) == 2
        assert [tomllib.loads(b.read_text())["scheduler"]["poll_interval"] for b in backups] == [2, 3]
        assert all(b.stat().st_mode & 0o777 == 0o600 for b in backups)

    def test_no_backup_without_a_change_or_a_file(self, monkeypatch, capsys, tmp_path, config_path):
        self._apply(monkeypatch, capsys, tmp_path, config_path, 1, "--backup", "3")
        self._apply(monkeypatch, capsys, tmp_path, config_path, 1, "--backup", "3")
        assert self._backups(config_path) == []

    def test_dry_run_makes_no_backup(self, monkeypatch, capsys, tmp_path, config_path):
        self._apply(monkeypatch, capsys, tmp_path, config_path, 1)
        self._apply(monkeypatch, capsys, tmp_path, config_path, 2, "--backup", "3", "--dry-run")
        assert self._backups(config_path) == []

    def test_a_backup_count_below_one_is_refused(self, monkeypatch, capsys, config_path):
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(config_path), "apply", "-f", "x", "--backup", "0"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "argument --backup" in capsys.readouterr().err
        assert not os.listdir(config_path.parent)
