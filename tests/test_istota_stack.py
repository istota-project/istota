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
  *" config --services "*)
    printf 'daemon\nweb\nnginx\n'
    case "$args" in *compose.devbox.yml*) echo devbox-alice ;; esac ;;
  *" build "*) exit "${STUB_BUILD_RC:-0}" ;;
  *" devbox compose-file"*)
    if [ -n "${STUB_DEVBOX_COMPOSE:-}" ]; then printf '%s\n' "$STUB_DEVBOX_COMPOSE"
    else echo '{"services": {}}'; fi ;;
  *" ps -a --filter label=com.docker.compose.project=istota "*) printf '%b' "${STUB_PROJECT_CONTAINERS:-}" ;;
  *" ps -q daemon"*) [ -n "${STUB_STACK_DOWN:-}" ] || echo running-container ;;
  *" ps -q "*) [ -n "${STUB_STACK_DOWN:-}" ] || echo running-container ;;
  *" --check-schema"*) exit "${STUB_CHECK_RC:-0}" ;;
  *" istota apply "*) exit "${STUB_APPLY_RC:-0}" ;;
  *" image ls "*) printf 'istota:v0\nistota:v1\nistota:old\nistota-istota:v0\nistota-istota:older\nistota-browser:old\nnginx:alpine\n' ;;
  *" image tag "*) touch "${STUB_CALLS}.tagged" ;;
  *" image inspect istota:"*)
    if [ -n "${STUB_RENAMED_IMAGE_MISSING:-}" ] && [ ! -e "${STUB_CALLS}.tagged" ]; then exit 1; fi
    [ -z "${STUB_IMAGE_ENV:-}" ] || printf '%s\n' "$STUB_IMAGE_ENV" ;;
  *" image inspect "*) [ -z "${STUB_IMAGE_ENV:-}" ] || printf '%s\n' "$STUB_IMAGE_ENV" ;;
  *" localize-config --check "*) exit "${STUB_LOCALIZE_CHECK_RC:-0}" ;;
  *" workspace localize "*) exit "${STUB_LOCALIZE_RC:-0}" ;;
  *" workspace localize-config "*)
    printf 'config read-only=%s\n' "${ISTOTA_CONFIG_READ_ONLY:-unset}" >> "$STUB_CALLS" ;;
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
            env={**self.env, **extra_env}, timeout=60, stdin=subprocess.DEVNULL,
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
    fixture.env["ISTOTA_MOUNT_DIR"] = str(tmp_path / "mount")
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
        assert builds and builds[0].endswith("build daemon"), builds
        assert any(call.endswith("up -d --remove-orphans") for call in calls)
        # The previous tag's images are kept for rollback, everything older goes.
        removed = {call.removeprefix("image rm ") for call in calls if call.startswith("image rm ")}
        assert removed == {"istota:old", "istota-istota:older", "istota-browser:old"}, removed
        # A release before the rename built istota-istota; its image for the
        # previous tag is what a rollback to it runs.
        assert "istota-istota:v0" not in removed

    def test_a_failed_build_puts_the_checkout_back(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        env_before, head_before = fx.env_text(), fx.head()

        result = fx.run("update", "v1", STUB_BUILD_RC="1")

        assert result.returncode == 1
        assert fx.env_text() == env_before
        assert fx.head() == head_before
        assert not any(" up " in f"{call} " for call in fx.docker_calls())

    def test_a_secret_added_since_setup_exists_before_the_stack_starts(self, fx, tmp_path):
        """compose refuses to start a service whose secret file is missing, so a
        release that declares a new secret would leave every VM down."""
        compose = fx.origin / "docker" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text(
            "secrets:\n"
            "  old_one:\n    file: ${ISTOTA_SECRETS_DIR:-./secrets}/old_one\n"
            "  new_one:\n    file: ${ISTOTA_SECRETS_DIR:-./secrets}/new_one\n"
        )
        _git(fx.origin, "add", "docker/docker-compose.yml", env=fx.env)
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        secrets = fx.stack / "secrets"
        secrets.mkdir()
        (secrets / "old_one").write_text("kept")
        seen = tmp_path / "secrets-at-up"
        stub = Path(fx.env["PATH"].split(":")[0]) / "docker"
        stub.write_text(DOCKER_STUB.replace(
            'case "$args" in\n',
            f'case "$args" in\n  *" up -d "*) ls "{secrets}" > "{seen}" ;;\n',
        ))

        result = fx.run("update", "v1")

        assert result.returncode == 0, result.stdout + result.stderr
        assert seen.read_text().split() == ["new_one", "old_one"]
        assert (secrets / "new_one").read_text() == ""
        assert (secrets / "old_one").read_text() == "kept"

    def test_a_link_planted_at_a_new_secret_is_replaced_not_followed(self, fx, tmp_path):
        """secrets/ is uid 10001's, so the missing-file pass goes through
        put-files rather than root's own install."""
        compose = fx.origin / "docker" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("secrets:\n  new_one:\n    file: ${ISTOTA_SECRETS_DIR:-./secrets}/new_one\n")
        _git(fx.origin, "add", "docker/docker-compose.yml", env=fx.env)
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        secrets = fx.stack / "secrets"
        secrets.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (secrets / "new_one").symlink_to(elsewhere)

        result = fx.run("update", "v1")

        assert result.returncode == 0, result.stdout + result.stderr
        assert not (secrets / "new_one").is_symlink()
        assert (secrets / "new_one").read_text() == ""
        assert list(elsewhere.iterdir()) == []

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


PROVISION_STUB = """#!/bin/bash
if [ "${1:-}" = "--digest" ]; then echo "digest-$(git -C "$(dirname "$0")/.." rev-parse HEAD)"; exit 0; fi
echo "provision ran" >> "$STUB_CALLS"
exit "${STUB_PROVISION_RC:-0}"
"""


class TestAStaleDevboxFile:
    """compose.devbox.yml is rendered by the release that wrote it, so on an
    update it can name what the new compose file no longer has (the service
    was `istota` before the rename) and compose refuses the merged project.
    The core images and the render run without it; the devbox images are built
    from the fresh one."""

    def test_the_old_devbox_file_is_kept_out_until_it_is_rendered_again(self, fx):
        (fx.stack / "compose.devbox.yml").write_text('{"services": {"istota": {"volumes": ["x:/y"]}}}\n')
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        rendered = '{"services": {"devbox-alice": {"image": "istota-devbox:v1"}}}'

        result = fx.run("update", "v1", STUB_DEVBOX_COMPOSE=rendered)

        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        render = next(i for i, c in enumerate(calls) if "devbox compose-file" in c)
        core = next(i for i, c in enumerate(calls) if " build " in f"{c} " and "devbox-alice" not in c)
        assert "compose.devbox.yml" not in calls[core], calls[core]
        assert "compose.devbox.yml" not in calls[render], calls[render]
        devbox = [c for c in calls[render:] if " build " in f"{c} " and "devbox-alice" in c]
        assert devbox and "compose.devbox.yml" in devbox[0], calls
        assert rendered in (fx.stack / "compose.devbox.yml").read_text()


class TestAnUpdateReprovisionsTheHost:
    """`provision.sh` installs istota-stack, the units and the host scripts, so
    an update that never ran it left them at whatever tag last provisioned."""

    @pytest.fixture
    def with_provision(self, fx):
        script = fx.origin / "host" / "provision.sh"
        script.parent.mkdir(parents=True)
        script.write_text(PROVISION_STUB)
        script.chmod(0o755)
        _git(fx.origin, "add", "host/provision.sh", env=fx.env)
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        return fx

    def test_a_release_whose_host_inputs_changed_is_provisioned(self, with_provision):
        fx = with_provision
        (fx.stack / ".provisioned").write_text("digest-of-the-previous-release\n")
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "provision ran" in fx.docker_calls()
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    def test_unchanged_inputs_are_not_provisioned_again(self, with_provision):
        fx = with_provision
        head = _git(fx.origin, "rev-parse", "v1^{commit}", env=fx.env)
        (fx.stack / ".provisioned").write_text(f"digest-{head}\n")
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "provision ran" not in fx.docker_calls()

    def test_a_failed_provision_fails_the_update_with_the_stack_on_the_tag(self, with_provision):
        fx = with_provision
        result = fx.run("update", "v1", STUB_PROVISION_RC="1")
        assert result.returncode == 1
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "provision.sh" in result.stdout + result.stderr

    def test_a_release_without_provision_sh_updates_as_before(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr


class TestARollbackReprovisionsTheHost:
    """ISSUE-710: a rollback left the host's scripts and units at the newer ref
    while the stack ran the older one. It now applies update's rule to the
    checkout it returns to."""

    @pytest.fixture
    def rolled_forward(self, fx):
        script = fx.origin / "host" / "provision.sh"
        script.parent.mkdir(parents=True)
        script.write_text(PROVISION_STUB)
        script.chmod(0o755)
        _git(fx.origin, "add", "host/provision.sh", env=fx.env)
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        fx.commit("v2")
        fx.tag("v2", key=fx.release_key)
        assert fx.run("update", "v1").returncode == 0
        assert fx.run("update", "v2").returncode == 0
        v2 = _git(fx.origin, "rev-parse", "v2^{commit}", env=fx.env)
        (fx.stack / ".provisioned").write_text(f"digest-{v2}\n")
        fx.calls.unlink()
        return fx

    def test_the_older_checkouts_provision_runs_after_the_switch(self, rolled_forward):
        fx = rolled_forward
        result = fx.run("rollback")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        calls = fx.docker_calls()
        assert "provision ran" in calls, result.stdout + result.stderr
        up = next(i for i, call in enumerate(calls) if call.endswith("up -d --remove-orphans"))
        assert up < calls.index("provision ran")

    def test_unchanged_inputs_are_not_provisioned_again(self, rolled_forward):
        """A record naming v1 means a v1 provision finished: provision.sh drops
        the record before it acts (`test_host_units.py`)."""
        fx = rolled_forward
        v1 = _git(fx.origin, "rev-parse", "v1^{commit}", env=fx.env)
        (fx.stack / ".provisioned").write_text(f"digest-{v1}\n")
        result = fx.run("rollback")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "provision ran" not in fx.docker_calls()

    def test_a_failed_provision_fails_the_rollback_with_the_stack_on_the_older_tag(self, rolled_forward):
        fx = rolled_forward
        result = fx.run("rollback", STUB_PROVISION_RC="1")
        assert result.returncode == 1
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "provision.sh" in result.stdout + result.stderr

    def test_a_refused_rollback_provisions_nothing(self, rolled_forward):
        """A guard on the order: the schema refusal comes before any provision."""
        fx = rolled_forward
        result = fx.run("rollback", STUB_CHECK_RC="3")
        assert result.returncode == 3
        assert "provision ran" not in fx.docker_calls()


class TestAHardenedUmask:
    """A host whose umask is 027 (zorg's) checked the tree out root-only, and
    the image copied those modes in: uid 10001 could not import istota.cli."""

    def test_the_checkout_is_world_readable_whatever_the_callers_umask(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = subprocess.run(
            ["bash", str(SCRIPT), "update", "v1"], capture_output=True, text=True,
            env=fx.env, timeout=60, preexec_fn=lambda: os.umask(0o077),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (fx.src / "file.txt").stat().st_mode & 0o777 == 0o644

    def test_provision_sets_it_before_cloning(self):
        text = (REPO / "host" / "provision.sh").read_text()
        assert text.index("\numask 022\n") < text.index("git clone")


class TestLatestFollowsMain:
    """`update --unsigned latest` deploys the tip of main with no tag at all, so
    a deployment tracking main needs nothing pushed but the merge. It is
    recorded as main-<12 hex>, a name for one commit that stays put, so a
    rollback has something to return to."""

    def _name(self, commit: str) -> str:
        return f"main-{commit[:12]}"

    def test_latest_deploys_the_tip_of_main(self, fx):
        commit = fx.commit("next")
        result = fx.run("update", "--unsigned", "latest")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == commit
        assert f"ISTOTA_TAG={self._name(commit)}\n" in fx.env_text()
        assert "ISTOTA_TAG_UNSIGNED=1\n" in fx.env_text()
        assert any(call.endswith("up -d --remove-orphans") for call in fx.docker_calls())

    def test_a_later_merge_moves_it_and_keeps_the_last(self, fx):
        first = fx.commit("first")
        assert fx.run("update", "--unsigned", "latest").returncode == 0
        second = fx.commit("second")
        result = fx.run("update", "--unsigned", "latest")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == second
        assert f"ISTOTA_PREVIOUS_TAG={self._name(first)}\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG_UNSIGNED=1\n" in fx.env_text()

    def test_rollback_returns_to_the_previous_commit_of_main(self, fx):
        first = fx.commit("first")
        assert fx.run("update", "--unsigned", "latest").returncode == 0
        fx.commit("second")
        assert fx.run("update", "--unsigned", "latest").returncode == 0
        result = fx.run("rollback")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == first
        assert f"ISTOTA_TAG={self._name(first)}\n" in fx.env_text()

    def test_latest_tag_names_the_tip_and_changes_nothing(self, fx):
        """What the management role compares with ISTOTA_TAG before it updates,
        since an update always rebuilds."""
        commit = fx.commit("next")
        before = fx.env_text(), fx.head(), fx.docker_calls()
        result = fx.run("latest-tag")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == self._name(commit)
        assert (fx.env_text(), fx.head(), fx.docker_calls()) == before

    def test_latest_tag_is_the_name_update_installs(self, fx):
        fx.commit("next")
        named = fx.run("latest-tag").stdout.strip()
        assert fx.run("update", "--unsigned", "latest").returncode == 0
        assert f"ISTOTA_TAG={named}\n" in fx.env_text()

    def test_latest_is_never_a_signed_release(self, fx):
        fx.commit("next")
        before = fx.env_text(), fx.head()
        result = fx.run("update", "latest")
        assert result.returncode == 65
        assert "--unsigned" in result.stderr
        assert (fx.env_text(), fx.head()) == before

    def test_a_named_commit_off_main_is_refused(self, fx):
        """main-<hex> names a commit of main; it is not a way to deploy any commit."""
        fx.commit("on main")
        _git(fx.origin, "checkout", "-q", "-b", "elsewhere", env=fx.env)
        stray = fx.commit("not on main")
        _git(fx.origin, "checkout", "-q", "main", env=fx.env)
        before = fx.env_text(), fx.head()
        _assert_unchanged(fx, fx.run("update", "--unsigned", self._name(stray)), *before)


class TestAnUnsignedTagOnRequest:
    """Only releases are signed. `--unsigned` installs any other tag, for that
    one run, and .env records it so a rollback can return to it."""

    def test_an_unsigned_annotated_tag_is_deployed(self, fx):
        commit = fx.commit("v1")
        fx.tag("v1")
        result = fx.run("update", "--unsigned", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == commit
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "ISTOTA_TAG_UNSIGNED=1\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG=v0\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG_UNSIGNED=\n" in fx.env_text()
        assert "unsigned" in result.stdout + result.stderr
        assert any(call.endswith("up -d --remove-orphans") for call in fx.docker_calls())

    def test_a_lightweight_tag_is_deployed(self, fx):
        commit = fx.commit("v1")
        fx.tag("v1", annotated=False)
        result = fx.run("update", "--unsigned", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == commit

    def test_no_release_key_is_needed(self, fx):
        (fx.stack / "allowed_signers").unlink()
        commit = fx.commit("v1")
        fx.tag("v1")
        result = fx.run("update", "--unsigned", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.head() == commit

    def test_a_branch_is_still_refused(self, fx):
        fx.commit("v1")
        _git(fx.origin, "branch", "release", env=fx.env)
        before = fx.env_text(), fx.head()
        _assert_unchanged(fx, fx.run("update", "--unsigned", "release"), *before)

    def test_a_bad_name_is_still_refused(self, fx):
        before = fx.env_text(), fx.head()
        _assert_unchanged(fx, fx.run("update", "--unsigned", "../v0"), *before)

    def test_without_the_flag_the_refusal_names_it(self, fx):
        fx.commit("v1")
        fx.tag("v1")
        result = fx.run("update", "v1")
        assert result.returncode == 65
        assert "--unsigned" in result.stderr

    def test_a_signed_release_after_it_carries_the_mark_to_previous(self, fx):
        fx.commit("v1")
        fx.tag("v1")
        assert fx.run("update", "--unsigned", "v1").returncode == 0
        fx.commit("v2")
        fx.tag("v2", key=fx.release_key)
        result = fx.run("update", "v2")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v2\n" in fx.env_text()
        assert "ISTOTA_TAG_UNSIGNED=\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG=v1\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG_UNSIGNED=1\n" in fx.env_text()

    def test_rollback_returns_to_a_tag_installed_unsigned(self, fx):
        fx.commit("v1")
        fx.tag("v1")
        assert fx.run("update", "--unsigned", "v1").returncode == 0
        fx.commit("v2")
        fx.tag("v2", key=fx.release_key)
        assert fx.run("update", "v2").returncode == 0
        result = fx.run("rollback")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "ISTOTA_TAG_UNSIGNED=1\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG=v2\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG_UNSIGNED=\n" in fx.env_text()

    def test_rollback_to_an_unsigned_tag_nobody_accepted_is_refused(self, fx):
        """The mark is what accepted it; an unsigned previous tag without one
        (.env edited by hand) is verified like any release."""
        fx.commit("v1")
        fx.tag("v1")
        (fx.stack / ".env").write_text(
            "COMPOSE_PROFILES=\nISTOTA_TAG=v0\nISTOTA_PREVIOUS_TAG=v1\n"
        )
        _git(fx.src, "fetch", "-q", "--tags", "origin", env=fx.env)
        before = fx.env_text(), fx.head()
        result = fx.run("rollback")
        _assert_unchanged(fx, result, *before)


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
        assert "--entrypoint istota-drop daemon istota init --check-schema" in calls[check]

    def test_a_release_from_before_the_rename_has_its_image_tagged_first(self, updated):
        """v0's image is istota-istota:v0. Unless it is tagged istota:v0 first,
        the schema check's `compose run` would build one from this checkout."""
        result = updated.run("rollback", STUB_RENAMED_IMAGE_MISSING="1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = updated.docker_calls()
        tag = calls.index("image tag istota-istota:v0 istota:v0")
        check = next(i for i, call in enumerate(calls) if "--check-schema" in call)
        assert tag < check


class TestTheServiceFollowsTheCheckout:
    """A rollback to a release from before the rename checks out a compose
    file whose daemon service is still called `istota`."""

    def test_an_old_checkout_is_exec_d_as_istota(self, fx):
        (fx.src / "docker" / "docker-compose.yml").write_text("services:\n  istota:\n    image: x\n")
        result = fx.run("exec", "istota", "doctor")
        assert result.returncode == 0, result.stderr
        assert fx.docker_calls()[-1].endswith("exec -T istota istota-drop istota doctor")

    def test_the_shipped_compose_file_is_exec_d_as_daemon(self, fx):
        shutil.copy(REPO / "docker" / "docker-compose.yml", fx.src / "docker" / "docker-compose.yml")
        result = fx.run("exec", "istota", "doctor")
        assert result.returncode == 0, result.stderr
        assert fx.docker_calls()[-1].endswith("exec -T daemon istota-drop istota doctor")


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
                f"daemon istota apply -f /plan/plan.toml{''.join(' ' + f for f in flags)}")
            for call in calls
        ), calls
        assert any(call.endswith(" restart") for call in calls) is restarted

    def test_an_added_devbox_user_is_rendered_and_brought_up(self, fx, tmp_path):
        """A change to [devbox] users needs the devbox services re-rendered and
        created, which a restart alone does not do."""
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        rendered = '{"services": {"devbox-alice": {}}}'
        result = fx.run("apply", str(plan), STUB_APPLY_RC="2", STUB_DEVBOX_COMPOSE=rendered)
        assert result.returncode == 2, result.stdout + result.stderr
        assert rendered in (fx.stack / "compose.devbox.yml").read_text()
        calls = fx.docker_calls()
        up = next(i for i, call in enumerate(calls) if call.endswith("up -d --remove-orphans"))
        restart = next(i for i, call in enumerate(calls) if call.endswith(" restart"))
        assert up < restart
        assert "compose.devbox.yml" in calls[up]

    def test_a_stack_that_is_down_stays_down(self, fx, tmp_path):
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = fx.run("apply", str(plan), STUB_APPLY_RC="2", STUB_STACK_DOWN="1")
        assert result.returncode == 2, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert not any(call.endswith(" restart") or " up " in f"{call} " for call in calls), calls


class TestAProfileTurnedOffStopsItsService:
    """Compose leaves a container running when its service's profile is no
    longer active: `down` and `up --remove-orphans` both count it as defined.
    Found by the vm tier with the browser profile (ISTOTA_VM_CONTROL unset,
    `tests/vm/test_management_role.py`)."""

    CONTAINERS = "c-daemon daemon\\nc-web web\\nc-browser browser\\n"

    def test_up_removes_the_container_of_a_service_no_profile_names(self, fx):
        result = fx.run("up", STUB_PROJECT_CONTAINERS=self.CONTAINERS)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        up = next(i for i, call in enumerate(calls) if call.endswith("up -d --remove-orphans"))
        removed = [i for i, call in enumerate(calls) if call.startswith("rm -f ")]
        assert [calls[i] for i in removed] == ["rm -f c-browser"], calls
        assert removed[0] > up

    def test_a_config_change_does_the_same(self, fx, tmp_path):
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = fx.run("apply", str(plan), STUB_APPLY_RC="2", STUB_PROJECT_CONTAINERS=self.CONTAINERS)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "rm -f c-browser" in fx.docker_calls()

    def test_an_update_does_the_same(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = fx.run("update", "v1", STUB_PROJECT_CONTAINERS=self.CONTAINERS)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "rm -f c-browser" in fx.docker_calls()

    def test_nothing_is_removed_when_every_service_is_active(self, fx):
        result = fx.run("up", STUB_PROJECT_CONTAINERS="c-daemon daemon\\nc-web web\\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not [call for call in fx.docker_calls() if call.startswith("rm ")]


class TestImportBareMetal:
    """The import replaces the database and the master key, so it runs only
    while the stack is down: the daemon's own lock is in a /tmp the one-shot
    container does not share."""

    def test_it_refuses_while_the_stack_is_up(self, fx, tmp_path):
        export = tmp_path / "import"
        export.mkdir()
        result = fx.run("import-bare-metal", str(export), "--ingress", "proxied")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "running" in result.stderr
        assert not any(" run " in f"{call} " for call in fx.docker_calls()), fx.docker_calls()

    def test_it_runs_in_a_one_shot_with_the_export_and_secrets_mounted(self, fx, tmp_path):
        export = tmp_path / "import"
        export.mkdir()
        result = fx.run(
            "import-bare-metal", str(export), "--ingress", "proxied", "--dry-run",
            STUB_STACK_DOWN="1",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.docker_calls()[-1].endswith(
            f"run --rm --no-deps -T --entrypoint istota-drop "
            f"-v {export}:/import:ro -v {fx.stack}/secrets:/secrets "
            "daemon istota migrate import-bare-metal --source /import --ingress proxied --dry-run"
        ), fx.docker_calls()

    def test_a_missing_export_directory_is_refused(self, fx, tmp_path):
        result = fx.run("import-bare-metal", str(tmp_path / "nowhere"), STUB_STACK_DOWN="1")
        assert result.returncode == 1
        assert fx.docker_calls() == []


class TestExec:
    def test_every_exec_goes_through_the_drop(self, fx):
        result = fx.run("exec", "istota", "doctor")
        assert result.returncode == 0, result.stderr
        assert fx.docker_calls()[-1].endswith("exec -T daemon istota-drop istota doctor")

    def test_the_vm_wrapper_is_the_exec_path(self, fx):
        wrapper = REPO / "host" / "istota"
        result = subprocess.run(
            ["sh", str(wrapper), "user", "list"], capture_output=True, text=True,
            env={**fx.env, "ISTOTA_STACK_BIN": str(SCRIPT)}, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert fx.docker_calls()[-1].endswith("exec -T daemon istota-drop istota user list")


def _source(fx: Fixture, script: str, **extra_env: str) -> subprocess.CompletedProcess:
    """Run `script` with istota-stack's functions defined (the main guard keeps
    sourcing inert)."""
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; {script}'], capture_output=True, text=True,
        env={**fx.env, **extra_env}, timeout=60,
    )


class TestContainerLogsReachTheJournal:
    """On the VM container output goes to the journal, which is what a host's
    log shipping reads; json-file kept it out of it (#712). Docker Desktop has
    no journald, so the compose file defaults to json-file and only
    `istota-stack` names journald."""

    def test_every_service_logs_through_the_selected_driver_with_its_name(self):
        import yaml

        compose = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())
        for name, service in compose["services"].items():
            assert service.get("logging") == {
                "driver": "${ISTOTA_LOG_DRIVER:-json-file}",
                "options": {"tag": "{{.Name}}"},
            }, name

    def test_istota_stack_selects_journald(self, fx):
        result = _source(fx, 'docker() { echo "driver=${ISTOTA_LOG_DRIVER-unset}"; }; compose ps')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "driver=journald"


class TestTheMount:
    def test_compose_binds_the_mount_from_outside_the_stack_directory(self, fx):
        result = _source(fx, 'docker() { echo "mount=${ISTOTA_MOUNT_DIR}"; }; compose ps')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"mount={fx.root / 'mount'}"


class TestRootNeverReadsWhatUid10001Can:
    """Root on the VM must not act on a file uid 10001 can write or replace.

    `/srv/app/istota/config` is 10001's (the one-shot setup and apply containers
    write it), and so is the setup staging directory. A process on the VM
    running as 10001, the rclone mount above all, can plant anything in either.
    The devbox compose file is passed to `docker compose` by root, so a copy in
    the config directory is a container definition 10001 chooses; and root
    copying the wizard's `.env` out of staging follows a planted symlink into
    whatever root can read.
    """

    def test_the_devbox_compose_file_is_rendered_outside_the_config_directory(self, fx, tmp_path):
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        rendered = '{"services": {"devbox-alice": {}}}'
        result = fx.run("apply", str(plan), STUB_APPLY_RC="2", STUB_DEVBOX_COMPOSE=rendered)
        assert result.returncode == 2, result.stdout + result.stderr
        assert rendered in (fx.stack / "compose.devbox.yml").read_text()
        assert not (fx.stack / "config" / "compose.devbox.yml").exists()

    def test_a_compose_file_planted_in_the_config_directory_is_never_used(self, fx):
        (fx.stack / "config" / "compose.devbox.yml").write_text('{"services": {"devbox-evil": {}}}\n')
        assert fx.run("compose", "ps").returncode == 0
        assert not any("compose.devbox.yml" in call for call in fx.docker_calls()), fx.docker_calls()

    def test_a_linked_devbox_compose_file_is_refused(self, fx, tmp_path):
        elsewhere = tmp_path / "elsewhere.yml"
        elsewhere.write_text('{"services": {"devbox-evil": {}}}\n')
        (fx.stack / "compose.devbox.yml").symlink_to(elsewhere)
        result = fx.run("compose", "ps")
        assert result.returncode != 0
        assert "compose.devbox.yml" in result.stderr
        assert fx.docker_calls() == []

    def test_a_group_writable_devbox_compose_file_is_refused(self, fx):
        path = fx.stack / "compose.devbox.yml"
        path.write_text('{"services": {"devbox-alice": {}}}\n')
        path.chmod(0o664)
        result = fx.run("compose", "ps")
        assert result.returncode != 0
        assert fx.docker_calls() == []

    def test_a_staged_file_that_is_a_link_is_not_followed(self, fx, tmp_path):
        secret = tmp_path / "shadow"
        secret.write_text("root:secret-hash:\n")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / ".env").symlink_to(secret)
        dest = fx.stack / ".env"
        before = dest.read_text()

        result = _source(fx, f'install_staged "{staging}/.env" "{dest}"')

        assert result.returncode != 0
        assert dest.read_text() == before
        assert "secret-hash" not in dest.read_text()

    def test_a_staged_regular_file_is_installed(self, fx, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / ".env").write_text("COMPOSE_PROFILES=\nISTOTA_TAG=v0\n")
        dest = fx.stack / ".env"

        result = _source(fx, f'install_staged "{staging}/.env" "{dest}"')

        assert result.returncode == 0, result.stderr
        assert dest.read_text() == "COMPOSE_PROFILES=\nISTOTA_TAG=v0\n"
        assert (dest.stat().st_mode & 0o777) == 0o644


class TestAProfileThatFailsToLoadChangesNothing:
    """`.env` names a tag only once that tag's AppArmor profile is loaded.

    Written the other way round, a profile the kernel refused left `.env`
    naming the new tag while the old profile stayed loaded, and the next
    `istota-stack up` started containers under `apparmor=istota` from a profile
    that never matched them.
    """

    @pytest.fixture
    def apparmor(self, fx, tmp_path):
        sysfs = tmp_path / "apparmor-sysfs"
        sysfs.mkdir()
        profiles = tmp_path / "apparmor.d"
        profiles.mkdir()
        (profiles / "istota").write_text("profile istota old\n")
        parser = Path(fx.env["PATH"].split(":")[0]) / "apparmor_parser"
        parser.write_text('#!/bin/bash\necho "$*" >> "$STUB_CALLS.apparmor"\nexit "${STUB_APPARMOR_RC:-0}"\n')
        parser.chmod(0o755)
        fx.env.update(ISTOTA_APPARMOR_SYSFS=str(sysfs), ISTOTA_APPARMOR_DIR=str(profiles))
        profile = fx.origin / "docker" / "istota" / "apparmor-istota"
        profile.parent.mkdir(parents=True)
        profile.write_text("profile istota new\n")
        _git(fx.origin, "add", "docker/istota/apparmor-istota", env=fx.env)
        commit = fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        return profiles, commit

    def test_a_refused_profile_leaves_env_the_checkout_and_the_file(self, fx, apparmor):
        profiles, _ = apparmor
        env_before, head_before = fx.env_text(), fx.head()

        result = fx.run("update", "v1", STUB_APPARMOR_RC="1")

        assert result.returncode == 1, result.stdout + result.stderr
        assert "AppArmor" in result.stderr
        assert fx.env_text() == env_before
        assert fx.head() == head_before
        assert (profiles / "istota").read_text() == "profile istota old\n"
        assert not any(" up " in f"{call} " for call in fx.docker_calls())

    def test_a_loaded_profile_is_installed_before_env_names_the_tag(self, fx, apparmor):
        profiles, commit = apparmor

        result = fx.run("update", "v1")

        assert result.returncode == 0, result.stdout + result.stderr
        assert (profiles / "istota").read_text() == "profile istota new\n"
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert fx.head() == commit


class TestPutFiles:
    """`istota-stack put-files` writes `secrets/` and `config/admins`, the two
    places in the stack uid 10001 owns, from JSON on stdin. Run as root it drops
    to 10001 before it opens anything, so a link 10001 planted there gains
    nothing; and it replaces a link rather than following it either way. Exit
    0 is no change, 2 a change, as for `apply`."""

    @pytest.fixture
    def secrets(self, fx):
        path = fx.stack / "secrets"
        path.mkdir(mode=0o700)
        return path

    def _put(self, fx, payload: dict, *args: str) -> subprocess.CompletedProcess:
        import json

        return subprocess.run(
            ["bash", str(SCRIPT), "put-files", *args], input=json.dumps(payload),
            capture_output=True, text=True, env=fx.env, timeout=60,
        )

    def test_it_writes_each_value_and_the_admins(self, fx, secrets):
        result = self._put(fx, {"secrets": {"anthropic_api_key": "sk-1", "istota_caldav_password": None},
                                "admins": "alice\nbob\n"})
        assert result.returncode == 2, result.stdout + result.stderr
        assert (secrets / "anthropic_api_key").read_text() == "sk-1"
        assert (secrets / "anthropic_api_key").stat().st_mode & 0o777 == 0o400
        assert (secrets / "istota_caldav_password").read_text() == ""
        assert (fx.stack / "config" / "admins").read_text() == "alice\nbob\n"
        assert (fx.stack / "config" / "admins").stat().st_mode & 0o777 == 0o600
        assert "sk-1" not in result.stdout + result.stderr

    def test_a_second_run_changes_nothing(self, fx, secrets):
        payload = {"secrets": {"anthropic_api_key": "sk-1", "istota_caldav_password": None}, "admins": "alice\n"}
        assert self._put(fx, payload).returncode == 2
        result = self._put(fx, payload)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_null_value_leaves_an_existing_file_alone(self, fx, secrets):
        (secrets / "istota_caldav_password").write_text("from-the-wizard")
        result = self._put(fx, {"secrets": {"istota_caldav_password": None}})
        assert result.returncode == 0, result.stdout + result.stderr
        assert (secrets / "istota_caldav_password").read_text() == "from-the-wizard"

    def test_the_dry_run_reports_and_writes_nothing(self, fx, secrets):
        result = self._put(fx, {"secrets": {"anthropic_api_key": "sk-1"}, "admins": "alice\n"}, "--dry-run")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "anthropic_api_key" in result.stdout
        assert list(secrets.iterdir()) == []
        assert not (fx.stack / "config" / "admins").exists()

    def test_a_link_to_a_directory_is_replaced_not_followed(self, fx, secrets, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (secrets / "anthropic_api_key").symlink_to(elsewhere)
        (fx.stack / "config" / "admins").symlink_to(elsewhere)

        result = self._put(fx, {"secrets": {"anthropic_api_key": "sk-1"}, "admins": "alice\n"})

        assert result.returncode == 2, result.stdout + result.stderr
        assert list(elsewhere.iterdir()) == []
        assert not (secrets / "anthropic_api_key").is_symlink()
        assert (secrets / "anthropic_api_key").read_text() == "sk-1"
        assert not (fx.stack / "config" / "admins").is_symlink()

    def test_a_name_that_is_not_a_secret_name_is_refused(self, fx, secrets):
        result = self._put(fx, {"secrets": {"../config/admins": "x"}})
        assert result.returncode == 1
        assert not (fx.stack / "config" / "admins").exists()

    def test_as_root_it_drops_to_10001_first(self, fx, secrets, tmp_path):
        bindir = Path(fx.env["PATH"].split(":")[0])
        (bindir / "id").write_text('#!/bin/bash\nif [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
        (bindir / "id").chmod(0o755)
        recorded = tmp_path / "setpriv"
        (bindir / "setpriv").write_text(
            f'#!/bin/bash\necho "$*" > "{recorded}"\n'
            'while [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n'
        )
        (bindir / "setpriv").chmod(0o755)

        result = self._put(fx, {"secrets": {"anthropic_api_key": "sk-1"}})

        assert result.returncode == 2, result.stdout + result.stderr
        flags = recorded.read_text().split()
        assert {"--reuid=10001", "--regid=10001", "--clear-groups", "--no-new-privs"} <= set(flags)
        assert (secrets / "anthropic_api_key").read_text() == "sk-1"


class TestCheckImage:
    """Whether the deployed image has what config.toml needs: Claude Code, at
    host.env's pin when it names one. `istota-stack setup` says so in words;
    the management role runs this after its apply and rebuilds on exit 2."""

    def _check(self, fx, image_env: str) -> subprocess.CompletedProcess:
        return fx.run("check-image", STUB_IMAGE_ENV=image_env)

    @pytest.fixture
    def claude_code(self, fx):
        (fx.stack / "config" / "config.toml").write_text('[brain]\nkind = "claude_code"\n')
        return fx

    def test_a_native_config_needs_nothing(self, fx):
        assert self._check(fx, "ISTOTA_CLAUDE_CODE_INSTALLED=0").returncode == 0

    def test_claude_code_missing_from_the_image(self, claude_code):
        result = self._check(claude_code, "ISTOTA_CLAUDE_CODE_INSTALLED=0\nISTOTA_CLAUDE_CODE_VERSION=2.1.296")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "istota-stack update v0" in result.stdout + result.stderr

    def test_claude_code_in_the_image_at_the_pin(self, claude_code):
        result = self._check(claude_code, "ISTOTA_CLAUDE_CODE_INSTALLED=1\nISTOTA_CLAUDE_CODE_VERSION=2.1.296")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_claude_code_at_another_version_than_the_pin(self, claude_code):
        result = self._check(claude_code, "ISTOTA_CLAUDE_CODE_INSTALLED=1\nISTOTA_CLAUDE_CODE_VERSION=2.0.1")
        assert result.returncode == 2, result.stdout + result.stderr

    def test_no_pin_takes_whatever_version_the_release_built(self, claude_code):
        (claude_code.stack / "host.env").write_text("INGRESS=local\nCLAUDE_CODE_VERSION=\n")
        result = self._check(claude_code, "ISTOTA_CLAUDE_CODE_INSTALLED=1\nISTOTA_CLAUDE_CODE_VERSION=2.0.1")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_setup_uses_the_same_check(self):
        text = SCRIPT.read_text()
        setup = text.split("\ncmd_setup() {", 1)[1].split("\n}\n", 1)[0]
        assert "image_lacks_what_config_needs" in setup
        assert "ISTOTA_CLAUDE_CODE_INSTALLED" not in setup


class TestTheClaudeCodePin:
    def test_an_empty_pin_leaves_the_dockerfiles_default(self, fx):
        """An empty build arg overrides the Dockerfile's ARG default, and the
        build then refuses ("CLAUDE_CODE_VERSION is empty"). host.env with no
        pin, which is what `istota setup` writes, must build the release's."""
        (fx.stack / "host.env").write_text("INGRESS=local\nCLAUDE_CODE_VERSION=\n")
        result = _source(fx, 'docker() { echo "pin=${CLAUDE_CODE_VERSION-unset}"; }; compose build')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "pin=unset"

    def test_a_pin_is_passed(self, fx):
        result = _source(fx, 'docker() { echo "pin=${CLAUDE_CODE_VERSION-unset}"; }; compose build')
        assert result.stdout.strip() == "pin=2.1.296"


class TestTheWrapperPassesStdin:
    def test_stdin_reaches_the_exec(self, fx, tmp_path):
        """`istota secret ensure --value-stdin` reads the value through every
        layer: the wrapper, `istota-stack exec`, `docker compose exec -T`."""
        seen = tmp_path / "stdin"
        stub = Path(fx.env["PATH"].split(":")[0]) / "docker"
        stub.write_text(DOCKER_STUB.replace(
            'case "$args" in\n', f'case "$args" in\n  *" exec "*) cat > "{seen}" ;;\n'))
        result = subprocess.run(
            ["sh", str(REPO / "host" / "istota"), "secret", "ensure", "--value-stdin"],
            input="the-value", capture_output=True, text=True,
            env={**fx.env, "ISTOTA_STACK_BIN": str(SCRIPT)}, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert seen.read_text() == "the-value"


class TestLocalizeWorkspace:
    """`localize-workspace`: the offline move off the rclone mount.

    The copy, the config edit and the reindex are `istota workspace` verbs in
    one-shot containers, so what is held here is the order, what each step's
    exit code does to the next, and that host.env changes only after the copy
    and the config edit have both succeeded."""

    COPY = (
        "--entrypoint istota-drop daemon istota workspace localize "
        "--from /mnt/shared --to /data/workspace"
    )

    @pytest.fixture
    def mounted(self, fx):
        (fx.stack / "host.env").write_text("INGRESS=local\nSTORAGE=nextcloud\nRCLONE_REMOTE=nextcloud\n")
        provision = fx.src / "host" / "provision.sh"
        provision.parent.mkdir(parents=True, exist_ok=True)
        provision.write_text(PROVISION_STUB)
        provision.chmod(0o755)
        return fx

    @staticmethod
    def _index(calls: list[str], fragment: str) -> int:
        matches = [i for i, call in enumerate(calls) if fragment in call]
        assert matches, f"no call with {fragment!r}: {calls}"
        return matches[0]

    def test_the_steps_run_in_order(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        order = [
            self._index(calls, "istota workspace localize-config --check --workspace /data/workspace"),
            self._index(calls, " stop daemon web"),
            self._index(calls, f"{self.COPY} --scheduler-stopped"),
            self._index(calls, "config read-only=false"),
            self._index(calls, "provision ran"),
            self._index(calls, "up -d --remove-orphans"),
            self._index(calls, "exec -T daemon istota-drop istota workspace reindex --forget-root /mnt/shared"),
        ]
        assert order == sorted(order), calls
        host_env = (fx.stack / "host.env").read_text()
        assert "STORAGE=local\n" in host_env
        assert "RCLONE_REMOTE=nextcloud\n" in host_env and "INGRESS=local\n" in host_env
        assert "config.toml.pre-localize" in result.stdout

    def test_a_config_the_plan_owns_is_refused_before_anything_stops(self, mounted):
        fx = mounted
        from istota import apply
        (fx.stack / "config" / "config.toml").write_text(apply.CONFIG_HEADER + '[brain]\nkind = "native"\n')
        result = fx.run("localize-workspace", STUB_LOCALIZE_CHECK_RC="1")
        assert result.returncode == 1
        assert "--copy-only" in result.stderr and "istota_storage: local" in result.stderr
        calls = fx.docker_calls()
        assert not [c for c in calls if " stop " in f"{c} " or "workspace localize " in f"{c} "], calls
        assert "STORAGE=nextcloud\n" in (fx.stack / "host.env").read_text()

    def test_copy_only_copies_and_changes_nothing_else(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", "--copy-only", STUB_LOCALIZE_CHECK_RC="1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any(c.endswith(f"{self.COPY} --scheduler-stopped") for c in calls), calls
        assert not [c for c in calls if "localize-config" in c or "config read-only" in c or " up " in f"{c} "], calls
        assert "provision ran" not in calls
        assert "STORAGE=nextcloud\n" in (fx.stack / "host.env").read_text()
        assert "change the plan" in result.stdout.lower()

    @pytest.mark.parametrize("rc", ["1", "3"])
    def test_a_failed_copy_leaves_the_config_and_host_env(self, mounted, rc):
        fx = mounted
        result = fx.run("localize-workspace", STUB_LOCALIZE_RC=rc)
        assert result.returncode == 1
        assert "--resume" in result.stderr and "istota-stack up" in result.stderr
        calls = fx.docker_calls()
        assert "config read-only=false" not in calls
        assert not [c for c in calls if " up " in f"{c} "], calls
        assert "STORAGE=nextcloud\n" in (fx.stack / "host.env").read_text()

    def test_skipped_entries_stop_before_the_config_without_a_terminal(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", STUB_LOCALIZE_RC="2")
        assert result.returncode == 1
        assert "--accept-skipped" in result.stderr
        assert "config read-only=false" not in fx.docker_calls()
        assert "STORAGE=nextcloud\n" in (fx.stack / "host.env").read_text()

    def test_skipped_entries_go_on_when_accepted(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", "--resume", "--accept-skipped", STUB_LOCALIZE_RC="2")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any(c.endswith(f"{self.COPY} --resume --scheduler-stopped") for c in calls), calls
        assert "config read-only=false" in calls
        assert "STORAGE=local\n" in (fx.stack / "host.env").read_text()

    def test_a_dry_run_stops_nothing_and_writes_nothing(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any(c.endswith(f"{self.COPY} --dry-run") for c in calls), calls
        assert not [c for c in calls if " stop " in f"{c} " or "localize-config" in c], calls
        assert "STORAGE=nextcloud\n" in (fx.stack / "host.env").read_text()

    def test_a_local_install_has_nothing_to_move(self, fx):
        result = fx.run("localize-workspace")
        assert result.returncode == 1
        assert "STORAGE=nextcloud" in result.stderr
        assert fx.docker_calls() == []
