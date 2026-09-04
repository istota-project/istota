"""Each brain's default model and effort are its own (ISSUE-418).

The top-level `model` / `effort` were the *claude_code* brain's defaults living
at the root, a vestige of there having been one brain. Because they sat there
they read as deployment-wide, and the executor treated them as one — filling
every request with them whatever brain was about to run. `NativeBrain` has
always had the right shape (`req.model or self._config.model`) and that `or` was
unreachable, so a room pinned to `native` with `[brain.native] model` set ran the
Claude model against the native endpoint, billed per token.

Three layers, and each one is separately capable of reintroducing the defect:
the executor must send a *task* pin or nothing, each brain must fill its own,
and the config loader must migrate the retired top-level keys onto the two CLI
brains rather than onto native.

The `_RecordingBrain` + `_config` + `_run` trio is the one from
`tests/test_executor_brain_identity.py`, whose stated job is to return the
`BrainRequest` the executor built.
"""

import re
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import db
from istota.brain import (
    BRAIN_CONFIG_BLOCK,
    KNOWN_BRAIN_KINDS,
    BrainRequest,
    BrainResult,
    configured_default_model_effort,
    make_brain,
)
from istota.brain.claude_code import OPUS, ClaudeCodeBrain
from istota.config import (
    BrainConfig,
    ClaudeCodeBrainConfig,
    Config,
    NativeBrainConfig,
    SecurityConfig,
    TmuxBrainConfig,
    load_config,
)


class _RecordingBrain:
    """Stands in for whatever `make_brain` would have returned."""

    model_namespace = "anthropic"
    supports_steering = False
    default_model = ""
    default_effort = ""

    def __init__(self):
        self.requests: list[BrainRequest] = []

    def resolve_model_name(self, name):
        return name

    def validate_alias_override(self, name, target):
        return []

    def execute(self, req: BrainRequest) -> BrainResult:
        self.requests.append(req)
        return BrainResult(success=True, result_text="ok", stop_reason="completed")


def _config(tmp_path, **overrides):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    skills_dir = tmp_path / "config" / "skills"
    skills_dir.mkdir(parents=True)
    kwargs = dict(
        db_path=db_path,
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty_bundled",
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(skill_proxy_enabled=False),
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _run(tmp_path, config=None, **task_kwargs) -> BrainRequest:
    config = config if config is not None else _config(tmp_path)
    (tmp_path / "temp" / "alice").mkdir(parents=True, exist_ok=True)
    brain = _RecordingBrain()
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn,
            prompt="do the thing",
            user_id="alice",
            source_type="talk",
            conversation_token="a1b2c3d4",
            **task_kwargs,
        )
        task = db.get_task(conn, task_id)
        with patch("istota.executor.make_brain", return_value=brain):
            from istota.executor import execute_task

            execute_task(task, config, [], conn=conn)
    assert brain.requests, "the brain was never called"
    return brain.requests[-1]


def _req(tmp_path=None, **kwargs) -> BrainRequest:
    """A minimal BrainRequest. Only model/effort matter to these tests."""
    fields = dict(
        prompt="hi",
        allowed_tools=[],
        cwd=Path("/tmp"),
        env={},
        timeout_seconds=60,
    )
    fields.update(kwargs)
    return BrainRequest(**fields)


