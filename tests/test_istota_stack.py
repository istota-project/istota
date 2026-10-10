"""`host/istota-stack`: release integrity (parity row 13) and the stack verbs.

`istota-stack update <tag>` is the only way new code reaches a VM, so what it
refuses is the boundary: a branch name, a lightweight tag, an unsigned tag, a
tag signed by any key but the one in `allowed_signers`, and a tag ref pointing
at another tag's object. Each refusal must leave `.env`, the checkout and the
running images as they were, which here means `.env`'s bytes, the checkout's
HEAD and no `build` or `up` reaching Docker.

The repository is built in the test with a signing key `ssh-keygen` makes on
the spot (`gpg.format=ssh`, an `allowed_signers` file); nothing reads the
user's keys or git configuration. `docker` is a stub on PATH that records its
argv and answers the few questions the script asks.

The negative controls are `scripts/test-stack-negative-control.sh`: verification
skipped, and verification against the user's own git configuration instead of
`allowed_signers`, each of which must turn named node ids here red.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support import parity

REPO = Path(__file__).resolve().parent.parent
#: The script under test. The negative-control driver points this at a broken copy.
SCRIPT = Path(os.environ.get("ISTOTA_STACK_SCRIPT") or REPO / "host" / "istota-stack")

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="needs ssh-keygen and git",
)

DOCKER_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_CALLS"
args=" $* "
case "$args" in
  *" config --services "*) printf 'istota\nweb\nwebhooks\nnginx\n' ;;
  *" build "*) exit "${STUB_BUILD_RC:-0}" ;;
  *" devbox compose-file"*) echo '{"services": {}}' ;;
  *" --check-schema"*) exit "${STUB_CHECK_RC:-0}" ;;
  *" istota apply "*) exit "${STUB_APPLY_RC:-0}" ;;
  *" image ls "*) printf 'istota-istota:v0\nistota-istota:v1\nistota-istota:old\nnginx:alpine\n' ;;
esac
exit 0
"""


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        env=env, check=True,
    )
    return result.stdout.strip()


@dataclass
class Fixture:
    root: Path
    origin: Path
    stack: Path
    calls: Path
    env: dict
    release_key: Path
    other_key: Path

    @property
    def src(self) -> Path:
        return self.stack / "src"

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True,
            env={**self.env, **extra_env}, timeout=60,
        )

    def tag(self, name: str, *, key: Path | None = None, annotated: bool = True) -> None:
        if not annotated:
            _git(self.origin, "tag", name, env=self.env)
        elif key is None:
            _git(self.origin, "tag", "-a", name, "-m", f"release {name}", env=self.env)
        else:
            _git(
                self.origin, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}.pub",
                "tag", "-s", name, "-m", f"release {name}", env=self.env,
            )

    def commit(self, message: str) -> str:
        (self.origin / "file.txt").write_text(message)
        _git(self.origin, "add", "file.txt", env=self.env)
        _git(self.origin, "commit", "-q", "-m", message, env=self.env)
        return _git(self.origin, "rev-parse", "HEAD", env=self.env)

    def head(self) -> str:
        return _git(self.src, "rev-parse", "HEAD", env=self.env)

    def env_text(self) -> str:
        return (self.stack / ".env").read_text()

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


def _keygen(path: Path) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)],
        check=True, capture_output=True,
    )
    return path


