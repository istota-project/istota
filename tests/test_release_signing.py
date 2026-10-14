"""Release tags are signed, and the release workflow builds the images as a check.

`host/istota-stack update` refuses any tag that is not SSH-signed by the key in
`/srv/app/istota/allowed_signers` (parity row 13), so `scripts/release.sh` must make
exactly that kind of tag, and the release workflow must build each image on each
architecture without pushing anything anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
RELEASE_SH = (REPO / "scripts" / "release.sh").read_text()
WORKFLOW = yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text())


def test_the_tag_is_signed_not_merely_annotated():
    tags = re.findall(r"^git tag .*$", RELEASE_SH, re.M)
    assert tags and all(" -s " in f"{line} " for line in tags), tags
    assert not any(" -a " in f"{line} " for line in tags)


def test_it_refuses_to_cut_without_an_ssh_signing_key():
    guard = RELEASE_SH.index('git config gpg.format')
    assert guard < RELEASE_SH.index("git tag -s")
    assert '!= "ssh"' in RELEASE_SH[guard:guard + 200]


def test_the_workflow_builds_every_image_on_both_architectures():
    job = WORKFLOW["jobs"]["build-images"]
    arches = {entry["arch"] for entry in job["strategy"]["matrix"]["include"]}
    assert arches == {"amd64", "arm64"}
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    for context in ("docker/istota/Dockerfile", "docker/devbox", "docker/whatsapp-baileys", "docker/browser"):
        assert context in runs, context


def test_nothing_is_pushed_or_logged_into():
    text = (REPO / ".github" / "workflows" / "release.yml").read_text()
    assert "docker push" not in text
    assert "push: true" not in text
    assert "docker/login-action" not in text and "docker login" not in text
    assert "cosign" not in text


def test_no_expression_is_interpolated_into_a_script():
    """`${{ inputs.tag }}` inside `run:` is pasted into the shell text before
    it runs, so whoever dispatches the workflow chooses a command. Every
    expression reaches a script through `env:` instead."""
    scripts = [
        step["run"]
        for job in WORKFLOW["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]
    assert scripts
    offenders = [script.splitlines()[0] for script in scripts if "${{" in script]
    assert offenders == [], offenders


def test_a_tag_it_signs_verifies_with_an_install_flags(tmp_path):
    """Sign a tag with the shipped lines and check it as `host/istota-stack` does.

    The notes come out of `$(...)` with no trailing newline, and git appends the
    signature straight after the message; 0.44.0 was cut with the signature
    glued to its last bullet, which `git verify-tag` cannot find.
    """
    import shutil
    import subprocess

    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen not installed")
    block = re.search(r'^TITLE="Release \$TAG"\n.*?\nfi\n', RELEASE_SH, re.M | re.S)
    assert block, "the tag-signing block in scripts/release.sh has moved"

    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pub = key.with_suffix(".pub").read_text().split()
    signers = tmp_path / "allowed_signers"
    signers.write_text(f"release {pub[0]} {pub[1]}\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": __import__("os").environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.invalid")
    git("config", "gpg.format", "ssh")
    git("config", "user.signingkey", str(key))

    script = 'TAG=v9.9.9\nCODENAME="Dumbo octopus"\nNOTES="### Fixed\n\n- A last bullet."\n' + block.group(0)
    r = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    body = git("cat-file", "tag", "v9.9.9").stdout
    assert "\n- A last bullet.\n-----BEGIN SSH SIGNATURE-----\n" in body
    assert body.splitlines()[5] == "Release v9.9.9 (Dumbo octopus)"
    verified = git(
        "-c", "gpg.format=ssh",
        "-c", f"gpg.ssh.allowedSignersFile={signers}",
        "-c", "gpg.openpgp.program=false",
        "-c", "gpg.x509.program=false",
        "verify-tag", "v9.9.9",
    )
    assert verified.returncode == 0, verified.stderr
