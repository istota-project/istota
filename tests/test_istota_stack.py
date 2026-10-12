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
`allowed_signers`, each of which must turn named node ids here red. Its
`setup-web` controls do the same for `setup --web`: staging installed without
the `setup-done` marker, the code and certificate minted into staging rather
than root's `.setup/`, and no setup lock. Each must turn exactly its named
node ids in the setup classes red.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support import parity
from tests.support.host_stubs import HeldLock, install_flock

REPO = Path(__file__).resolve().parent.parent
#: The script under test. The negative-control driver points this at a broken copy.
SCRIPT = Path(os.environ.get("ISTOTA_STACK_SCRIPT") or REPO / "host" / "istota-stack")

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="needs ssh-keygen and git",
)

DOCKER_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_CALLS"
[ -z "${COMPOSE_IGNORE_ORPHANS:-}" ] || printf '%s\n' "$*" >> "$STUB_CALLS.ignore-orphans"
args=" $* "
for ref in ${STUB_MISSING_IMAGES:-}; do
  case "$args" in *" image inspect $ref "*) exit 1 ;; esac
done
case "$args" in
  *" setup --web "*)
    # The setup server: snapshot what root handed it, run STUB_DURING while it
    # "serves", then write what a finished (or a failed) run leaves behind.
    vm=""; prev=""
    for a in "$@"; do
      if [ "$prev" = -v ] && [ "${a%:/vm}" != "$a" ]; then vm="${a%:/vm}"; fi
      prev="$a"
    done
    echo "read-only=${ISTOTA_CONFIG_READ_ONLY:-unset}" >> "$STUB_CALLS"
    rm -rf "$STUB_CALLS.staging" "$STUB_CALLS.setup-dir"
    cp -Rp "$vm" "$STUB_CALLS.staging"
    cp -Rp "$ISTOTA_STACK_DIR/.setup" "$STUB_CALLS.setup-dir"
    if [ -n "${STUB_DURING:-}" ]; then
      # Unset for the child: a second setup run that is not refused must not
      # start a third.
      env -u STUB_DURING bash -c "$STUB_DURING" > "$STUB_CALLS.during" 2>&1
      echo "rc=$?" >> "$STUB_CALLS.during"
    fi
    if [ -n "${STUB_CONFIG:-}" ]; then printf '%s' "$STUB_CONFIG" > "$ISTOTA_CONFIG_DIR/config.toml"; fi
    if [ -n "${STUB_SETUP_DONE:-}" ]; then
      printf '%s' "${STUB_STAGED_ENV:-}" > "$vm/.env"
      printf '%s' "${STUB_STAGED_HOST_ENV:-}" > "$vm/host.env"
      : > "$vm/setup-done"
    fi
    exit "${STUB_SETUP_RC:-0}" ;;
  *" migrate restore-workspace"*)
    cat > "$STUB_CALLS.stdin"
    vm=""; prev=""
    for a in "$@"; do
      if [ "$prev" = -v ] && [ "${a%:/vm}" != "$a" ]; then vm="${a%:/vm}"; fi
      prev="$a"
    done
    if [ -n "$vm" ] && [ -n "${STUB_RESTORED_STACK_ENV:-}" ]; then printf '%s' "$STUB_RESTORED_STACK_ENV" > "$vm/stack.env"; fi
    if [ -n "$vm" ] && [ -n "${STUB_RESTORED_HOST_ENV:-}" ]; then printf '%s' "$STUB_RESTORED_HOST_ENV" > "$vm/host.env"; fi
    exit "${STUB_RESTORE_RC:-0}" ;;
  *" migrate import-standalone"*)
    cat > "$STUB_CALLS.stdin"
    vm=""; prev=""
    for a in "$@"; do
      if [ "$prev" = -v ] && [ "${a%:/vm}" != "$a" ]; then vm="${a%:/vm}"; fi
      prev="$a"
    done
    if [ -n "$vm" ] && [ -n "${STUB_IMPORTED_ENV:-}" ]; then printf '%s' "$STUB_IMPORTED_ENV" > "$vm/.env"; fi
    if [ -n "$vm" ] && [ -n "${STUB_IMPORTED_HOST_ENV:-}" ]; then printf '%s' "$STUB_IMPORTED_HOST_ENV" > "$vm/host.env"; fi
    exit "${STUB_IMPORT_RC:-0}" ;;
  *" config --services "*)
    if [ -n "${STUB_SERVICES:-}" ]; then printf '%b' "$STUB_SERVICES"; else printf 'daemon\nweb\nnginx\n'; fi
    case "$args" in *compose.devbox.yml*) echo devbox-alice ;; esac ;;
  *" build "*) exit "${STUB_BUILD_RC:-0}" ;;
  *" up -d "*) exit "${STUB_UP_RC:-0}" ;;
  *" builder prune "*|*" image prune "*)
    echo 'Total reclaimed space: 1.5GB'
    exit "${STUB_PRUNE_RC:-0}" ;;
  *" devbox compose-file"*)
    if [ -n "${STUB_DEVBOX_COMPOSE:-}" ]; then printf '%s\n' "$STUB_DEVBOX_COMPOSE"
    else echo '{"services": {}}'; fi ;;
  *" ps -a --filter label=com.docker.compose.project=istota "*) printf '%b' "${STUB_PROJECT_CONTAINERS:-}" ;;
  *" ps -q daemon"*) [ -n "${STUB_STACK_DOWN:-}" ] || echo running-container ;;
  *" ps -q "*) [ -n "${STUB_STACK_DOWN:-}" ] || echo running-container ;;
  *" --check-schema"*) exit "${STUB_CHECK_RC:-0}" ;;
  *" istota apply "*) exit "${STUB_APPLY_RC:-0}" ;;
  *" image ls "*) if [ -n "${STUB_IMAGES:-}" ]; then printf "%b" "$STUB_IMAGES"; exit 0; fi; printf 'istota:v0\nistota:v1\nistota:old\nistota-istota:v0\nistota-istota:older\nistota-browser:old\nnginx:alpine\n' ;;
  *" image tag "*) touch "${STUB_CALLS}.tagged" ;;
  *" image inspect istota:"*)
    if [ -n "${STUB_RENAMED_IMAGE_MISSING:-}" ] && [ ! -e "${STUB_CALLS}.tagged" ]; then exit 1; fi
    [ -z "${STUB_IMAGE_ENV:-}" ] || printf '%s\n' "$STUB_IMAGE_ENV" ;;
  *" image inspect "*) [ -z "${STUB_IMAGE_ENV:-}" ] || printf '%s\n' "$STUB_IMAGE_ENV" ;;
  *" localize-config --check "*) exit "${STUB_LOCALIZE_CHECK_RC:-0}" ;;
  *" workspace localize --check "*) exit "${STUB_LOCALIZE_DEST_RC:-0}" ;;
  *" workspace localize "*) exit "${STUB_LOCALIZE_RC:-0}" ;;
  *" workspace moved-note "*) exit "${STUB_NOTE_RC:-0}" ;;
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
    install_flock(bindir)
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


