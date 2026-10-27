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
    def test_execution_error_json_is_one_safe_result(self, monkeypatch, capsys, tmp_path, config_path):
        def fail(*args):
            raise OSError("private credential value")

        monkeypatch.setattr(apply, "write_config_with_backup", fail)
        plan = _write_plan(tmp_path / "p.toml")
        code, out, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--format", "json")
        result = json.loads(out)
        assert code == 1 and result["errors"][0]["code"] == "execution_failed"
        assert result["operations"][0]["status"] == "failed"
        assert not result["changed"] and "private credential value" not in out + err

    def test_json_config_diff_redacts_nested_credentials(self, monkeypatch, capsys, tmp_path, config_path):
        config = {"brain": {"native": {"extra_headers": {"x.tenant": "private-header-value"}}}}
        plan = _write_plan(tmp_path / "p.toml", config)
        code, out, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--format", "json", "--dry-run")
        assert code == 2 and json.loads(out)["dry_run"]
        assert "private-header-value" not in out + err
        assert not config_path.exists()

    def test_text_plan_is_printed_before_writing_and_rechecks_config(self, monkeypatch, tmp_path, config_path):
        from istota.settings_plan import engine

        plan = _write_plan(tmp_path / "p.toml")
        original = engine.execute_plan
        output = []

        def interleave(operations, context):
            assert output and not config_path.exists()
            config_path.write_text('bot_name = "External edit"\n')
            return original(operations, context)

        monkeypatch.setattr(engine, "execute_plan", interleave)
        errors = []
        code = apply.run(plan, config_path, dry_run=False, out=output.append, err=errors.append)
        assert code == 1 and any("state_changed" in line for line in errors)
        assert config_path.read_text() == 'bot_name = "External edit"\n'

    def test_execution_json_reports_config_change_then_no_drift(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "p.toml")
        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--format", "json")
        result = json.loads(out)
        assert code == 2 and result["changed"] and result["restart_required"]
        assert result["operations"][0]["status"] == "applied"
        assert result["counts"] == {"planned": 1, "applied": 1, "failed": 0, "skipped": 0}
        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--format", "json", "--dry-run")
        assert code == 0
        assert json.loads(out)["operations"] == []

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
    def test_check_rejects_execution_format(self, monkeypatch, capsys):
        code, out, err = self._check(monkeypatch, capsys, "[config]\n", "--format", "json")
        assert code == 1 and "--json" in err
        assert not out

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


