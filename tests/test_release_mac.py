"""The Mac app's release builder and the checks it runs on what it ships.

`scripts/release-artifacts.py` is the content check (row M7: no Claude Code or
Google Chrome in a release artifact), the builder-identity check on the disk,
the licence check and the package list. It runs here against fixture trees,
docker-save archives and sparse disk images, with and without a Claude Code
file. `scripts/build-appliance.sh` is checked for what it refuses before any VM
exists and held to the file list the Mac CLI reads from a release directory.
What it builds is the one real build per release, and the vm tier's appliance
mode boots it (`tests/vm/test_appliance.py`).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.support import parity

REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "scripts" / "release-artifacts.py"
BUILD = REPO / "scripts" / "build-appliance.sh"
MAC_CLI = REPO / "host" / "mac" / "istota-stack"

# Claude Code's licence sentence, assembled as the check assembles it so that
# no file in the checkout, which ships in the disk, carries it whole.
MARKER = " ".join(["© Anthropic PBC.", "All rights", "reserved.", "Use is subject to the Legal Agreements"])

DPKG_STATUS = (
    "Package: libc6\nStatus: install ok installed\nVersion: 2.41-12\n\n"
    "Package: gone\nStatus: deinstall ok config-files\nVersion: 1.0\n"
)


def check(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", "-I", str(CHECK), *args], capture_output=True, text=True, timeout=120)


def layer(files: dict[str, bytes]) -> bytes:
    """A gzipped layer tar holding `files`."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def archive(path: Path, images: dict[str, list[dict[str, bytes]]]) -> Path:
    """A `docker save` archive in the containerd store's layout: blobs plus manifest.json."""
    manifest, blobs = [], {}
    for ref, layers in images.items():
        paths = []
        for files in layers:
            blob = layer(files)
            name = f"blobs/sha256/{hashlib.sha256(blob).hexdigest()}"
            blobs[name] = blob
            paths.append(name)
        config = json.dumps({"ref": ref}).encode()
        config_name = f"blobs/sha256/{hashlib.sha256(config).hexdigest()}"
        blobs[config_name] = config
        manifest.append({"Config": config_name, "RepoTags": [ref], "Layers": paths})
    blobs["manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(path, "w") as tar:
        for name, data in blobs.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def clean_image() -> list[dict[str, bytes]]:
    return [
        {"var/lib/dpkg/status": DPKG_STATUS.encode(), "usr/bin/env": b"\x7fELF"},
        {
            "app/.venv/lib/python3.12/site-packages/httpx-0.28.1.dist-info/METADATA":
                b"Metadata-Version: 2.4\nName: httpx\nVersion: 0.28.1\nLicense-Expression: BSD-3-Clause\n",
            "usr/local/lib/node_modules/npm/node_modules/semver/package.json":
                json.dumps({"name": "semver", "version": "7.7.2", "license": "ISC"}).encode(),
            "app/docker/istota/Dockerfile": b'RUN npm install -g "@anthropic-ai/claude-code@${V}"\n',
        },
    ]


@parity.witness("M7")
class TestNoReleaseArtifactCarriesClaudeCodeOrChrome:
    def test_a_clean_tree_passes(self, tmp_path):
        (tmp_path / "Contents" / "MacOS").mkdir(parents=True)
        (tmp_path / "Contents" / "MacOS" / "Istota").write_bytes(b"\xcf\xfa\xed\xfe")
        assert check("tree", str(tmp_path)).returncode == 0

    @pytest.mark.parametrize("rel", [
        "Contents/Resources/node_modules/@anthropic-ai/claude-code/package.json",
        "lib/node_modules/@anthropic-ai/claude-code-linux-arm64/claude",
        "opt/google/chrome/chrome",
        "usr/bin/google-chrome-stable",
    ])
    def test_a_tree_with_either_is_refused(self, tmp_path, rel):
        path = tmp_path / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        result = check("tree", str(tmp_path))
        assert result.returncode == 1
        assert rel in result.stdout and result.stdout.startswith("M7: ")

    def test_claude_codes_licence_text_anywhere_in_a_tree_is_refused(self, tmp_path):
        (tmp_path / "renamed.bin").write_bytes(b"\0" * 5000 + MARKER.encode() + b"\0")
        result = check("tree", str(tmp_path))
        assert result.returncode == 1
        assert "renamed.bin: Claude Code's licence text" in result.stdout

    def test_a_clean_images_archive_passes_and_naming_the_package_is_not_shipping_it(self, tmp_path):
        tar = archive(tmp_path / "images.tar", {"istota:v1": clean_image()})
        result = check("images", str(tar))
        assert result.returncode == 0, result.stdout

    def test_the_npm_package_in_an_image_layer_is_refused(self, tmp_path):
        """The spec's control: Claude Code's npm package added to the archive."""
        layers = clean_image() + [{
            "usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json":
                json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.1.296",
                            "license": "SEE LICENSE IN README.md"}).encode(),
            "usr/local/lib/node_modules/@anthropic-ai/claude-code/LICENSE.md": MARKER.encode(),
        }]
        tar = archive(tmp_path / "images.tar", {"istota:v1": layers})
        result = check("images", str(tar))
        assert result.returncode == 1
        assert "Claude Code's npm package" in result.stdout
        assert "Claude Code's licence text" in result.stdout

    def test_chrome_in_an_image_layer_is_refused(self, tmp_path):
        layers = clean_image() + [{"opt/google/chrome/chrome": b"\x7fELF",
                                   "var/lib/dpkg/info/google-chrome-stable.list": b"/opt/google\n"}]
        tar = archive(tmp_path / "images.tar", {"istota-browser:v1": layers})
        result = check("images", str(tar))
        assert result.returncode == 1
        assert result.stdout.count("Google Chrome") == 2

    def test_the_disks_file_list_is_checked_by_path(self, tmp_path):
        listing = tmp_path / "paths.txt"
        # bash-completion ships a completion file named for Chrome on every Debian.
        listing.write_text("/usr/bin/env\n/srv/app/istota/src/docker/browser/chrome.py\n"
                           "/usr/share/bash-completion/completions/google-chrome-stable\n")
        assert check("paths", str(listing)).returncode == 0
        listing.write_text("/usr/bin/env\n/opt/google/chrome/chrome\n")
        result = check("paths", str(listing))
        assert result.returncode == 1 and "/opt/google/chrome/chrome: Google Chrome" in result.stdout

    def test_the_licence_text_in_the_disks_data_is_refused(self, tmp_path):
        raw = tmp_path / "disk.raw"
        with open(raw, "wb") as f:
            f.truncate(64 << 20)
            f.seek(40 << 20)
            f.write(MARKER.encode())
        result = check("disk", str(raw))
        assert result.returncode == 1
        at = (40 << 20) + MARKER.encode().index(b"Anthropic")
        assert f"Claude Code's licence text at byte {at}" in result.stdout


