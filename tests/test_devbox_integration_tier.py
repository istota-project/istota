"""The real-devbox tier must distinguish a skipped probe from a passing one."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INTEGRATION_TEST = REPO / "tests" / "test_skills_devbox_integration.py"
TESTING_DOC = REPO / "docs" / "development" / "testing.md"
DEVBOX_SKILL = REPO / "src" / "istota" / "skills" / "devbox" / "skill.md"
ENV_PROBE = REPO / "tests" / "support" / "devbox_user_option_probe.py"


def _run_devbox_tier(
    *extra_args: str,
    test_path: Path = INTEGRATION_TEST,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ISTOTA_USER_ID", None)
    env.update(env_updates or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            "-n0",
            str(test_path),
            *extra_args,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_requested_devbox_tier_fails_when_the_transport_is_unreachable():
    result = _run_devbox_tier("--devbox-user=issue-326-no-such-user")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "--devbox-user requested a real devbox" in output
    assert "14 skipped" not in output


def test_unrequested_devbox_tier_still_skips_when_no_devbox_is_configured():
    result = _run_devbox_tier()

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "14 skipped" in output


def test_blank_devbox_user_is_a_failure_not_an_unrequested_skip():
    result = _run_devbox_tier("--devbox-user=")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "--devbox-user requires a non-empty user id" in output
    assert "14 skipped" not in output


def test_devbox_user_option_restores_the_user_id_after_environment_scrubbing():
    user_id = "issue-326-probe-user"
    result = _run_devbox_tier(
        f"--devbox-user={user_id}",
        test_path=ENV_PROBE,
        env_updates={"ISTOTA_TEST_EXPECT_DEVBOX_USER": user_id},
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_documented_invocation_requests_a_real_devbox_run():
    command = (
        "uv run pytest -m integration tests/test_skills_devbox_integration.py "
        "-n0 --devbox-user=<user>"
    )

    assert command in INTEGRATION_TEST.read_text()
    assert command in TESTING_DOC.read_text()


def test_the_devbox_skill_warns_that_prefixed_environment_does_not_cross_shims():
    body = DEVBOX_SKILL.read_text().lower()

    assert "prefixed environment variables" in body
    assert "shimmed command" in body
