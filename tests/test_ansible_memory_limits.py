"""Stage 2 of the host-and-scheduler-robustness spec: swap and the unit limits.

The spec says Ansible changes are not unit tested and that verification is a run
against a staging host. That holds for the parts only a real host can answer —
whether zram actually compresses, whether the kernel honours the priorities. It
does not hold for the templates: a Jinja typo in a systemd unit is a service
that will not start, and the feedback loop for finding it is a deploy against
production. These tests are the cheap half, and they use the seam
``test_ansible_user_provisioning.py`` already established — parse
``tasks/main.yml`` as YAML, render templates through a bare Jinja environment.

What is deliberately *not* asserted here: that the swapfile commands work. They
are shelled out to (``fallocate``, ``mkswap``, ``swapon``) and only a real host
can say. What is asserted is that they are gated, ordered, and idempotent by
construction, because those are the properties that make a re-run safe.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TEMPLATES = ANSIBLE / "templates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Enough of the inventory for the unit templates to render. Values are generic
# on purpose — these strings end up in assertions, and a real host or user has
# no business in a committed test.
BASE_VARS = {
    "istota_namespace": "istota",
    "istota_package": "istota",
    "istota_user": "istota",
    "istota_group": "istota",
    "istota_home": "/srv/app/istota",
    "istota_repo_dir": "/srv/app/istota",
    "istota_use_nextcloud_mount": True,
    "istota_nextcloud_mount_path": "/srv/mnt/workspace",
    "istota_use_environment_file": True,
    "istota_whisper_enabled": False,
    "istota_whisper_max_model": "base",
    "istota_webhooks_port": 8081,
    "istota_web_port": 8080,
    "istota_web_token_key": "unused-in-these-assertions",
    "istota_web_graceful_shutdown_seconds": 30,
    "istota_web_stop_timeout_seconds": 40,
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
    """Render a role template the way Ansible would, with the role defaults."""
    variables = {**defaults(), **BASE_VARS, **overrides}
    env = Environment(keep_trailing_newline=True)
    source = (TEMPLATES / template_name).read_text()
    return env.from_string(source).render(**variables)


def service_directives(rendered: str) -> dict[str, str]:
    """The ``[Service]`` section of a rendered unit, as a dict.

    systemd units are ini-shaped but allow a key to repeat (``ReadWritePaths``,
    ``Environment``), which configparser rejects by default — hence
    ``strict=False``, which keeps the last occurrence. None of the directives
    under test repeat.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd directives are case-sensitive
    parser.read_string(rendered)
    return dict(parser["Service"])


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_every_new_variable_has_a_default(self):
        """A role variable referenced by a template but absent from defaults
        renders empty and produces a silently broken unit."""
        d = defaults()
        for name in (
            "istota_zram_enabled",
            "istota_zram_size",
            "istota_zram_algorithm",
            "istota_zram_priority",
            "istota_swapfile_enabled",
            "istota_swapfile_size_mb",
            "istota_swapfile_path",
            "istota_swapfile_priority",
            "istota_scheduler_memory_high",
            "istota_scheduler_cpu_weight",
            "istota_web_memory_high",
        ):
            assert name in d, f"{name} missing from defaults/main.yml"

    def test_zram_on_swapfile_off(self):
        """zram is the priority item — it is what turns a memory shortfall back
        into something survivable. The disk swapfile is the optional second
        tier and stays off until an operator asks for it."""
        d = defaults()
        assert d["istota_zram_enabled"] is True
        assert d["istota_swapfile_enabled"] is False

    def test_zram_outranks_the_swapfile(self):
        """The whole point of calling the swapfile "second-tier".

        Linux prefers the *higher* priority number, so zram must sit above the
        disk. Inverted, every cold page would go to the disk that was already
        the saturated resource during the incident — 1.7 GB/s of forced
        re-reads — which is the outcome choosing zram was meant to avoid.
        """
        d = defaults()
        assert d["istota_zram_priority"] > d["istota_swapfile_priority"]

    def test_zram_sized_at_half_of_ram(self):
        d = defaults()
        assert d["istota_zram_size"] == "ram / 2"
        assert d["istota_zram_algorithm"] == "zstd"

    def test_scheduler_cpu_weight_yields_to_everything_else(self):
        """Below the systemd default of 100, so every other unit wins under
        contention. A hard CPUQuota is deliberately not set: PSI showed
        `cpu full avg10=0` during the incident, so the cores were idle-waiting
        on memory, not oversubscribed."""
        assert defaults()["istota_scheduler_cpu_weight"] < 100