class TestTheExecutorSendsATaskPinOrNothing:
    """The half of the defect that lives in `execute_task`.

    A deployment default reaching the request is what made every brain's own
    `or` unreachable. These assertions are the ones that could not have passed
    before the change: the pre-fix executor substituted `config.model` here.
    """

    def test_an_unpinned_task_carries_no_model(self, tmp_path):
        config = _config(tmp_path, model="claude-sonnet-4-6", effort="medium")
        req = _run(tmp_path, config=config)
        assert req.model == ""
        assert req.effort == ""

    def test_a_pinned_task_still_carries_its_pin(self, tmp_path):
        """The change must not stop a genuine per-task pin reaching the brain."""
        config = _config(tmp_path, model="claude-sonnet-4-6")
        req = _run(tmp_path, config=config, model="claude-opus-5")
        assert req.model == "claude-opus-5"

    def test_a_pinned_effort_survives_with_its_model(self, tmp_path):
        config = _config(tmp_path, model="claude-sonnet-4-6", effort="medium")
        req = _run(tmp_path, config=config, model="claude-opus-5", effort="high")
        assert (req.model, req.effort) == ("claude-opus-5", "high")

    def test_a_model_pin_without_an_effort_still_drops_effort(self, tmp_path):
        """The pre-existing rule, which the change must leave standing.

        An effort chosen for one model need not be valid on another — Haiku
        rejects the flag outright — so a per-task model pin drops an inherited
        effort. That used to bite on the top-level default and now bites on the
        brain's own; either way a pinned task carries no effort it did not ask
        for.
        """
        config = _config(tmp_path, model="claude-sonnet-4-6", effort="high")
        req = _run(tmp_path, config=config, model="claude-haiku-4-5")
        assert req.effort == ""


class TestClaudeCodeAppliesItsOwn:
    """The half that lives in the brain: the `or` NativeBrain always had."""

    def _brain(self, **kwargs):
        return make_brain(
            BrainConfig(kind="claude_code", claude_code=ClaudeCodeBrainConfig(**kwargs))
        )

    def test_the_configured_default_fills_an_unpinned_request(self):
        brain = self._brain(model="claude-opus-5", effort="high")
        req = _req()
        filled = brain.with_defaults(req)
        assert (filled.model, filled.effort) == ("claude-opus-5", "high")

    def test_a_request_pin_outranks_the_configured_default(self):
        brain = self._brain(model="claude-opus-5", effort="high")
        filled = brain.with_defaults(_req(model="claude-haiku-4-5"))
        assert filled.model == "claude-haiku-4-5"

    def test_a_pinned_model_takes_no_effort_from_the_block(self):
        """Same rule as the executor's, applied where the default now lives."""
        brain = self._brain(model="claude-opus-5", effort="high")
        filled = brain.with_defaults(_req(model="claude-haiku-4-5"))
        assert filled.effort == ""

    def test_an_alias_in_the_block_resolves_through_the_alias_table(self):
        """`model = "opus"` must mean what `!model opus` means."""
        brain = self._brain(model="opus")
        filled = brain.with_defaults(_req())
        assert filled.model == OPUS

    def test_an_alias_carrying_an_effort_supplies_it(self):
        brain = self._brain(model="opus:high")
        filled = brain.with_defaults(_req())
        assert (filled.model, filled.effort) == (OPUS, "high")

    def test_the_blocks_own_effort_outranks_the_aliases(self):
        """The precedence that makes the migration behaviour-preserving.

        `model = "smart"` with `[models.aliases] smart = "opus:high"` resolves
        through an effort-carrying alias. The block's own `effort = "low"` must
        still win: the operator wrote it beside that model, while the alias's is
        a default for the alias. Getting this backwards made the block's key
        unreachable — and broke the migration's one promise, since the old path
        resolved the model with `resolve_model_name` (which strips the modifier)
        and took the top-level `effort` verbatim, so this config ran at `low`
        before and would have run at `high` after.
        """
        from istota.brain._roles import set_alias_overrides

        set_alias_overrides({"smart": "opus:high"})
        try:
            brain = self._brain(model="smart", effort="low")
            filled = brain.with_defaults(_req())
            assert filled.model == OPUS
            assert filled.effort == "low"
        finally:
            set_alias_overrides({})

    def test_the_alias_effort_is_used_when_the_block_names_none(self):
        from istota.brain._roles import set_alias_overrides

        set_alias_overrides({"smart": "opus:high"})
        try:
            brain = self._brain(model="smart")
            assert brain.with_defaults(_req()).effort == "high"
        finally:
            set_alias_overrides({})

    def test_a_request_effort_outranks_both(self):
        from istota.brain._roles import set_alias_overrides

        set_alias_overrides({"smart": "opus:high"})
        try:
            brain = self._brain(model="smart", effort="low")
            assert brain.with_defaults(_req(effort="max")).effort == "max"
        finally:
            set_alias_overrides({})

    def test_it_is_idempotent(self):
        """Applied by the brain and, on some paths, by a caller before it."""
        brain = self._brain(model="opus:high", effort="low")
        once = brain.with_defaults(_req())
        assert brain.with_defaults(once) == once

    def test_no_configured_default_leaves_the_request_untouched(self):
        """Empty means the CLI's own default, which is an omitted --model."""
        brain = self._brain()
        req = _req()
        assert brain.with_defaults(req) is req

    def test_a_bare_brain_has_no_default(self):
        """The construction used by the resolver-only callers."""
        assert ClaudeCodeBrain().default_model == ""
        assert ClaudeCodeBrain().default_effort == ""

    def test_the_default_reaches_the_argv(self, tmp_path):
        """End to end through the flag builder, which is what the CLI reads."""
        from istota.brain.claude_code import build_claude_cli_flags

        brain = self._brain(model="claude-opus-5", effort="high")
        filled = brain.with_defaults(_req(allowed_tools=["Bash"]))
        flags = build_claude_cli_flags(filled)
        assert flags[flags.index("--model") + 1] == "claude-opus-5"
        assert flags[flags.index("--effort") + 1] == "high"