@pytest.fixture
def fx(tmp_path) -> Fixture:
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(DOCKER_STUB)
    (bindir / "docker").chmod(0o755)
    calls = tmp_path / "docker-calls"
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Release", "GIT_AUTHOR_EMAIL": "releases@example.com",
        "GIT_COMMITTER_NAME": "Release", "GIT_COMMITTER_EMAIL": "releases@example.com",
        "STUB_CALLS": str(calls),
        "LANG": "C.UTF-8",
    }
    release_key = _keygen(tmp_path / "release_key")
    other_key = _keygen(tmp_path / "other_key")

    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main", env=env)
    fixture = Fixture(tmp_path, origin, tmp_path / "srv", calls, env, release_key, other_key)
    fixture.commit("v0")
    fixture.tag("v0", key=release_key)

    stack = fixture.stack
    stack.mkdir()
    subprocess.run(["git", "clone", "-q", str(origin), str(stack / "src")], check=True, env=env)
    (stack / "src" / "docker").mkdir()
    pubkey = (release_key.with_suffix(".pub")).read_text().strip()
    (stack / "allowed_signers").write_text(f'releases@example.com namespaces="git" {pubkey}\n')
    (stack / ".env").write_text("COMPOSE_PROFILES=\nISTOTA_TAG=v0\nISTOTA_PREVIOUS_TAG=\n")
    (stack / "host.env").write_text("INGRESS=local\nCLAUDE_CODE_VERSION=2.1.296\n")
    (stack / "config").mkdir()
    (stack / "config" / "config.toml").write_text('[brain]\nkind = "native"\n')
    _git(stack / "src", "checkout", "-q", "--detach", "v0", env=env)
    fixture.env["ISTOTA_STACK_DIR"] = str(stack)
    return fixture


def _assert_unchanged(fx: Fixture, result, env_before: str, head_before: str) -> None:
    assert result.returncode == 65, result.stdout + result.stderr
    assert "REFUSE" in result.stderr
    assert fx.env_text() == env_before, ".env changed on a refused tag"
    assert fx.head() == head_before, "the checkout moved on a refused tag"
    touched = [call for call in fx.docker_calls() if " build" in call or " up " in f"{call} "]
    assert touched == [], f"a refused tag reached Docker: {touched}"


@parity.witness(13)
class TestARefusedTagChangesNothing:
    @pytest.fixture
    def before(self, fx):
        fx.commit("v1")
        return fx.env_text(), fx.head()

    def test_an_unsigned_tag_is_refused(self, fx, before):
        fx.tag("v1")
        _assert_unchanged(fx, fx.run("update", "v1"), *before)

    def test_a_lightweight_tag_is_refused(self, fx, before):
        fx.tag("v1", annotated=False)
        _assert_unchanged(fx, fx.run("update", "v1"), *before)

    def test_a_tag_signed_by_another_key_is_refused(self, fx, before):
        fx.tag("v1", key=fx.other_key)
        _assert_unchanged(fx, fx.run("update", "v1"), *before)

    def test_a_branch_name_is_refused(self, fx, before):
        _git(fx.origin, "branch", "release", env=fx.env)
        _assert_unchanged(fx, fx.run("update", "release"), *before)

    def test_another_key_in_the_users_own_git_config_is_not_trusted(self, fx, before):
        """The release key is allowed_signers', not whatever the operator's own
        git configuration trusts."""
        home = Path(fx.env["HOME"])
        pubkey = fx.other_key.with_suffix(".pub").read_text().strip()
        (home / "allowed_signers").write_text(f"me@example.com {pubkey}\n")
        (home / ".gitconfig").write_text(
            f"[gpg]\n\tformat = ssh\n[gpg \"ssh\"]\n\tallowedSignersFile = {home / 'allowed_signers'}\n"
        )
        fx.tag("v1", key=fx.other_key)
        _assert_unchanged(fx, fx.run("update", "v1"), *before)

    def test_a_tag_ref_naming_another_releases_object_is_refused(self, fx, before):
        """A downgrade by moving refs: v9 pointed at v0's signed object."""
        fx.tag("v1", key=fx.release_key)
        v0 = _git(fx.origin, "rev-parse", "refs/tags/v0", env=fx.env)
        _git(fx.origin, "update-ref", "refs/tags/v9", v0, env=fx.env)
        _assert_unchanged(fx, fx.run("update", "v9"), *before)


