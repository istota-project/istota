"""The devbox home volume's ownership: who repairs it, and what it says when it can't.

Docker seeds a named volume from the image's own directory only while the
volume is *empty*. A `/home/dev` volume created before `DEV_UID` / `DEV_GID`
existed therefore keeps the uid the image had then — 1000, the old default —
while every rebuilt container's `dev` is the daemon's uid, and nothing re-seeds
it. The whole compose `environment:` block puts the package caches inside that
volume, so the symptom is every build in the devbox failing EACCES with nothing
anywhere naming a uid.

`istota-exec-run` has carried a self-healing `chown` for this since the volume
shape was designed, and on a live deployment it does nothing at all: it reaches
root through `sudo`, and `no-new-privileges` — set daemon-wide in
`/etc/docker/daemon.json` there — makes sudo refuse whatever the image's
sudoers grant says. Worse than nothing, in fact, because the failure path then
printed *the wrong diagnosis*: `could not put the top directory back; the next
start will read it as repaired and not retry`, when in truth the `chown -R`
never ran, the top directory was never touched, and the next start retries.

The fix had two halves. The host side repaired the volume from the Ansible
role, the only side with a route to root, and went with that role. The half
held here is the in-container path: it asks whether root is reachable *before*
attempting anything, so the two failures — a `chown -R` that died half-way and
one that could never start — keep the different words they need.

The shell half is executed rather than pattern-matched. `id`, `stat` and `sudo`
are stubbed so the control flow is deterministic and the test runs the same on
a machine with no `dev` account and no GNU `stat`; what is under test is the
branch order in `repair_home_ownership`, which is exactly what changed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR = REPO / "docker" / "devbox" / "scripts" / "istota-exec-run"


def _run_repair(tmp_path: Path, *, sudo_ok: bool) -> str:
    """Call `repair_home_ownership` against a stubbed environment, return stderr.

    The supervisor ends in an unconditional `main "$@"`, so the harness sources
    a copy with that final line removed and then overrides `HOME_DIR`. It is
    deliberately not made configurable in the real script: the comment above it
    records that a recursive chown wants a target nobody can move.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home-dev"
    home.mkdir()

    # `dev` is 4242:4242; whoever runs this script is 501, so the two disagree
    # and the repair branch is reached. Real `stat` is bypassed because BSD
    # `stat` has no `-c`.
    (bindir / "id").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "-u dev") echo 4242 ;;\n'
        '  "-g dev") echo 4242 ;;\n'
        '  -u) echo 501 ;;\n'
        '  -g) echo 501 ;;\n'
        '  *) echo 501 ;;\n'
        "esac\n"
    )
    (bindir / "stat").write_text("#!/bin/sh\necho 501\n")
    (bindir / "sudo").write_text(
        "#!/bin/sh\nexit %d\n" % (0 if sudo_ok else 1)
    )
    (bindir / "chown").write_text("#!/bin/sh\nexit 0\n")
    for stub in bindir.iterdir():
        stub.chmod(0o755)

    stripped = tmp_path / "supervisor.sh"
    body = SUPERVISOR.read_text().replace('\nmain "$@"\n', "\n")
    assert 'main "$@"' not in body, "the harness must not run the supervisor loop"
    stripped.write_text(body)

    driver = tmp_path / "driver.sh"
    driver.write_text(
        f". {stripped}\n"
        f'HOME_DIR="{home}"\n'
        "repair_home_ownership\n"
    )

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    proc = subprocess.run(
        ["sh", str(driver)], capture_output=True, text=True, env=env, timeout=60
    )
    return proc.stderr


class TestTheContainerSaysWhatItActuallyDid:
    def test_no_route_to_root_is_named_as_such(self, tmp_path):
        err = _run_repair(tmp_path, sudo_ok=False)
        assert "no route to root" in err
        assert "no-new-privileges" in err

    def test_it_does_not_claim_the_next_start_will_skip_the_repair(self, tmp_path):
        """The bug this file exists for. Nothing ran, so nothing needs undoing
        and the next start retries; the old text sent an operator looking for
        damage that was never done."""
        err = _run_repair(tmp_path, sudo_ok=False)
        assert "not retry" not in err

    def test_it_points_at_the_side_that_can_fix_it(self, tmp_path):
        err = _run_repair(tmp_path, sudo_ok=False)
        assert "from the host" in err
        assert "chown -R 4242:4242 /mnt" in err

    def test_a_reachable_root_still_takes_the_repair_path(self, tmp_path):
        """The control. Without it the assertions above pass just as well
        against a script that has stopped attempting the chown at all."""
        err = _run_repair(tmp_path, sudo_ok=True)
        assert "no route to root" not in err
        assert "chown" in err and "done" in err
