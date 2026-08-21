"""The smoke tier's own wiring, held down without a Docker daemon.

`tests/smoke/` needs Docker and is deselected by default, so everything it
depends on that *can* be checked cheaply is checked here instead — the same
split, and for the same reason, as `tests/test_image_tier.py` one tier below.

The parts worth guarding are the ones whose failure mode is silence: a marker
that stops deselecting (so `uv run pytest` starts building images), a
`wait_ready` that never returns for a service with no health check, a compose
`ps` parser that reads every state as "not started yet", and a probe that builds
a WHERE clause ignoring its filters and therefore matches every row.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from .support import compose as compose_support
from .support.probe import Probe

REPO = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO / "docker" / "docker-compose.test.yml"

# Deliberately not `os.environ`: these checks are about what rides in the
# argument list, and an inherited environment would satisfy them either way.
_MINIMAL_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


class TestTheMarkerIsWired:
    def test_the_smoke_marker_is_registered_and_deselected_by_default(self):
        body = (REPO / "pyproject.toml").read_text()

        assert '"smoke:' in body, "the smoke marker is not registered"
        assert "not smoke" in body, (
            "smoke is not in the default deselection, so `uv run pytest` would "
            "try to build and start a compose stack"
        )

    def test_the_default_run_collects_nothing_from_the_smoke_directory(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--collect-only",
                "-q",
                "tests/smoke/",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 5, f"{result.stdout}\n{result.stderr}"
        assert "deselected" in result.stdout, result.stdout

    def test_every_smoke_test_carries_the_marker(self):
        # A file added without `pytestmark` would run in the default suite and
        # hang on a Docker build. The marker is applied at module level, so this
        # is a check that the module-level line is present in each file.
        files = sorted((REPO / "tests" / "smoke").glob("test_*.py"))
        assert files, "no smoke tests found; this guard would pass vacuously"
        for path in files:
            assert "pytestmark = pytest.mark.smoke" in path.read_text(), path


class TestTheComposeFileIsAddressable:
    """`docker compose config` is the parser, not a YAML load — it applies the
    interpolation and schema rules the real invocation will."""

    def _config(self, args: list[str], *, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            args + ["config", "--services"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_the_compose_file_is_valid_and_names_one_service(self, tmp_path):
        if not compose_support.docker_available():
            pytest.skip("no Docker daemon available")
        env_file = tmp_path / "compose.env"
        env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n")
        args = compose_support.compose_args(
            COMPOSE_FILE, project="cfg-check", env_file=env_file
        )

        result = self._config(args, env=_MINIMAL_ENV)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert result.stdout.split() == ["istota"], result.stdout

    def test_the_env_file_alone_satisfies_interpolation(self, tmp_path):
        """The bug this tier actually shipped once, pinned.

        Compose interpolates the file on *every* subcommand, not just `up`. The
        first harness exported `ISTOTA_TEST_CONFIG_DIR` into the environment of
        `up` only, so `ps`, `exec`, `logs` and `down` all failed before touching
        a container: `wait_ready` read "no container yet" until it timed out,
        and `down` — which deliberately swallows failures — left the stack and
        its named volume behind. Two leaked stacks before it was noticed.

        Running `config` with a *minimal* environment is what makes this
        non-vacuous. With the ambient environment inherited it would pass
        whether or not the variable rides in the argument list, which is the
        exact distinction that broke.
        """
        if not compose_support.docker_available():
            pytest.skip("no Docker daemon available")
        env_file = tmp_path / "compose.env"
        env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n")

        from_args = compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=env_file
        )
        without = compose_support.compose_args(COMPOSE_FILE, project="p")

        assert self._config(from_args, env=_MINIMAL_ENV).returncode == 0
        # And the control: no env file, no ambient variable, so it must fail.
        # Without this half the assertion above proves nothing about where the
        # value came from.
        assert self._config(without, env=_MINIMAL_ENV).returncode != 0

    def test_the_config_dir_is_required_rather_than_defaulted(self):
        # `:?` not `:-`. A default would silently mount some other directory and
        # the daemon would boot with no config, which surfaces as a health-check
        # timeout rather than as "the harness forgot to supply this".
        body = COMPOSE_FILE.read_text()

        assert "${ISTOTA_TEST_CONFIG_DIR:?" in body, body[:400]


class TestTheStackStartsTheWayTheDeploymentDoes:
    def test_the_schema_is_created_before_the_scheduler_runs(self):
        """`init` is not optional, and nothing else does it.

        `db.init_db` is reached from the `init` subcommand alone — the scheduler
        opens the DB without creating a schema. The shipped entrypoint runs
        `istota … init` before exec'ing the scheduler; a lean stack that skips
        it comes up and then logs "no such table: tasks" on every tick, forever.
        Measured, before this was fixed.
        """
        body = COMPOSE_FILE.read_text()

        assert "init &&" in body, "the lean stack does not run `istota init`"
        assert "istota-scheduler" in body

    def test_the_health_check_asks_for_the_schema_not_the_file(self):
        """A file check is satisfied before `init` has run.

        Anything that opens the DB creates the file, so `test -f` reports
        healthy on a daemon that cannot dispatch. The health check has to name
        a table.
        """
        body = COMPOSE_FILE.read_text()

        assert "test -f /data/db/istota.db" not in body, (
            "the health check is back to a bare file test, which passes on a "
            "container with no schema"
        )
        assert "sqlite_master" in body and "'tasks'" in body, body


class TestComposeArgs:
    def test_the_project_name_is_always_present(self):
        args = compose_support.compose_args(COMPOSE_FILE, project="p")

        assert "--project-name" in args and "p" in args
        assert str(COMPOSE_FILE) in args

    def test_the_env_file_is_omitted_when_absent(self):
        # Passing `--env-file` with an empty value makes compose fail with a
        # confusing "no such file" rather than falling back to no env file.
        assert "--env-file" not in compose_support.compose_args(
            COMPOSE_FILE, project="p"
        )
        assert "--env-file" in compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=Path("/tmp/x.env")
        )


class TestServiceStateParsing:
    """`compose ps --format json` changed shape between compose versions."""

    def _with_ps_output(self, monkeypatch, stdout: str):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout, "")

        monkeypatch.setattr(compose_support.subprocess, "run", fake_run)

    def test_a_json_array_is_understood(self, monkeypatch):
        self._with_ps_output(
            monkeypatch,
            json.dumps([{"Service": "istota", "State": "running", "Health": "healthy"}]),
        )

        assert compose_support._service_state([], "istota") == ("running", "healthy")

    def test_one_object_per_line_is_understood(self, monkeypatch):
        self._with_ps_output(
            monkeypatch,
            json.dumps({"Service": "istota", "State": "running", "Health": ""}),
        )

        assert compose_support._service_state([], "istota") == ("running", "")

    def test_no_container_reads_as_empty_not_as_a_crash(self, monkeypatch):
        self._with_ps_output(monkeypatch, "")

        assert compose_support._service_state([], "istota") == ("", "")


class TestWaitReady:
    def test_running_without_a_health_check_is_ready(self, monkeypatch):
        """The case that would otherwise hang for the full timeout.

        A service declaring no `healthcheck` never reports a health status, so
        waiting for "healthy" waits forever on a stack that came up correctly.
        """
        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service: ("running", "")
        )

        compose_support.wait_ready([], "istota", timeout=5)

    def test_running_but_unhealthy_is_not_ready(self, monkeypatch):
        # The inverse, and the reason the two cases cannot be collapsed into
        # "state == running": a container with a health check that is still
        # starting is `running` and not yet usable.
        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service: ("running", "starting")
        )
        monkeypatch.setattr(compose_support, "logs", lambda *a, **k: "(logs)")

        with pytest.raises(TimeoutError):
            compose_support.wait_ready([], "istota", timeout=1)

    def test_the_timeout_message_carries_the_service_logs(self, monkeypatch):
        """A bare timeout says nothing about why the service did not start.

        By the time the caller could look, teardown has removed the container,
        so the logs have to be captured into the exception at the moment it is
        raised.
        """
        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service: ("exited", "")
        )
        monkeypatch.setattr(
            compose_support, "logs", lambda *a, **k: "Traceback: config is malformed"
        )

        with pytest.raises(TimeoutError, match="config is malformed"):
            compose_support.wait_ready([], "istota", timeout=5)

    def test_an_exited_service_fails_fast_rather_than_waiting_out_the_timeout(
        self, monkeypatch
    ):
        import time

        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service: ("exited", "")
        )
        monkeypatch.setattr(compose_support, "logs", lambda *a, **k: "")

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            compose_support.wait_ready([], "istota", timeout=30)

        assert time.monotonic() - started < 5, "waited out the timeout on a dead service"


@pytest.fixture
def framework_db(tmp_path) -> Path:
    """A real SQLite file with the real schema, for the local probe path."""
    path = tmp_path / "istota.db"
    connection = sqlite3.connect(path)
    connection.executescript((REPO / "schema.sql").read_text())
    connection.executemany(
        "INSERT INTO tasks (source_type, user_id, prompt, status) VALUES (?, ?, ?, ?)",
        [
            ("cli", "alice", "first", "completed"),
            ("cli", "bob", "second", "pending"),
            ("talk", "alice", "third", "failed"),
        ],
    )
    connection.commit()
    connection.close()
    return path


class TestProbe:
    def test_it_refuses_to_be_built_with_neither_access_mode(self):
        # Silently defaulting to one of them would produce a probe that queries
        # the wrong database and reports "no rows" rather than a setup error.
        with pytest.raises(ValueError):
            Probe()

    def test_filters_are_applied_and_not_ignored(self, framework_db):
        probe = Probe(local=framework_db)

        assert len(probe.tasks()) == 3
        assert [t["prompt"] for t in probe.tasks(user_id="alice")] == ["first", "third"]
        assert [t["prompt"] for t in probe.tasks(status="pending")] == ["second"]
        assert [t["prompt"] for t in probe.tasks(source_type="talk")] == ["third"]

    def test_a_task_id_narrows_to_exactly_one_row(self, framework_db):
        """The filter the smoke tier actually needs.

        `user_id` alone is not selective enough against a running daemon: the
        scheduler queues its own work for the same user at startup — a feeds
        poll, a sleep cycle — so a wait filtered on the user returns whichever
        task finished first. The smoke tests came back asserting against a
        `source_type='scheduled'` row before this existed.
        """
        probe = Probe(local=framework_db)

        assert [t["prompt"] for t in probe.tasks(task_id=2)] == ["second"]

    def test_wait_for_task_honours_a_task_id(self, framework_db):
        # Task 1 is completed and task 2 is pending. Without the id filter the
        # wait would return task 1 immediately; with it, task 2 must time out.
        probe = Probe(local=framework_db)

        assert probe.wait_for_task(status="completed", task_id=1, timeout=5)["id"] == 1
        with pytest.raises(TimeoutError):
            probe.wait_for_task(status="completed", task_id=2, timeout=1)

    def test_filters_combine_rather_than_replace_each_other(self, framework_db):
        probe = Probe(local=framework_db)

        assert [
            t["prompt"] for t in probe.tasks(user_id="alice", source_type="cli")
        ] == ["first"]

    def test_the_filter_value_is_a_parameter_not_interpolated(self, framework_db):
        # A quote in a filter value would end the string and change the query.
        # Nothing here is attacker-controlled, but a probe that broke on a
        # value like that would break confusingly and far from the cause.
        probe = Probe(local=framework_db)

        assert probe.tasks(user_id="o'brien") == []

    def test_wait_for_task_returns_a_task_that_already_reached_the_status(
        self, framework_db
    ):
        probe = Probe(local=framework_db)

        task = probe.wait_for_task(status="completed", user_id="alice", timeout=5)

        assert task["prompt"] == "first"

    def test_wait_for_task_returns_a_failure_instead_of_waiting_it_out(
        self, framework_db
    ):
        """Waiting for `completed` on a task that already failed.

        Spending the whole timeout and then reporting "nothing reached
        completed" throws away the one thing worth knowing — that it failed, and
        with what error. The terminal row comes back and the caller's own
        assertion on `status` is what fails.
        """
        probe = Probe(local=framework_db)

        task = probe.wait_for_task(status="completed", user_id="alice", source_type="talk")

        assert task["status"] == "failed"

    def test_wait_for_task_times_out_when_nothing_is_terminal(self, framework_db):
        probe = Probe(local=framework_db)

        with pytest.raises(TimeoutError, match="pending"):
            probe.wait_for_task(status="completed", user_id="bob", timeout=1)

    def test_task_logs_are_scoped_to_one_task(self, framework_db):
        connection = sqlite3.connect(framework_db)
        connection.executemany(
            "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
            [(1, "info", "for one"), (2, "info", "for two")],
        )
        connection.commit()
        connection.close()

        assert [row["message"] for row in Probe(local=framework_db).task_logs(1)] == [
            "for one"
        ]

    def test_the_local_reader_opens_the_database_read_only(self, framework_db):
        """The daemon is writing this file while the probe reads it.

        A probe that took a write lock could stall the thing it is observing,
        and the resulting failure would appear somewhere else entirely.
        """
        probe = Probe(local=framework_db)

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            probe.query("DELETE FROM tasks")