class TestASignedTagIsDeployed:
    def test_it_is_checked_out_built_written_and_started(self, fx):
        commit = fx.commit("v1")
        fx.tag("v1", key=fx.release_key)

        result = fx.run("update", "v1")

        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == commit
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG=v0\n" in fx.env_text()
        calls = fx.docker_calls()
        builds = [call for call in calls if " build" in call]
        assert builds and builds[0].endswith("build istota"), builds
        assert any(call.endswith("up -d --remove-orphans") for call in calls)
        # The previous tag's images are kept for rollback, everything older goes.
        assert any(call == "image rm istota-istota:old" for call in calls)
        assert not any(call.startswith("image rm istota-istota:v") for call in calls)

    def test_a_failed_build_puts_the_checkout_back(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        env_before, head_before = fx.env_text(), fx.head()

        result = fx.run("update", "v1", STUB_BUILD_RC="1")

        assert result.returncode == 1
        assert fx.env_text() == env_before
        assert fx.head() == head_before
        assert not any(" up " in f"{call} " for call in fx.docker_calls())

    def test_native_only_builds_without_claude_code(self, fx, tmp_path):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        recorder = tmp_path / "build-env"
        stub = Path(fx.env["PATH"].split(":")[0]) / "docker"
        stub.write_text(DOCKER_STUB.replace(
            '*" build "*) exit',
            f'*" build "*) echo "$INSTALL_CLAUDE_CODE $CLAUDE_CODE_VERSION" > {recorder}; exit',
        ))
        assert fx.run("update", "v1").returncode == 0
        assert recorder.read_text().split() == ["0", "2.1.296"]


class TestRollback:
    @pytest.fixture
    def updated(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        assert fx.run("update", "v1").returncode == 0
        fx.calls.unlink()
        return fx

    def test_an_older_image_that_refuses_the_database_changes_nothing(self, updated):
        env_before, head_before = updated.env_text(), updated.head()
        result = updated.run("rollback", STUB_CHECK_RC="3")
        assert result.returncode == 3, result.stdout + result.stderr
        assert "REFUSE" in result.stderr
        assert updated.env_text() == env_before
        assert updated.head() == head_before
        assert not any(" up " in f"{call} " for call in updated.docker_calls())

    def test_it_asks_the_older_image_before_switching(self, updated):
        result = updated.run("rollback")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v0\n" in updated.env_text()
        assert "ISTOTA_PREVIOUS_TAG=v1\n" in updated.env_text()
        calls = updated.docker_calls()
        check = next(i for i, call in enumerate(calls) if "--check-schema" in call)
        up = next(i for i, call in enumerate(calls) if call.endswith("up -d --remove-orphans"))
        assert check < up
        assert "--entrypoint istota-drop istota istota init --check-schema" in calls[check]


class TestApply:
    @pytest.mark.parametrize(("rc", "flags", "restarted"), [
        ("2", [], True), ("0", [], False), ("2", ["--dry-run"], False), ("1", [], False),
    ])
    def test_the_stack_restarts_only_on_a_real_change(self, fx, tmp_path, rc, flags, restarted):
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = fx.run("apply", str(plan), *flags, STUB_APPLY_RC=rc)
        assert result.returncode == int(rc), result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any(
            "--entrypoint istota-drop" in call and call.endswith(
                f"istota istota apply -f /plan/plan.toml{''.join(' ' + f for f in flags)}")
            for call in calls
        ), calls
        assert any(call.endswith(" restart") for call in calls) is restarted


class TestExec:
    def test_every_exec_goes_through_the_drop(self, fx):
        result = fx.run("exec", "istota", "doctor")
        assert result.returncode == 0, result.stderr
        assert fx.docker_calls()[-1].endswith("exec -T istota istota-drop istota doctor")

    def test_the_vm_wrapper_is_the_exec_path(self, fx):
        wrapper = REPO / "host" / "istota"
        result = subprocess.run(
            ["sh", str(wrapper), "user", "list"], capture_output=True, text=True,
            env={**fx.env, "ISTOTA_STACK_BIN": str(SCRIPT)}, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert fx.docker_calls()[-1].endswith("exec -T istota istota-drop istota user list")