class TestTheDiskCarriesNoBuilderIdentity:
    def test_a_forbidden_term_is_found_and_named_by_its_label_alone(self, tmp_path):
        raw = tmp_path / "disk.raw"
        with open(raw, "wb") as f:
            f.truncate(32 << 20)
            # Across a read boundary of the scanner: the overlap must catch it.
            f.seek((8 << 20) - 3)
            f.write(b"/Users/someone-private/x")
        result = check("disk", str(raw), "--forbid", "the builder's home path=/Users/someone-private")
        assert result.returncode == 1
        assert result.stdout.startswith("identity: disk.raw: the builder's home path at byte")
        assert "someone-private" not in result.stdout

    def test_a_disk_without_them_passes(self, tmp_path):
        raw = tmp_path / "disk.raw"
        raw.write_bytes(b"debian\0" * 1000)
        assert check("disk", str(raw), "--forbid", "login=someone-private").returncode == 0

    def test_a_term_needs_a_label(self, tmp_path):
        assert check("disk", str(tmp_path), "--forbid", "someone").returncode == 64


class TestTheLicenceCheck:
    @pytest.mark.parametrize("files, refused", [
        ({"x/node_modules/left/package.json": {"name": "left", "version": "1", "license": "UNLICENSED"}},
         "UNLICENSED"),
        ({"x/node_modules/@acme/sdk/package.json": {"name": "@acme/sdk", "version": "1",
                                                    "license": "SEE LICENSE IN EULA.txt"}},
         "a licence of its own"),
        ({"x/node_modules/b/package.json": {"name": "b", "version": "1", "license": "BUSL-1.1"}},
         "Business Source"),
        ({"site-packages/acme-1.0.dist-info/METADATA":
          "Metadata-Version: 2.1\nName: acme\nVersion: 1.0\nClassifier: License :: Other/Proprietary License\n"},
         "proprietary"),
    ])
    def test_a_licence_that_forbids_redistribution_fails_the_release(self, tmp_path, files, refused):
        layer_files = {name: (json.dumps(doc) if isinstance(doc, dict) else doc).encode()
                       for name, doc in files.items()}
        tar = archive(tmp_path / "images.tar", {"istota-x:v1": clean_image() + [layer_files]})
        result = check("images", str(tar))
        assert result.returncode == 1
        assert result.stdout.startswith("licence: istota-x:v1: ") and refused in result.stdout

    def test_open_licences_and_whole_licence_texts_pass(self, tmp_path):
        apache_text = "Apache License\nVersion 2.0\n" + "any and all other commercial damages or losses\n" * 20
        layer_files = {
            "x/node_modules/pd/package.json": json.dumps({"name": "pd", "version": "1", "license": "Unlicense"}).encode(),
            "site-packages/long-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: long\nVersion: 1.0\nLicense: "
                + apache_text.replace("\n", "\n        ") + "\n").encode(),
            "site-packages/bare-2.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: bare\nVersion: 2.0\n",
        }
        tar = archive(tmp_path / "images.tar", {"istota-x:v1": clean_image() + [layer_files]})
        out = tmp_path / "packages.txt"
        result = check("images", str(tar), "--packages", str(out), "--tag", "v1")
        assert result.returncode == 0, result.stdout
        assert "bare 2.0 (none declared)" in out.read_text()


