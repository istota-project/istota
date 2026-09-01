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

    def test_every_shared_default_matches(self):
        """Derived, not a hand-written list of four.

        The header above argues that restating field names is the pattern that
        carried the defect, and a hand-written parity list has the same shape:
        a sixth shared constant added later would be guarded by nothing. So
        pair the two namespaces mechanically and require the pairing to be
        non-empty, which is what catches the module losing a constant as well
        as the two values drifting.
        """
        slog = SessionLogConfig()
        pairs = {
            f.name: f"DEFAULT_{f.name.upper()}"
            for f in dataclasses.fields(SessionLogConfig)
            if hasattr(session_log, f"DEFAULT_{f.name.upper()}")
        }
        assert pairs, "no field pairs with a DEFAULT_* constant; the pairing has rotted"
        mismatched = {
            name: (getattr(slog, name), getattr(session_log, const))
            for name, const in pairs.items()
            if getattr(slog, name) != getattr(session_log, const)
        }
        assert not mismatched, f"config and session_log disagree: {mismatched}"

    def test_every_default_constant_has_a_field(self):
        """The other direction: a ``DEFAULT_*`` the config never mirrors.

        ``DEFAULT_RETENTION_DAYS`` and ``DEFAULT_MAX_TOTAL_GB`` are read by
        nothing in ``src/`` — ``sweep_session_logs`` takes both as required
        keyword arguments — so their only job is to be the module's written-down
        shipped policy. That is worth keeping and worth pinning; it is not worth
        keeping silently.
        """
        constants = {
            n for n in dir(session_log)
            if n.startswith("DEFAULT_") and isinstance(getattr(session_log, n), (int, float))
        }
        fields = {f"DEFAULT_{f.name.upper()}" for f in dataclasses.fields(SessionLogConfig)}
        assert constants - fields == set(), (
            f"{sorted(constants - fields)} is a shipped default with no config field "
            "mirroring it; either give it one or drop the constant."
        )

    def test_the_documented_ceiling_floor_is_the_real_one(self):
        """``config.example.toml`` and the dataclass docstring both write the
        0.5 floor as a literal. Nothing else held them to the constant."""
        assert session_log.MIN_MAX_TOTAL_GB == 0.5
        text = EXAMPLE_CONFIG.read_text()
        block = text.split("[brain.native.session_log]", 1)[1]
        assert f"{session_log.MIN_MAX_TOTAL_GB} floor" in block

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
        knobs and runs whatever this resolves to, so the comment has to name
        the real directory.

        The ``db_path`` is read out of ``render-config.sh`` rather than written
        here. An earlier version of this test asserted ``/data/istota.db`` ->
        ``/data/logs``, a shape the deployment never produces: the generator
        writes ``/data/db/istota.db``, so the real answer is ``/data/db/logs``.
        It passed and confirmed a fabrication, and the documentation it was
        standing behind was wrong in a way that matters — ``/data/logs`` is a
        sibling of ``db_path.parent`` and would sit outside the mask that
        ``/data/db/logs`` is inside.
        """
        render = (REPO / "docker" / "istota" / "render-config.sh").read_text()
        match = re.search(r'^db_path\s*=\s*"([^"]+)"', render, re.M)
        assert match, "render-config.sh no longer writes a literal db_path; re-read it"
        db_path = Path(match.group(1))
        resolved = session_log.resolve_session_log_dir(db_path, "")
        assert resolved == db_path.parent / "logs"
        # And the example config names that directory rather than another one.
        block = EXAMPLE_CONFIG.read_text().split("[brain.native.session_log]", 1)[0]
        assert str(resolved) in block

    def test_an_absolute_directory_is_used_as_given(self):
        resolved = session_log.resolve_session_log_dir(
            Path("/srv/app/istota/data/istota.db"), "/var/lib/istota/transcripts"
        )
        assert resolved == Path("/var/lib/istota/transcripts")

    def test_a_relative_directory_resolves_against_nothing(self):
        """Used as given, not joined to ``db_path.parent`` and not made
        absolute — the behaviour the spec's test strategy names.

        Note which way the cost runs, because the first draft of this comment
        had it backwards: *not* resolving is what lets the daemon, the sweep
        and a shell command disagree, since each then follows its own working
        directory. Resolving once would give one answer. It is not done here
        because the specified behaviour is "used as given" and because this is
        a pure function with no filesystem access; the consequence is that an
        operator should write an absolute path, which the example config now
        says.
        """
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

    @pytest.mark.parametrize("db_path", [b"/data/istota.db", 5, ["/data"], object()])
    def test_a_db_path_of_the_wrong_type_takes_the_default(self, db_path):
        """"Never raises" has to mean outside the annotated types too.

        ``Path(b"/data/istota.db")`` raises ``TypeError``, and the function was
        asymmetric about it: a bad ``configured`` was coerced and a bad
        ``db_path`` was not. Nothing ``load_config`` produces reaches here —
        ``coerce_path`` accepts only ``Path``/``str`` — but the three callers
        are the task path, a scheduler tick and ``doctor``, and none of them
        has anywhere to put an exception.
        """
        assert session_log.resolve_session_log_dir(db_path, "") == Path("logs")

    @pytest.mark.parametrize("configured", [5, True, ["/a"], {"a": 1}, object()])
    def test_a_configured_value_of_the_wrong_type_takes_the_default(self, configured):
        """It used to become a directory named after its ``repr``.

        Measured before the fix: ``5`` yielded ``Path('5')`` and ``['/a']``
        yielded ``Path("['/a']")`` — a relative directory in the daemon's cwd,
        silently. ``is_one_component`` in this same module opens with an
        ``isinstance`` check for exactly this reason.
        """
        assert session_log.resolve_session_log_dir(
            Path("/data/istota.db"), configured
        ) == Path("/data/logs")

    @pytest.mark.parametrize("configured", ["/", "//", ".", "..", "../..", "a/..", "  /  "])
    def test_a_value_naming_no_directory_is_refused(self, configured, caplog):
        """The blast-radius guard, and the reason it is not merely tidiness.

        The resolved directory is handed to ``sweep_session_logs``, which takes
        every subdirectory of it as a user and unlinks ``*.jsonl`` under each.
        ``dir = "/"`` is therefore a whole-filesystem delete of every ``.jsonl``
        older than ``retention_days``, run as the daemon user from the
        scheduler. Refusing a value that names no directory of its own costs
        nothing and contradicts no specified behaviour — a relative path with a
        name is still honoured, which the test above holds.

        This is NOT general containment: ``/var/log`` still resolves as
        written. Bounding an operator-set root against an ancestor belongs with
        the sweep, where every other delete path in this repo puts it.
        """
        with caplog.at_level(logging.WARNING, logger="istota.session.session_log"):
            resolved = session_log.resolve_session_log_dir(Path("/data/istota.db"), configured)
        assert resolved == Path("/data/logs")
        assert any("names no directory" in r.getMessage() for r in caplog.records)

    def test_a_null_byte_is_refused(self, caplog):
        """``Path`` does not validate one and ``.strip()`` does not remove it.

        Left in, it survives to ``makedirs`` (caught, since the writer wraps
        everything) and to ``Path.iterdir`` in the sweep, which raises
        ``ValueError`` where the guard there catches ``OSError``.
        """
        with caplog.at_level(logging.WARNING, logger="istota.session.session_log"):
            resolved = session_log.resolve_session_log_dir(
                Path("/data/istota.db"), "/var/lo\x00gs"
            )
        assert resolved == Path("/data/logs")
        assert any("null byte" in r.getMessage() for r in caplog.records)

    def test_a_named_directory_is_still_honoured_however_broad(self):
        """The limit of the guard above, stated so nobody reads it as
        containment. Closing this is the retention sweep's job."""
        assert session_log.resolve_session_log_dir(
            Path("/data/istota.db"), "/var/log"
        ) == Path("/var/log")

    def test_a_string_db_path_is_accepted(self):
        assert session_log.resolve_session_log_dir(
            "/srv/app/istota/data/istota.db", ""
        ) == Path("/srv/app/istota/data/logs")

    @pytest.mark.parametrize("shape,db_path,temp_dir", [
        ("ansible", "/srv/app/istota/data/istota.db", "/tmp/istota"),
        ("docker", "/data/db/istota.db", "/data/tmp"),
        ("standalone", "/home/alice/.istota/istota.db", "/home/alice/.istota/tmp"),
    ])
    def test_it_is_never_under_the_sandbox_writable_temp_dir(self, shape, db_path, temp_dir):
        """The one placement that would be wrong on every shape.

        ``temp_dir`` is bound read-write into every sandbox because it doubles
        as ``ISTOTA_DEFERRED_DIR``. Logs there would hand every task the full
        transcript of every previous task for that user — assembled prompt,
        memory and channel context included — and let it rewrite the record of
        what it did.

        Parametrized over the three shipped shapes with absolute paths, because
        the first version of this used a default ``Config()``, whose
        ``db_path`` is the *relative* ``data/istota.db``. ``.resolve()`` then
        made the answer "pytest's working directory plus data/logs", so the
        assertion was really "the repo checkout is not under /tmp/istota" —
        true by accident of where the suite runs, exercising no deployment, and
        red for nothing if the suite ever ran from under ``/tmp/istota``.

        The standalone row is the one that matters: there ``db_path.parent`` is
        the workspace and ``temp_dir`` is a child of it, which is the shape
        where the sandbox mask is refused. The logs land as a *sibling* of the
        temp dir, not inside it.
        """
        resolved = session_log.resolve_session_log_dir(Path(db_path), "")
        assert resolved.is_absolute()
        assert not resolved.is_relative_to(Path(temp_dir)), shape


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