class TestUpdateBuildCleanup:
    cache_prune = "builder prune --all --force --keep-storage 2GB"
    image_prune = "image prune --force --filter label=com.docker.compose.project=istota"

    def test_cleanup_runs_before_build_and_after_old_tags_are_removed(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stderr
        calls = fx.docker_calls()
        build = next(i for i, c in enumerate(calls) if " build " in f"{c} ")
        up = next(i for i, c in enumerate(calls) if " up -d " in f"{c} ")
        assert calls.count(self.cache_prune) == 2
        assert calls.count(self.image_prune) == 2
        assert calls.index(self.image_prune) < calls.index(self.cache_prune) < build
        assert up < calls.index("image rm istota:old") < len(calls) - 2
        assert calls[-2:] == [self.image_prune, self.cache_prune]
        assert "Total reclaimed space: 1.5GB" in result.stdout

    @pytest.mark.parametrize("failure", ["STUB_BUILD_RC", "STUB_UP_RC"])
    @pytest.mark.parametrize("prune_rc", ["0", "1"])
    def test_a_failed_attempt_still_cleans_and_keeps_its_exit_status(self, fx, failure, prune_rc):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        before = fx.env_text(), fx.head()
        result = fx.run("update", "v1", STUB_PRUNE_RC=prune_rc, **{failure: "1"})
        assert result.returncode == 1, result.stderr
        calls = fx.docker_calls()
        assert calls.count(self.cache_prune) == 2
        assert calls[-2:] == [self.image_prune, self.cache_prune]
        assert "image rm istota:v0" not in calls
        if failure == "STUB_BUILD_RC":
            assert (fx.env_text(), fx.head()) == before
            assert "image rm istota:v1" in calls  # discard an incomplete candidate

    def test_cleanup_errors_warn_without_failing_a_successful_update(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = fx.run("update", "v1", STUB_PRUNE_RC="1")
        assert result.returncode == 0, result.stderr
        assert "WARNING: could not prune dangling Istota images" in result.stdout
        assert "WARNING: could not prune build cache" in result.stdout
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    def test_repeated_updates_and_same_tag_rebuild_preserve_rollback(self, fx):
        for tag, previous in [("v1", "v0"), ("v2", "v1"), ("v3", "v2"), ("v3", "v2")]:
            if tag != "v3" or "ISTOTA_TAG=v3\n" not in fx.env_text():
                fx.commit(tag)
                fx.tag(tag, key=fx.release_key)
            fx.calls.unlink(missing_ok=True)
            images = "".join(f"{repo}:{t}\n" for repo in ["istota", "istota-devbox", "istota-browser"]
                             for t in ["v0", "v1", "v2", "v3"])
            result = fx.run("update", tag, STUB_IMAGES=images + "unrelated:old\n")
            assert result.returncode == 0, result.stderr
            calls = fx.docker_calls()
            for ref in images.splitlines():
                assert (f"image rm {ref}" in calls) == (ref.split(":")[1] not in {tag, previous})
            assert "image rm unrelated:old" not in calls
            assert calls.count(self.cache_prune) == 2
            assert f"ISTOTA_PREVIOUS_TAG={previous}\n" in fx.env_text()
        fx.calls.unlink()
        result = fx.run("rollback")
        assert result.returncode == 0, result.stderr
        assert "ISTOTA_TAG=v2\n" in fx.env_text()
        assert not any(" build " in f"{c} " for c in fx.docker_calls())

    def test_failed_build_preserves_both_existing_deployment_tags(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        assert fx.run("update", "v1").returncode == 0
        fx.commit("v2")
        fx.tag("v2", key=fx.release_key)
        fx.calls.unlink()
        before = fx.env_text(), fx.head()
        result = fx.run("update", "v2", STUB_BUILD_RC="1",
                        STUB_IMAGES="istota:v0\nistota:v1\nistota:v2\n")
        assert result.returncode == 1
        assert (fx.env_text(), fx.head()) == before
        removed = [c for c in fx.docker_calls() if c.startswith("image rm ")]
        assert removed == ["image rm istota:v2"]

    def test_builds_use_the_same_default_builder_that_cleanup_prunes(self, fx, tmp_path):
        recorder = tmp_path / "builders"
        stub = Path(fx.env["PATH"].split(":")[0]) / "docker"
        stub.write_text(DOCKER_STUB.replace(
            '*" build "*) exit',
            f'*" build "*) echo "$BUILDX_BUILDER" >> {recorder}; exit',
        ).replace(
            '*" builder prune "*|*" image prune "*)',
            f'*" builder prune "*|*" image prune "*) echo "$BUILDX_BUILDER" >> {recorder};',
        ))
        (fx.stack / "compose.devbox.yml").write_text('{"services": {"devbox-alice": {}}}')
        result = _source(fx, 'build_images v0; build_devbox_images v0; prune_build_artifacts', BUILDX_BUILDER="unrelated-builder")
        assert result.returncode == 0, result.stderr
        assert recorder.read_text().splitlines() == ["default", "default", "unrelated-builder", "default"]

    def test_an_unverified_tag_does_not_prune_anything(self, fx):
        fx.commit("v1")
        fx.tag("v1")
        result = fx.run("update", "v1")
        assert result.returncode == 65
        assert not fx.docker_calls()


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


RELEASE_STACK_STUB = """#!/bin/bash
# ISTOTA_STACK_LOCK_INHERITED
echo "release istota-stack: $* inherited=$(printenv | grep -c '^ISTOTA_STACK_LOCK_INH')" >> "$STUB_CALLS"
"""


class TestTheReleasesOwnScriptBringsTheStackUp:
    """The istota-stack running an update is the previous release's. When its
    `up` and the new one's (run by provision.sh) pass compose different
    variables, the containers were recreated twice. The checkout's own script
    brings the stack up instead, on the lock the update holds."""

    def _release_with(self, fx, script: str) -> None:
        path = fx.origin / "host" / "istota-stack"
        path.parent.mkdir(parents=True)
        path.write_text(script)
        path.chmod(0o755)
        _git(fx.origin, "add", "host/istota-stack", env=fx.env)
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)

    def test_the_checkouts_script_runs_up_under_the_held_lock(self, fx):
        self._release_with(fx, RELEASE_STACK_STUB)
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert "release istota-stack: up inherited=1" in calls
        assert not any(" up -d " in f"{c} " for c in calls), calls

    def test_a_script_that_cannot_inherit_the_lock_is_not_asked_to(self, fx):
        """A rollback checks out an older script, which would wait on the lock."""
        self._release_with(fx, RELEASE_STACK_STUB.replace("# ISTOTA_STACK_LOCK_INHERITED\n", ""))
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert not any(c.startswith("release istota-stack") for c in calls)
        assert any(c.endswith("up -d --remove-orphans") for c in calls)

    def test_the_real_script_comes_up_without_waiting_on_its_parent(self, fx):
        self._release_with(fx, SCRIPT.read_text())
        result = fx.run("update", "v1", ISTOTA_STACK_LOCK_WAIT="2")
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(c.endswith("up -d --remove-orphans") for c in fx.docker_calls())

    def test_the_variable_alone_does_not_skip_a_held_lock(self, fx):
        held = HeldLock(fx.stack / ".stack.lock", "update")
        try:
            result = fx.run("up", ISTOTA_STACK_LOCK_WAIT="0", ISTOTA_STACK_LOCK_INHERITED="1")
        finally:
            held.release()
        assert result.returncode == 1, result.stdout + result.stderr
        assert "istota-stack update" in result.stderr
        assert fx.docker_calls() == []


def test_the_steps_without_the_devbox_file_ignore_its_running_containers(fx):
    """Without compose.devbox.yml, compose sees the devbox's containers as
    orphans and warns on every `run`; nothing in those steps removes orphans."""
    (fx.stack / "compose.devbox.yml").write_text('{"services": {"devbox-alice": {"image": "x"}}}\n')
    fx.commit("v1")
    fx.tag("v1", key=fx.release_key)
    result = fx.run("update", "v1")
    assert result.returncode == 0, result.stdout + result.stderr
    ignoring = Path(f"{fx.calls}.ignore-orphans").read_text().splitlines()
    assert any("devbox compose-file" in c for c in ignoring), ignoring
    assert not any("--remove-orphans" in c for c in ignoring), ignoring


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


class TestRestore:
    """The local VM's recovery, VM half: the master key on stdin, the config and
    the databases from the workspace's backups, in a one-shot container with the
    stack down (`istota migrate restore-workspace`)."""

    KEY = "5e" * 32

    def _restore(self, fx: Fixture, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), "restore", *args], capture_output=True, text=True,
            input=self.KEY + "\n", env={**fx.env, **extra_env}, timeout=60,
        )

    def test_it_refuses_while_the_stack_is_up(self, fx):
        result = self._restore(fx)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "running" in result.stderr
        assert not any(" run " in f"{call} " for call in fx.docker_calls()), fx.docker_calls()

    def test_the_key_reaches_the_one_shot_on_stdin_alone(self, fx):
        result = self._restore(fx, "--date", "2026-10-10", STUB_STACK_DOWN="1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        run = next(call for call in calls if "restore-workspace" in call)
        assert re.search(
            r"run --rm --no-deps -T --entrypoint istota-drop -v \S+/\.restore\.\w+:/vm "
            r"daemon istota migrate restore-workspace --vm-dir /vm --date 2026-10-10$", run), run
        assert Path(f"{fx.calls}.stdin").read_text() == self.KEY + "\n"
        assert not any(self.KEY in call for call in calls)
        assert (fx.stack / "config").is_dir()

    def test_its_exit_status_comes_back(self, fx):
        result = self._restore(fx, STUB_STACK_DOWN="1", STUB_RESTORE_RC="1")
        assert result.returncode == 1, result.stdout + result.stderr

    def test_without_an_image_nothing_runs(self, fx):
        (fx.stack / ".env").write_text("COMPOSE_PROFILES=\n")
        result = self._restore(fx, STUB_STACK_DOWN="1")
        assert result.returncode == 1, result.stdout + result.stderr
        assert not any(" run " in f"{call} " for call in fx.docker_calls())

    def test_the_stack_files_go_back_with_this_vms_tag(self, fx):
        """The backed-up .env names the old VM's tag, whose images this VM never
        built; every other line is the old install's."""
        result = self._restore(
            fx, STUB_STACK_DOWN="1",
            STUB_RESTORED_STACK_ENV="COMPOSE_PROFILES=browser\nNGINX_PUBLISH=127.0.0.1:8181:80\n"
                                    "ISTOTA_TAG=v-old\nISTOTA_PREVIOUS_TAG=v-older\n",
            STUB_RESTORED_HOST_ENV="STORAGE=host-folder\nINGRESS=local\nHOST_LOOPBACK_ALLOW=11434\n")
        assert result.returncode == 0, result.stdout + result.stderr
        env = fx.env_text()
        assert "COMPOSE_PROFILES=browser\n" in env
        assert "NGINX_PUBLISH=127.0.0.1:8181:80\n" in env
        assert "ISTOTA_TAG=v0\n" in env and "v-old" not in env and "v-older" not in env
        host = (fx.stack / "host.env").read_text()
        assert host == "STORAGE=host-folder\nINGRESS=local\nHOST_LOOPBACK_ALLOW=11434\n"
        assert not list(fx.stack.glob(".restore.*")), "the staging directory was left behind"

    def test_without_stack_files_the_vms_own_stay(self, fx):
        env_before = fx.env_text()
        host_before = (fx.stack / "host.env").read_text()
        result = self._restore(fx, STUB_STACK_DOWN="1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.env_text() == env_before
        assert (fx.stack / "host.env").read_text() == host_before

    def test_a_refused_restore_puts_nothing_in_place(self, fx):
        result = self._restore(
            fx, STUB_STACK_DOWN="1", STUB_RESTORE_RC="1",
            STUB_RESTORED_STACK_ENV="COMPOSE_PROFILES=browser\n")
        assert result.returncode == 1
        assert "COMPOSE_PROFILES=browser" not in fx.env_text()

    def test_a_profile_the_restored_env_turns_on_is_built(self, fx):
        result = self._restore(
            fx, STUB_STACK_DOWN="1", STUB_RESTORED_STACK_ENV="COMPOSE_PROFILES=browser\n",
            STUB_SERVICES="daemon\\nweb\\nnginx\\nbrowser\\n", STUB_MISSING_IMAGES="istota-browser:v0")
        assert result.returncode == 0, result.stdout + result.stderr
        builds = [call for call in fx.docker_calls() if " build " in f"{call} "]
        assert builds and builds[-1].endswith(" build browser"), builds


class TestImportStandalone:
    """A standalone install the Mac staged, its istota.env on stdin, in a
    one-shot container with the stack down (`istota migrate import-standalone`);
    the .env and host.env it stages go in place as setup's do."""

    ENV = "ISTOTA_SECRET_KEY=" + "6a" * 32 + "\n"

    def _import(self, fx: Fixture, source: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), "import-standalone", str(source), "--hostname", "localhost:8080", *args],
            capture_output=True, text=True, input=self.ENV, env={**fx.env, **extra_env}, timeout=60,
        )

    @pytest.fixture
    def source(self, tmp_path) -> Path:
        staged = tmp_path / "standalone"
        staged.mkdir()
        return staged

    def test_it_refuses_while_the_stack_is_up(self, fx, source):
        result = self._import(fx, source)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "running" in result.stderr
        assert not any(" run " in f"{call} " for call in fx.docker_calls()), fx.docker_calls()

    def test_the_env_file_reaches_the_one_shot_on_stdin_alone(self, fx, source):
        result = self._import(fx, source, STUB_STACK_DOWN="1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        run = next(call for call in calls if "import-standalone" in call)
        assert re.search(
            rf"run --rm --no-deps -T --entrypoint istota-drop -v {source}:/import:ro "
            rf"-v \S+/\.setup\.\w+:/vm -v {fx.stack}/secrets:/vm/secrets "
            r"daemon istota migrate import-standalone --source /import --vm-dir /vm "
            r"--hostname localhost:8080$", run), run
        assert Path(f"{fx.calls}.stdin").read_text() == self.ENV
        assert not any("6a" * 32 in call for call in calls)

    def test_the_staged_stack_files_go_in_place_with_this_vms_tag(self, fx, source):
        result = self._import(
            fx, source, STUB_STACK_DOWN="1",
            STUB_IMPORTED_ENV="COMPOSE_PROFILES=\nNGINX_PUBLISH=127.0.0.1:8080:80\nISTOTA_TAG=v0\n",
            STUB_IMPORTED_HOST_ENV="STORAGE=host-folder\nINGRESS=local\n")
        assert result.returncode == 0, result.stdout + result.stderr
        env = fx.env_text()
        assert "NGINX_PUBLISH=127.0.0.1:8080:80\n" in env and "ISTOTA_TAG=v0\n" in env
        assert (fx.stack / "host.env").read_text() == "STORAGE=host-folder\nINGRESS=local\n"
        assert not list(fx.stack.glob(".setup.*")), "the staging directory was left behind"

    def test_a_refused_import_puts_nothing_in_place(self, fx, source):
        env_before = fx.env_text()
        result = self._import(
            fx, source, STUB_STACK_DOWN="1", STUB_IMPORT_RC="1",
            STUB_IMPORTED_ENV="COMPOSE_PROFILES=browser\n")
        assert result.returncode == 1
        assert fx.env_text() == env_before

    def test_a_dry_run_puts_nothing_in_place(self, fx, source):
        env_before = fx.env_text()
        result = self._import(
            fx, source, "--dry-run", STUB_STACK_DOWN="1", STUB_IMPORTED_ENV="COMPOSE_PROFILES=browser\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.env_text() == env_before

    def test_a_missing_staging_directory_is_refused(self, fx, tmp_path):
        result = self._import(fx, tmp_path / "nowhere", STUB_STACK_DOWN="1")
        assert result.returncode == 1
        assert fx.docker_calls() == []


def _utc_today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


@pytest.fixture
def mounted(fx) -> Fixture:
    """A host-folder VM whose folder is mounted and marked."""
    bindir = Path(fx.env["PATH"].split(":")[0])
    (bindir / "mountpoint").write_text('#!/bin/bash\n[ -z "${STUB_NOT_MOUNTED:-}" ]\n')
    (bindir / "mountpoint").chmod(0o755)
    (fx.stack / "host.env").write_text("STORAGE=host-folder\nINGRESS=local\n")
    mount = Path(fx.env["ISTOTA_MOUNT_DIR"])
    mount.mkdir()
    (mount / ".istota-workspace").write_text("")
    return fx


class TestTheStackFilesAreCopiedOut:
    """`.env` and `host.env` are the VM's, outside anything db_backup reads, so
    istota-stack copies them into the workspace's `Backups/config/<date>/` on
    every verb that changes them: update, setup, apply. Only where the
    workspace is a mount, as db_backup's own copy."""

    def _copied(self, fx: Fixture) -> Path:
        return Path(fx.env["ISTOTA_MOUNT_DIR"]) / "Backups" / "config" / _utc_today()

    def test_apply_copies_both_private(self, mounted, tmp_path):
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = mounted.run("apply", str(plan))
        assert result.returncode == 0, result.stdout + result.stderr
        day = self._copied(mounted)
        assert (day / "stack.env").read_text() == mounted.env_text()
        assert (day / "host.env").read_text() == (mounted.stack / "host.env").read_text()
        assert stat.S_IMODE(day.stat().st_mode) == 0o700
        for name in ("stack.env", "host.env"):
            assert stat.S_IMODE((day / name).stat().st_mode) == 0o600, name

    def test_update_copies_them_with_the_new_tag(self, mounted):
        mounted.commit("v1")
        mounted.tag("v1", key=mounted.release_key)
        result = mounted.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in (self._copied(mounted) / "stack.env").read_text()

    def test_a_vm_with_no_config_keeps_the_days_set(self, mounted):
        """The Mac's restore runs `create --no-setup`, whose update runs on a
        new VM with no config: its tag-only `.env` and bare `host.env` must
        not replace the set the old VM copied in that day, which the restore
        is about to read."""
        (mounted.stack / "config" / "config.toml").unlink()
        day = self._copied(mounted)
        day.mkdir(parents=True)
        old_stack_env = "COMPOSE_PROFILES=browser\nNGINX_PUBLISH=127.0.0.1:8080\nISTOTA_TAG=v0\n"
        old_host_env = "STORAGE=host-folder\nHOST_LOOPBACK_ALLOW=11434\nCLAUDE_CODE_VERSION=2.1.296\n"
        (day / "stack.env").write_text(old_stack_env)
        (day / "host.env").write_text(old_host_env)
        mounted.commit("v1")
        mounted.tag("v1", key=mounted.release_key)
        result = mounted.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in mounted.env_text()
        assert (day / "stack.env").read_text() == old_stack_env
        assert (day / "host.env").read_text() == old_host_env

    def test_a_dry_run_apply_copies_them_too(self, mounted, tmp_path):
        """Nothing in either file changes under apply; the copy is what keeps
        the newest set within the snapshots' retention."""
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        assert mounted.run("apply", str(plan), "--dry-run").returncode == 0
        assert (self._copied(mounted) / "stack.env").is_file()

    @pytest.mark.parametrize("change", ["not-mounted", "local", "no-marker"])
    def test_nothing_is_copied_off_a_mount(self, mounted, tmp_path, change):
        env = {}
        if change == "not-mounted":
            env["STUB_NOT_MOUNTED"] = "1"
        elif change == "local":
            (mounted.stack / "host.env").write_text("STORAGE=local\n")
        else:
            (Path(mounted.env["ISTOTA_MOUNT_DIR"]) / ".istota-workspace").unlink()
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = mounted.run("apply", str(plan), **env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (Path(mounted.env["ISTOTA_MOUNT_DIR"]) / "Backups").exists()

    def test_a_link_in_the_path_is_not_followed_and_fails_nothing(self, mounted, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (Path(mounted.env["ISTOTA_MOUNT_DIR"]) / "Backups").symlink_to(elsewhere)
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        result = mounted.run("apply", str(plan))
        assert result.returncode == 0, result.stdout + result.stderr
        assert list(elsewhere.iterdir()) == []
        assert "WARNING" in result.stdout + result.stderr

    def test_setup_web_copies_them(self, web, tmp_path):
        bindir = Path(web.env["PATH"].split(":")[0])
        (bindir / "mountpoint").write_text("#!/bin/bash\nexit 0\n")
        (bindir / "mountpoint").chmod(0o755)
        mount = Path(web.env["ISTOTA_MOUNT_DIR"])
        mount.mkdir()
        (mount / ".istota-workspace").write_text("")
        result = web.run("setup", "--web", **_finished(
            STUB_STAGED_HOST_ENV="STORAGE=host-folder\nINGRESS=local\n"))
        assert result.returncode == 0, result.stdout + result.stderr
        day = mount / "Backups" / "config" / _utc_today()
        assert (day / "host.env").read_text() == "STORAGE=host-folder\nINGRESS=local\n"
        assert (day / "stack.env").read_text() == web.env_text()


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

    @pytest.mark.parametrize("storage,mount,fstype,marker", [
        ("host-folder", "/srv/mount/host-folder", "virtiofs", "1"),
        ("nextcloud", "/srv/mount/nextcloud/content", "fuse.rclone", ""),
        ("local", "/srv/mount/nextcloud/content", "", ""),
    ])
    def test_storage_decides_what_the_containers_are_told_about_the_mount(
        self, fx, storage, mount, fstype, marker,
    ):
        """What host.env's STORAGE means for the workspace bind: the directory
        on the VM, the filesystem type the entrypoint requires there, and
        whether the folder must carry `.istota-workspace`."""
        (fx.stack / "host.env").write_text(f"STORAGE={storage}\nINGRESS=local\n")
        env = {k: v for k, v in fx.env.items() if k != "ISTOTA_MOUNT_DIR"}
        result = subprocess.run(
            ["bash", "-c", f'source "{SCRIPT}"; docker() {{ echo "mount=${{ISTOTA_MOUNT_DIR}} '
             'fstype=${ISTOTA_WORKSPACE_FSTYPE-unset} marker=${ISTOTA_WORKSPACE_MARKER_REQUIRED-unset}"; }; '
             "compose ps"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"mount={mount} fstype={fstype} marker={marker}"

    def test_the_daemon_and_web_receive_both_from_compose(self):
        import yaml

        from istota.lib import workspace_marker

        services = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())["services"]
        for name in ("daemon", "web"):
            environment = services[name]["environment"]
            assert environment["ISTOTA_WORKSPACE_FSTYPE"] == "${ISTOTA_WORKSPACE_FSTYPE:-}", name
            assert environment[workspace_marker.REQUIRED_ENV] == (
                "${" + workspace_marker.REQUIRED_ENV + ":-}"), name


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

    @staticmethod
    def _staging(tmp_path: Path) -> Path:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / ".env").write_text("COMPOSE_PROFILES=\nISTOTA_TAG=v0\n")
        (staging / "host.env").write_text("STORAGE=nextcloud\nRCLONE_REMOTE=nextcloud\n")
        return staging

    def test_setup_installs_a_staged_rclone_conf_private(self, fx, tmp_path):
        staging = self._staging(tmp_path)
        conf = "[nextcloud]\ntype = webdav\npass = obscured\n"
        (staging / "rclone.conf").write_text(conf)
        chown = tmp_path / "bin" / "chown"
        chown.write_text('#!/bin/bash\necho "chown $*" >> "$STUB_CALLS"\n')
        chown.chmod(0o755)

        result = _source(fx, f'install_setup_staging "{staging}"')

        assert result.returncode == 0, result.stderr
        installed = fx.stack / "rclone.conf"
        assert installed.read_text() == conf
        assert (installed.stat().st_mode & 0o777) == 0o600
        assert "RCLONE_REMOTE=nextcloud" in (fx.stack / "host.env").read_text()
        # Handed back to the mount's user: provision.sh, which would also do
        # it, does not run when a re-run kept the same bytes (#720).
        assert f"chown 10001:10001 {installed}" in fx.calls.read_text().splitlines()

    def test_setup_without_a_staged_rclone_conf_leaves_the_existing_one(self, fx, tmp_path):
        staging = self._staging(tmp_path)
        (fx.stack / "rclone.conf").write_text("[mine]\n")

        result = _source(fx, f'install_setup_staging "{staging}"')

        assert result.returncode == 0, result.stderr
        assert (fx.stack / "rclone.conf").read_text() == "[mine]\n"

    def test_new_staging_carries_the_installed_rclone_conf(self, fx):
        """The wizard keeps an rclone.conf that says the same thing, so a
        re-run leaves `provision.sh --digest` unchanged (#720); it can only
        compare against the installed file if staging holds a copy."""
        (fx.stack / "rclone.conf").write_text("[nextcloud]\npass = obscured\n")

        result = _source(fx, 'new_staging; cat "${staging}/rclone.conf"; rm -rf "$staging"')

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[nextcloud]\npass = obscured\n"

    def test_a_staged_rclone_conf_that_is_a_link_is_not_followed(self, fx, tmp_path):
        secret = tmp_path / "shadow"
        secret.write_text("root:secret-hash:\n")
        staging = self._staging(tmp_path)
        (staging / "rclone.conf").symlink_to(secret)

        result = _source(fx, f'install_setup_staging "{staging}"')

        assert result.returncode != 0
        assert not (fx.stack / "rclone.conf").exists()

    def test_cmd_setup_installs_through_the_same_function(self):
        text = SCRIPT.read_text()
        setup = text.split("\ncmd_setup() {", 1)[1].split("\n}\n", 1)[0]
        assert 'install_setup_staging "$staging"' in setup


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

    def _marker(self, fx) -> dict:
        import json

        return json.loads((fx.stack / "config" / "managed.json").read_text())

    def test_the_managed_entries_are_written_for_the_resources_named(self, fx, secrets):
        import json

        config = {"managed": False, "source": "plan", "at": "2026-10-01T00:00:00Z"}
        (fx.stack / "config" / "managed.json").write_text(json.dumps({"config": config}))

        result = self._put(fx, {"secrets": {"anthropic_api_key": "sk-1"},
                                "managed": {"source": "ansible", "secrets": True, "admins": False}})

        assert result.returncode == 2, result.stdout + result.stderr
        marker = self._marker(fx)
        assert marker["config"] == config
        assert (marker["secrets"]["managed"], marker["secrets"]["source"]) == (True, "ansible")
        assert (marker["admins"]["managed"], marker["admins"]["source"]) == (False, "ansible")
        assert (fx.stack / "config" / "managed.json").stat().st_mode & 0o777 == 0o600

    def test_a_marker_only_change_answers_0(self, fx, secrets):
        """As for `apply`: who manages a file is not a change to it, so a role
        claiming an unchanged install does not restart the stack."""
        payload = {"secrets": {"anthropic_api_key": "sk-1"}}
        assert self._put(fx, payload).returncode == 2
        result = self._put(fx, dict(payload, managed={"source": "ansible", "secrets": True}))
        assert result.returncode == 0, result.stdout + result.stderr
        assert self._marker(fx)["secrets"]["managed"] is True
        assert "management marker" in result.stdout

    def test_the_same_marker_again_rewrites_nothing(self, fx, secrets):
        payload = {"secrets": {}, "managed": {"source": "ansible", "secrets": True}}
        self._put(fx, payload)
        marker = fx.stack / "config" / "managed.json"
        before = (marker.read_bytes(), marker.stat().st_mtime_ns)
        result = self._put(fx, payload)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before

    def test_the_dry_run_writes_no_marker(self, fx, secrets):
        result = self._put(fx, {"managed": {"source": "ansible", "secrets": True}}, "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (fx.stack / "config" / "managed.json").exists()

    @pytest.mark.parametrize("managed", [
        "ansible", {"source": 7, "secrets": True}, {"source": "x" * 65, "secrets": True},
        {"source": "ansible", "secrets": "yes"}, {"source": "ansible", "config": True},
    ])
    def test_a_bad_marker_is_refused_before_anything_is_written(self, fx, secrets, managed):
        result = self._put(fx, {"secrets": {"anthropic_api_key": "sk-1"}, "managed": managed})
        assert result.returncode == 1, result.stdout + result.stderr
        assert list(secrets.iterdir()) == []
        assert not (fx.stack / "config" / "managed.json").exists()

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


class TestHostEnv:
    """`istota-stack host-env`: how the Mac CLI writes the local VM's host.env
    keys (STORAGE, INGRESS, the loopback block) and edits the allow list."""

    def test_it_sets_keys_and_keeps_every_other_line(self, fx):
        (fx.stack / "host.env").write_text("# operator's note\nINGRESS=direct\nCLAUDE_CODE_VERSION=2.1.296\n")
        result = fx.run("host-env", "INGRESS=local", "HOST_LOOPBACK_ALLOW=1234 11434")
        assert result.returncode == 0, result.stderr
        assert (fx.stack / "host.env").read_text() == (
            "# operator's note\nINGRESS=local\nCLAUDE_CODE_VERSION=2.1.296\nHOST_LOOPBACK_ALLOW=1234 11434\n")

    def test_it_prints_one_value(self, fx):
        result = fx.run("host-env", "CLAUDE_CODE_VERSION")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "2.1.296\n"

    @pytest.mark.parametrize("arg", ["lower=1", "=1", "A-B=1", "lower"])
    def test_a_key_not_in_host_envs_shape_is_refused(self, fx, arg):
        before = (fx.stack / "host.env").read_text()
        result = fx.run("host-env", arg)
        assert result.returncode != 0
        assert (fx.stack / "host.env").read_text() == before

    def test_a_mix_of_reads_and_writes_is_a_usage_error(self, fx):
        assert fx.run("host-env", "INGRESS=local", "STORAGE").returncode == 64


class TestStaging:
    """`stage` and `unstage`: the Mac CLI's file arguments land in a fresh
    0700 directory under staging/, and only such a directory is removed."""

    def test_stage_moves_the_path_into_a_fresh_private_directory(self, fx, tmp_path):
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        (incoming / "plan.toml").write_text("[config]\n")
        result = fx.run("stage", str(incoming / "plan.toml"))
        assert result.returncode == 0, result.stderr
        staged = Path(result.stdout.strip())
        assert staged.name == "plan.toml"
        assert staged.parent.parent == fx.stack / "staging"
        assert staged.read_text() == "[config]\n"
        assert staged.parent.stat().st_mode & 0o777 == 0o700
        assert not (incoming / "plan.toml").exists()

    def test_a_link_is_not_staged(self, fx, tmp_path):
        link = tmp_path / "plan.toml"
        link.symlink_to("/etc/hosts")
        result = fx.run("stage", str(link))
        assert result.returncode != 0
        assert not (fx.stack / "staging").exists() or not any((fx.stack / "staging").iterdir())

    def test_unstage_removes_the_staged_directory(self, fx, tmp_path):
        source = tmp_path / "export"
        source.mkdir()
        staged = Path(fx.run("stage", "--daemon", str(source)).stdout.strip())
        assert staged.is_dir()
        result = fx.run("unstage", str(staged))
        assert result.returncode == 0, result.stderr
        assert not staged.parent.exists()
        assert (fx.stack / "staging").is_dir()

    @pytest.mark.parametrize("path", ["{stack}/config/config.toml", "{stack}/staging/x/../../config/x", "/tmp/x"])
    def test_unstage_removes_nothing_outside_staging(self, fx, path):
        (fx.stack / "staging" / "x").mkdir(parents=True)
        result = fx.run("unstage", path.format(stack=fx.stack))
        assert result.returncode != 0
        assert (fx.stack / "config" / "config.toml").exists()
        assert (fx.stack / "staging" / "x").is_dir()


class TestTheStackLock:
    """Every verb that writes takes one lock, `flock` on `.stack.lock`, so a
    config-apply cannot interleave with an update, a setup, a put-files or an
    import. A waiter gives up after a bound and names the holder's verb."""

    def _lock(self, fx) -> Path:
        return fx.stack / ".stack.lock"

    def test_the_holder_writes_its_verb(self, fx):
        result = fx.run("up")
        assert result.returncode == 0, result.stdout + result.stderr
        assert self._lock(fx).read_text().split() == ["up"]

    def test_a_verb_that_writes_waits_then_names_the_holder(self, fx):
        held = HeldLock(self._lock(fx), "update")
        try:
            result = fx.run("up", ISTOTA_STACK_LOCK_WAIT="1")
        finally:
            held.release()
        assert result.returncode == 1, result.stdout + result.stderr
        assert "istota-stack update" in result.stderr
        assert fx.docker_calls() == []

    def test_a_waiter_proceeds_once_the_holder_finishes(self, fx):
        import threading

        held = HeldLock(self._lock(fx), "update")
        timer = threading.Timer(0.5, held.release)
        timer.start()
        try:
            result = fx.run("up", ISTOTA_STACK_LOCK_WAIT="20")
        finally:
            timer.join()
            held.release()
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(call.endswith("up -d --remove-orphans") for call in fx.docker_calls())

    @pytest.mark.parametrize("args", [
        ("update",), ("rollback",), ("setup",), ("apply",), ("import-bare-metal",),
        ("restore",), ("import-standalone",), ("retire-bare-metal",), ("put-files",),
        ("localize-workspace",), ("up",), ("down",), ("host-env", "STORAGE=local"),
    ])
    def test_every_writing_verb_is_locked(self, fx, args):
        before = (fx.stack / "host.env").read_text()
        held = HeldLock(self._lock(fx), "update")
        try:
            result = fx.run(*args, ISTOTA_STACK_LOCK_WAIT="0")
        finally:
            held.release()
        assert result.returncode == 1, (args, result.stdout, result.stderr)
        assert "istota-stack update" in result.stderr, args
        assert (fx.stack / "host.env").read_text() == before, args

    @pytest.mark.parametrize("args", [("exec", "istota", "doctor"), ("compose", "ps"), ("check-image",),
                                      ("host-env", "INGRESS")])
    def test_a_verb_that_writes_nothing_takes_no_lock(self, fx, args):
        held = HeldLock(self._lock(fx), "update")
        try:
            result = fx.run(*args, ISTOTA_STACK_LOCK_WAIT="0")
        finally:
            held.release()
        assert result.returncode == 0, result.stdout + result.stderr

    def test_staging_takes_no_lock(self, fx, tmp_path):
        """stage and unstage make and remove a directory of their own; the verb
        the staged path is handed to takes the lock."""
        plan = tmp_path / "plan.toml"
        plan.write_text("[config]\n")
        held = HeldLock(self._lock(fx), "update")
        try:
            staged = fx.run("stage", str(plan), ISTOTA_STACK_LOCK_WAIT="0")
            assert staged.returncode == 0, staged.stdout + staged.stderr
            removed = fx.run("unstage", staged.stdout.strip(), ISTOTA_STACK_LOCK_WAIT="0")
        finally:
            held.release()
        assert removed.returncode == 0, removed.stdout + removed.stderr

    def test_a_restore_releases_the_lock_before_provisioning(self, fx):
        """restore runs provision.sh as a child, which may start
        istota-stack.service, whose `up` would wait on the lock restore holds."""
        script = fx.stack / "src" / "host" / "provision.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/bin/bash\n"
            'if [ "${1:-}" = "--digest" ]; then echo new; exit 0; fi\n'
            "python3 -c 'import fcntl, sys\n"
            "f = open(sys.argv[1], \"a\")\n"
            "try:\n"
            "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print(\"lock free\")\n"
            "except BlockingIOError:\n"
            "    print(\"lock held\")' \"$ISTOTA_STACK_DIR/.stack.lock\" >> \"$STUB_CALLS\"\n"
        )
        script.chmod(0o755)
        result = subprocess.run(
            ["bash", str(SCRIPT), "restore"], capture_output=True, text=True,
            input=TestRestore.KEY + "\n", env={**fx.env, "STUB_STACK_DOWN": "1"}, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "lock free" in fx.docker_calls(), fx.docker_calls()

    def test_an_update_releases_the_lock_before_provisioning(self, fx):
        """provision.sh, exec'd last, runs `istota-stack up` (and may restart
        istota-stack.service, whose ExecStart is `up`): with the lock still held
        through the inherited descriptor, that `up` would wait on its own parent."""
        script = fx.origin / "host" / "provision.sh"
        script.parent.mkdir(parents=True)
        script.write_text(
            "#!/bin/bash\n"
            'if [ "${1:-}" = "--digest" ]; then echo new; exit 0; fi\n'
            "python3 -c 'import fcntl, sys\n"
            "f = open(sys.argv[1], \"a\")\n"
            "try:\n"
            "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print(\"lock free\")\n"
            "except BlockingIOError:\n"
            "    print(\"lock held\")' \"$ISTOTA_STACK_DIR/.stack.lock\" >> \"$STUB_CALLS\"\n"
        )
        script.chmod(0o755)
        _git(fx.origin, "add", "host/provision.sh", env=fx.env)
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "lock free" in fx.docker_calls(), fx.docker_calls()


#: Arms the config-apply tests add in front of the docker stub's own: `take`
#: hands over the next request a test queued, as real files in the bound /out,
#: so no later step can be skipped vacuously; `finish` keeps the output it was
#: given; the dry run and the real apply answer separately.
CONFIG_APPLY_ARMS = r"""  *" config-request take "*)
    out=""; prev=""
    for a in "$@"; do
      if [ "$prev" = -v ]; then case "$a" in *:/out) out="${a%:/out}" ;; esac; fi
      prev="$a"
    done
    next="$(ls "$STUB_REQUESTS" 2>/dev/null | sort -n | head -n 1)"
    [ -n "$next" ] || exit 0
    ls -ld "$out" > "${STUB_CALLS}.staging-$next"
    for f in plan.json files.json claude-code; do cp "$STUB_REQUESTS/$next/$f" "$out/$f"; chmod 0400 "$out/$f"; done
    rm -rf "${STUB_REQUESTS:?}/$next"
    echo "$next"
    exit 0 ;;
  *" config-request finish "*)
    out=""; prev=""; id=""
    for a in "$@"; do
      if [ "$prev" = -v ]; then case "$a" in *:/out:ro) out="${a%:/out:ro}" ;; esac; fi
      if [ "$prev" = --id ]; then id="$a"; fi
      prev="$a"
    done
    cp "$out/output" "${STUB_CALLS}.output-$id"
    exit 0 ;;
  *" istota apply "*" --dry-run "*) exit "${STUB_DRY_RC:-0}" ;;
  *" istota apply "*" --backup "*)
    ls "$STUB_SECRETS" > "${STUB_CALLS}.secrets-at-apply"
    exit "${STUB_APPLY_RC:-0}" ;;
"""


class TestConfigApply:
    """`istota-stack config-apply`: the host half of the web config editor.

    Under the stack lock it loops: take a request in a one-shot, check
    everything (both dry runs), build the image if the new config needs Claude
    Code and the image lacks it, write credentials and admins through put-files
    and the config through `istota apply --backup`, record the outcome, restart.
    Nothing is written unless every check passed."""

    @pytest.fixture
    def ca(self, fx, tmp_path):
        stub = Path(fx.env["PATH"].split(":")[0]) / "docker"
        stub.write_text(
            DOCKER_STUB
            .replace('case "$args" in\n', 'case "$args" in\n' + CONFIG_APPLY_ARMS, 1)
            .replace('*" build "*) exit',
                     '*" build "*) echo "INSTALL_CLAUDE_CODE=${INSTALL_CLAUDE_CODE:-}" >> "${STUB_CALLS}.build-env"; exit')
        )
        queue = tmp_path / "queue"
        queue.mkdir()
        run = tmp_path / "run"
        run.mkdir()
        secrets = fx.stack / "secrets"
        secrets.mkdir(mode=0o700)
        fx.env.update(STUB_REQUESTS=str(queue), STUB_SECRETS=str(secrets),
                      ISTOTA_CONFIG_APPLY_RUN_DIR=str(run))
        fx.queue, fx.rundir, fx.secrets = queue, run, secrets
        return fx

    def _queue(self, fx, request_id: int, *, secrets=None, admins=None, claude_code=False):
        import json

        d = fx.queue / str(request_id)
        d.mkdir()
        brain = {"kind": "claude_code" if claude_code else "native"}
        (d / "plan.json").write_text(json.dumps(
            {"meta": {"managed": False, "source": "web", "request": request_id},
             "config": {"brain": brain}}))
        files = {}
        if secrets:
            files["secrets"] = secrets
        if admins is not None:
            files["admins"] = admins
        resources = {name: False for name in ("secrets", "admins") if name in files}
        if resources:
            files["managed"] = {"source": "web", **resources}
        (d / "files.json").write_text(json.dumps(files))
        (d / "claude-code").write_text("1\n" if claude_code else "0\n")

    def _index(self, calls, needle):
        return next(i for i, call in enumerate(calls) if needle in call)

    def test_the_order_and_the_outcome(self, ca):
        fx = ca
        self._queue(fx, 7, secrets={"istota_email_imap_password": "pw-7"}, claude_code=True)

        result = fx.run("config-apply", STUB_DRY_RC="2", STUB_APPLY_RC="2",
                        STUB_IMAGE_ENV="ISTOTA_CLAUDE_CODE_INSTALLED=0")

        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        take = self._index(calls, "config-request take --out /out")
        dry = self._index(calls, "istota apply -f /plan/plan.json --dry-run")
        mark = self._index(calls, "config-request mark --id 7 --status building")
        build = self._index(calls, " build ")
        apply = self._index(calls, "istota apply -f /plan/plan.json --backup 10")
        finish = self._index(calls, "config-request finish --id 7 --status applied --output-file /out/output")
        restart = next(i for i, call in enumerate(calls) if call.endswith(" restart"))
        assert take < dry < mark < build < apply < finish < restart, calls
        assert "--entrypoint istota-drop" in calls[apply]
        assert (fx.root / "docker-calls.build-env").read_text().split() == ["INSTALL_CLAUDE_CODE=1"]
        # put-files wrote the credential before the config was applied, and
        # never through the dry run.
        assert (fx.root / "docker-calls.secrets-at-apply").read_text().split() == ["istota_email_imap_password"]
        assert (fx.secrets / "istota_email_imap_password").read_text() == "pw-7"
        assert "pw-7" not in result.stdout + result.stderr
        output = (fx.root / "docker-calls.output-7").read_text()
        assert "would write istota_email_imap_password" in output
        assert "wrote istota_email_imap_password" in output
        assert "pw-7" not in output
        assert list(fx.rundir.iterdir()) == []

    def test_an_unchanged_config_records_unchanged_and_does_not_restart(self, ca):
        fx = ca
        self._queue(fx, 3)
        result = fx.run("config-apply")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any("config-request finish --id 3 --status unchanged" in call for call in calls), calls
        assert not any(call.endswith(" restart") for call in calls), calls
        assert not any(" build " in f"{call} " for call in calls), calls

    def test_no_build_when_the_image_already_has_claude_code(self, ca):
        fx = ca
        self._queue(fx, 4, claude_code=True)
        result = fx.run("config-apply", STUB_APPLY_RC="2",
                        STUB_IMAGE_ENV="ISTOTA_CLAUDE_CODE_INSTALLED=1\nISTOTA_CLAUDE_CODE_VERSION=2.1.296")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert not any(" build " in f"{call} " for call in calls), calls
        assert not any("--status building" in call for call in calls), calls
        assert any("config-request finish --id 4 --status applied" in call for call in calls), calls

    def test_a_refused_dry_run_writes_nothing_and_does_not_restart(self, ca):
        fx = ca
        self._queue(fx, 5, secrets={"istota_email_imap_password": "pw-5"}, admins="alice\n")
        result = fx.run("config-apply", STUB_DRY_RC="1", STUB_APPLY_RC="2")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any("config-request finish --id 5 --status refused" in call for call in calls), calls
        assert not any("--backup" in call for call in calls), calls
        assert not any(call.endswith(" restart") for call in calls), calls
        assert list(fx.secrets.iterdir()) == []
        assert not (fx.stack / "config" / "admins").exists()

    def test_a_refused_put_files_dry_run_writes_nothing(self, ca):
        fx = ca
        self._queue(fx, 6, secrets={"../escape": "x"})
        result = fx.run("config-apply", STUB_DRY_RC="2", STUB_APPLY_RC="2")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any("config-request finish --id 6 --status refused" in call for call in calls), calls
        assert not any("--backup" in call for call in calls), calls
        assert not any(call.endswith(" restart") for call in calls), calls

    def test_a_failed_build_writes_nothing(self, ca):
        fx = ca
        self._queue(fx, 8, secrets={"istota_email_imap_password": "pw-8"}, claude_code=True)
        result = fx.run("config-apply", STUB_DRY_RC="2", STUB_APPLY_RC="2", STUB_BUILD_RC="1",
                        STUB_IMAGE_ENV="ISTOTA_CLAUDE_CODE_INSTALLED=0")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any("--status building" in call for call in calls), calls
        assert any("config-request finish --id 8 --status refused" in call for call in calls), calls
        assert not any("--backup" in call for call in calls), calls
        assert not any(call.endswith(" restart") for call in calls), calls
        assert list(fx.secrets.iterdir()) == []
        assert "build" in (fx.root / "docker-calls.output-8").read_text()

    def test_a_failure_after_put_files_wrote_restarts(self, ca):
        fx = ca
        self._queue(fx, 9, secrets={"istota_email_imap_password": "pw-9"})
        result = fx.run("config-apply", STUB_DRY_RC="2", STUB_APPLY_RC="1")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert any("config-request finish --id 9 --status failed" in call for call in calls), calls
        assert any(call.endswith(" restart") for call in calls), calls
        assert (fx.secrets / "istota_email_imap_password").read_text() == "pw-9"

    def test_the_loop_takes_two_requests_in_one_run(self, ca):
        fx = ca
        self._queue(fx, 11)
        self._queue(fx, 12)
        result = fx.run("config-apply")
        assert result.returncode == 0, result.stdout + result.stderr
        finished = [call for call in fx.docker_calls() if "config-request finish" in call]
        assert [call.split("--id ")[1].split()[0] for call in finished] == ["11", "12"]
        takes = [call for call in fx.docker_calls() if "config-request take" in call]
        assert len(takes) == 3, takes

    def test_nothing_pending_ends_at_once(self, ca):
        fx = ca
        result = fx.run("config-apply")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert len([call for call in calls if "config-request take" in call]) == 1
        assert not any("istota apply" in call for call in calls), calls
        assert list(fx.rundir.iterdir()) == []

    def test_a_busy_lock_exits_0_without_a_container(self, ca):
        fx = ca
        self._queue(fx, 13)
        held = HeldLock(fx.stack / ".stack.lock", "update")
        try:
            result = fx.run("config-apply")
        finally:
            held.release()
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.docker_calls() == []
        assert (fx.queue / "13").is_dir()

    def test_before_setup_it_takes_no_lock_and_starts_nothing(self, ca):
        fx = ca
        (fx.stack / "config" / "config.toml").unlink()
        result = fx.run("config-apply")
        assert result.returncode == 0, result.stdout + result.stderr
        assert fx.docker_calls() == []
        assert not (fx.stack / ".stack.lock").exists()

    def test_staging_is_private_and_removed_when_a_step_fails(self, ca):
        fx = ca
        self._queue(fx, 14)
        result = fx.run("config-apply", STUB_DRY_RC="2", STUB_APPLY_RC="1")
        assert result.returncode == 0, result.stdout + result.stderr
        take = next(call for call in fx.docker_calls() if "config-request take" in call)
        staging = Path(take.split("-v ", 1)[1].split(":/out", 1)[0])
        assert staging.parent == fx.rundir
        assert staging.name.startswith("istota-config.")
        assert (fx.root / "docker-calls.staging-14").read_text().startswith("drwx------")
        assert list(fx.rundir.iterdir()) == []

    def test_staging_defaults_to_run(self, fx):
        result = _source(fx, 'echo "$CONFIG_APPLY_RUN"')
        assert result.stdout.strip() == "/run"

    def test_a_failed_take_stops_the_loop(self, ca):
        fx = ca
        stub = Path(fx.env["PATH"].split(":")[0]) / "docker"
        stub.write_text(stub.read_text().replace('  *" config-request take "*)\n',
                                                 '  *" config-request take "*)\n    exit 1\n', 1))
        self._queue(fx, 15)
        result = fx.run("config-apply")
        assert result.returncode == 1, result.stdout + result.stderr
        assert len([call for call in fx.docker_calls() if "config-request take" in call]) == 1
        assert list(fx.rundir.iterdir()) == []


class TestTheConfigApplyComposeFile:
    """The web container gets the requests bind and ISTOTA_CONFIG_APPLIER=host
    only from docker-compose.config-apply.yml, and istota-stack adds that file
    only on a VM provision.sh has made a requests directory on."""

    PROBE = 'docker() { echo "requests=${ISTOTA_REQUESTS_DIR-unset} $*"; }; compose ps'

    def test_it_is_added_only_when_the_requests_directory_exists(self, fx):
        shutil.copy(REPO / "docker" / "docker-compose.config-apply.yml", fx.src / "docker")

        without = _source(fx, self.PROBE)
        assert without.returncode == 0, without.stderr
        assert "docker-compose.config-apply.yml" not in without.stdout
        assert "requests=unset" in without.stdout

        (fx.stack / "requests").mkdir()
        with_dir = _source(fx, self.PROBE)
        assert with_dir.returncode == 0, with_dir.stderr
        assert f"-f {fx.src}/docker/docker-compose.config-apply.yml" in with_dir.stdout
        assert f"requests={fx.stack}/requests" in with_dir.stdout

    def test_a_checkout_without_the_file_is_not_given_it(self, fx):
        """A rollback to a release from before the applier."""
        (fx.stack / "requests").mkdir()
        result = _source(fx, self.PROBE)
        assert result.returncode == 0, result.stderr
        assert "config-apply" not in result.stdout

    def test_the_file_binds_the_requests_directory_into_web_alone(self):
        import yaml

        doc = yaml.safe_load((REPO / "docker" / "docker-compose.config-apply.yml").read_text())
        assert set(doc["services"]) == {"web"}
        web = doc["services"]["web"]
        assert web["environment"] == {"ISTOTA_CONFIG_APPLIER": "host"}
        assert len(web["volumes"]) == 1
        source, target = web["volumes"][0].rsplit(":", 1)
        assert target == "/data/requests"
        assert source.startswith("${ISTOTA_REQUESTS_DIR")

    def test_the_base_file_carries_no_applier(self):
        text = (REPO / "docker" / "docker-compose.yml").read_text()
        assert "ISTOTA_CONFIG_APPLIER" not in text
        assert "/data/requests" not in text

    def test_the_web_writes_where_the_bind_lands(self):
        from istota.config_editor import applier

        assert applier.DEFAULT_REQUESTS_DIR == "/data/requests"


class TestNeedsClaudeCode:
    """`needs_claude_code` reads a config path, or a plan's `config` table, so
    config-apply decides on the new config rather than the one on disk."""

    @pytest.mark.parametrize(("kind", "expected"), [("native", "0"), ("claude_code", "1")])
    def test_it_reads_a_plan(self, fx, tmp_path, kind, expected):
        import json

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"meta": {}, "config": {"brain": {"kind": kind}}}))
        result = _source(fx, f'needs_claude_code "{plan}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected

    def test_it_defaults_to_the_config_on_disk(self, fx):
        assert _source(fx, "needs_claude_code").stdout.strip() == "0"
        (fx.stack / "config" / "config.toml").write_text('[brain]\nkind = "claude_code"\n')
        assert _source(fx, "needs_claude_code").stdout.strip() == "1"

    def test_an_explicit_install_claude_code_wins_in_a_build(self, fx):
        result = _source(
            fx, 'docker() { case " $* " in *" build "*) echo "icc=$INSTALL_CLAUDE_CODE" ;; esac; }; '
                'INSTALL_CLAUDE_CODE=1 build_images v0')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "icc=1"


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
    CHECK = (
        "--entrypoint istota-drop daemon istota workspace localize --check "
        "--from /mnt/shared --to /data/workspace"
    )
    NOTE = "--entrypoint istota-drop daemon istota workspace moved-note --from /mnt/shared"

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
            self._index(calls, self.CHECK),
            self._index(calls, " stop daemon web"),
            self._index(calls, f"{self.COPY} --scheduler-stopped"),
            self._index(calls, "config read-only=false"),
            self._index(calls, self.NOTE),
            self._index(calls, "provision ran"),
            self._index(calls, "up -d --remove-orphans"),
            self._index(calls, "exec -T daemon istota-drop istota workspace reindex --forget-root /mnt/shared"),
        ]
        assert order == sorted(order), calls
        host_env = (fx.stack / "host.env").read_text()
        assert "STORAGE=local\n" in host_env
        assert "RCLONE_REMOTE=nextcloud\n" in host_env and "INGRESS=local\n" in host_env
        assert "config.toml.pre-localize" in result.stdout

    def test_the_lock_is_released_while_provision_runs_and_taken_back(self, mounted):
        """provision.sh can start istota-stack.service, whose `up` takes the
        stack lock: held through the move, that `up` would wait on its parent."""
        fx = mounted
        (fx.src / "host" / "provision.sh").write_text(
            "#!/bin/bash\n"
            "python3 -c 'import fcntl, sys\n"
            "f = open(sys.argv[1], \"a\")\n"
            "try:\n"
            "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print(\"provision ran, lock free\")\n"
            "except BlockingIOError:\n"
            "    print(\"provision ran, lock held\")' \"$ISTOTA_STACK_DIR/.stack.lock\" >> \"$STUB_CALLS\"\n"
        )
        result = fx.run("localize-workspace")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "provision ran, lock free" in fx.docker_calls(), fx.docker_calls()
        assert (fx.stack / ".stack.lock").read_text().split() == ["localize-workspace"]

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

    def test_a_destination_with_files_is_refused_before_anything_stops(self, mounted):
        """ISSUE-722: the empty-destination refusal came from the copy, after
        `compose stop`, so a VM with leftovers in /data/workspace was left down."""
        fx = mounted
        result = fx.run("localize-workspace", STUB_LOCALIZE_DEST_RC="1")
        assert result.returncode == 1
        assert "nothing was stopped" in result.stderr
        calls = fx.docker_calls()
        assert any(c.endswith(self.CHECK) for c in calls), calls
        assert not [c for c in calls if " stop " in f"{c} " or c.endswith("--scheduler-stopped")], calls
        assert "STORAGE=nextcloud\n" in (fx.stack / "host.env").read_text()

    def test_the_destination_check_is_asked_with_resume(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", "--resume")
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(c.endswith(f"{self.CHECK} --resume") for c in fx.docker_calls()), fx.docker_calls()

    def test_copy_only_checks_the_destination_and_writes_no_note(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", "--copy-only", STUB_LOCALIZE_DEST_RC="1")
        assert result.returncode == 1
        assert not [c for c in fx.docker_calls() if " stop " in f"{c} "], fx.docker_calls()
        fx.calls.unlink()
        result = fx.run("localize-workspace", "--copy-only")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not [c for c in fx.docker_calls() if "moved-note" in c], fx.docker_calls()

    @pytest.mark.parametrize("rc", ["1", "2", "3"])
    def test_a_copy_that_stops_writes_no_note(self, mounted, rc):
        """ISSUE-722: exit 2 wrote the note and then stopped before the config,
        so `istota-stack up` ran on the mount with a note saying edits there
        were ignored."""
        fx = mounted
        result = fx.run("localize-workspace", STUB_LOCALIZE_RC=rc)
        assert result.returncode == 1
        assert not [c for c in fx.docker_calls() if "moved-note" in c], fx.docker_calls()

    def test_no_note_skips_the_note(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", "--no-note")
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert "config read-only=false" in calls
        assert not [c for c in calls if "moved-note" in c or "--no-note" in c], calls

    def test_a_failed_note_does_not_stop_the_move(self, mounted):
        fx = mounted
        result = fx.run("localize-workspace", STUB_NOTE_RC="2")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING" in result.stdout + result.stderr
        assert "STORAGE=local\n" in (fx.stack / "host.env").read_text()
        assert "provision ran" in fx.docker_calls()

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


NATIVE_CONFIG = '[brain]\nkind = "native"\n'
STAGED_ENV = "COMPOSE_PROFILES=browser\nISTOTA_SECRETS_DIR=./secrets\n"
STAGED_HOST_ENV = "STORAGE=local\nINGRESS=local\nRCLONE_REMOTE=\n"
CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{5}(?:-[0-9A-HJKMNP-TV-Z]{5}){3}")


def _finished(**env: str) -> dict:
    """What a setup server that reached Finish leaves: the config, the staged
    env files and the marker."""
    return {"STUB_SETUP_DONE": "1", "STUB_CONFIG": NATIVE_CONFIG,
            "STUB_STAGED_ENV": STAGED_ENV, "STUB_STAGED_HOST_ENV": STAGED_HOST_ENV, **env}


@pytest.fixture
def web(fx) -> Fixture:
    """A VM after `update`, before any config. Ownership is root's to change,
    so `install -o` and `chown` are stubs that drop or record it; `ip` answers
    the route lookup; `systemctl` records."""
    bindir = Path(fx.env["PATH"].split(":")[0])
    real_install = shutil.which("install")
    (bindir / "install").write_text(
        "#!/bin/bash\nargs=()\n"
        'while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done\n'
        f'exec {real_install} "${{args[@]}}"\n'
    )
    (bindir / "chown").write_text('#!/bin/bash\necho "chown $*" >> "$STUB_CALLS"\n')
    (bindir / "systemctl").write_text('#!/bin/bash\necho "systemctl $*" >> "$STUB_CALLS"\n')
    (bindir / "ip").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  "-4 route get "*) [ -n "${STUB_IP4:-}" ] || exit 2; echo "1.1.1.1 via 10.0.0.1 dev eth0 src $STUB_IP4 uid 0" ;;\n'
        '  "-6 route get "*) [ -n "${STUB_IP6:-}" ] || exit 2; echo "2606:4700:4700::1111 from :: via fe80::1 dev eth0 src $STUB_IP6 metric 1024" ;;\n'
        "esac\n"
    )
    for name in ("install", "chown", "systemctl", "ip"):
        (bindir / name).chmod(0o755)
    (fx.stack / "config" / "config.toml").unlink()
    fx.env["STUB_IP4"] = "203.0.113.5"
    return fx


def _staged(fx: Fixture) -> Path:
    return Path(f"{fx.calls}.staging")


def _setup_call(fx: Fixture) -> str:
    return next(call for call in fx.docker_calls() if " setup --web " in f"{call} ")


def _x509(cert: Path, *args: str) -> str:
    return subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", *args],
                          capture_output=True, text=True, check=True).stdout


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@needs_openssl
class TestSetupWebRefuses:
    def test_a_config_in_place_is_refused_before_anything(self, web):
        (web.stack / "config" / "config.toml").write_text(NATIVE_CONFIG)
        result = web.run("setup", "--web")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "already set up" in result.stderr
        assert "istota-stack setup --force" in result.stderr
        assert web.docker_calls() == []
        assert not (web.stack / ".setup").exists()

    def test_force_does_not_go_with_web(self, web):
        result = web.run("setup", "--web", "--force")
        assert result.returncode == 64
        assert web.docker_calls() == []

    def test_a_second_run_is_refused_while_the_first_serves(self, web):
        result = web.run("setup", "--web", **_finished(STUB_DURING=f'bash "{SCRIPT}" setup --web'))
        assert result.returncode == 0, result.stdout + result.stderr
        during = Path(f"{web.calls}.during").read_text()
        assert "a setup is already running" in during and during.endswith("rc=1\n"), during
        # The refused run left the first one's files alone: it went on to finish.
        assert "systemctl enable --now istota-stack" in web.docker_calls()
        assert len([c for c in web.docker_calls() if " setup --web " in f"{c} "]) == 1

    def test_another_verb_holding_the_stack_lock_refuses_at_once(self, web):
        """A setup --web can hold the lock for an hour, so it never queues
        behind another verb either: it refuses straight away, naming it."""
        held = HeldLock(web.stack / ".stack.lock", "update")
        try:
            started = time.monotonic()
            result = web.run("setup", "--web", ISTOTA_STACK_LOCK_WAIT="30", **_finished())
            elapsed = time.monotonic() - started
        finally:
            held.release()
        assert result.returncode == 1, result.stdout + result.stderr
        assert "istota-stack update" in result.stderr
        assert elapsed < 15
        assert web.docker_calls() == []
        assert not (web.stack / ".setup").exists()

    def test_up_without_a_config_names_both_ways_in(self, web):
        result = web.run("up")
        assert result.returncode == 1
        assert "istota-stack setup --web" in result.stderr
        assert "systemctl start istota-setup" in result.stderr


@needs_openssl
class TestSetupWebMints:
    """Root mints the code and the certificate in its own .setup/, prints from
    there, and hands uid 10001 read-only copies in staging."""

    def test_the_code_and_certificate_are_root_s_and_the_copies_read_only(self, web):
        result = web.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr

        setup_dir = Path(f"{web.calls}.setup-dir")
        assert setup_dir.stat().st_mode & 0o777 == 0o700
        for name in ("code", "cert.pem", "key.pem", "instructions"):
            assert (setup_dir / name).stat().st_mode & 0o777 == 0o600, name
        code = (setup_dir / "code").read_text().strip()
        assert CODE.fullmatch(code), code

        staged = _staged(web)
        for name in ("setup-code", "cert.pem", "key.pem"):
            assert (staged / name).stat().st_mode & 0o777 == 0o400, name
        assert (staged / "setup-code").read_text().strip() == code
        assert (staged / "cert.pem").read_bytes() == (setup_dir / "cert.pem").read_bytes()
        calls = web.docker_calls()
        staging = re.search(r" -v (\S+):/vm ", _setup_call(web)).group(1)
        assert calls.index(f"chown -R 10001:10001 {staging}") < calls.index(_setup_call(web))
        assert calls.index(_setup_call(web)) < calls.index(f"chown root:root {staging}")

    def test_the_instructions_carry_the_address_code_and_fingerprint(self, web):
        result = web.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        setup_dir = Path(f"{web.calls}.setup-dir")
        code = (setup_dir / "code").read_text().strip()
        cert = setup_dir / "cert.pem"
        fingerprint = _x509(cert, "-fingerprint", "-sha256").split("=", 1)[1].strip()
        instructions = (setup_dir / "instructions").read_text()

        assert f"  https://203.0.113.5/istota/setup#code={code}\n" in instructions
        assert f"  {fingerprint}\n" in instructions
        assert f"Setup code (if you open the page some other way): {code}" in instructions
        assert instructions in result.stdout
        assert "CN=istota-setup" in _x509(cert, "-subject").replace(" ", "")
        assert "IP Address:203.0.113.5" in _x509(cert, "-ext", "subjectAltName")

    def test_a_domain_in_host_env_is_offered_and_named_in_the_certificate(self, web):
        (web.stack / "host.env").write_text("INGRESS=local\nDOMAIN=istota.example.com\n")
        assert web.run("setup", "--web", **_finished()).returncode == 0
        setup_dir = Path(f"{web.calls}.setup-dir")
        code = (setup_dir / "code").read_text().strip()
        assert f"https://istota.example.com/istota/setup#code={code}" in (setup_dir / "instructions").read_text()
        assert "DNS:istota.example.com" in _x509(setup_dir / "cert.pem", "-ext", "subjectAltName")

    def test_an_ipv6_only_machine_gets_a_bracketed_address(self, web):
        result = web.run("setup", "--web", **_finished(STUB_IP4="", STUB_IP6="2001:db8::5"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "https://[2001:db8::5]/istota/setup#code=" in result.stdout
        # An IPv4 publish would not answer the address it printed.
        assert " -p [::]:443:8443 " in _setup_call(web)

    def test_it_publishes_443_to_the_server_with_the_config_open(self, web):
        assert web.run("setup", "--web", **_finished()).returncode == 0
        call = _setup_call(web)
        assert re.search(
            r"run --rm --no-deps -T --entrypoint istota-drop -p 0\.0\.0\.0:443:8443 "
            rf"-v \S+/\.setup\.\w+:/vm -v {re.escape(str(web.stack))}/secrets:/vm/secrets "
            r"daemon istota setup --web --vm-dir /vm$", call), call
        assert "read-only=false" in web.docker_calls()

    def test_the_publish_address_can_be_moved(self, web):
        result = web.run("setup", "--web", **_finished(ISTOTA_SETUP_PUBLISH="0.0.0.0:8443"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert " -p 0.0.0.0:8443:8443 " in _setup_call(web)
        assert "https://203.0.113.5:8443/istota/setup#code=" in result.stdout

    def test_a_malformed_publish_address_is_refused(self, web):
        result = web.run("setup", "--web", ISTOTA_SETUP_PUBLISH="443")
        assert result.returncode == 1
        assert "ISTOTA_SETUP_PUBLISH" in result.stderr
        assert web.docker_calls() == []


class TestSetupWebLocal:
    def test_local_is_plain_http_on_loopback_with_no_certificate(self, web):
        result = web.run("setup", "--web", "--local", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        call = _setup_call(web)
        assert " -p 127.0.0.1:8080:8080 " in call
        assert call.endswith("daemon istota setup --web --vm-dir /vm --local")
        staged = _staged(web)
        assert (staged / "setup-code").exists()
        assert not (staged / "cert.pem").exists() and not (staged / "key.pem").exists()
        code = (staged / "setup-code").read_text().strip()
        assert f"  http://localhost:8080/istota/setup#code={code}\n" in result.stdout
        assert "fingerprint" not in result.stdout


@needs_openssl
class TestSetupWebUnfinished:
    """Without setup-done nothing reaches the host: the server may have stopped
    anywhere, and only the marker says the first admin exists."""

    def test_a_failed_run_changes_neither_env_file_and_cleans_up(self, web):
        env_before = web.env_text()
        host_before = (web.stack / "host.env").read_text()
        result = web.run("setup", "--web", STUB_SETUP_RC="1", STUB_CONFIG=NATIVE_CONFIG,
                         STUB_STAGED_ENV="COMPOSE_PROFILES=evil\n")
        assert result.returncode == 1, result.stdout + result.stderr
        assert web.env_text() == env_before
        assert (web.stack / "host.env").read_text() == host_before
        assert not (web.stack / ".setup").exists()
        assert list(web.stack.glob(".setup.*")) == []
        assert not any(call.startswith("systemctl") for call in web.docker_calls())
        assert "setup did not finish; nothing was installed on the host" in result.stdout
        assert f"{web.stack}/config/config.toml" in result.stdout
        assert "ISTOTA_SETUP_PUBLISH" in result.stdout

    def test_a_clean_exit_without_the_marker_is_still_a_failure(self, web):
        env_before = web.env_text()
        result = web.run("setup", "--web", STUB_SETUP_RC="0")
        assert result.returncode == 1
        assert web.env_text() == env_before

    def test_a_timeout_says_so_and_keeps_its_code(self, web):
        result = web.run("setup", "--web", STUB_SETUP_RC="3")
        assert result.returncode == 3
        assert "timed out" in result.stdout
        assert not (web.stack / ".setup").exists()

    def test_a_lockout_says_to_run_again_and_keeps_its_code(self, web):
        result = web.run("setup", "--web", STUB_SETUP_RC="4")
        assert result.returncode == 4
        assert "too many wrong setup codes" in result.stdout
        assert "istota-stack setup --web" in result.stdout
        assert not (web.stack / ".setup").exists()

    def test_a_secret_written_before_the_failure_is_named(self, web):
        secret = web.stack / "secrets" / "istota_brain_native_api_key"
        result = web.run("setup", "--web", STUB_SETUP_RC="1",
                         STUB_DURING=f'sleep 1; echo key > "{secret}"')
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"{secret} before it stopped" in result.stdout


@needs_openssl
class TestSetupWebFinished:
    def test_the_staged_files_are_installed_and_the_stack_started(self, web):
        result = web.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        assert web.env_text() == STAGED_ENV + "ISTOTA_TAG=v0\n"
        assert (web.stack / "host.env").read_text() == STAGED_HOST_ENV
        calls = web.docker_calls()
        assert calls.index("systemctl enable --now istota-stack") > calls.index(_setup_call(web))
        assert not (web.stack / ".setup").exists()
        assert list(web.stack.glob(".setup.*")) == []

    def test_a_linked_marker_is_not_a_finished_setup(self, web, tmp_path):
        """setup-done is uid 10001's; a link is not the file the server writes."""
        elsewhere = tmp_path / "marker"
        elsewhere.touch()
        result = web.run("setup", "--web", STUB_CONFIG=NATIVE_CONFIG, STUB_DURING=(
            f'ln -s "{elsewhere}" "$(ls -d "{web.stack}"/.setup.*)/setup-done"'))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "rc=0" in Path(f"{web.calls}.during").read_text()
        assert not any(call.startswith("systemctl") for call in web.docker_calls())

    def test_an_image_the_new_profile_needs_is_built_alone(self, web):
        result = web.run("setup", "--web", **_finished(
            STUB_SERVICES="daemon\\nweb\\nnginx\\nbrowser\\n", STUB_MISSING_IMAGES="istota-browser:v0"))
        assert result.returncode == 0, result.stdout + result.stderr
        calls = web.docker_calls()
        builds = [call for call in calls if " build " in f"{call} "]
        assert len(builds) == 1 and builds[0].endswith(" build browser"), builds
        assert calls.index(builds[0]) < calls.index("systemctl enable --now istota-stack")

    def test_claude_code_rebuilds_every_image(self, web):
        result = web.run("setup", "--web", **_finished(
            STUB_CONFIG='[brain]\nkind = "claude_code"\n',
            STUB_IMAGE_ENV="ISTOTA_CLAUDE_CODE_INSTALLED=0",
            STUB_SERVICES="daemon\\nweb\\nnginx\\nbrowser\\n", STUB_MISSING_IMAGES="istota-browser:v0"))
        assert result.returncode == 0, result.stdout + result.stderr
        builds = [call for call in web.docker_calls() if " build " in f"{call} "]
        assert len(builds) == 1 and builds[0].endswith(" build daemon browser"), builds

    def test_nothing_missing_builds_nothing(self, web):
        assert web.run("setup", "--web", **_finished()).returncode == 0
        assert not [call for call in web.docker_calls() if " build " in f"{call} "]

    def test_a_failed_build_stops_before_the_start(self, web):
        result = web.run("setup", "--web", **_finished(
            STUB_SERVICES="daemon\\nbrowser\\n", STUB_MISSING_IMAGES="istota-browser:v0", STUB_BUILD_RC="1"))
        assert result.returncode == 1
        assert "config is in place" in result.stderr
        assert not any(call.startswith("systemctl") for call in web.docker_calls())
        assert not (web.stack / ".setup").exists()

    @pytest.fixture
    def with_provision(self, web):
        script = web.src / "host" / "provision.sh"
        script.parent.mkdir()
        script.write_text(PROVISION_STUB)
        script.chmod(0o755)
        return web

    def test_changed_host_inputs_are_provisioned_before_the_start(self, with_provision):
        fx = with_provision
        (fx.stack / ".provisioned").write_text("digest-of-the-update\n")
        result = fx.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert calls.index("provision ran") < calls.index("systemctl enable --now istota-stack")

    def test_unchanged_host_inputs_are_not_provisioned_again(self, with_provision):
        fx = with_provision
        (fx.stack / ".provisioned").write_text(f"digest-{fx.head()}\n")
        result = fx.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        assert "provision ran" not in fx.docker_calls()
        assert "systemctl enable --now istota-stack" in fx.docker_calls()

    def test_a_failed_provision_does_not_start_the_stack(self, with_provision):
        fx = with_provision
        result = fx.run("setup", "--web", **_finished(STUB_PROVISION_RC="1"))
        assert result.returncode == 1
        assert "provision.sh failed" in result.stderr
        assert not any(call.startswith("systemctl") for call in fx.docker_calls())


#: Runs `istota-stack up` as the unit's ExecStart would, with no wait: an `up`
#: that inherited the caller's held lock fails at once instead of hanging.
STACK_UP_PROBE = f'ISTOTA_STACK_LOCK_WAIT=0 bash "{SCRIPT}" up >/dev/null 2>&1; echo "up rc=$?" >> "$STUB_CALLS"\n'


@needs_openssl
class TestSetupWebReleasesTheStackLock:
    """setup --web holds the stack lock, and both of its last steps start
    `istota-stack up`: provision.sh directly, `systemctl enable --now` through
    the unit. Neither may wait on the lock its caller still holds."""

    def test_before_the_unit_is_started(self, web):
        (Path(web.env["PATH"].split(":")[0]) / "systemctl").write_text(
            '#!/bin/bash\necho "systemctl $*" >> "$STUB_CALLS"\n'
            'if [ "$*" = "enable --now istota-stack" ]; then ' + STACK_UP_PROBE.rstrip("\n") + "; fi\n")
        result = web.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        calls = web.docker_calls()
        assert "up rc=0" in calls, calls
        assert calls.index("systemctl enable --now istota-stack") < calls.index("up rc=0")

    def test_before_provision_sh(self, web):
        script = web.src / "host" / "provision.sh"
        script.parent.mkdir()
        script.write_text(
            "#!/bin/bash\n"
            'if [ "${1:-}" = "--digest" ]; then echo new; exit 0; fi\n'
            'echo "provision ran" >> "$STUB_CALLS"\n' + STACK_UP_PROBE)
        script.chmod(0o755)
        result = web.run("setup", "--web", **_finished())
        assert result.returncode == 0, result.stdout + result.stderr
        calls = web.docker_calls()
        assert calls.index("provision ran") < calls.index("up rc=0"), calls
        assert calls.index("up rc=0") < calls.index("systemctl enable --now istota-stack")


@needs_openssl
class TestSetupCode:
    def test_with_no_setup_it_says_so(self, web):
        result = web.run("setup-code")
        assert result.returncode == 1
        assert "no setup in progress" in result.stderr

    def test_files_a_dead_run_left_are_not_a_setup_in_progress(self, web):
        setup_dir = web.stack / ".setup"
        setup_dir.mkdir()
        (setup_dir / "lock").touch()
        (setup_dir / "instructions").write_text("stale\n")
        result = web.run("setup-code")
        assert result.returncode == 1
        assert "no setup in progress" in result.stderr

    def test_during_a_setup_it_prints_the_instructions(self, web):
        result = web.run("setup", "--web", **_finished(STUB_DURING=f'bash "{SCRIPT}" setup-code'))
        assert result.returncode == 0, result.stdout + result.stderr
        during = Path(f"{web.calls}.during").read_text()
        instructions = Path(f"{web.calls}.setup-dir", "instructions").read_text()
        assert during == instructions + "rc=0\n"
