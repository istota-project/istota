"""``[brain.native.session_log]`` — parse, defaults, and the directory resolver.

Stage 2 of the native-brain-session-logs spec. The block is pure configuration:
nothing reads it yet, `NativeBrain` is wired in Stage 3 and the scheduler's
sweep in Stage 4. What this file is for is ruling out the two defect classes
``config_mapper.py``'s own docstring records, both of which are invisible to
every other test in the suite because the symptom is a setting that reads as
having worked:

**A field the loader never read.** Eleven settings were declared on a
dataclass, written by the Ansible template, documented in
``config.example.toml`` and simply missing a line in the hand-written loader,
so the operator set the value and the daemon ran the hardcoded default.
``config_mapper`` walks ``dataclasses.fields`` now, and a nested dataclass is
supposed to recurse with no loader change at all — but "supposed to" is the
same standing this had before, so ``test_every_declared_field_is_settable``
walks the real dataclass rather than restating a list somebody has to
remember to extend.

**Two defaults for one field.** The dataclass said ``True`` and the loader's
``.get(key, default)`` said ``False``, so a bare section header switched the
nightly memory extraction off. Here the dataclass default is the only default,
and the bare-header and absent-block cases assert it directly.

There is a third copy of these numbers and it is deliberate.
``session/session_log.py`` is a stdlib-only leaf that imports no config, and
``config.py`` sits below the session layer in the import graph — it is loaded
by the daemon, the web app, the webhook receiver, every CLI invocation and
every host-side skill CLI the proxy spawns per call, so it does not import
upward to reach a constant. ``TestOneSourceOfTruthForTheDefaults`` is what
holds the two copies equal, on the ``sandbox_cache_sweeper`` precedent.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import textwrap
from pathlib import Path

import pytest

from istota.config import Config, NativeBrainConfig, SessionLogConfig, load_config
from istota.session import session_log

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO / "config" / "config.example.toml"


def _load(tmp_path, body: str) -> Config:
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(body))
    return load_config(cfg)


class TestDefaults:
    """An absent block is the documented shipping behaviour."""

    def test_the_documented_defaults(self, tmp_path):
        """The table in the spec's "Data & config changes", field by field.

        Spelled out rather than compared to ``SessionLogConfig()`` so that
        changing a shipped default is a visible diff here and not a silent
        pass, which is the whole point of writing the numbers down twice.
        """
        slog = _load(tmp_path, '[brain]\nkind = "native"\n').brain.native.session_log
        assert slog.enabled is True
        assert slog.dir == ""
        assert slog.retention_days == 14
        assert slog.max_total_gb == 2.0
        assert slog.max_content_chars == 32768
        assert slog.max_args_chars == 8192
        assert slog.include_thinking is True

    def test_an_absent_block_equals_the_dataclass(self, tmp_path):
        config = _load(tmp_path, '[brain]\nkind = "native"\n')
        assert config.brain.native.session_log == SessionLogConfig()

    def test_no_brain_section_at_all(self, tmp_path):
        config = _load(tmp_path, '[bot]\nname = "Istota"\n')
        assert config.brain.native.session_log == SessionLogConfig()

    def test_a_bare_header_leaves_every_default_intact(self, tmp_path):
        """The "two defaults for one field" case, asserted directly.

        A section header with no keys under it must not move a single field.
        The defect this reproduces switched the nightly memory extraction off
        for anyone who wrote ``[sleep_cycle]`` and nothing beneath it.
        """
        config = _load(tmp_path, """
            [brain]
            kind = "native"

            [brain.native.session_log]
        """)
        assert config.brain.native.session_log == SessionLogConfig()

    def test_a_bare_parent_header_leaves_every_default_intact(self, tmp_path):
        config = _load(tmp_path, '[brain.native]\n')
        assert config.brain.native.session_log == SessionLogConfig()

    def test_a_non_table_block_is_ignored(self, tmp_path):
        config = _load(tmp_path, '[brain.native]\nsession_log = "yes"\n')
        assert config.brain.native.session_log == SessionLogConfig()

    def test_the_field_is_not_shared_between_instances(self):
        """``field(default_factory=...)``, not a mutable class attribute.

        A shared instance would make one deployment's parsed value the default
        for the next ``Config`` built in the same process — which the tests
        themselves do, dozens of times.
        """
        a, b = NativeBrainConfig(), NativeBrainConfig()
        assert a.session_log is not b.session_log


class TestParse:
    """A full block maps onto every field — the nested dataclass recursion."""

    def test_every_field_parses(self, tmp_path):
        config = _load(tmp_path, """
            [brain]
            kind = "native"

            [brain.native.session_log]
            enabled = false
            dir = "/srv/app/istota/transcripts"
            retention_days = 30
            max_total_gb = 7.5
            max_content_chars = 4096
            max_args_chars = 1024
            include_thinking = false
        """)
        slog = config.brain.native.session_log
        assert slog.enabled is False
        assert slog.dir == "/srv/app/istota/transcripts"
        assert slog.retention_days == 30
        assert slog.max_total_gb == 7.5
        assert slog.max_content_chars == 4096
        assert slog.max_args_chars == 1024
        assert slog.include_thinking is False

    def test_every_declared_field_is_settable(self, tmp_path):
        """Walk the dataclass, not a list somebody has to remember to extend.

        This is the guard against the defect class where a field is declared,
        documented and templated, and the loader never reads it. Restating the
        field names above would reproduce exactly the duplicated schema that
        carried the defect, so the check derives them: every field gets a value
        that differs from its own default, and every field has to come back
        holding it.
        """
        fields = dataclasses.fields(SessionLogConfig)
        assert fields, "SessionLogConfig declares no fields; the walk has rotted"

        lines, expected = [], {}
        for f in fields:
            default = getattr(SessionLogConfig(), f.name)
            if isinstance(default, bool):
                value, literal = (not default), str(not default).lower()
            elif isinstance(default, str):
                value = literal = "/tmp/istota-session-log-probe"
                literal = f'"{value}"'
            elif isinstance(default, int):
                value, literal = default + 7, str(default + 7)
            elif isinstance(default, float):
                value, literal = default + 0.5, repr(default + 0.5)
            else:  # pragma: no cover - a new field shape needs a decision here
                pytest.fail(f"{f.name} has type {type(default).__name__}; teach this test")
            lines.append(f"{f.name} = {literal}")
            expected[f.name] = value

        body = "[brain.native.session_log]\n" + "\n".join(lines) + "\n"
        slog = _load(tmp_path, body).brain.native.session_log
        got = {name: getattr(slog, name) for name in expected}
        assert got == expected

    def test_an_integer_ceiling_becomes_a_float(self, tmp_path):
        """TOML ``5`` and ``5.0`` must not produce different types.

        The sweep divides by this and compares bytes against it; an int here
        would work and would still be a different object than every other
        deployment holds.
        """
        config = _load(tmp_path, '[brain.native.session_log]\nmax_total_gb = 5\n')
        assert isinstance(config.brain.native.session_log.max_total_gb, float)
        assert config.brain.native.session_log.max_total_gb == 5.0

    def test_a_quoted_boolean_is_read_as_a_boolean(self, tmp_path):
        """A rendered config is where a quoted boolean comes from.

        ``bool("false")`` is True, and ``enabled`` is the field that decides
        whether the deployment writes user-private content to disk at all.
        "Operator wrote false, the writer stayed on" is the one failure this
        parse must not have.
        """
        config = _load(tmp_path, '[brain.native.session_log]\nenabled = "false"\n')
        assert config.brain.native.session_log.enabled is False


class TestABadValueNeverStopsTheDaemon:
    """``load_config`` runs in the scheduler, the web app, the webhook receiver
    and every host-side skill CLI the proxy spawns per call. A typo on an
    observability knob must not stop any of them from starting."""

    def test_an_unknown_key_warns_and_does_not_raise(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.native.session_log]
                enabled = false
                retention_dayz = 30
            """)
        # The typo is named, so an operator can act on it...
        assert any(
            "brain.native.session_log.retention_dayz" in r.getMessage()
            for r in caplog.records if r.name == "istota.config"
        )
        # ...and the keys beside it still took effect.
        assert config.brain.native.session_log.enabled is False
        assert config.brain.native.session_log.retention_days == 14

    @pytest.mark.parametrize("body", [
        "retention_days = 'thirty'",
        "max_total_gb = nan",
        "max_total_gb = inf",
        "max_content_chars = true",
        "enabled = 'maybe'",
    ])
    def test_an_uninterpretable_value_takes_the_default(self, tmp_path, body):
        config = _load(tmp_path, f"[brain.native.session_log]\n{body}\n")
        assert config.brain.native.session_log == SessionLogConfig()


