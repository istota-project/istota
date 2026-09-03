"""The dataclass-driven half of ``load_config``.

Two of these classes are the defects that motivated the walk. Both were found
by comparing the loader against the dataclasses mechanically, not by reading,
and both had been shipping: a setting an operator can write in three places and
the daemon never reads, and a default that depends on whether a section header
happens to be present.
"""

import dataclasses
import logging
from pathlib import Path

import pytest

from istota import config as config_module
from istota import config_mapper
from istota.config import Config, load_config
from istota.config_mapper import (
    _KEEP,
    apply_section,
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_str_list,
)


def write(tmp_path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


class TestFieldsTheLoaderNeverRead:
    """Declared, documented, generator-written -- and dropped on the floor.

    Every key here is on a dataclass, in ``config/config.example.toml``, and
    written by the Ansible template or the Docker render or both. The
    hand-written loader had no line for it, so the operator set a value, every
    surface agreed the setting existed, and the daemon ran the hardcoded
    default. ``security.sandbox_ro_paths`` was found this way once before; this
    is the rest of the class.

    ``max_subtasks_per_task`` is the one to keep in view: it caps how many
    subtasks a single task may spawn, which is the blast radius of a
    prompt-injected task. An operator tightening it from 10 to 3 got 10.
    """

    def test_the_subtask_caps_are_read(self, tmp_path):
        p = write(tmp_path, """
[scheduler]
max_subtasks_per_task = 3
max_subtask_depth = 7
max_subtask_prompt_chars = 111
""")
        cfg = load_config(p)
        assert cfg.scheduler.max_subtasks_per_task == 3
        assert cfg.scheduler.max_subtask_depth == 7
        assert cfg.scheduler.max_subtask_prompt_chars == 111

    def test_the_remaining_scheduler_keys_are_read(self, tmp_path):
        p = write(tmp_path, """
[scheduler]
talk_cache_max_per_conversation = 13
location_ping_retention_days = 17
log_channel_show_skills = false
""")
        cfg = load_config(p)
        assert cfg.scheduler.talk_cache_max_per_conversation == 13
        assert cfg.scheduler.location_ping_retention_days == 17
        assert cfg.scheduler.log_channel_show_skills is False

    def test_the_sleep_cycle_models_are_read(self, tmp_path):
        p = write(tmp_path, """
[sleep_cycle]
extraction_model = "sonnet"
curation_model = "haiku"
knowledge_graph_audit_retention_days = 11

[channel_sleep_cycle]
extraction_model = "opus"
""")
        cfg = load_config(p)
        assert cfg.sleep_cycle.extraction_model == "sonnet"
        assert cfg.sleep_cycle.curation_model == "haiku"
        assert cfg.sleep_cycle.knowledge_graph_audit_retention_days == 11
        assert cfg.channel_sleep_cycle.extraction_model == "opus"

    def test_the_memory_search_half_life_is_read(self, tmp_path):
        p = write(tmp_path, "[memory_search]\nrecency_half_life_days = 5.0\n")
        assert load_config(p).memory_search.recency_half_life_days == 5.0


class TestOneDefaultPerField:
    """A field's default must not depend on whether its section header exists.

    The dataclass said one thing and the loader's ``.get(key, default)`` said
    another, so ``[sleep_cycle]`` with nothing under it switched the nightly
    memory extraction *off* -- dataclass ``True``, loader ``False``. The
    dataclass default is now the only default.

    Every shipped generator writes all three of these keys explicitly, which is
    why resolving the divergence changes nothing for a generated deployment;
    the config this catches is a hand-written one.
    """

    @pytest.mark.parametrize("section, attr, field", [
        ("conversation", "conversation", "lookback_count"),
        ("sleep_cycle", "sleep_cycle", "enabled"),
        ("playbooks", "playbooks", "retention_days"),
        # The fourth, and the one with a running cost. `istota setup` writes a
        # `[scheduler]` header with one unrelated key under it, so the local
        # install was on the loader's 5 while the dataclass said 2 -- resolving
        # the wrong way would have put every laptop install on 2.5x the
        # database polling. The dataclass moved to 5 to match what the
        # generators, the example config and the wizard all already produce.
        ("scheduler", "scheduler", "poll_interval"),
    ])
    def test_an_empty_section_matches_no_section(self, tmp_path, section, attr, field):
        absent = load_config(write(tmp_path, "bot_name = 'x'\n"))
        empty = load_config(write(tmp_path, f"[{section}]\n"))
        assert getattr(getattr(absent, attr), field) == getattr(getattr(empty, attr), field)
        assert getattr(getattr(empty, attr), field) == getattr(
            getattr(Config(), attr), field
        )


class TestUnknownKeys:
    """A typo is reported. It is still not fatal.

    Unknown keys are tolerated on purpose: a config written for a newer version
    has to load on an older one, or a rollback does not work. What changed is
    that the operator now hears about it.
    """

    def test_a_misspelled_key_is_named(self, tmp_path, caplog):
        p = write(tmp_path, "[scheduler]\nmax_subtask_dept = 3\n")
        with caplog.at_level(logging.WARNING):
            cfg = load_config(p)
        assert "scheduler.max_subtask_dept" in caplog.text
        assert cfg.scheduler.max_subtask_depth == Config().scheduler.max_subtask_depth

    def test_a_misspelled_section_is_named(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            load_config(write(tmp_path, "[breifings]\nenabled = true\n"))
        assert "breifings" in caplog.text

    def test_an_unknown_key_does_not_stop_the_load(self, tmp_path):
        cfg = load_config(write(tmp_path, "bot_name = 'Kept'\nfuture_key = 1\n"))
        assert cfg.bot_name == "Kept"

    def test_a_known_key_is_not_reported(self, tmp_path, caplog):
        """Asserts on the key, not on the wording.

        Checking that the word "unrecognised" is absent passes vacuously on any
        rewording of the message -- including the American spelling.
        """
        with caplog.at_level(logging.WARNING):
            load_config(write(tmp_path, "[scheduler]\nmax_subtask_depth = 3\n"))
        assert "max_subtask_depth" not in caplog.text


class TestFieldsThatAreNotSettings:
    """A declared field the loader owns must not be writable from the file.

    Being a real field is what keeps these out of the unknown-key report, so
    silence here is the one case nothing else would ever surface.
    """

    def test_the_config_path_cannot_be_redirected_by_the_file(self, tmp_path, caplog):
        p = write(tmp_path, 'config_path = "/etc/decoy/other.toml"\n')
        with caplog.at_level(logging.WARNING):
            cfg = load_config(p)
        # It is exported as ISTOTA_CONFIG_PATH to every task, cron command and
        # skill CLI, so a file naming another path would split the daemon's
        # config from its subprocesses'.
        assert cfg.config_path == p
        assert "config_path" in caplog.text

    def test_the_bundled_skills_dir_cannot_be_set_by_the_file(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = load_config(write(tmp_path, 'bundled_skills_dir = "/tmp/elsewhere"\n'))
        assert cfg.bundled_skills_dir is None
        assert "bundled_skills_dir" in caplog.text

    def test_admin_users_from_the_file_is_reported(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            load_config(write(tmp_path, 'admin_users = ["alice"]\n'))
        assert "admin_users" in caplog.text


class TestARenamedKeyKeepsWorking:
    """The one key with two accepted spellings.

    A walk over field names cannot express a second spelling, and a hook is
    keyed on the new name and never sees the old one -- so without this the
    value silently reverted to its default, which is the failure the whole
    change is about.
    """

    def test_the_old_spelling_still_sets_the_value(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = load_config(write(
                tmp_path, "[scheduler]\nistota_file_poll_interval = 99\n",
            ))
        assert cfg.scheduler.tasks_file_poll_interval == 99
        assert "renamed" in caplog.text

    def test_the_current_spelling_wins_when_both_are_present(self, tmp_path):
        cfg = load_config(write(tmp_path, (
            "[scheduler]\n"
            "istota_file_poll_interval = 99\n"
            "tasks_file_poll_interval = 7\n"
        )))
        assert cfg.scheduler.tasks_file_poll_interval == 7

    def test_the_old_spelling_is_not_also_called_a_typo(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            load_config(write(
                tmp_path, "[scheduler]\nistota_file_poll_interval = 99\n",
            ))
        assert "unrecognised" not in caplog.text


class TestTheTablesStayHonest:
    """The three tables driving the walk must keep naming real things.

    This is the check that makes the split safe to maintain. Each table is a
    claim about the schema, and a claim that quietly stops being true is
    exactly the failure the walk was built to end: a key nothing reads, with
    every surface still advertising it.
    """

    def dotted_keys(self):
        """Every dotted key the Config dataclass tree declares."""
        keys: set[str] = set()

        def walk(instance, prefix=""):
            for f in dataclasses.fields(instance):
                key = f"{prefix}.{f.name}" if prefix else f.name
                keys.add(key)
                value = getattr(instance, f.name, None)
                if dataclasses.is_dataclass(value) and not isinstance(value, type):
                    walk(value, key)

        walk(Config())
        return keys

    def test_every_hand_parsed_key_is_a_real_field(self):
        stale = sorted(config_module._PARSED_BY_HAND - self.dotted_keys())
        assert not stale, (
            f"_PARSED_BY_HAND names {stale}, which no dataclass field declares. "
            "Either the field was renamed and nothing parses it now, or the "
            "entry outlived the code that read it."
        )

    def test_every_hooked_key_is_a_real_field(self):
        stale = sorted(set(config_module._CONFIG_HOOKS) - self.dotted_keys())
        assert not stale, (
            f"_CONFIG_HOOKS is keyed on {stale}, which no dataclass field "
            "declares. The hook is dead code and the field it meant to parse "
            "is falling through to the generic coercion."
        )

    def test_a_retired_key_is_not_still_a_field(self):
        live = sorted(config_module._RETIRED & self.dotted_keys())
        assert not live, (
            f"_RETIRED names {live}, which the dataclass tree still declares. "
            "The walk is skipping a live field, so the setting is unreadable "
            "while every surface says it exists."
        )

    def test_a_key_is_not_both_hand_parsed_and_hooked(self):
        """Checked by prefix, not by exact key.

        `skip` is matched against the *ancestor* and `apply_section` returns
        before descending, so a hook keyed on `models.aliases` -- under the
        skipped `models` -- would never run while passing an exact-match check
        and every other test in this class.
        """
        skipped = config_module._PARSED_BY_HAND | config_module._RETIRED
        shadowed = sorted(
            key for key in config_module._CONFIG_HOOKS
            if any(key == s or key.startswith(f"{s}.") for s in skipped)
        )
        assert not shadowed, (
            f"{shadowed} is hooked but sits at or under a skipped key. The skip "
            "wins and returns before descending, so the hook never runs."
        )

    def test_every_declared_field_resolves_to_a_coercion(self):
        """The check that keeps the coercion table from becoming a second schema.

        Without this, a field annotated `str | None` or `list[float]` is
        ignored with nothing but a log line -- which is byte-identical to the
        "field the loader never read" defect this module exists to make
        impossible, reintroduced one annotation at a time. It is exactly the
        shape that shipped for `dict` and was only found by a test failing for
        an unrelated reason.
        """
        unmapped = []

        def walk(instance, prefix=""):
            for f in dataclasses.fields(instance):
                key = f"{prefix}.{f.name}" if prefix else f.name
                value = getattr(instance, f.name, None)
                if dataclasses.is_dataclass(value) and not isinstance(value, type):
                    walk(value, key)
                    continue
                if key in config_module._PARSED_BY_HAND:
                    continue
                if key in config_module._NOT_CONFIGURATION:
                    continue
                if key in config_module._CONFIG_HOOKS:
                    continue
                if config_mapper._coercion_for(f) is None:
                    unmapped.append(f"{key}: {f.type}")

        walk(Config())
        assert not unmapped, (
            f"no coercion resolves for {unmapped}. Each is a declared, "
            "documented setting that the walk will ignore with only a log "
            "line. Teach `_resolve` the shape, or give the field a hook."
        )

    def test_a_field_that_is_not_configuration_is_a_real_field(self):
        live = config_module._NOT_CONFIGURATION - self.dotted_keys()
        assert not live, (
            f"_NOT_CONFIGURATION names {sorted(live)}, which no dataclass field "
            "declares. The entry is doing nothing."
        )

    def test_no_nested_dataclass_is_shared_between_two_configs(self):
        """The invariant the in-place mutation depends on.

        The walk mutates nested dataclasses rather than rebuilding them, so a
        nested field declared `field(default=X())` instead of
        `default_factory=X` would be one object shared by every `Config()` in
        the process -- and one load would leak its values into every other.
        Nothing in the tree does that today; this is what keeps it that way.
        """
        a, b = Config(), Config()
        shared = []

        def walk(x, y, prefix=""):
            for f in dataclasses.fields(x):
                va, vb = getattr(x, f.name, None), getattr(y, f.name, None)
                key = f"{prefix}.{f.name}" if prefix else f.name
                if dataclasses.is_dataclass(va) and not isinstance(va, type):
                    if va is vb:
                        shared.append(key)
                    else:
                        walk(va, vb, key)
                elif isinstance(va, (list, dict, set)) and va is vb:
                    shared.append(key)

        walk(a, b)
        assert not shared, (
            f"{shared} is one object shared by every Config(). The walk mutates "
            "in place, so a load would leak its values process-wide. Use "
            "field(default_factory=...)."
        )


class TestCoercion:
    """The traps a uniform cast would walk into.

    ``bool("false")`` is ``True``; the security block carried a hand-written
    copy of this table for the one key where getting it backwards left a delete
    path running on a cadence. ``int(True)`` is ``1``. ``NaN`` compares false
    against every threshold, so a non-finite ceiling switches off the
    comparison it feeds rather than failing.
    """

    @pytest.mark.parametrize("raw, expected", [
        (True, True), (False, False),
        ("true", True), ("false", False), ("  ON ", True), ("off", False),
        ("1", True), ("0", False),
    ])
    def test_bool_strings_are_read_as_written(self, raw, expected):
        assert coerce_bool(raw, "k") is expected

    def test_a_bool_that_is_neither_keeps_the_default(self):
        assert coerce_bool("maybe", "k") is _KEEP

    def test_a_bool_is_not_an_int(self):
        assert coerce_int(True, "k") is _KEEP

    def test_a_non_finite_float_keeps_the_default(self):
        assert coerce_float(float("nan"), "k") is _KEEP
        assert coerce_float(float("inf"), "k") is _KEEP

    def test_a_numeric_string_is_read(self):
        assert coerce_int("42", "k") == 42
        assert coerce_float("1.5", "k") == 1.5

    def test_a_fractional_float_is_not_an_int(self):
        assert coerce_int(1.5, "k") is _KEEP
        assert coerce_int(2.0, "k") == 2

    def test_one_bad_element_drops_itself_not_the_list(self):
        assert coerce_str_list(["a", {"b": 1}, "c"], "k") == ["a", "c"]

    def test_a_non_list_keeps_the_default(self):
        assert coerce_str_list("a,b", "k") is _KEEP


class TestApplySection:
    """The walk itself, against a dataclass tree the product does not own."""

    def test_a_nested_section_is_mutated_not_rebuilt(self):
        cfg = Config()
        cfg.scheduler.poll_interval = 99
        apply_section(cfg, {"scheduler": {"email_poll_interval": 7}})
        assert cfg.scheduler.email_poll_interval == 7
        # The pre-set sibling survives, which is what a caller relying on a
        # hook to build part of a section depends on.
        assert cfg.scheduler.poll_interval == 99

    def test_a_hook_wins_over_the_coercion(self):
        cfg = Config()
        apply_section(
            cfg, {"bot_name": "raw"},
            hooks={"bot_name": lambda raw, key: raw.upper()},
        )
        assert cfg.bot_name == "RAW"

    def test_a_hook_may_keep_the_default(self):
        cfg = Config()
        before = cfg.bot_name
        apply_section(cfg, {"bot_name": "x"}, hooks={"bot_name": lambda raw, key: _KEEP})
        assert cfg.bot_name == before

    def test_a_skipped_key_is_neither_mapped_nor_reported(self):
        cfg = Config()
        before = cfg.bot_name
        unknown: list[str] = []
        apply_section(
            cfg, {"bot_name": "x"}, unknown=unknown, skip=frozenset({"bot_name"}),
        )
        assert cfg.bot_name == before
        assert unknown == []

    def test_a_section_that_is_not_a_table_is_ignored(self, caplog):
        cfg = Config()
        with caplog.at_level(logging.WARNING):
            apply_section(cfg, {"scheduler": "nope"})
        assert cfg.scheduler.poll_interval == Config().scheduler.poll_interval
        assert "must be a table" in caplog.text

    def test_unknown_keys_are_collected_with_their_dotted_path(self):
        cfg = Config()
        unknown: list[str] = []
        apply_section(cfg, {"scheduler": {"nope": 1}, "alsonope": 2}, unknown=unknown)
        assert sorted(unknown) == ["alsonope", "scheduler.nope"]


class TestTalkPollTimeoutIsRefusedAtZero:
    """``scheduler.talk_poll_timeout`` at ``0`` silently kills Talk inbound.

    The setting has two consumers meaning different durations. It is sent to
    Nextcloud as the ``timeout`` query parameter — how long the *server* holds
    the request — and it used to be handed to ``asyncio.wait`` as well, as how
    long the client waits for every room at once. At ``0`` the second one
    returns before any round trip can finish: ``done`` comes back empty, the
    ``if done and pending`` grace window is skipped for want of an early
    responder, every request is cancelled mid-flight and ``results`` is empty on
    every cycle. No exception, no log line, and the poll loop keeps running
    while messages simply never arrive.

    ISSUE-399 split the two durations, so a ``0`` no longer starves the wait.
    It is still refused, because a zero-second server-side long-poll is a
    request that can never carry news and only costs a round trip — and because
    a value that turns a whole inbound surface into a no-op is a trap rather
    than a setting.
    """

    def test_zero_keeps_the_default(self, tmp_path, caplog):
        p = write(tmp_path, """
[scheduler]
talk_poll_timeout = 0
""")
        with caplog.at_level(logging.WARNING):
            cfg = load_config(p)
        assert cfg.scheduler.talk_poll_timeout == 30, (
            "0 was accepted; it makes every long-poll a round trip that cannot "
            "carry news"
        )
        assert any(
            "talk_poll_timeout" in r.getMessage() for r in caplog.records
        ), "the refusal was silent"

    def test_a_negative_keeps_the_default(self, tmp_path):
        p = write(tmp_path, """
[scheduler]
talk_poll_timeout = -5
""")
        assert load_config(p).scheduler.talk_poll_timeout == 30

    def test_an_ordinary_value_is_still_read(self, tmp_path):
        p = write(tmp_path, """
[scheduler]
talk_poll_timeout = 3
""")
        assert load_config(p).scheduler.talk_poll_timeout == 3

    def test_the_full_sweep_interval_is_read(self, tmp_path):
        """Including ``0``, which is a mode rather than a broken value here.

        The gate is switched off by making every cycle a full sweep, so unlike
        ``talk_poll_timeout`` this one must accept zero — which is exactly the
        distinction ``_positive_int`` and ``_non_negative_int`` exist to keep
        apart.
        """
        p = write(tmp_path, """
[scheduler]
talk_poll_full_sweep_interval = 0
""")
        assert load_config(p).scheduler.talk_poll_full_sweep_interval == 0

        p = write(tmp_path, """
[scheduler]
talk_poll_full_sweep_interval = 900
""")
        assert load_config(p).scheduler.talk_poll_full_sweep_interval == 900

    def test_a_negative_sweep_interval_keeps_the_default(self, tmp_path):
        """`0` is a mode; a negative is a typo that would read as that mode.

        Both `_gate_enabled` and the sweep predicate ask `<= 0`, so `-1` would
        silently disable the gate while every document says `0` is the switch.
        """
        p = write(tmp_path, """
[scheduler]
talk_poll_full_sweep_interval = -1
""")
        assert load_config(p).scheduler.talk_poll_full_sweep_interval == 300

    def test_a_negative_poll_wait_keeps_the_default(self, tmp_path):
        """`talk_poll_wait` became load-bearing arithmetic with this fix.

        The cycle deadline is `talk_poll_timeout + talk_poll_wait`, so a
        negative puts the deadline back inside the server's own hold and
        restores the defect the pair exists to remove. It also makes the
        straggler grace `asyncio.wait(pending, timeout=<negative>)` return
        immediately.
        """
        p = write(tmp_path, """
[scheduler]
talk_poll_wait = -3.0
""")
        assert load_config(p).scheduler.talk_poll_wait == 2.0
