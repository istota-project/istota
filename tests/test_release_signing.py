"""Release tags are signed, and the release workflow builds the images as a check.

`host/istota-stack update` refuses any tag that is not SSH-signed by the key in
`/srv/istota/allowed_signers` (parity row 13), so `scripts/release.sh` must make
exactly that kind of tag, and the release workflow must build each image on each
architecture without pushing anything anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

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
