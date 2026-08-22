"""The pre-commit hook's decision to run, warn, or refuse (ISSUE-291).

Two scanners guard a commit, and each can be absent. Absence used to print a
warning and let the commit through — right for a human at a workstation, wrong
for the unattended agent commits made on the deployment, which is the one
configuration where nothing installs gitleaks and nobody reads the warning.

So the hook now has a second axis besides "did the scan find something": can
the scan run at all, and does that matter here. Both are asserted, because both
failed silently before: the deployment ran for weeks with the credential half
of the gate inactive, and the only trace was a line of hook output nobody saw.

The third case is subtler and is why the hook probes for a capability rather
than a version. `gitleaks git` replaced `detect`/`protect` in 8.19 and Debian
13 ships 8.16, so the obvious `apt install gitleaks` puts a binary on the host
that exits nonzero for an unknown subcommand — indistinguishable, to a hook
reading only the exit status, from a real finding. That would turn a missing
scanner into a hook that refuses every commit and blames a secret that was
never there.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".githooks" / "pre-commit"

# A usable gitleaks: `git --help` succeeds, so the hook probe passes, and the
# scan reports whatever FAKE_GITLEAKS_EXIT says.
STUB_USABLE = """#!/usr/bin/env bash
if [ "$1" = "git" ] && [ "$2" = "--help" ]; then
  exit 0
fi
if [ "$1" = "git" ]; then
  echo "$*" >> "$FAKE_GITLEAKS_LOG"
  exit "${FAKE_GITLEAKS_EXIT:-0}"
fi
exit 1
"""

# What Debian 13's 8.16 does: no `git` subcommand, so cobra errors and exits 1
# for every invocation of it, `--help` included.
STUB_TOO_OLD = """#!/usr/bin/env bash
if [ "$1" = "detect" ] || [ "$1" = "protect" ] || [ "$1" = "version" ]; then
  exit 0
