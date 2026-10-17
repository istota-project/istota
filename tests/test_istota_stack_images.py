"""`istota-stack update <tag> --images DIR` (the Mac app spec's row M10).

The Mac app carries each release's images as one `docker save` archive, so a VM
never builds them. The stack runs only images a manifest signed by the pinned
release key names: after the tag is verified, `images-<tag>.json` must carry a
good `istota-images` signature against `allowed_signers`, name this tag and the
archive's SHA-256, and every image `docker load` reports must be one the
manifest names, at the ID it names. Each refusal leaves `.env`, the checkout and
the running stack alone, and a refusal after the load puts back what the load
replaced.

`docker` is `tests/test_istota_stack.py`'s recording stub: `load -i` reports
the images `STUB_LOAD_IDS` names, and `image inspect --format {{.Id}}` answers
from them. `ssh-keygen` is real. The `image` tier half, a real archive loaded
and run, is not here.

The negative control is `scripts/test-stack-negative-control.sh images-skip-signature`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_istota_stack as stack_tests
from tests.support import parity

fx = stack_tests.fx
Fixture = stack_tests.Fixture
_git = stack_tests._git
REPO = stack_tests.REPO
SIGNER = REPO / "scripts" / "sign-images-manifest.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="needs ssh-keygen and git",
)

ISTOTA_ID = "sha256:" + "1" * 64
BROWSER_ID = "sha256:" + "2" * 64
NGINX_ID = "sha256:" + "3" * 64
OTHER_ID = "sha256:" + "9" * 64
ARCHIVE = b"a docker save archive\n"


def _images(tag: str) -> list[dict]:
    return [
        {"ref": f"istota:{tag}", "id": ISTOTA_ID},
        {"ref": f"istota-browser-base:{tag}", "id": BROWSER_ID},
        {"ref": "nginx:alpine", "id": NGINX_ID},
    ]


def _load_ids(images: list[dict]) -> str:
    return " ".join(f"{entry['ref']}={entry['id']}" for entry in images)


def _canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _sign(path: Path, key: Path, namespace: str = "istota-images") -> None:
    sig = path.with_name(path.name + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace, str(path)],
        check=True, capture_output=True,
    )


def _release_images(
    fx: Fixture, tag: str, *, key: Path | None = None, namespace: str = "istota-images",
    manifest_tag: str | None = None, archive_sha256: str | None = None,
    images: list[dict] | None = None, text: str | None = None,
) -> Path:
    """The release directory the app mounts: archive, manifest and signature."""
    directory = fx.root / "release" / tag
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"images-{tag}.tar").write_bytes(ARCHIVE)
    doc = {
        "tag": manifest_tag or tag,
        "archive_sha256": archive_sha256 or hashlib.sha256(ARCHIVE).hexdigest(),
        "images": images if images is not None else _images(tag),
    }
    manifest = directory / f"images-{tag}.json"
    manifest.write_text(text if text is not None else _canonical(doc))
    if key is not None:
        _sign(manifest, key, namespace)
    return directory


def _bundle(fx: Fixture, tag: str) -> Path:
    path = fx.root / f"istota-{tag}.bundle"
    _git(fx.origin, "bundle", "create", str(path), f"refs/tags/{tag}", env=fx.env)
    return path


def _signed_release(fx: Fixture, tag: str = "v1") -> Path:
    fx.commit(tag)
    fx.tag(tag, key=fx.release_key)
    return _bundle(fx, tag)


def _compose_builds(calls: list[str]) -> list[str]:
    """Builds of the release's images, as opposed to `local-layers`' `docker build`."""
    return [c for c in calls if c.startswith("compose ") and " build " in f" {c} "]


def _loads(fx: Fixture) -> list[str]:
    return [call for call in fx.docker_calls() if call.startswith("load ")]


def _assert_nothing_changed(fx: Fixture, result, before: tuple[str, str], rc: int = 65) -> None:
    assert result.returncode == rc, result.stdout + result.stderr
    assert fx.env_text() == before[0], ".env changed"
    assert fx.head() == before[1], "the checkout moved"
    started = [c for c in fx.docker_calls() if " build" in c or " up " in f"{c} "]
    assert started == [], f"reached the stack: {started}"


@parity.witness("M10")
class TestOnlyImagesTheSignedManifestNamesLoad:
    @pytest.fixture
    def before(self, fx):
        return fx.env_text(), fx.head()

    def _update(self, fx, release: Path, **env):
        bundle = _signed_release(fx)
        return fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                      STUB_LOAD_IDS=env.pop("load_ids", _load_ids(_images("v1"))), **env)

    def test_an_unsigned_manifest_is_refused(self, fx, before):
        result = self._update(fx, _release_images(fx, "v1"))
        _assert_nothing_changed(fx, result, before)
        assert "REFUSE" in result.stderr
        assert _loads(fx) == []

    def test_a_manifest_signed_by_another_key_is_refused(self, fx, before):
        result = self._update(fx, _release_images(fx, "v1", key=fx.other_key))
        _assert_nothing_changed(fx, result, before)
        assert "not signed by the key" in result.stderr
        assert _loads(fx) == []

    def test_a_signature_that_is_not_one_is_refused(self, fx, before):
        release = _release_images(fx, "v1", key=fx.release_key)
        (release / "images-v1.json.sig").write_text("-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n")
        result = self._update(fx, release)
        _assert_nothing_changed(fx, result, before)
        assert _loads(fx) == []

    def test_the_release_keys_git_signature_is_not_an_images_signature(self, fx, before):
        """The namespace is in what was signed: a tag signature cannot be
        passed off as a manifest signature."""
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key, namespace="git"))
        _assert_nothing_changed(fx, result, before)
        assert _loads(fx) == []

    def test_a_key_trusted_for_git_alone_does_not_sign_images(self, fx, before):
        """allowed_signers as every writer wrote it before this release."""
        signers = fx.stack / "allowed_signers"
        signers.write_text(signers.read_text().replace('namespaces="git,istota-images"', 'namespaces="git"'))
        assert 'namespaces="git" ' in signers.read_text()
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key))
        _assert_nothing_changed(fx, result, before)
        assert "istota-images" in result.stderr
        assert _loads(fx) == []

    def test_a_manifest_for_another_tag_is_refused(self, fx, before):
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key, manifest_tag="v2"))
        _assert_nothing_changed(fx, result, before)
        assert "v2" in result.stderr
        assert _loads(fx) == []

    def test_an_archive_with_another_digest_is_refused(self, fx, before):
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key, archive_sha256="0" * 64))
        _assert_nothing_changed(fx, result, before)
        assert "SHA-256" in result.stderr
        assert _loads(fx) == []

    def test_an_image_at_another_id_is_refused_and_removed(self, fx, before):
        loaded = _images("v1")
        loaded[0] = {"ref": "istota:v1", "id": OTHER_ID}
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key), load_ids=_load_ids(loaded))
        _assert_nothing_changed(fx, result, before)
        assert len(_loads(fx)) == 1
        removed = fx.docker_calls()
        for ref in ("istota:v1", "istota-browser-base:v1", "nginx:alpine"):
            assert f"image rm {ref}" in removed, removed

    def test_an_image_the_manifest_does_not_name_is_refused_and_removed(self, fx, before):
        loaded = _images("v1") + [{"ref": "", "id": OTHER_ID}]
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key), load_ids=_load_ids(loaded))
        _assert_nothing_changed(fx, result, before)
        assert f"image rm {OTHER_ID}" in fx.docker_calls()

    def test_a_refusal_after_the_load_puts_the_replaced_image_back(self, fx, before):
        """nginx:alpine is the running stack's: a refused archive must not
        leave the name on an image the manifest did not vouch for."""
        loaded = _images("v1")
        loaded[0] = {"ref": "istota:v1", "id": OTHER_ID}
        old_nginx = "sha256:" + "4" * 64
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key), load_ids=_load_ids(loaded),
                              STUB_PRESENT_IDS=f"nginx:alpine={old_nginx}")
        _assert_nothing_changed(fx, result, before)
        calls = fx.docker_calls()
        assert f"image tag {old_nginx} nginx:alpine" in calls, calls
        assert "image rm nginx:alpine" not in calls

    def test_an_istota_image_for_another_tag_is_refused(self, fx, before):
        """The previous tag's images are what rollback runs."""
        images = _images("v1") + [{"ref": "istota:v0", "id": OTHER_ID}]
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key, images=images))
        _assert_nothing_changed(fx, result, before)
        assert _loads(fx) == []

    def test_a_manifest_that_is_not_canonical_is_refused(self, fx, before):
        doc = {"tag": "v1", "archive_sha256": hashlib.sha256(ARCHIVE).hexdigest(), "images": _images("v1")}
        text = json.dumps(doc, indent=2)
        result = self._update(fx, _release_images(fx, "v1", key=fx.release_key, text=text))
        _assert_nothing_changed(fx, result, before)
        assert _loads(fx) == []

    def test_unsigned_is_refused_with_images(self, fx, before):
        release = _release_images(fx, "v1", key=fx.release_key)
        _signed_release(fx)
        result = fx.run("update", "--unsigned", "v1", "--images", str(release))
        _assert_nothing_changed(fx, result, before)
        assert "--images" in result.stderr

    def test_an_unsigned_tag_is_refused_before_the_images_are_read(self, fx, before):
        fx.commit("v1")
        fx.tag("v1")
        result = fx.run("update", "v1", "--bundle", str(_bundle(fx, "v1")),
                        "--images", str(_release_images(fx, "v1", key=fx.release_key)))
        _assert_nothing_changed(fx, result, before)
        assert _loads(fx) == []