# ---------------------------------------------------------------------------
# zram-generator.conf
# ---------------------------------------------------------------------------


class TestZramGeneratorConfig:
    def test_renders_a_zram0_section_from_the_role_variables(self):
        text = render(
            "zram-generator.conf.j2",
            istota_zram_size="ram / 4",
            istota_zram_algorithm="lz4",
            istota_zram_priority=90,
        )
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)

        assert parser.has_section("zram0")
        assert parser["zram0"]["zram-size"] == "ram / 4"
        assert parser["zram0"]["compression-algorithm"] == "lz4"
        assert parser["zram0"]["swap-priority"] == "90"

    def test_declares_itself_a_swap_device(self):
        """Without fs-type the generator would make a filesystem, not swap, and
        the device would be useless for reclaim."""
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(render("zram-generator.conf.j2"))
        assert parser["zram0"]["fs-type"] == "swap"

    def test_defaults_render_half_of_ram_with_zstd(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(render("zram-generator.conf.j2"))
        assert parser["zram0"]["zram-size"] == "ram / 2"
        assert parser["zram0"]["compression-algorithm"] == "zstd"


# ---------------------------------------------------------------------------
# Unit limits
# ---------------------------------------------------------------------------


class TestSchedulerUnitLimits:
    def test_memory_high_and_cpu_weight_come_from_the_role(self):
        directives = service_directives(
            render(
                "istota-scheduler.service.j2",
                istota_scheduler_memory_high="5G",
                istota_scheduler_cpu_weight=25,
            )
        )
        assert directives["MemoryHigh"] == "5G"
        assert directives["CPUWeight"] == "25"

    def test_memory_high_not_memory_max(self):
        """A hard cap turns a slow daemon into a killed daemon, and taking the
        daemon down takes everything down. MemoryHigh throttles instead."""
        directives = service_directives(render("istota-scheduler.service.j2"))
        assert "MemoryHigh" in directives
        assert "MemoryMax" not in directives

    def test_no_cpu_quota(self):
        """A regression pin, not a proof of this change — no CPUQuota was set
        before it either. It exists because ISSUE-257 proposed one on a
        CPU-saturation reading that PSI refuted, and the next person to read
        that issue should not quietly add it back."""
        assert "CPUQuota" not in service_directives(render("istota-scheduler.service.j2"))

    def test_memory_accounting_is_explicit(self):
        """MemoryHigh enables accounting implicitly, but Stage 5's per-task
        cgroups depend on it, and a directive Stage 5 relies on should be
        visible in the unit rather than a side effect of another one."""
        assert service_directives(render("istota-scheduler.service.j2"))["MemoryAccounting"] == "yes"

    def test_delegates_the_controllers_stage_five_needs(self):
        """Delegate= is what makes systemd chown the unit's cgroup subtree to
        its User=, which is the whole mechanism behind a per-task cgroup. Inert
        until Stage 5 creates one."""
        delegate = service_directives(render("istota-scheduler.service.j2"))["Delegate"]
        assert set(delegate.split()) == {"memory", "pids", "cpu"}

    def test_the_payload_runs_in_a_delegated_subgroup(self):
        """Delegate= alone leaves Stage 5 broken, silently.

        cgroup v2 forbids a non-root cgroup from both holding processes and
        enabling controllers for its children — the write to
        cgroup.subtree_control returns EBUSY. Type=simple puts the daemon
        directly in the unit cgroup, so Stage 5 would mkdir task-<id>/ fine and
        then find no memory.max in it, and A6's fail-open rule would swallow
        that. DelegateSubgroup= moves the payload to a leaf so the unit cgroup
        can enable controllers.
        """
        directives = service_directives(render("istota-scheduler.service.j2"))
        assert directives["DelegateSubgroup"] == "supervisor"

    def test_an_empty_memory_high_omits_the_directive(self):
        """The escape hatch. An operator who wants no throttle must be able to
        get none, rather than getting `MemoryHigh=` with an empty value, which
        systemd rejects and which would refuse to start the unit."""
        directives = service_directives(
            render("istota-scheduler.service.j2", istota_scheduler_memory_high="")
        )
        assert "MemoryHigh" not in directives

    def test_an_empty_cpu_weight_omits_the_directive(self):
        directives = service_directives(
            render("istota-scheduler.service.j2", istota_scheduler_cpu_weight="")
        )
        assert "CPUWeight" not in directives

    def test_control_groups_are_not_protected_away(self):
        """A pin against a future hardening pass, not a proof of this change.

        ProtectControlGroups=yes mounts /sys/fs/cgroup read-only for the unit,
        which would silently defeat Delegate= — the subtree handed over and
        then unwritable. Nothing sets it today and nothing did before, so this
        assertion passes trivially right now; its value is that it will stop
        passing the day someone adds it, at which point Stage 5 would otherwise
        break in a way that only shows up as a log line.
        """
        directives = service_directives(render("istota-scheduler.service.j2"))
        assert "ProtectControlGroups" not in directives
        # ProtectKernelTunables=yes mounts /sys read-only and would defeat
        # delegation identically — at least as likely a reach for a future
        # hardening pass.
        assert "ProtectKernelTunables" not in directives

    def test_protect_system_strict_still_leaves_sys_writable(self):
        """ProtectSystem=strict is already on the unit and must stay. It makes
        the file hierarchy read-only *except* /dev, /proc and /sys, so the
        delegated cgroup subtree is reachable. Recorded because the two
        directives look like they should conflict and do not."""
        directives = service_directives(render("istota-scheduler.service.j2"))
        assert directives["ProtectSystem"] == "strict"
        assert directives["Delegate"]

    def test_the_existing_directives_survive(self):
        """The unit carries hard-won settings — KillMode=mixed came out of
        ISSUE-191, LimitNOFILE out of an EMFILE burst. Adding limits must not
        disturb them."""
        directives = service_directives(render("istota-scheduler.service.j2"))
        assert directives["KillMode"] == "mixed"
        assert directives["LimitNOFILE"] == "65536"
        assert directives["Restart"] == "always"


class TestWebAndWebhookUnitLimits:
    def test_web_unit_gets_memory_high(self):
        directives = service_directives(
            render("istota-web.service.j2", istota_web_memory_high="2G")
        )
        assert directives["MemoryHigh"] == "2G"

    def test_webhooks_unit_gets_memory_high(self):
        directives = service_directives(
            render("istota-webhooks.service.j2", istota_web_memory_high="2G")
        )
        assert directives["MemoryHigh"] == "2G"

    def test_empty_omits_it_on_both(self):
        for template in ("istota-web.service.j2", "istota-webhooks.service.j2"):
            directives = service_directives(render(template, istota_web_memory_high=""))
            assert "MemoryHigh" not in directives, template

    def test_neither_gets_delegate_or_cpu_weight(self):
        """A pin: neither unit carried these before this change either.

        It records the intent. Only the scheduler spawns task subprocesses, so
        only the scheduler needs a delegated subtree, and CPUWeight is
        scheduler-only because the web UI yielding CPU would be felt directly
        by a person waiting on a page."""
        for template in ("istota-web.service.j2", "istota-webhooks.service.j2"):
            directives = service_directives(render(template))
            assert "Delegate" not in directives, template
            assert "CPUWeight" not in directives, template


# ---------------------------------------------------------------------------
# The tasks
# ---------------------------------------------------------------------------


class TestZramTasks:
    def test_installs_the_generator_package(self):
        task = find_task("Install systemd-zram-generator")
        assert "systemd-zram-generator" in task["apt"]["name"]

    def test_renders_the_generator_config(self):
        task = find_task("Configure zram swap device")
        assert task["template"]["src"] == "zram-generator.conf.j2"
        assert task["template"]["dest"] == "/etc/systemd/zram-generator.conf"


    def test_starts_the_swap_unit_not_the_setup_unit(self):
        """The distinction that decides whether this stage does anything.

        ``systemd-zram-setup@.service`` is a static template shipped by the
        package: it creates /dev/zram0, sets its geometry and mkswaps it, and
        never calls swapon. ``dev-zram0.swap`` is what the generator
        synthesises, and it is the unit that activates swap. Its Requires=/
        After= point *at* the setup service, so starting the swap unit pulls
        the setup service in and starting the setup service does not pull the
        swap unit in. Target the setup service and the play goes green on a
        host with Total swap = 0 until the next reboot.
        """
        task = find_task("Enable zram swap device")
        assert task["systemd"]["name"] == "dev-zram0.swap"
        assert task["systemd"]["state"] == "started"

    def test_does_not_claim_to_enable_a_generated_unit(self):
        """The generated unit has no [Install], so `systemctl is-enabled`
        answers "static", Ansible records enabled=true and does nothing.
        Persistence comes from the generator re-creating the
        swap.target.wants symlink on every reload and boot, so an `enabled:`
        here would be a comforting no-op."""
        assert "enabled" not in find_task("Enable zram swap device")["systemd"]

    def test_verifies_against_proc_swaps_rather_than_a_green_play(self):
        """A swap change whose only evidence is "Ansible reported ok" is
        exactly how the first draft of this block shipped a no-op."""
        task = find_task("Verify zram swap is actually active")
        assert "/proc/swaps" in task["command"]
        assert task["changed_when"] is False
        assert "rc != 0" in str(task["failed_when"])

    def test_daemon_reload_runs_before_the_swap_unit_is_started(self):
        """dev-zram0.swap is generated into /run from the config, so the
        generators have to re-run before there is a unit to start."""
        names = [t.get("name") for t in tasks() if isinstance(t, dict)]
        assert names.index("Re-run systemd generators after a zram config change") < names.index(
            "Enable zram swap device"
        )

    def test_a_geometry_change_rebuilds_rather_than_reloads(self):
        """A live zram0 keeps its size and algorithm until it is torn down, so
        changing the config alone changes nothing on the host."""
        task = find_task("Rebuild zram device after a geometry change")
        assert "istota_zram_config is changed" in str(task["when"])
        assert "systemctl restart systemd-zram-setup@zram0.service" in task["shell"]

    def test_the_rebuild_stops_and_restarts_the_swap_unit_around_the_device(self):
        """Restarting the setup service alone is worse than doing nothing.

        Requires= propagates the *stop* to dev-zram0.swap, and nothing
        propagates the start back, so a size change on a running host took swap
        to zero and left it there until reboot — during the operation meant to
        add headroom. The swap unit has to be stopped and started explicitly
        around the device rebuild.
        """
        script = find_task("Rebuild zram device after a geometry change")["shell"]
        stop = script.index("systemctl stop dev-zram0.swap")
        rebuild = script.index("systemctl restart systemd-zram-setup@zram0.service")
        start = script.index("systemctl start dev-zram0.swap")
        assert stop < rebuild < start

    def test_the_rebuild_refuses_to_swapoff_a_loaded_device(self):
        """swapoff on a full zram device faults every page back into RAM. Doing
        that on a box already short of memory is the exact failure this track
        exists to prevent, so a loaded device is left alone and says so."""
        task = find_task("Rebuild zram device after a geometry change")
        assert "/proc/swaps" in task["shell"]
        assert "too much to swapoff safely" in task["shell"]

    def test_the_rebuild_reads_the_used_column(self):
        """/proc/swaps is Filename Type Size Used Priority. Reading Size ($3)
        instead of Used ($4) compares the device's capacity against the
        threshold and skips the rebuild every time."""
        script = find_task("Rebuild zram device after a geometry change")["shell"]
        assert "print $4" in script
        assert "print $3" not in script

    def test_the_rebuild_fails_loudly_rather_than_silently(self):
        """Without `set -e` the script's exit status is echo's, so a failed
        systemctl still reports `rebuilt` and the operator sees a green
        geometry change on a device that never changed."""
        assert "set -euo pipefail" in find_task(
            "Rebuild zram device after a geometry change"
        )["shell"]

    def test_a_skipped_rebuild_is_surfaced(self):
        """The skip leaves the config on disk disagreeing with the live device,
        permanently — the next run sees no config change and never retries. A
        green `ok` task whose stdout Ansible does not print is not good enough."""
        task = find_task("Warn when the zram geometry did not converge")
        assert "skipped:" in str(task["when"])

    def test_every_zram_task_is_gated_on_the_flag(self):
        """`istota_zram_enabled: false` must leave the host exactly as it is,
        so an operator who arranged swap another way is not fought with. One
        ungated task in the group breaks that promise."""
        for name in (
            "Install systemd-zram-generator",
            "Configure zram swap device",
            "Re-run systemd generators after a zram config change",
            "Enable zram swap device",
            "Rebuild zram device after a geometry change",
            "Verify zram swap is actually active",
        ):
            condition = str(find_task(name).get("when", ""))
            assert "istota_zram_enabled" in condition, f"{name} is not gated"


class TestSwapfileTasks:
    SWAPFILE_TASKS = (
        "Create the second-tier swapfile",
        "Restrict swapfile permissions",
        "Check whether the swapfile carries a swap signature",
        "Format the swapfile",
        "Check whether the swapfile is already active",
        "Activate the swapfile",
        "Record the swapfile in fstab",
    )

    def test_every_swapfile_task_is_gated_on_its_own_flag(self):
        """Default off. A role that created a 2 GB file on every host because
        the flag defaulted the other way would be a nasty surprise."""
        for name in self.SWAPFILE_TASKS:
            condition = str(find_task(name).get("when", ""))
            assert "istota_swapfile_enabled" in condition, f"{name} is not gated"

    def test_creation_is_idempotent_by_construction(self):
        """`creates:` is what stops a re-run truncating a live swapfile."""
        task = find_task("Create the second-tier swapfile")
        assert task["args"]["creates"] == "{{ istota_swapfile_path }}"

    def test_mkswap_is_gated_on_the_absence_of_a_swap_signature(self):
        """Ask the file, do not infer it from "did this run create it".

        `creates:` makes the create task report ok on every later run, so
        gating mkswap on `is changed` means a run interrupted between fallocate
        and mkswap leaves a file that is never formatted — not on that run and
        not on any future one. Re-running mkswap over an *active* swapfile
        corrupts it, which is what the signature check actually prevents.
        """
        condition = str(find_task("Format the swapfile").get("when", ""))
        assert "istota_swapfile_sig" in condition
        assert "is changed" not in condition
        probe = find_task("Check whether the swapfile carries a swap signature")
        assert "blkid" in probe["command"]
        assert probe["changed_when"] is False

    def test_fstab_is_written_only_after_activation_succeeds(self):
        """Written earlier, a failed activation still leaves a boot-time
        reference to a file that may carry no swap header — a failed
        swap.target on the next reboot."""
        names = [t.get("name") for t in tasks() if isinstance(t, dict)]
        assert names.index("Activate the swapfile") < names.index(
            "Record the swapfile in fstab"
        )

    def test_the_fstab_entry_carries_nofail(self):
        """Swap that cannot be activated must degrade to a missing mitigation,
        never to a boot problem."""
        opts = find_task("Record the swapfile in fstab")["ansible.posix.mount"]["opts"]
        assert "nofail" in opts

    def test_the_swapfile_is_not_world_readable(self):
        """Swap holds whatever was paged out of memory. Mode 600 is not
        cosmetic — mkswap itself warns about an insecure swapfile."""
        assert find_task("Restrict swapfile permissions")["file"]["mode"] == "0600"

    def test_activation_checks_proc_swaps_rather_than_ignoring_failures(self):
        """A `failed_when: false` on swapon would hide a genuine failure just
        as effectively as it hides the already-on case."""
        check = find_task("Check whether the swapfile is already active")
        assert check["changed_when"] is False
        activate = find_task("Activate the swapfile")
        assert "rc" in str(activate.get("when", ""))

    def test_fstab_entry_carries_the_second_tier_priority(self):
        opts = find_task("Record the swapfile in fstab")["ansible.posix.mount"]["opts"]
        assert "pri={{ istota_swapfile_priority }}" in opts