fi
echo 'Error: unknown command "'"$1"'" for "gitleaks"' >&2
exit 1
"""


@pytest.fixture
def repo(tmp_path):
    """A git repo with one staged file and the hook's own scripts in place.

    The private-data scanner is copied in so that half of the hook behaves as
    it does in a real checkout; the tests below vary the gitleaks half.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, value in (("user.email", "hook-test"), ("user.name", "hook-test")):
        subprocess.run(["git", "-C", str(tmp_path), "config", name, value], check=True)
    for artifact in (".private-data-patterns", "scripts/check-private-data.sh"):
        dest = tmp_path / artifact
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / artifact, dest)
    (tmp_path / "scripts" / "check-private-data.sh").chmod(0o755)
    (tmp_path / "sample.txt").write_text("nothing to see here\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def run_hook(repo: Path, *, stub: str | None = None, **env_overrides):
    """Run the hook in ``repo`` with a PATH that contains only what we put there.

    The stub directory comes first and the rest of PATH is trimmed to the system
    directories, so "gitleaks is not installed" is a real condition on a machine
    that has it installed — which is every machine that runs this suite.

    Trimming PATH also drops the bash a developer machine installed alongside
    gitleaks, and `/bin/bash` is 3.2 on macOS, which has no `mapfile` — the
    private-data scanner needs it. So the interpreter is linked into the stub
    directory rather than left to a PATH lookup that now resolves elsewhere.
    """
    bin_dir = repo / "_stub_bin"
    bin_dir.mkdir(exist_ok=True)
    bash = Path(shutil.which("bash") or "/bin/bash")
    link = bin_dir / "bash"
    if not link.exists():
        link.symlink_to(bash)
    if stub is not None:
        target = bin_dir / "gitleaks"
        target.write_text(stub)
        target.chmod(0o755)

    env = dict(os.environ)
    for marker in ("DEVELOPER_REPOS_DIR", "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"):
        env.pop(marker, None)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    if stub is None:
        # State the precondition rather than assume it. On a host with gitleaks
        # in /usr/bin — which is exactly the Debian configuration this change is
        # about — the "not installed" cases would otherwise pass through the
        # "too old" branch, or go red for a reason that is not the code's.
        assert shutil.which("gitleaks", path=env["PATH"]) is None, (
            "a gitleaks on the trimmed PATH makes the 'not installed' cases "
            "test something other than what they name"
        )
    env["FAKE_GITLEAKS_LOG"] = str(repo / "gitleaks.log")
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    return subprocess.run(
        [str(bash), str(HOOK)],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
    )


class TestTheHookStillRunsTheScan:
    """The premise of everything below: a usable gitleaks is actually invoked."""

    def test_a_clean_tree_with_a_usable_gitleaks_passes(self, repo):
        result = run_hook(repo, stub=STUB_USABLE)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_scan_is_invoked_with_staged(self, repo):
        run_hook(repo, stub=STUB_USABLE)
        assert "--staged" in (repo / "gitleaks.log").read_text()

    def test_a_finding_aborts_the_commit(self, repo):
        result = run_hook(repo, stub=STUB_USABLE, FAKE_GITLEAKS_EXIT="1")
        assert result.returncode == 1
        assert "exited nonzero" in result.stdout

    def test_an_agent_commit_with_a_working_gitleaks_goes_through(self, repo):
        """The path the deployment actually runs, and the one this change could
        most easily wedge: the marker set, the scanner present and healthy."""
        result = run_hook(
            repo, stub=STUB_USABLE, DEVELOPER_REPOS_DIR="/srv/repos"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "--staged" in (repo / "gitleaks.log").read_text()

    def test_the_scan_is_not_attempted_when_the_probe_fails(self, repo):
        """A refusal has to mean the scan did not run, not that it ran and was
        ignored — otherwise the log below would show a scan nobody read."""
        run_hook(repo, stub=STUB_TOO_OLD, DEVELOPER_REPOS_DIR="/srv/repos")
        assert not (repo / "gitleaks.log").exists()


class TestAMissingGitleaksFailsClosedForAnAgent:
    """ISSUE-291. The deployment had no gitleaks and committed anyway."""

    def test_a_human_gets_a_warning_and_the_commit_proceeds(self, repo):
        result = run_hook(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING" in result.stdout

    def test_an_agent_commit_is_refused(self, repo):
        """`DEVELOPER_REPOS_DIR` is exported into the shell the developer skill
        runs git in, and nothing else sets it, so it identifies the unattended
        case without any deployment-side configuration."""
        result = run_hook(repo, DEVELOPER_REPOS_DIR="/srv/repos")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "ERROR" in result.stdout

    def test_the_refusal_says_the_scan_did_not_run(self, repo):
        """A commit refused here found nothing; saying "secrets detected" would
        send someone hunting for a credential that is not in the diff."""
        result = run_hook(repo, DEVELOPER_REPOS_DIR="/srv/repos")
        assert "secrets detected" not in result.stdout
        assert "did not run" in result.stdout

    def test_a_sandboxed_task_is_refused_even_without_the_developer_skill(self, repo):
        """`DEVELOPER_REPOS_DIR` only appears for a task authorized for the
        developer skill. `ISTOTA_SANDBOXED` is set for every task the model
        runs, so it covers the commits the narrower marker would miss."""
        result = run_hook(repo, ISTOTA_SANDBOXED="1")
        assert result.returncode == 1

    def test_the_override_can_demand_the_scan_on_a_workstation(self, repo):
        result = run_hook(repo, PRECOMMIT_SCANS_REQUIRED="1")
        assert result.returncode == 1

    @pytest.mark.parametrize("value", ["true", "yes", "on"])
    def test_the_spelled_out_affirmatives_also_demand_it(self, repo, value):
        """`PRECOMMIT_SCANS_REQUIRED=true` is somebody opting in to the scans.
        Reading only the literal `1` gave them the opposite."""
        assert run_hook(repo, PRECOMMIT_SCANS_REQUIRED=value).returncode == 1

    def test_an_unrecognised_value_demands_the_scan_and_says_so(self, repo):
        """The failure direction matters: an unclear setting must not resolve
        to the permissive reading in silence."""
        result = run_hook(repo, PRECOMMIT_SCANS_REQUIRED="maybe")
        assert result.returncode == 1
        assert "not" in result.stdout and "maybe" in result.stdout

    def test_the_refusal_does_not_hand_the_agent_its_own_bypass(self, repo):
        """This branch is reached only when nobody is watching, so its only
        reader is the automated committer being refused. Printing the override
        there makes the gate advisory against the actor it exists to bind."""
        result = run_hook(repo, DEVELOPER_REPOS_DIR="/srv/repos")
        assert "PRECOMMIT_SCANS_REQUIRED=0" not in result.stdout

    def test_the_override_can_release_an_agent_commit(self, repo):
        """The escape hatch. Without one, a broken gitleaks on the deployment
        wedges every agent commit with no way through short of --no-verify,
        which disables the private-data scan as well."""
        result = run_hook(
            repo, DEVELOPER_REPOS_DIR="/srv/repos", PRECOMMIT_SCANS_REQUIRED="0"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING" in result.stdout


class TestATooOldGitleaksIsNamedAsSuch:
    """Debian 13 ships 8.16, four minor versions before `gitleaks git` existed."""

    def test_it_is_reported_as_too_old_rather_than_as_a_finding(self, repo):
        result = run_hook(repo, stub=STUB_TOO_OLD)
        assert "8.19" in result.stdout
        assert "secrets detected" not in result.stdout

    def test_a_human_is_not_blocked_by_it(self, repo):
        result = run_hook(repo, stub=STUB_TOO_OLD)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_agent_commit_is_refused_by_it(self, repo):
        result = run_hook(repo, stub=STUB_TOO_OLD, DEVELOPER_REPOS_DIR="/srv/repos")
        assert result.returncode == 1


class TestTheOtherHalfOfTheGateFailsClosedToo:
    """The private-data scanner skips just as silently when it is not there."""

    def test_a_human_gets_a_warning(self, repo):
        (repo / "scripts" / "check-private-data.sh").unlink()
        result = run_hook(repo, stub=STUB_USABLE)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING" in result.stdout

    def test_an_agent_commit_is_refused(self, repo):
        (repo / "scripts" / "check-private-data.sh").unlink()
        result = run_hook(
            repo, stub=STUB_USABLE, DEVELOPER_REPOS_DIR="/srv/repos"
        )
        assert result.returncode == 1
        assert "check-private-data.sh" in result.stdout


class TestTheDaemonMarksItsOwnUnattendedShells:
    """Cron `command` jobs and heartbeat shell commands get `build_stripped_env`,
    which is the daemon's own environment — neither per-task marker is in it, so
    a `git commit` from one used to read as a human at a terminal."""

    def test_the_stripped_env_demands_the_scans(self):
        from istota.executor import build_stripped_env

        assert build_stripped_env().get("PRECOMMIT_SCANS_REQUIRED") == "1"

    def test_the_hook_honours_what_the_daemon_sets(self, repo):
        """The two halves have to agree on the spelling, or the marker is a
        variable nothing reads."""
        from istota.executor import build_stripped_env

        value = build_stripped_env()["PRECOMMIT_SCANS_REQUIRED"]
        assert run_hook(repo, PRECOMMIT_SCANS_REQUIRED=value).returncode == 1


class TestSetupSaysWhichHalfIsInactive:
    """`scripts/setup.sh` is where a fresh clone learns the hook is half-armed,
    and it carried the same presence-only check the hook did — so it would call
    a Debian 8.16 install fine and leave the operator to discover otherwise at
    their first commit."""

    def test_setup_probes_the_subcommand_not_just_the_binary(self):
        text = (REPO_ROOT / "scripts" / "setup.sh").read_text()
        assert "gitleaks git --help" in text

    def test_setup_offers_a_linux_install_route(self):
        """The macOS-only hint is why the Linux deployment never had it."""
        text = (REPO_ROOT / "scripts" / "setup.sh").read_text()
        assert "github.com/gitleaks/gitleaks/releases" in text