class TestNativeReachesItsOwnDefault:
    """The reported symptom, at the seam it was reported at.

    A room pinned to `native` on a `claude_code` deployment. The pin worked and
    the model did not: what the brain was *handed* was the top-level default.
    """

    def test_the_native_block_is_what_an_unpinned_native_task_runs(self):
        brain = make_brain(
            BrainConfig(
                kind="native",
                native=NativeBrainConfig(model="z-ai/glm-5.3-flash", effort="high"),
            )
        )
        assert brain.default_model == "z-ai/glm-5.3-flash"
        assert brain.default_effort == "high"

    def test_the_top_level_default_never_migrates_onto_native(self, tmp_path):
        """The one direction the migration must refuse.

        An Anthropic model id cannot carry to an openai_compat endpoint, so
        migrating the retired key here would be the defect inside the fix.
        """
        config = _load(
            tmp_path,
            """
            model = "claude-opus-5"
            [brain]
            kind = "native"
            [brain.native]
            model = "z-ai/glm-5.3-flash"
            """,
        )
        assert config.brain.native.model == "z-ai/glm-5.3-flash"

    def test_an_unset_native_model_is_not_filled_from_the_top_level(self, tmp_path):
        config = _load(
            tmp_path,
            """
            model = "claude-opus-5"
            [brain]
            kind = "native"
            """,
        )
        assert config.brain.native.model == ""


def _load(tmp_path, body: str):
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(body))
    return load_config(cfg)