class TestOneSourceOfTruthForTheDefaults:
    """``session_log.py`` imports no config and carries its own copy of these
    numbers. This is what stops the two drifting."""

    def test_the_policy_defaults_match(self):
        slog = SessionLogConfig()
        assert slog.max_content_chars == session_log.DEFAULT_MAX_CONTENT_CHARS
        assert slog.max_args_chars == session_log.DEFAULT_MAX_ARGS_CHARS

    def test_the_sweep_defaults_match(self):
        slog = SessionLogConfig()
        assert slog.retention_days == session_log.DEFAULT_RETENTION_DAYS
        assert slog.max_total_gb == session_log.DEFAULT_MAX_TOTAL_GB

    def test_the_shipped_policy_is_reachable_from_the_config(self):
        """A ``SessionLogPolicy`` built from an untouched config is the
        writer's own default, so the disabled and default paths agree."""
        slog = SessionLogConfig()
        built = session_log.SessionLogPolicy(
            max_content_chars=slog.max_content_chars,
            max_args_chars=slog.max_args_chars,
            include_thinking=slog.include_thinking,
        )
        assert built == session_log.SessionLogPolicy()


class TestResolveSessionLogDir:
    """The ``""`` → ``{db_path.parent}/logs`` rule, in one pure function.

    A free function of ``db_path`` and the configured string rather than a
    method on ``Config``: the writer, the sweep, ``doctor`` and the skill proxy
    all have to agree on one answer, and a second copy of the rule is how a
    checker starts passing while the real thing is wrong.
    """

    def test_blank_resolves_beside_the_framework_database(self):
        resolved = session_log.resolve_session_log_dir(Path("/srv/app/istota/data/istota.db"), "")
        assert resolved == Path("/srv/app/istota/data/logs")

    def test_blank_resolves_from_a_loaded_config(self, tmp_path):
        config = _load(tmp_path, f"""
            db_path = "{tmp_path / 'data' / 'istota.db'}"
        """)
        resolved = session_log.resolve_session_log_dir(
            config.db_path, config.brain.native.session_log.dir
        )
        assert resolved == tmp_path / "data" / "logs"

    def test_the_docker_shape_resolves_onto_the_data_volume(self):
        """The decision recorded in ``config.example.toml``: Docker gets no
        knobs and runs this."""
        assert session_log.resolve_session_log_dir(Path("/data/istota.db"), "") == Path("/data/logs")

    def test_an_absolute_directory_is_used_as_given(self):
        resolved = session_log.resolve_session_log_dir(
            Path("/srv/app/istota/data/istota.db"), "/var/lib/istota/transcripts"
        )
        assert resolved == Path("/var/lib/istota/transcripts")

    def test_a_relative_directory_resolves_against_nothing(self):
        """Used as given, not joined to ``db_path.parent`` and not made
        absolute. Resolving it here would give the daemon and a CLI run from
        another directory two different answers for one config file."""
        resolved = session_log.resolve_session_log_dir(
            Path("/srv/app/istota/data/istota.db"), "transcripts"
        )
        assert resolved == Path("transcripts")
        assert not resolved.is_absolute()

    def test_nothing_expands_a_tilde(self):
        """Matching every other path in ``config.py``, none of which expands
        one. Silently expanding here would be the only place that did."""
        resolved = session_log.resolve_session_log_dir(Path("/data/istota.db"), "~/logs")
        assert resolved == Path("~/logs")

    @pytest.mark.parametrize("configured", ["", "   ", "\t\n"])
    def test_whitespace_only_is_blank(self, configured):
        assert session_log.resolve_session_log_dir(
            Path("/data/istota.db"), configured
        ) == Path("/data/logs")

    def test_surrounding_whitespace_is_dropped(self):
        """A rendered config is where a stray space comes from; a directory
        deliberately named with one is not."""
        assert session_log.resolve_session_log_dir(
            Path("/data/istota.db"), "  /var/logs/istota  "
        ) == Path("/var/logs/istota")

    def test_a_missing_db_path_still_answers(self):
        """Never raises: ``Config.db_path`` carries a default so no shipped
        shape reaches this, and the callers are all on the task path."""
        assert session_log.resolve_session_log_dir(None, "") == Path("logs")
        assert session_log.resolve_session_log_dir("", "") == Path("logs")

    def test_a_string_db_path_is_accepted(self):
        assert session_log.resolve_session_log_dir(
            "/srv/app/istota/data/istota.db", ""
        ) == Path("/srv/app/istota/data/logs")

    def test_it_is_never_under_the_sandbox_writable_temp_dir(self):
        """The one placement that would be wrong on every shape.

        ``temp_dir`` is bound read-write into every sandbox because it doubles
        as ``ISTOTA_DEFERRED_DIR``. Logs there would hand every task the full
        transcript of every previous task for that user — assembled prompt,
        memory and channel context included — and let it rewrite the record of
        what it did.
        """
        config = Config()
        resolved = session_log.resolve_session_log_dir(
            config.db_path, config.brain.native.session_log.dir
        )
        temp_dir = Path(config.temp_dir).resolve()
        assert not resolved.resolve().is_relative_to(temp_dir)


