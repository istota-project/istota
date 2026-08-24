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

Two halves are held here, because the fix has two:

  * the host now repairs the volume from the Ansible role, which is the only
    side with a route to root;
  * the in-container path asks whether root is reachable *before* attempting
    anything, so the two failures — a `chown -R` that died half-way and one
    that could never start — keep the different words they need.

The shell half is executed rather than pattern-matched. `id`, `stat` and `sudo`
are stubbed so the control flow is deterministic and the test runs the same on
a machine with no `dev` account and no GNU `stat`; what is under test is the
branch order in `repair_home_ownership`, which is exactly what changed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO / "deploy" / "ansible" / "tasks" / "main.yml"
SUPERVISOR = REPO / "docker" / "devbox" / "scripts" / "istota-exec-run"

READ_TASK = "Read each devbox home volume's current ownership"
REPAIR_TASK = "Repair a devbox home volume whose ownership predates the daemon's uid"
REPORT_TASK = "Report a devbox home volume whose ownership could not be read"


# ---------------------------------------------------------------------------
# The Ansible half
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tasks() -> list[dict]:
    return yaml.safe_load(TASKS_FILE.read_text())


def _named(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r} in {TASKS_FILE}")


class TestTheHostRepairsTheVolume:
    def test_the_role_reads_each_volumes_ownership(self, tasks):
        assert _named(tasks, READ_TASK)

    def test_it_asks_before_mounting_so_a_fresh_install_gets_no_empty_volume(self, tasks):
        """`docker run -v` creates a missing volume.

        Creating one here would hand the first real container an empty volume
        that Docker has already stopped treating as new — losing the seed that
        gives a fresh install the right ownership for free.
        """
        shell = _named(tasks, READ_TASK)["ansible.builtin.shell"]
        assert "docker volume inspect" in shell
        assert "echo absent" in shell

    def test_the_probe_cannot_write(self, tasks):
        """A check that can modify what it checks is not a check."""
        shell = _named(tasks, READ_TASK)["ansible.builtin.shell"]
        assert ":/mnt:ro" in shell

    def test_the_repair_targets_the_daemons_own_uid(self, tasks):
        """Not a literal. The whole defect is a volume pinned to a stale uid,
        and a hardcoded 1000 here would reintroduce it from the other side."""
        argv = _named(tasks, REPAIR_TASK)["ansible.builtin.command"]["argv"]
        assert "chown" in argv
        assert "-R" in argv
        assert "{{ istota_devbox_uid }}:{{ istota_devbox_gid }}" in argv

    def test_the_repair_only_runs_on_a_volume_that_disagrees(self, tasks):
        """An unconditional recursive chown on every deploy would walk a
        multi-gigabyte volume for nothing, every time."""
        conditions = " ".join(str(c) for c in _named(tasks, REPAIR_TASK)["when"])
        assert "istota_devbox_uid ~ ':' ~ istota_devbox_gid" in conditions
        assert "absent" in conditions

    def test_the_repair_runs_as_root(self, tasks):
        """The container's own `dev` is precisely the identity that cannot do
        this; a repair inheriting the image's default user repeats the bug."""
        argv = _named(tasks, REPAIR_TASK)["ansible.builtin.command"]["argv"]
        assert "--user" in argv
        assert argv[argv.index("--user") + 1] == "0:0"

    def test_it_names_the_volume_the_compose_template_declares(self, tasks):
        """Two files, one name, and nothing held them together.

        The repair looks the volume up by name. If the template's `name:` ever
        changes, `docker volume inspect` fails, the read echoes `absent`, the
        repair is skipped and the play reports `ok` — reinstating the exact
        silently-does-nothing property this block was written to remove.
        """
        template = (
            REPO / "deploy" / "ansible" / "templates"
            / "docker-compose.devbox.yml.j2"
        ).read_text()
        declared = "name: {{ istota_namespace }}-devbox-home-{{ devbox_user }}"
        assert declared in template, "the compose template's volume name moved"

        read = _named(tasks, READ_TASK)["ansible.builtin.shell"]
        argv = _named(tasks, REPAIR_TASK)["ansible.builtin.command"]["argv"]
        # Same name, with the role's loop variable in place of the template's.
        assert "{{ istota_namespace }}-devbox-home-{{ item }}" in read
        assert any(
            "{{ istota_namespace }}-devbox-home-{{ item.item }}:/mnt" in str(a)
            for a in argv
        )

    def test_an_unreadable_probe_is_reported_rather_than_skipped_quietly(self, tasks):
        """Every failure mode of the probe used to land as empty stdout and
        skip. Failing closed is right; reporting `ok` while doing nothing is
        the thing this block exists to stop doing."""
        read = _named(tasks, READ_TASK)["ansible.builtin.shell"]
        assert "unreadable" in read

        conditions = " ".join(str(c) for c in _named(tasks, REPAIR_TASK)["when"])
        assert "unreadable" in conditions

        report = _named(tasks, REPORT_TASK)
        assert "unreadable" in " ".join(str(c) for c in report["when"])


# ---------------------------------------------------------------------------
# The in-container half, executed
# ---------------------------------------------------------------------------

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