class TestTheLegacyKeysMigrate:
    """`_apply_legacy_brain_defaults`, the third layer.

    Every existing deployment sets the top-level keys, so the migration is what
    makes the change behaviour-preserving rather than a silent model swap on
    upgrade.
    """

    def test_the_top_level_model_fills_both_cli_brains(self, tmp_path):
        config = _load(tmp_path, 'model = "claude-opus-5"\n')
        assert config.brain.claude_code.model == "claude-opus-5"
        assert config.brain.tmux.model == "claude-opus-5"

    def test_the_top_level_effort_fills_both_cli_brains(self, tmp_path):
        config = _load(tmp_path, 'effort = "high"\n')
        assert config.brain.claude_code.effort == "high"
        assert config.brain.tmux.effort == "high"

    def test_an_explicit_block_value_wins(self, tmp_path):
        """The new spelling always wins; the migration only fills a gap."""
        config = _load(
            tmp_path,
            """
            model = "claude-opus-5"
            [brain.claude_code]
            model = "claude-haiku-4-5"
            """,
        )
        assert config.brain.claude_code.model == "claude-haiku-4-5"
        assert config.brain.tmux.model == "claude-opus-5"

    def test_the_migration_warns(self, tmp_path, caplog):
        with caplog.at_level("WARNING", logger="istota.config"):
            _load(tmp_path, 'model = "claude-opus-5"\n')
        assert any("ISSUE-418" in r.getMessage() for r in caplog.records)

    def test_a_file_without_the_key_does_not_warn(self, tmp_path, caplog):
        """A deployment already on the new spelling must be quiet.

        Keyed on the key's presence in the file rather than on the resolved
        value, so an operator who wrote `model = ""` deliberately is still told
        the key is retired, while one who has migrated hears nothing.
        """
        with caplog.at_level("WARNING", logger="istota.config"):
            _load(
                tmp_path,
                """
                [brain.claude_code]
                model = "claude-opus-5"
                """,
            )
        assert not [r for r in caplog.records if "ISSUE-418" in r.getMessage()]

    def test_an_explicitly_empty_top_level_key_still_warns(self, tmp_path, caplog):
        with caplog.at_level("WARNING", logger="istota.config"):
            _load(tmp_path, 'model = ""\n')
        assert [r for r in caplog.records if "ISSUE-418" in r.getMessage()]

    def test_an_absent_top_level_leaves_the_blocks_empty(self, tmp_path):
        config = _load(tmp_path, "[brain]\nkind = \"claude_code\"\n")
        assert config.brain.claude_code.model == ""
        assert config.brain.tmux.model == ""


class TestTheConfiguredDefaultLookup:
    """`configured_default_model_effort` — a lookup, not a construction.

    Its callers only report the default; building a brain to ask costs a CLI
    version probe per task on the tmux kind and a provider client on native.
    """

    def test_it_reads_the_block_the_kind_names(self):
        cfg = BrainConfig(
            kind="claude_code",
            claude_code=ClaudeCodeBrainConfig(model="claude-opus-5", effort="high"),
        )
        assert configured_default_model_effort(cfg) == ("claude-opus-5", "high")

    def test_tmux_reads_the_tmux_block(self):
        """The one place kind and block name disagree."""
        cfg = BrainConfig(
            kind="tmux_claude", tmux=TmuxBrainConfig(model="claude-opus-5")
        )
        assert configured_default_model_effort(cfg) == ("claude-opus-5", "")

    def test_native_reads_the_native_block(self):
        cfg = BrainConfig(
            kind="native", native=NativeBrainConfig(model="z-ai/glm-5.3-flash")
        )
        assert configured_default_model_effort(cfg) == ("z-ai/glm-5.3-flash", "")

    def test_an_unknown_kind_answers_empty_rather_than_raising(self):
        assert configured_default_model_effort(BrainConfig(kind="nope")) == ("", "")

    def test_it_agrees_with_the_brain_it_describes(self):
        """The lookup and the construction must not drift.

        Two ways to ask one question is how they start disagreeing, so this
        pins them together rather than trusting the table.
        """
        for cfg in (
            BrainConfig(
                kind="claude_code",
                claude_code=ClaudeCodeBrainConfig(model="claude-opus-5", effort="high"),
            ),
            BrainConfig(
                kind="native", native=NativeBrainConfig(model="glm", effort="low")
            ),
        ):
            brain = make_brain(cfg)
            assert configured_default_model_effort(cfg) == (
                brain.default_model,
                brain.default_effort,
            )