class TestTheExampleConfigDocumentsIt:
    """A field the operator cannot learn about is a field nobody sets."""

    def test_every_field_appears_in_the_commented_block(self):
        text = EXAMPLE_CONFIG.read_text()
        assert "[brain.native.session_log]" in text
        block = text.split("[brain.native.session_log]", 1)[1]
        # Up to the next commented section header.
        block = re.split(r"^# \[", block, maxsplit=1, flags=re.M)[0]
        missing = [
            f.name for f in dataclasses.fields(SessionLogConfig)
            if not re.search(rf"^#\s*{f.name}\s*=", block, re.M)
        ]
        assert not missing, (
            f"{missing} are settable and undocumented in config.example.toml. "
            "They work and there is no way to learn they exist."
        )

    def test_the_docker_decision_is_written_down(self):
        """Resolved before implementation, item 2: Docker gets no knobs.

        Recorded as a comment rather than left implicit, because the
        half-wired version — a variable the generator reads and compose never
        passes — is the documented defect class, and an implicit decision is
        one the next person re-makes badly. The repo-wide invariant is held by
        ``test_render_config.py``'s passthrough guard, which already scans
        every ``ISTOTA_*`` name; this only pins that the reasoning stays where
        somebody will find it.
        """
        text = EXAMPLE_CONFIG.read_text()
        block = text.split("[brain.native.session_log]", 1)[0].rsplit("# [brain.native]", 1)[-1]
        assert "DOCKER" in block
        assert "render-config.sh" in block and "docker-compose.yml" in block