class TestThePackageList:
    def test_every_package_of_the_disk_and_each_image_with_its_licence(self, tmp_path):
        tar = archive(tmp_path / "images.tar", {
            "istota:v1": clean_image(),
            "nginx:alpine": [{"lib/apk/db/installed": b"P:musl\nV:1.2.5-r10\n\nP:nginx\nV:1.29.1-r0\n"}],
        })
        disk = tmp_path / "disk-packages.txt"
        disk.write_text("docker-ce 5:29.9.1-1~debian.13~trixie\n")
        out = tmp_path / "packages.txt"
        result = check("images", str(tar), "--packages", str(out), "--disk-packages", str(disk),
                       "--apt-snapshot", "20261016T000000Z", "--tag", "v1")
        assert result.returncode == 0, result.stdout
        text = out.read_text()
        assert "snapshot.debian.org at 20261016T000000Z" in text
        assert "## disk istota-appliance-v1.raw.zst (dpkg)\ndocker-ce 5:29.9.1-1~debian.13~trixie" in text
        assert "## image istota:v1 (dpkg)\nlibc6 2.41-12\n" in text
        assert "gone" not in text, "a removed package is listed"
        assert "httpx 0.28.1 BSD-3-Clause" in text
        assert "semver 7.7.2 ISC" in text
        assert "## image nginx:alpine (apk)\nmusl 1.2.5-r10\nnginx 1.29.1-r0" in text


# --- scripts/build-appliance.sh ------------------------------------------------


def build(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BUILD), *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, **(env or {})})


def signed_repo(tmp_path: Path, *, sign: bool = True, annotated: bool = True) -> tuple[Path, Path]:
    key = tmp_path / "release"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test-release", "-f", str(key)], check=True)
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    base = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "host" / "lima").mkdir(parents=True)
    (repo / "host" / "lima" / "istota.yaml").write_text("minimumLimaVersion: 2.0.0\n")
    subprocess.run([*base, "add", "-A"], check=True, env=env)
    subprocess.run([*base, "commit", "-q", "-m", "one"], check=True, env=env)
    if not annotated:
        subprocess.run([*base, "tag", "v9.9.9"], check=True, env=env)
    elif sign:
        subprocess.run([*base, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
                        "tag", "-s", "-m", "v9.9.9", "v9.9.9"], check=True, env=env)
    else:
        subprocess.run([*base, "tag", "-a", "-m", "v9.9.9", "v9.9.9"], check=True, env=env)
    return repo, key