class TestTheProtocolAndTheLookupAgree:
    """Two ways to ask one question, held together (ISSUE-418).

    `Brain.default_model` / `default_effort` are declared on the protocol with
    concrete bodies returning `""`, so a brain that forgets to override them
    still satisfies it and reports "no default" — while
    `configured_default_model_effort` reads the config block directly and
    answers correctly. The two are read by different surfaces
    (`web_app._admin_models_section` uses the brain, the scheduler uses the
    lookup), so a silent disagreement shows up as one surface naming a model
    and the other naming none.

    Parametrized over every buildable kind rather than a hand-listed few, so a
    fourth brain fails this test rather than the deployment.
    """

    @pytest.mark.parametrize("kind", sorted(KNOWN_BRAIN_KINDS))
    def test_every_buildable_kind_reports_its_own_block(self, kind):
        block = BRAIN_CONFIG_BLOCK[kind]
        cfg = BrainConfig(kind=kind)
        setattr(
            getattr(cfg, block),
            "model",
            "claude-opus-5" if kind != "native" else "vendor/m",
        )
        getattr(cfg, block).effort = "high"

        brain = make_brain(cfg)
        assert configured_default_model_effort(cfg) == (
            brain.default_model,
            brain.default_effort,
        )
        assert brain.default_model, "the brain reports no default at all"

    def test_every_buildable_kind_has_a_config_block(self):
        """`BRAIN_CONFIG_BLOCK` must cover what `make_brain` can build."""
        assert set(BRAIN_CONFIG_BLOCK) == set(KNOWN_BRAIN_KINDS)


class TestTheTmuxBrainBorrowsOnlyModelAndEffort:
    """`TmuxClaudeBrain` hands its own `[brain.tmux]` block to `ClaudeCodeBrain`.

    That works because the only attributes `ClaudeCodeBrain` reads off a config
    are `model` and `effort`, and `TmuxBrainConfig` declares both. Every read is
    a `getattr` with a swallowing default, so if it later reads anything else —
    `subscription_usage` is the obvious candidate, and it lives on the *other*
    dataclass — the tmux-composed instance would silently take the default
    instead of the operator's value, with no attribute error and nothing red.

    This pins the premise rather than the consequence: it fails when the set of
    attributes read off `_config` grows, which is the moment to decide whether
    to pass a narrower object instead.
    """

    def test_it_reads_only_model_and_effort_off_its_config(self):
        import inspect

        from istota.brain import claude_code as cc

        source = inspect.getsource(cc.ClaudeCodeBrain)
        read = set(re.findall(r'self\._config,\s*"([a-z_]+)"', source))
        read |= set(re.findall(r"self\._config\.([a-z_]+)", source))
        assert read <= {"model", "effort"}, (
            "ClaudeCodeBrain now reads "
            f"{sorted(read - {'model', 'effort'})} off its config, which "
            "TmuxClaudeBrain does not supply — it passes a TmuxBrainConfig. "
            "Pass a narrower object, or add the field to both blocks."
        )

    def test_the_tmux_block_supplies_what_is_read(self):
        brain = make_brain(
            BrainConfig(
                kind="tmux_claude",
                tmux=TmuxBrainConfig(model="claude-opus-5", effort="high"),
            )
        )
        assert brain.default_model == "claude-opus-5"
        assert brain.default_effort == "high"


class TestTheEffortTheAttemptRanWith:
    """`BrainResult.effort_used`, the counterpart of `model_used`.

    A brain fills its configured default onto a `dataclasses.replace` copy, so
    the executor's own `req.effort` stops describing the attempt — and
    `task_usage.effort` recorded the empty string for every unpinned task, which
    is what `!usage --by effort` reads.
    """

    def test_the_brain_reports_the_effort_it_defaulted_to(self):
        brain = make_brain(
            BrainConfig(
                kind="claude_code",
                claude_code=ClaudeCodeBrainConfig(effort="high"),
            )
        )
        with patch("istota.brain.claude_code.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            result = brain.execute(_req())

        assert result.effort_used == "high"

    def test_a_request_pin_is_what_gets_reported(self):
        brain = make_brain(
            BrainConfig(
                kind="claude_code",
                claude_code=ClaudeCodeBrainConfig(effort="high"),
            )
        )
        with patch("istota.brain.claude_code.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            result = brain.execute(_req(effort="low"))

        assert result.effort_used == "low"

    def test_it_defaults_empty_so_an_older_brain_conforms(self):
        assert BrainResult(success=True, result_text="x").effort_used == ""
