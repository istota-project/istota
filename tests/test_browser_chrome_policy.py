"""The managed Chrome policy the browser image installs.

Chrome's own save prompts open over the page, and an X11 click aimed at a field
under one lands on the prompt instead (#954). Istota keeps credentials and cards
itself, so Chrome is told not to offer to.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BROWSER_DIR = REPO / "docker" / "browser"
MANAGED_DIR = "/etc/opt/chrome/policies/managed/"


def _policy() -> dict:
    return json.loads((BROWSER_DIR / "chrome-policy.json").read_text())


def _installed_path() -> str:
    dockerfile = (BROWSER_DIR / "Dockerfile").read_text()
    found = re.findall(r"^COPY chrome-policy\.json (\S+)$", dockerfile, re.MULTILINE)
    assert len(found) == 1, found
    return found[0]


def test_chrome_offers_to_save_nothing():
    policy = _policy()
    assert policy["PasswordManagerEnabled"] is False
    assert policy["PasswordLeakDetectionEnabled"] is False
    assert policy["AutofillCreditCardEnabled"] is False
    assert policy["AutofillAddressEnabled"] is False


def test_the_egress_policy_is_kept():
    assert _policy()["WebRtcIPHandling"] == "disable_non_proxied_udp"


def test_the_image_installs_the_policy_where_chrome_reads_managed_policy():
    assert _installed_path().startswith(MANAGED_DIR)


def test_the_image_tier_moves_the_file_the_image_installs():
    # The egress guard's control arm takes the policy out of Chrome's reach by
    # path; a stale path there would leave the policy in place for both arms.
    script = (REPO / "tests" / "image" / "egress_guard_run.py").read_text()
    assert f'POLICY = "{_installed_path()}"' in script