needs_git_and_ssh = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None, reason="needs ssh-keygen and git")


class TestTheBuilderRefusesBeforeAnyVm:
    def test_the_default_lima_home_is_refused(self, tmp_path):
        result = build("--tag", "v9.9.9", "--key", str(tmp_path / "k"),
                       env={"ISTOTA_BUILD_LIMA_HOME": str(Path.home() / ".lima")})
        assert result.returncode == 1 and "default ~/.lima" in result.stderr

    @pytest.mark.parametrize("args", [[], ["--tag", "v1"], ["--tag", "../v1", "--key", "k"],
                                      ["--tag", "v1", "--key", "k", "--apt-snapshot", "yesterday"]])
    def test_usage(self, args):
        assert build(*args).returncode == 64

    @needs_git_and_ssh
    @pytest.mark.parametrize("sign, annotated, message", [
        (False, True, "is not signed by the key"),
        (True, False, "is not an annotated tag"),
    ])
    def test_a_tag_the_key_did_not_sign_builds_nothing(self, tmp_path, sign, annotated, message):
        repo, key = signed_repo(tmp_path, sign=sign, annotated=annotated)
        out = tmp_path / "out"
        result = build("--tag", "v9.9.9", "--key", str(key), "--repo", str(repo), "--out", str(out),
                       env={"ISTOTA_BUILD_LIMA_HOME": str(tmp_path / "lima")})
        assert result.returncode == 1 and message in result.stderr
        assert not (tmp_path / "lima").exists(), "a VM home was made for a refused tag"

    @needs_git_and_ssh
    def test_a_signed_tag_from_before_the_appliance_is_refused(self, tmp_path):
        repo, key = signed_repo(tmp_path)
        result = build("--tag", "v9.9.9", "--key", str(key), "--repo", str(repo), "--out", str(tmp_path / "out"),
                       env={"ISTOTA_BUILD_LIMA_HOME": str(tmp_path / "lima")})
        assert result.returncode == 1 and "predates the appliance" in result.stderr


class TestWhatTheBuilderWrites:
    text = BUILD.read_text()

    def test_it_writes_every_file_the_mac_cli_reads_from_a_release(self):
        cli = MAC_CLI.read_text()
        body = re.search(r"\nappliance_files\(\) \{\n(.*?)\n\}\n", cli, re.S)
        assert body, "host/mac/istota-stack's appliance_files moved"
        wanted = {name.replace("${tag}", "TAG") for name in re.findall(r'"([^"]*\$\{tag\}[^"]*)"', body.group(1))}
        assert len(wanted) == 5, wanted
        moved = set(re.search(r'for name in (.*?); do', self.text, re.S).group(1).replace("\\\n", " ").split())
        moved = {name.strip('"').replace("${tag}", "TAG") for name in moved}
        assert wanted <= moved, f"the release directory lacks {wanted - moved}"
        assert "packages-TAG.txt" in moved

    def test_the_images_are_the_ones_the_spec_ships(self):
        assert "INSTALL_CLAUDE_CODE=0" in self.text
        assert '--target base -t "istota-browser-base:${tag}"' in self.text
        assert 'refs=("istota:${tag}" "istota-browser-base:${tag}" "istota-whatsapp-baileys:${tag}" nginx:alpine)' in self.text

    def test_the_manifest_names_the_compressed_disk(self):
        # appliance_sha256 is the .raw.zst's digest; the Mac CLI checks exactly that file.
        assert '--appliance "${raw}.zst"' in self.text
        # The Mac CLI unpacks with --long=31; a wider window would not open.
        assert int(re.search(r"zstd .*--long=(\d+)", self.text).group(1)) <= 31

    def test_the_build_vm_runs_as_a_user_that_is_not_the_builder(self):
        assert '.user = {"name": "build"' in self.text
        # Read at runtime, and looked for in the forms a leaked account takes.
        assert 'login="$(id -un)"' in self.text and "home path=${HOME}" in self.text
        for form in ("=/home/${login}.", "=${login}:x:", "=${login} ALL="):
            assert form in self.text, form
