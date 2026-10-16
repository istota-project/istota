"""`istota-stack update <tag> --bundle FILE` and `trust-signer` (the Mac app spec's row M5).

The Mac app carries each release as a git bundle and installs it with no fetch
from origin. Only the fetch changes: the one tag comes out of the bundle, and
everything after it is the update `tests/test_istota_stack.py` holds, starting
with `verify_release_tag` against `allowed_signers`. So what a bundle may not
do is what a remote may not do: install an unsigned tag, a lightweight tag or a
tag another key signed, and it may not move a tag the checkout already has.
`--bundle` with `--unsigned` is refused outright, since the app never installs
an unsigned tag. Each refusal leaves `.env`, the checkout and Docker alone.

`trust-signer KEY` is the only way the release key changes on an existing
install: it prints the new key's fingerprint and replaces `allowed_signers` and
the host.env line provision.sh rewrites it from. Not claimed: protection from a
caller allowed to run it.

The fixture repository, the signing keys and the docker stub are
`tests/test_istota_stack.py`'s. The negative control is
`scripts/test-stack-negative-control.sh bundle-skip-verify`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_istota_stack as stack_tests
from tests.support import parity

# The same fixture: the origin repository, the stack directory, the keys and
# the recording docker stub.
fx = stack_tests.fx
Fixture = stack_tests.Fixture
_git = stack_tests._git

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="needs ssh-keygen and git",
)


def _bundle(fx: Fixture, tag: str) -> Path:
    """A bundle of the tag with its full history, as the release builder makes it."""
    path = fx.root / f"istota-{tag}.bundle"
    _git(fx.origin, "bundle", "create", str(path), f"refs/tags/{tag}", env=fx.env)
    return path


def _cut_off_origin(fx: Fixture) -> None:
    """No network: origin is a path that does not exist, so any fetch from it fails."""
    _git(fx.src, "remote", "set-url", "origin", str(fx.root / "no-such-origin"), env=fx.env)


def _touched(fx: Fixture) -> list[str]:
    return [call for call in fx.docker_calls() if " build" in call or " up " in f"{call} "]


def _assert_nothing_changed(fx: Fixture, result, before: tuple[str, str], rc: int = 65) -> None:
    assert result.returncode == rc, result.stdout + result.stderr
    assert fx.env_text() == before[0], ".env changed"
    assert fx.head() == before[1], "the checkout moved"
    assert _touched(fx) == [], f"reached Docker: {_touched(fx)}"


def _release(fx: Fixture, name: str, **tag_kwargs) -> Path:
    fx.commit(name)
    fx.tag(name, **tag_kwargs)
    bundle = _bundle(fx, name)
    _cut_off_origin(fx)
    return bundle


@parity.witness("M5")
class TestABundledTagIsVerified:
    @pytest.fixture
    def before(self, fx):
        return fx.env_text(), fx.head()

    def test_an_unsigned_tag_is_refused(self, fx, before):
        bundle = _release(fx, "v1")
        result = fx.run("update", "v1", "--bundle", str(bundle))
        _assert_nothing_changed(fx, result, before)
        assert "REFUSE" in result.stderr

    def test_a_lightweight_tag_is_refused(self, fx, before):
        bundle = _release(fx, "v1", annotated=False)
        result = fx.run("update", "v1", "--bundle", str(bundle))
        _assert_nothing_changed(fx, result, before)
        assert "lightweight" in result.stderr

    def test_a_tag_signed_by_another_key_is_refused(self, fx, before):
        bundle = _release(fx, "v1", key=fx.other_key)
        result = fx.run("update", "v1", "--bundle", str(bundle))
        _assert_nothing_changed(fx, result, before)
        assert "not signed by the key" in result.stderr

    def test_unsigned_is_refused_with_a_bundle(self, fx, before):
        bundle = _release(fx, "v1", key=fx.release_key)
        for args in (("--unsigned", "v1", "--bundle", str(bundle)),
                     ("v1", "--bundle", str(bundle), "--unsigned")):
            result = fx.run("update", *args)
            _assert_nothing_changed(fx, result, before)
            assert "--bundle" in result.stderr
        assert _git(fx.src, "tag", "--list", "v1", env=fx.env) == "", "the tag was fetched anyway"

    def test_a_tag_the_checkout_has_is_not_moved_by_the_bundle(self, fx, before):
        """A bundle naming an existing tag at another object would otherwise
        rewrite what that name installs, here and for a later rollback."""
        bundle = _release(fx, "v1", key=fx.release_key)
        _git(fx.src, "tag", "v1", "v0^{commit}", env=fx.env)
        kept = _git(fx.src, "rev-parse", "refs/tags/v1", env=fx.env)
        result = fx.run("update", "v1", "--bundle", str(bundle))
        _assert_nothing_changed(fx, result, before, rc=1)
        assert _git(fx.src, "rev-parse", "refs/tags/v1", env=fx.env) == kept

    def test_a_tag_the_bundle_lacks_changes_nothing(self, fx, before):
        bundle = _release(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v2", "--bundle", str(bundle))
        _assert_nothing_changed(fx, result, before, rc=1)
        assert "v2" in result.stderr

    def test_a_missing_bundle_changes_nothing(self, fx, before):
        _cut_off_origin(fx)
        result = fx.run("update", "v1", "--bundle", str(fx.root / "nothing.bundle"))
        _assert_nothing_changed(fx, result, before, rc=1)


class TestUpdateFromABundle:
    def test_a_signed_tag_installs_with_origin_unreachable(self, fx):
        commit = fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        bundle = _bundle(fx, "v1")
        _cut_off_origin(fx)
        signers = (fx.stack / "allowed_signers").read_text()
        result = fx.run("update", "v1", "--bundle", str(bundle))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "ISTOTA_TAG_UNSIGNED=\n" in fx.env_text()
        assert "ISTOTA_PREVIOUS_TAG=v0\n" in fx.env_text()
        assert fx.head() == commit
        assert any(" build " in f"{c} " for c in fx.docker_calls())
        assert (fx.stack / "allowed_signers").read_text() == signers, "an update rewrote allowed_signers"

    def test_the_option_may_come_first(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        bundle = _bundle(fx, "v1")
        _cut_off_origin(fx)
        result = fx.run("update", "--bundle", str(bundle), "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    @pytest.mark.parametrize("args", [("--bundle",), ("--bundle", "x.bundle"), ("v1", "v2", "--bundle", "x.bundle")])
    def test_a_bundle_needs_one_tag_and_a_file(self, fx, args):
        before = fx.env_text(), fx.head()
        result = fx.run("update", *args)
        assert result.returncode == 64, result.stdout + result.stderr
        assert (fx.env_text(), fx.head()) == before
        assert fx.docker_calls() == []

    def test_without_a_bundle_update_still_fetches_from_origin(self, fx):
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        result = fx.run("update", "v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()


def _fingerprint(pub: Path) -> str:
    out = subprocess.run(["ssh-keygen", "-l", "-f", str(pub)], capture_output=True, text=True, check=True)
    return out.stdout.split()[1]


class TestTrustSigner:
    def test_it_prints_the_fingerprint_and_replaces_the_release_key(self, fx):
        pubkey = fx.other_key.with_suffix(".pub").read_text().strip()
        (fx.stack / "host.env").write_text(
            "INGRESS=local\nRELEASE_SIGNING_KEY=ssh-ed25519 AAAAold releases\nCLAUDE_CODE_VERSION=2.1.296\n"
        )
        result = fx.run("trust-signer", pubkey)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _fingerprint(fx.other_key.with_suffix(".pub")) in result.stdout
        assert (fx.stack / "allowed_signers").read_text() == f'releases namespaces="git,istota-images" {pubkey}\n'
        host_env = (fx.stack / "host.env").read_text().splitlines()
        assert f"RELEASE_SIGNING_KEY={pubkey}" in host_env, "provision.sh would put the old key back"
        assert "INGRESS=local" in host_env and "CLAUDE_CODE_VERSION=2.1.296" in host_env

    def test_after_it_only_the_new_keys_tags_install(self, fx):
        assert fx.run("trust-signer", fx.other_key.with_suffix(".pub").read_text().strip()).returncode == 0
        fx.commit("v1")
        fx.tag("v1", key=fx.release_key)
        fx.commit("v2")
        fx.tag("v2", key=fx.other_key)
        _git(fx.src, "fetch", "-q", "--tags", "origin", env=fx.env)
        refused = fx.run("update", "v1")
        assert refused.returncode == 65, refused.stdout + refused.stderr
        installed = fx.run("update", "v2")
        assert installed.returncode == 0, installed.stdout + installed.stderr
        assert "ISTOTA_TAG=v2\n" in fx.env_text()

    @pytest.mark.parametrize("key", [
        "",
        "not a key",
        "ssh-ed25519 AAAA\nssh-ed25519 BBBB",
        'cert-authority ssh-ed25519 {pub}',
        'namespaces="file" ssh-ed25519 {pub}',
    ])
    def test_anything_but_one_public_key_changes_nothing(self, fx, key):
        pub = " ".join(fx.other_key.with_suffix(".pub").read_text().split()[:2])
        signers = (fx.stack / "allowed_signers").read_text()
        host_env = (fx.stack / "host.env").read_text()
        result = fx.run("trust-signer", key.format(pub=pub))
        assert result.returncode != 0, result.stdout + result.stderr
        assert (fx.stack / "allowed_signers").read_text() == signers
        assert (fx.stack / "host.env").read_text() == host_env

    def test_it_takes_one_key(self, fx):
        assert fx.run("trust-signer").returncode == 64
