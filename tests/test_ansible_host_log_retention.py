"""Host-side retention: journald, auditd, DB backups, and stale claude versions.

These four are what filled the production root filesystem to 84%. Three of them
were misconfigurations that Ansible did not manage at all, so a manual cleanup
would have been undone by the next thing that wrote a log:

  * ``journald.conf`` had an empty ``[Journal]`` section, so the cap fell back
    to 10% of the filesystem — 4G on a 40G disk, and it had reached 3.4G.
  * ``auditd.conf`` shipped ``max_log_file_action = keep_logs``, which overrides
    ``num_logs`` and never deletes. 140 rotated files had accumulated.
  * The claude CLI self-updates and leaves every previous version behind at
    ~320M each.

The fourth, DB backup retention, was working exactly as configured — the
configuration was just expensive, and it applied the same window to the local
copies and to the off-host ones on the mount. Those want opposite things: local
is a fast-restore working set, remote is the durable tail.

Same seam as ``test_ansible_memory_limits.py`` — parse ``tasks/main.yml`` as
YAML, render templates through a bare Jinja environment. What is deliberately
not asserted is that any of these commands work on a real host; what is
asserted is that they are gated, bounded, and cannot delete the thing currently
in use.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TEMPLATES = ANSIBLE / "templates"


BASE_VARS = {
    "istota_namespace": "istota",
    "istota_package": "istota",
    "istota_user": "istota",
    "istota_group": "istota",
    "istota_home": "/srv/app/istota",
    "istota_repo_dir": "/srv/app/istota",
    "istota_use_nextcloud_mount": True,
    "istota_nextcloud_mount_path": "/srv/mnt/workspace",
    "istota_rclone_remote": "nextcloud",
    "istota_whisper_enabled": False,
    "istota_web_enabled": False,
    "istota_location_enabled": False,
}


def defaults() -> dict:
    return yaml.safe_load(DEFAULTS_FILE.read_text())


def tasks() -> list:
    return yaml.safe_load(TASKS_FILE.read_text())


def find_task(name: str) -> dict:
    for task in tasks():
        if isinstance(task, dict) and task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


def render(template_name: str, **overrides) -> str:
    variables = {**defaults(), **BASE_VARS, **overrides}
    env = Environment(keep_trailing_newline=True)
    source = (TEMPLATES / template_name).read_text()
    return env.from_string(source).render(**variables)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_every_new_variable_has_a_default(self):
        """A role variable referenced by a template but absent from defaults
        renders empty — here that means an unbounded log or an unguarded
        delete."""
        d = defaults()
        for name in (
            "istota_journald_manage",
            "istota_journald_max_use",
            "istota_journald_max_file_size",
            "istota_auditd_manage",
            "istota_auditd_num_logs",
            "istota_auditd_max_log_file",
            "istota_auditd_max_log_file_action",
            "istota_claude_versions_keep",
            "istota_backup_db_daily_retention",
            "istota_backup_db_weekly_retention",
            "istota_backup_db_daily_retention_remote",
            "istota_backup_db_weekly_retention_remote",
        ):
            assert name in d, f"{name} missing from defaults/main.yml"

    def test_local_backup_window_is_shorter_than_remote(self):
        """Local snapshots are a fast-restore working set on the same disk that
        is running out of space. The off-host copies are the durable tail and
        cost nothing locally, so they keep the longer window."""
        d = defaults()
        assert d["istota_backup_db_daily_retention"] < d["istota_backup_db_daily_retention_remote"]
        assert d["istota_backup_db_weekly_retention"] < d["istota_backup_db_weekly_retention_remote"]

    def test_auditd_action_actually_deletes(self):
        """``keep_logs`` is the Debian default and it silently overrides
        ``num_logs``: auditd rotates forever and never reclaims. ROTATE is what
        makes num_logs a bound rather than a suggestion."""
        d = defaults()
        assert d["istota_auditd_max_log_file_action"] == "ROTATE"
        assert d["istota_auditd_num_logs"] >= 1

    def test_claude_keeps_a_rollback_version(self):
        """Pruning to exactly one version leaves no way back from a bad CLI
        release, which is the whole reason the versions directory exists."""
        assert defaults()["istota_claude_versions_keep"] >= 2

    def test_journald_cap_is_bounded_and_explicit(self):
        d = defaults()
        assert d["istota_journald_max_use"], "an empty cap is the 10%-of-disk default again"
        assert d["istota_journald_manage"] is True


# ---------------------------------------------------------------------------
# journald
# ---------------------------------------------------------------------------


class TestJournald:
    def test_drops_a_conf_d_file_rather_than_editing_the_package_config(self):
        """``/etc/systemd/journald.conf`` is package-managed; a drop-in survives
        a package upgrade without a merge conflict."""
        task = find_task("Cap the systemd journal")
        dest = task["template"]["dest"]
        assert dest.startswith("/etc/systemd/journald.conf.d/")
        assert dest.endswith(".conf"), "systemd ignores drop-ins not ending in .conf"

    def test_creates_the_drop_in_directory_first(self):
        """systemd ships journald.conf but no journald.conf.d/, and ``template``
        does not create parents — verified absent on the production host, where
        this would have failed the play on first run."""
        mkdir = find_task("Create the journald drop-in directory")
        assert mkdir["file"]["path"] == "/etc/systemd/journald.conf.d"
        assert mkdir["file"]["state"] == "directory"
        names = [t.get("name") for t in tasks() if isinstance(t, dict)]
        assert names.index("Create the journald drop-in directory") < names.index(
            "Cap the systemd journal"
        ), "the directory must be created before the template lands in it"

    def test_drop_in_sorts_after_vendor_files(self):
        """Drop-ins apply in lexicographic order, last wins. An unprefixed
        ``istota.conf`` loses to anything sorting after it and the cap is
        silently undone."""
        dest = find_task("Cap the systemd journal")["template"]["dest"]
        basename = dest.rsplit("/", 1)[-1]
        assert basename.startswith("99-"), f"{basename} does not sort last"

    def test_restarts_journald_so_the_cap_takes_effect(self):
        task = find_task("Cap the systemd journal")
        notify = task.get("notify")
        notify = [notify] if isinstance(notify, str) else (notify or [])
        assert any("journald" in n for n in notify), "a cap that needs a reboot is not a cap"

    def test_handler_exists(self):
        handlers = yaml.safe_load((ANSIBLE / "handlers" / "main.yml").read_text())
        names = {h.get("name") for h in handlers if isinstance(h, dict)}
        task = find_task("Cap the systemd journal")
        notify = task.get("notify")
        notify = [notify] if isinstance(notify, str) else (notify or [])
        for n in notify:
            assert n in names, f"task notifies {n!r} but no such handler"

    def test_gated_on_the_manage_flag(self):
        """An operator who manages journald elsewhere needs a way to opt out."""
        assert "istota_journald_manage" in find_task("Cap the systemd journal")["when"]

    def test_rendered_drop_in_is_a_valid_journal_section(self):
        rendered = render("journald-istota.conf.j2")
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_string(rendered)
        journal = dict(parser["Journal"])
        assert journal["SystemMaxUse"] == str(defaults()["istota_journald_max_use"])
        assert journal["SystemMaxFileSize"] == str(defaults()["istota_journald_max_file_size"])


# ---------------------------------------------------------------------------
# auditd
# ---------------------------------------------------------------------------


class TestAuditd:
    def test_sets_all_three_rotation_keys(self):
        """num_logs alone is inert while the action is keep_logs, and the action
        alone is unbounded while num_logs is unset. They are one setting."""
        task = find_task("Bound the auditd log rotation")
        lines = task["lineinfile"] if "lineinfile" in task else task
        loop = task.get("loop") or task.get("with_items") or []
        keys = {item["key"] for item in loop}
        assert keys == {"num_logs", "max_log_file", "max_log_file_action"}
        assert lines["path"] == "/etc/audit/auditd.conf"

    def test_anchored_so_it_replaces_rather_than_appends(self):
        """An unanchored lineinfile regexp appends a second ``num_logs`` line;
        auditd reads the first and the change looks applied but is not."""
        task = find_task("Bound the auditd log rotation")
        regexp = task["lineinfile"]["regexp"]
        assert regexp.startswith("^"), "an unanchored regexp can match a comment"

    def test_skipped_when_auditd_is_not_installed(self):
        """Writing auditd.conf on a host with no auditd creates a config that
        nothing reads and that a later package install will conflict with."""
        task = find_task("Bound the auditd log rotation")
        when = task["when"]
        when = [when] if isinstance(when, str) else when
        joined = " ".join(when)
        assert "istota_auditd_manage" in joined
        assert "auditd_conf" in joined, "should be gated on a stat of the config"

    def test_restarts_auditd(self):
        task = find_task("Bound the auditd log rotation")
        notify = task.get("notify")
        notify = [notify] if isinstance(notify, str) else (notify or [])
        assert any("auditd" in n for n in notify)

    def test_restart_failure_is_not_swallowed(self):
        """``failed_when: false`` would report ok while the daemon kept the old
        rotation policy — the config is on disk but unread until a reboot. The
        role already learned this on the zram track: a change whose only
        evidence is a green play is not a change."""
        handlers = yaml.safe_load((ANSIBLE / "handlers" / "main.yml").read_text())
        handler = next(h for h in handlers if h.get("name") == "restart auditd")
        assert "failed_when" not in handler or handler["failed_when"] is not False

    def test_strands_are_reaped_not_just_capped(self):
        """ROTATE only shifts audit.log.1..num_logs-1. Every file the previous
        keep_logs setting left above that index is stranded forever, so capping
        alone leaves the disk exactly as full as it was."""
        find_t = find_task("Find stranded auditd logs above the rotation bound")
        assert find_t["find"]["paths"] == "/var/log/audit"
        rm = find_task("Remove stranded auditd logs above the rotation bound")
        assert rm["file"]["state"] == "absent"
        when = " ".join(rm["when"])
        assert "istota_auditd_num_logs" in when, "the bound must come from the same variable"

    def test_reap_keeps_the_files_within_the_bound(self):
        """The reap condition is an integer compare against num_logs. Guard the
        boundary directly: .5 must survive at num_logs=5, .6 must not."""
        import re

        rm = find_task("Remove stranded auditd logs above the rotation bound")
        when = " ".join(rm["when"])
        pattern = re.search(r"regex_search\('([^']+)'", when).group(1).replace("\\\\", "\\")
        for name, suffix in [("audit.log.5", 5), ("audit.log.6", 6), ("audit.log.140", 140)]:
            m = re.search(pattern, name)
            assert m, f"{name} should match the suffix pattern"
            assert int(m.group(1)) == suffix


# ---------------------------------------------------------------------------
# Backup retention: local and remote are separate windows
# ---------------------------------------------------------------------------


class TestBackupRetention:
    # ISSUE-262 moved rotation out of the per-DB success path into one sweep,
    # so the four `find` calls became four `_prune_tier` calls that take the
    # window as an argument. These still guard the same thing they always did:
    # which window each tier is pruned by. That the windows then behave
    # differently is proved by executing the script in
    # tests/test_ansible_backup_script.py.
    def _prune_calls(self) -> list[str]:
        return [
            line.strip()
            for line in render("istota-backup.sh.j2").splitlines()
            if line.strip().startswith("_prune_tier ")
        ]

    def test_remote_rotation_uses_the_remote_window(self):
        """The bug this guards: both tiers read the same variable, so shrinking
        the local window to save disk silently shortened the off-host tail
        too — the copies that exist precisely because the local disk can be
        lost."""
        remote = [c for c in self._prune_calls() if "REMOTE_DIR" in c]
        assert len(remote) == 2, f"expected daily+weekly remote rotation, got {remote}"
        assert any("DAILY_RETENTION_REMOTE" in c for c in remote)
        assert any("WEEKLY_RETENTION_REMOTE" in c for c in remote)

    def test_local_rotation_uses_the_local_window(self):
        local = [c for c in self._prune_calls() if "LOCAL_DIR" in c]
        assert len(local) == 2
        for call in local:
            assert "_REMOTE" not in call, "local rotation must not read the remote window"

    def test_every_retention_variable_is_a_bare_integer(self):
        """These land in ``find -mtime +N``. A quoted or empty value makes find
        error out, and the rotation is swallowed by ``|| true`` — retention
        would stop with nothing in the log."""
        rendered = render("istota-backup.sh.j2")
        for name in (
            "DAILY_RETENTION",
            "WEEKLY_RETENTION",
            "DAILY_RETENTION_REMOTE",
            "WEEKLY_RETENTION_REMOTE",
        ):
            line = next(
                ln for ln in rendered.splitlines() if ln.startswith(f"{name}=")
            )
            value = line.split("=", 1)[1].strip()
            assert value.isdigit(), f"{name} rendered as {value!r}"


# ---------------------------------------------------------------------------
# Stale claude CLI versions
# ---------------------------------------------------------------------------


class TestClaudeVersionPrune:
    """These run the rendered shell rather than grepping it.

    The first cut of this class asserted ``"readlink" in prune`` and
    ``"|| true" in prune``. Both passed against a prune that deletes the live
    binary, which is the vacuous-assertion trap CLAUDE.md names: a test that
    could not fail for the defect it is named after. Everything below builds a
    fixture tree, executes the block, and looks at what survived.
    """

    def test_keep_count_comes_from_the_role_variable(self):
        rendered = render("istota-update.sh.j2")
        line = next(
            ln for ln in rendered.splitlines() if ln.startswith("CLAUDE_VERSIONS_KEEP=")
        )
        value = line.split("=", 1)[1].strip()
        assert value == str(defaults()["istota_claude_versions_keep"])
        assert value.isdigit(), f"{value!r} feeds $((KEEP + 1)); empty evaluates to 1"

    def test_runs_before_the_already_up_to_date_early_exits(self):
        """claude releases on its own cadence. Gating the prune on an istota
        update meant a tag-pinned host never reaped anything."""
        lines = render("istota-update.sh.j2").splitlines()
        prune_at = next(i for i, ln in enumerate(lines) if ln.startswith("CLAUDE_VERSIONS_KEEP="))
        first_exit = next(
            i for i, ln in enumerate(lines) if ln.strip() == "exit 0" and i > 25
        )
        assert prune_at < first_exit, "prune is unreachable when the repo is up to date"

    def test_keeps_newest_and_removes_the_rest(self):
        surviving = _run_prune(versions=["v1", "v2", "v3", "v4", "v5"], live="v5")
        assert surviving == {"v4", "v5"}

    def test_spares_the_live_build_when_it_is_not_the_newest(self):
        """The rollback case: the symlink deliberately points at an older
        build, which mtime order puts inside the delete range."""
        surviving = _run_prune(versions=["v1", "v2", "v3", "v4", "v5"], live="v2")
        assert "v2" in surviving, "pruned the build the launcher points at"

    def test_spares_the_live_build_through_a_symlinked_parent(self):
        """``readlink -f`` canonicalises every component while a concatenated
        path does not, so comparing the two only matches when no parent is a
        symlink. Comparing entry names instead is immune."""
        surviving = _run_prune(
            versions=["v1", "v2", "v3", "v4", "v5"], live="v2", symlink_parent=True
        )
        assert "v2" in surviving, "guard defeated by a symlinked parent directory"

    def test_refuses_to_prune_when_the_keep_count_is_not_a_number(self):
        """Bash evaluates $(( + 1)) as 1, so an empty override would select
        every entry for deletion."""
        surviving = _run_prune(versions=["v1", "v2", "v3"], live="v3", keep="")
        assert surviving == {"v1", "v2", "v3"}, "an unusable keep count must delete nothing"

    def test_refuses_to_prune_when_the_live_build_cannot_be_resolved(self):
        """A guard that cannot name the live build must not delete anything."""
        surviving = _run_prune(versions=["v1", "v2", "v3"], live=None)
        assert surviving == {"v1", "v2", "v3"}

    def test_survives_an_absent_versions_directory(self):
        """The script is ``set -euo pipefail``; the prune must not abort a run."""
        rc, _ = _run_prune_raw(versions=None, live=None)
        assert rc == 0

    def test_survives_an_empty_versions_directory(self):
        rc, surviving = _run_prune_raw(versions=[], live=None)
        assert rc == 0
        assert surviving == set()


def _prune_block(rendered: str) -> str:
    """The prune, from the keep-count assignment to the ``fi`` that closes it."""
    lines = rendered.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("CLAUDE_VERSIONS_KEEP="))
    end = next(i for i, ln in enumerate(lines) if i > start and ln == "fi")
    return "\n".join(lines[start : end + 1])


def _run_prune_raw(versions, live, keep=None, symlink_parent=False):
    """Execute the rendered prune against a fixture tree; return (rc, survivors)."""
    import subprocess
    import tempfile

    rendered = render("istota-update.sh.j2")
    block = _prune_block(rendered)
    if keep is not None:
        block = "\n".join(
            f"CLAUDE_VERSIONS_KEEP={keep}" if ln.startswith("CLAUDE_VERSIONS_KEEP=") else ln
            for ln in block.splitlines()
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_home = root / "real" / "app" / "istota"
        vdir = real_home / ".local" / "share" / "claude" / "versions"
        (real_home / ".local" / "bin").mkdir(parents=True)
        if versions is not None:
            vdir.mkdir(parents=True)
            # Distinct, ascending mtimes so `ls -1t` order is deterministic.
            for i, name in enumerate(versions):
                f = vdir / name
                f.write_text("binary")
                os.utime(f, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
        if live is not None:
            (real_home / ".local" / "bin" / "claude").symlink_to(vdir / live)

        if symlink_parent:
            # HOME_DIR reached through a symlinked component, which is what
            # defeats a raw string comparison against `readlink -f` output.
            link = root / "app-link"
            link.symlink_to(real_home)
            home_dir = str(link)
        else:
            home_dir = str(real_home)

        script = root / "prune.sh"
        script.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f'HOME_DIR="{home_dir}"\n'
            'log() { echo "$*"; }\n' + block + "\n"
        )
        rc = subprocess.run(["bash", str(script)], capture_output=True, text=True).returncode
        survivors = {p.name for p in vdir.iterdir()} if vdir.exists() else set()
    return rc, survivors


def _run_prune(**kwargs) -> set[str]:
    rc, survivors = _run_prune_raw(**kwargs)
    assert rc == 0, "prune aborted the run under set -e"
    return survivors
