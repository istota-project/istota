"""`entrypoint.sh`'s two room-lookup helpers, run for real against a fake OCS.

These exist because the full tier found the pair broken and nothing else could
have. They are the whole of the recovery-by-name path — the one the script's own
comment describes as "safe to retry — existing rooms get reused, not
duplicated" — and until Stage 3 of the deployment-testbed spec they had no
witness at any layer: the image tier asserts only that `entrypoint.sh` parses,
the upgrade tier runs an older copy of it against a stub that returns one canned
room so `find_room_by_name` never matches, and `provision_rooms.py` (the Ansible
path's implementation of the same rule) is a different file asserted against
`MagicMock`.

**The bug they hold down.** Both helpers were written as

    python3 - "$arg" <<'PY' < "$body_file"

which sets stdin to the here-document and then redirects it to the file. The
last redirection wins, so `python3 -` read the *JSON body* as its program — and
a JSON object is a valid Python expression statement, so it evaluated, printed
nothing and exited 0. Both helpers therefore answered "nothing found" for every
input, silently and successfully. The visible effect was a boot that had lost
`/data/config/.api-provisioned` creating a second `#general`, `#logs` and
`#alerts` rather than adopting the existing three, forever, one set per boot.

**Why the functions are extracted rather than sourced.** `entrypoint.sh` runs
top-level code as soon as it is read — it writes `/data/config/admins` and then
polls for a provisioning flag for 600 seconds — so sourcing it outside a
container is not possible. `sed` between the exact function boundaries is, and
an extraction that stopped matching would leave the harness with no function to
call, which is a loud failure rather than a vacuous pass.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO / "docker" / "istota" / "entrypoint.sh"

BOT = "istota"
USER = "testuser"

#: What Talk answers for a bot that is in three group rooms and one 1:1. Room
#: tokens are Talk's own shape — eight lowercase alphanumerics.
ROOM_LIST = {
    "ocs": {
        "data": [
            {"token": "aaaa1111", "name": "general", "displayName": "general"},
            {"token": "bbbb2222", "name": "logs", "displayName": "logs"},
            {"token": "cccc3333", "name": "alerts", "displayName": "alerts"},
            {"token": "dddd4444", "name": USER, "displayName": USER},
        ]
    }
}

PARTICIPANTS = {
    "aaaa1111": [BOT, USER],
    "bbbb2222": [BOT, USER],
    # A room the bot is in on its own — the shape a failed invite leaves, and
    # the one the USER_NAME scoping exists to reject.
    "cccc3333": [BOT],
    "dddd4444": [BOT, USER],
}


def _extract(name: str) -> str:
    """One shell function out of `entrypoint.sh`, by its exact boundaries."""
    body = ENTRYPOINT.read_text()
    match = re.search(rf"^{name}\(\) \{{\n(?:.*?\n)*?^\}}$", body, re.M)
    assert match, (
        f"no {name}() found in {ENTRYPOINT}; this harness extracts the real "
        "function rather than reimplementing it, so a rename has to be followed "
        "here rather than silently passing against nothing"
    )
    return match.group(0)


@pytest.fixture
def harness(tmp_path):
    """A shell that has the two helpers and a `curl` that answers from fixtures.

    The `curl` shim reads the OCS path out of the argument list and writes the
    matching fixture to whatever `-o` names, which is exactly the contract the
    helpers depend on. It is a shim rather than a live server because the thing
    under test is the shell, not the HTTP.
    """
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "rooms.json").write_text(json.dumps(ROOM_LIST))
    for token, actors in PARTICIPANTS.items():
        (fixtures / f"{token}.json").write_text(
            json.dumps(
                {"ocs": {"data": [{"actorId": actor} for actor in actors]}}
            )
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        "out=''; url=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        '    http*) url="$1"; shift ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f'fixtures="{fixtures}"\n'
        'case "$url" in\n'
        '  */room/*/participants*) token="${url##*/room/}"; token="${token%%/*}";'
        ' cp "$fixtures/$token.json" "$out" ;;\n'
        '  */room?format=json*|*/room*) cp "$fixtures/rooms.json" "$out" ;;\n'
        '  *) : > "$out" ;;\n'
        "esac\n"
    )
    curl.chmod(0o755)

    script = tmp_path / "helpers.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'export PATH="{bin_dir}:$PATH"\n'
        f'BOT_USER="{BOT}"\nBOT_PASSWORD="not-a-real-password"\n'
        f'USER_NAME="{USER}"\nNC_URL="http://nextcloud"\n'
        + _extract("room_has_participant")
        + "\n"
        + _extract("find_room_by_name")
        + "\n"
    )

    def run(snippet: str) -> subprocess.CompletedProcess:
        if shutil.which("bash") is None:  # pragma: no cover - bash is universal
            pytest.skip("no bash")
        return subprocess.run(
            ["bash", "-c", f"source {script}\n{snippet}"],
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run


class TestRoomHasParticipant:
    def test_a_member_is_found(self, harness):
        result = harness(f'room_has_participant aaaa1111 "{USER}" && echo YES')

        assert result.returncode == 0, result.stderr
        assert "YES" in result.stdout

    def test_a_non_member_is_not(self, harness):
        result = harness(f'room_has_participant cccc3333 "{USER}" || echo NO')

        assert "NO" in result.stdout, result.stdout + result.stderr


class TestFindRoomByName:
    def test_an_existing_room_the_user_is_in_is_returned(self, harness):
        """The whole point of the recovery path: adopt, do not duplicate."""
        result = harness('find_room_by_name general')

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "aaaa1111"

    def test_a_room_the_user_is_not_in_is_skipped(self, harness):
        """A bot-only room with the right name is another deployment's, or a
        room whose invite failed. Returning it would bind this user's alerts
        channel to a room they cannot see."""
        result = harness('find_room_by_name alerts')

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    def test_an_unknown_name_returns_nothing_without_failing(self, harness):
        """`create_group_room` calls this and then branches on the empty string;
        a non-zero exit would abort the boot under `set -e`."""
        result = harness('find_room_by_name nosuchroom; echo "rc=$?"')

        assert "rc=0" in result.stdout, result.stdout + result.stderr


class TestTheRedirectionBugItself:
    def test_no_helper_feeds_a_file_into_a_heredoc_program(self):
        """A positive control on the shape, not on the behaviour.

        The behaviour tests above would catch a regression in these two
        functions. This catches the *next* one: `entrypoint.sh` has four
        `python3 - … <<'PY'` blocks and the pattern is easy to copy. A trailing
        `< "$file"` on that line makes python read the file as its program, and
        because a JSON body is valid Python the failure is silent and exits 0.
        """
        offenders = [
            line
            for line in ENTRYPOINT.read_text().splitlines()
            if "python3 -" in line and "<<" in line and re.search(r"<\s*\"\$", line)
        ]

        assert not offenders, offenders

    def test_the_detector_would_fire_on_the_old_form(self):
        """Because a scanner whose regex quietly stops matching reports clean."""
        old = """    candidates=$(python3 - "$room_name" <<'PY' < "$body_file" """

        assert "python3 -" in old and "<<" in old and re.search(r"<\s*\"\$", old)