class TestUpdateFromTheImagesArchive:
    def test_a_signed_release_installs_without_a_build(self, fx):
        commit = _signed_release(fx) and _git(fx.origin, "rev-parse", "v1^{commit}", env=fx.env)
        release = _release_images(fx, "v1", key=fx.release_key)
        signers = (fx.stack / "allowed_signers").read_text()
        result = fx.run("update", "v1", "--bundle", str(fx.root / "istota-v1.bundle"), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")))
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        assert _loads(fx) == [f"load -i {release / 'images-v1.tar'}"]
        assert _compose_builds(calls) == [], calls
        assert any(" up -d " in f"{c} " for c in calls), calls
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert fx.head() == commit
        assert (fx.stack / "allowed_signers").read_text() == signers

    def test_the_local_layers_are_built_once_the_stack_is_up(self, fx):
        """Claude Code and Chrome are no release's to carry: built on the VM
        after the stack runs the release's own images, and then run."""
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")), STUB_MISSING_IMAGES="istota-browser:v1",
                        STUB_IMAGE_ENV_CC=stack_tests.CC_ENV)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        first_up = next(i for i, c in enumerate(calls) if " up -d " in f"{c} ")
        layers = [i for i, c in enumerate(calls) if c.startswith("build ")]
        assert [calls[i].split(" -t ")[1].split()[0] for i in layers] == ["istota:v1-cc", "istota-browser:v1"]
        assert first_up < layers[0]
        assert any(" up -d " in f"{c} " for c in calls[layers[-1]:]), "not recreated on the layer"
        assert "ISTOTA_IMAGE=istota:v1-cc\n" in fx.env_text()

    @staticmethod
    def _desktop_with_browser(fx: Fixture) -> dict:
        host = fx.stack / "host.env"
        host.write_text(host.read_text() + "ISTOTA_DESKTOP_APP=1\n")
        return {"STUB_SERVICES": "daemon\nweb\nnginx\nbrowser\n", "STUB_MISSING_IMAGES": "istota-browser:v1"}

    @staticmethod
    def _ups(calls: list[str]) -> list[str]:
        return [c for c in calls if c.startswith("compose ") and " up " in f" {c} "]

    def test_the_app_never_builds_a_missing_image_on_up(self, fx):
        """Only istota-browser-base is in the archive. A compose `up` would
        build the whole browser image (a Chrome download) and fail offline
        with .env already on the new tag; the app's VM starts what it has,
        and the browser once its layer exists."""
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")), STUB_BUILD_FAIL="istota-browser:v1",
                        **self._desktop_with_browser(fx))
        assert result.returncode == 0, result.stdout + result.stderr
        ups = self._ups(fx.docker_calls())
        assert ups, fx.docker_calls()
        for up in ups:
            assert "--no-build" in up.split(), up
            assert "browser" not in up.split(), up
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    def test_the_browser_comes_up_once_its_layer_is_built(self, fx):
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")), STUB_BUILD_FAIL="istota:v1-cc",
                        **self._desktop_with_browser(fx))
        assert result.returncode == 0, result.stdout + result.stderr
        calls = fx.docker_calls()
        chrome = next(i for i, c in enumerate(calls) if c.startswith("build ") and "istota-browser:v1" in c)
        later = self._ups(calls[chrome:])
        assert later, "nothing started the browser after its layer"
        assert all("--no-build" in up.split() for up in later)
        assert "browser" in later[-1].split() or not any(
            s in later[-1].split() for s in ("daemon", "web", "nginx")), later[-1]

    def test_a_layer_that_fails_does_not_fail_the_update(self, fx):
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")), STUB_BUILD_FAIL="istota:v1-cc")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "local-layers --status" in result.stderr + result.stdout
        assert "ISTOTA_TAG=v1\n" in fx.env_text()
        assert "ISTOTA_IMAGE=\n" in fx.env_text()

    def test_no_layer_is_built_before_there_is_a_config(self, fx):
        """First launch: no network, and nothing is up to build on."""
        (fx.stack / "config" / "config.toml").unlink()
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")), STUB_MISSING_IMAGES="istota-browser:v1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not [c for c in fx.docker_calls() if c.startswith("build ")]

    def test_the_releases_own_script_is_told_not_to_build(self, fx):
        """The handoff: the release's istota-stack runs the rest of the update."""
        script = fx.origin / "host" / "istota-stack"
        script.parent.mkdir(parents=True)
        script.write_text(stack_tests.SCRIPT.read_text())
        script.chmod(0o755)
        _git(fx.origin, "add", "host/istota-stack", env=fx.env)
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(release),
                        STUB_LOAD_IDS=_load_ids(_images("v1")))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "handing the update" in result.stdout + result.stderr
        assert _compose_builds(fx.docker_calls()) == []
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    def test_the_options_may_come_in_any_order(self, fx):
        bundle = _signed_release(fx)
        release = _release_images(fx, "v1", key=fx.release_key)
        result = fx.run("update", "--images", str(release), "--bundle", str(bundle), "v1",
                        STUB_LOAD_IDS=_load_ids(_images("v1")))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    def test_a_missing_directory_changes_nothing(self, fx):
        before = fx.env_text(), fx.head()
        bundle = _signed_release(fx)
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(fx.root / "nothing"))
        _assert_nothing_changed(fx, result, before, rc=1)
        assert _loads(fx) == []

    @pytest.mark.parametrize("args", [("--images",), ("--images", "dir"), ("v1", "v2", "--images", "dir")])
    def test_images_need_one_tag_and_a_directory(self, fx, args):
        before = fx.env_text(), fx.head()
        result = fx.run("update", *args)
        assert result.returncode == 64, result.stdout + result.stderr
        assert (fx.env_text(), fx.head()) == before
        assert fx.docker_calls() == []