class TestModelFields:
    """A model field naming a provider no config has is refused (`model`, in
    `BLOCKING_KINDS`); anything else that does not resolve is `model_unknown`,
    a warning, so no config written before providers existed is refused."""

    @staticmethod
    def _config(**extra) -> dict:
        return {**PLAN_CONFIG, **extra}

    def _kinds(self, result) -> set[tuple[str, tuple]]:
        return {(p["kind"], tuple(p["path"])) for p in result.problems}

    def test_an_unconfigured_provider_prefix_is_a_model_problem(self):
        result = apply.collect(self._config(brain={"claude_code": {"model": "nosuch/claude-x"}}))
        assert ("model", ("brain", "claude_code", "model")) in self._kinds(result)
        assert result.refused

    def test_apply_refuses_an_unconfigured_provider_and_writes_nothing(
        self, monkeypatch, capsys, tmp_path, config_path,
    ):
        config_path.write_text('bot_name = "Before"\n')
        before = config_path.read_bytes()
        plan = _write_plan(tmp_path / "p.toml", self._config(brain={"claude_code": {"model": "nosuch/claude-x"}}))
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        assert code == apply.EXIT_ERROR, err
        assert config_path.read_bytes() == before

    def test_check_json_answers_1_for_a_model_problem(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"config": self._config(brain={"claude_code": {"advisor_model": "nosuch/claude-x"}})})))
        monkeypatch.delenv("ISTOTA_CONFIG_PATH", raising=False)
        monkeypatch.setattr("sys.argv", ["istota", "apply", "--check", "-f", "-", "--json"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        kinds = [p["kind"] for p in json.loads(capsys.readouterr().out)["problems"]]
        assert "model" in kinds

    @pytest.mark.parametrize("extra", [
        {"brain": {"claude_code": {"model": "anthropic/claude-nope-9"}}},
        {"brain": {"claude_code": {"model": "claude-opus"}}},
        {"brain": {"claude_code": {"advisor_model": "opus:high"}}},
    ], ids=["unknown_ref", "pattern", "effort_on_effortless_field"])
    def test_other_unresolvable_values_warn_and_do_not_refuse(self, extra):
        result = apply.collect(self._config(**extra))
        kinds = {kind for kind, _ in self._kinds(result)}
        assert "model_unknown" in kinds
        assert "model" not in kinds
        assert not result.refused

    @pytest.mark.parametrize("extra", [
        {"brain": {"claude_code": {"model": "sonnet"}}},
        {"brain": {"claude_code": {"model": "smart:high"}}},
        {"brain": {"claude_code": {"model": "claude-sonnet-5-5"}}},
        {"brain": {"claude_code": {"model": "anthropic/claude-sonnet-5-5:low"}}},
        {"developer": {"review": {"model": "general:medium"}}},
    ], ids=["shortcut", "tier_effort", "bare_id", "ref", "review_alias_effort"])
    def test_pre_provider_shapes_and_refs_raise_no_model_problem(self, extra):
        result = apply.collect(self._config(**extra))
        assert not [p for p in result.problems if p["kind"].startswith("model")], result.problems
        assert not result.refused

    def test_an_openrouter_id_on_native_is_never_refused(self):
        """OpenRouter ids carry a `/` whose head is a vendor, not a provider,
        so the unconfigured-prefix rule cannot apply to a native field."""
        result = apply.collect(self._config(brain={"native": {"provider": "openrouter", "model": "z-ai/glm-5.3-flash"}}))
        assert not result.refused, result.problems

    def test_check_result_refuses_by_kind(self):
        def result(kind):
            return apply.CheckResult(problems=[{"kind": kind, "path": [], "message": ""}])

        assert result("model").refused
        assert result("discarded").refused
        assert apply.CheckResult(error="boom").refused
        assert not result("model_unknown").refused
        assert not result("warning").refused

    def test_check_and_apply_read_one_blocking_set(self):
        from istota.config_editor import check

        assert check.BLOCKING_KINDS is apply.BLOCKING_KINDS
        assert {"discarded", "error", "model"} <= apply.BLOCKING_KINDS
        assert "model_unknown" not in apply.BLOCKING_KINDS


class TestTheManagementMarker:
    def test_snapshot_preserves_management_marker(self, monkeypatch, capsys, tmp_path, config_path):
        plan = _write_plan(tmp_path / "p.toml", meta={"managed": True, "source": "ansible"})
        _invoke(monkeypatch, capsys, config_path, "-f", str(plan))
        marker = config_path.parent / "managed.json"
        before = marker.read_bytes()
        snapshot = _write_plan(tmp_path / "snapshot.toml", meta={"source": "snapshot"})
        code, _, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(snapshot))
        assert code == 0 and marker.read_bytes() == before

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
    def test_retention_failure_reports_the_config_already_written(self, monkeypatch, capsys, tmp_path, config_path):
        config_path.write_text('bot_name = "Before"\n')

        def fail(*args):
            raise OSError("private path")

        monkeypatch.setattr(apply, "_keep_newest_backups", fail)
        plan = _write_plan(tmp_path / "p.toml")
        code, out, _ = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--backup", "2", "--format", "json")
        result = json.loads(out)
        assert code == 1 and result["changed"] and result["partial"] and result["restart_required"]
        assert result["operations"][0]["status"] == "applied"
        assert tomllib.loads(config_path.read_text()) == PLAN_CONFIG

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


class TestBackupsOfAMovedCredential:
    """ISSUE-769: once a credential's secret file holds a value, the file wins
    and an inline copy is never read, so no backup of config.toml keeps one."""

    VALUE = "s3cret-moved-value"
    BACKUP_NAME = "config.toml.bak.20261017T000000.000000Z"

    def _config(self, poll: int, *, password: str | None) -> dict:
        nextcloud = {"url": "https://cloud.example.com"}
        if password is not None:
            nextcloud["app_password"] = password
        return dict(
            PLAN_CONFIG, workspace_path="/mnt/shared",
            scheduler={"poll_interval": poll}, nextcloud=nextcloud,
        )

    def _apply(self, monkeypatch, capsys, tmp_path, config_path, document) -> None:
        plan = _write_plan(tmp_path / f"p{len(os.listdir(tmp_path))}.toml", document)
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--backup", "10")
        assert code == 2, err

    def _backups(self, config_path: Path) -> list[Path]:
        return sorted(config_path.parent.glob("config.toml.bak.*"))

    def test_a_move_scrubs_every_backup_holding_the_value(self, monkeypatch, capsys, tmp_path, config_path):
        monkeypatch.delenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", raising=False)
        for poll in (1, 2):
            self._apply(monkeypatch, capsys, tmp_path, config_path, self._config(poll, password=self.VALUE))
        assert any(self.VALUE in b.read_text() for b in self._backups(config_path))

        monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", self.VALUE)
        self._apply(monkeypatch, capsys, tmp_path, config_path, self._config(3, password=None))

        backups = self._backups(config_path)
        assert len(backups) == 2
        for backup in backups:
            assert self.VALUE not in backup.read_text()
            assert backup.stat().st_mode & 0o777 == 0o600
        documents = [tomllib.loads(b.read_text()) for b in backups]
        assert [d["scheduler"]["poll_interval"] for d in documents] == [1, 2]
        assert [d["nextcloud"] for d in documents] == [{"url": "https://cloud.example.com"}] * 2

    def test_a_backup_keeps_the_value_while_its_file_is_empty(self, monkeypatch, capsys, tmp_path, config_path):
        """The inline copy is the one in use then, and a rollback needs it."""
        monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", " ")
        for poll in (1, 2):
            self._apply(monkeypatch, capsys, tmp_path, config_path, self._config(poll, password=self.VALUE))

        (backup,) = self._backups(config_path)
        assert tomllib.loads(backup.read_text())["nextcloud"]["app_password"] == self.VALUE

    def test_a_credential_with_no_secret_file_is_left_alone(self, tmp_path):
        copy = tmp_path / self.BACKUP_NAME
        text = '[sms.twilio]\naccount_sid = "AC0000"\n'
        copy.write_text(text)
        environ = {"ISTOTA_SMS_TWILIO_ACCOUNT_SID": "AC0000"}
        assert apply.scrub_moved_credentials(copy, environ) is False
        assert copy.read_text() == text

    def test_headers_stay_while_their_variable_does_not_parse(self, tmp_path):
        """The loader keeps the inline table then, so the copy needs it."""
        copy = tmp_path / self.BACKUP_NAME
        copy.write_text('[brain.native.extra_headers]\nX-Org = "abc"\n')
        assert apply.scrub_moved_credentials(copy, {"ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS": "not json"}) is False
        assert apply.scrub_moved_credentials(copy, {"ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS": '{"X-Org": "abc"}'})
        assert "abc" not in copy.read_text()

    def test_the_pre_localize_copy_is_scrubbed_and_kept(self, monkeypatch, capsys, tmp_path, config_path):
        pre_localize = config_path.with_name("config.toml.pre-localize")
        pre_localize.write_text(f'[nextcloud]\napp_password = "{self.VALUE}"\n')
        monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD", self.VALUE)
        self._apply(monkeypatch, capsys, tmp_path, config_path, self._config(1, password=None))
        self._apply(monkeypatch, capsys, tmp_path, config_path, self._config(2, password=None))

        assert self.VALUE not in pre_localize.read_text()
        assert len(self._backups(config_path)) == 1

    def test_a_copy_that_cannot_be_rewritten_does_not_fail_the_apply(
        self, monkeypatch, capsys, tmp_path, config_path,
    ):
        def fail(copy, environ=None):
            raise PermissionError(13, "Permission denied")

        self._apply(monkeypatch, capsys, tmp_path, config_path, self._config(1, password=None))
        monkeypatch.setattr(apply, "scrub_moved_credentials", fail)
        plan = _write_plan(tmp_path / "late.toml", self._config(2, password=None))
        code, _, err = _invoke(monkeypatch, capsys, config_path, "-f", str(plan), "--backup", "10")

        assert code == 2
        assert "could not remove moved credentials" in err
        assert tomllib.loads(config_path.read_text())["scheduler"]["poll_interval"] == 2

    def test_a_copy_that_does_not_parse_is_left_alone(self, tmp_path):
        copy = tmp_path / self.BACKUP_NAME
        copy.write_text("not = [toml\n")
        assert apply.scrub_moved_credentials(copy, {"ISTOTA_NEXTCLOUD_APP_PASSWORD": "x"}) is False
        assert copy.read_text() == "not = [toml\n"


class TestReadingTheMarker:
    def test_a_missing_file_is_nothing_managed(self, tmp_path):
        assert apply.read_managed(tmp_path) == {}
        assert apply.read_managed(tmp_path, fail_closed=True) == {}

    @pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"\xff"])
    def test_a_damaged_file_fails_closed_when_asked(self, tmp_path, raw):
        """The writer repairs a damaged marker by reading it as empty; the
        editor's view must not read it as nothing managed."""
        (tmp_path / apply.MANAGED_FILE).write_bytes(raw)
        assert apply.read_managed(tmp_path) == {}
        damaged = apply.read_managed(tmp_path, fail_closed=True)
        assert set(damaged) == set(apply.MANAGED_RESOURCES)
        assert all(entry == {"managed": True, "source": "unreadable"} for entry in damaged.values())