class TestTheSigningHelper:
    """`scripts/sign-images-manifest.sh` writes what `update --images` reads."""

    def _sign(self, fx, tag: str, *pairs: str, key: Path | None = None) -> subprocess.CompletedProcess:
        directory = fx.root / "built" / tag
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"images-{tag}.tar").write_bytes(ARCHIVE)
        return subprocess.run(
            ["bash", str(SIGNER), "--tag", tag, "--archive", str(directory / f"images-{tag}.tar"),
             "--key", str(key or fx.release_key), *pairs],
            capture_output=True, text=True, env=fx.env, timeout=60,
        )

    def test_its_manifest_is_canonical_and_installs(self, fx):
        bundle = _signed_release(fx)
        pairs = [f"{e['ref']}={e['id']}" for e in _images("v1")]
        signed = self._sign(fx, "v1", *pairs, "--claude-code-version", "2.1.296")
        assert signed.returncode == 0, signed.stdout + signed.stderr
        directory = fx.root / "built" / "v1"
        text = (directory / "images-v1.json").read_text()
        doc = json.loads(text)
        assert text == _canonical(doc)
        assert doc["tag"] == "v1"
        assert doc["archive_sha256"] == hashlib.sha256(ARCHIVE).hexdigest()
        assert doc["images"] == _images("v1")
        assert doc["claude_code_version"] == "2.1.296"
        result = fx.run("update", "v1", "--bundle", str(bundle), "--images", str(directory),
                        STUB_LOAD_IDS=_load_ids(_images("v1")))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ISTOTA_TAG=v1\n" in fx.env_text()

    @pytest.mark.parametrize("pair", ["istota:v1", "istota:v1=sha256:short", "=sha256:" + "1" * 64])
    def test_it_refuses_a_malformed_image(self, fx, pair):
        result = self._sign(fx, "v1", pair)
        assert result.returncode != 0
        assert not (fx.root / "built" / "v1" / "images-v1.json.sig").exists()

    def test_it_needs_an_image(self, fx):
        result = self._sign(fx, "v1")
        assert result.returncode != 0
