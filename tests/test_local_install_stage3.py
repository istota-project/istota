"""Stage 3 tests for the local single-user install: the ``istota setup`` wizard.

Drives ``setup_wizard.run_setup`` with mocked stdin + ``shutil.which`` across
the three brain branches, asserts the written ``config.toml`` / ``istota.env``
fields, the DB + workspace bootstrap, and the clobber guard.
"""

from types import SimpleNamespace

import pytest

from istota import setup_wizard
from istota.setup_wizard import Answers, render_config_toml, render_env_file


def _args(**kw):
    base = dict(
        config=None, workspace=None, brain=None, native_base_url=None,
        native_model=None, native_api_key=None, user=None, display_name=None,
        timezone=None, port=None, email=False, location=False, no_money=False,
        yes=False, force=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Pure renderers
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_config_has_local_defaults(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="stefan", web_port=8766)
        toml = render_config_toml(a)
        assert 'auth = "none"' in toml
        assert "[talk]\nenabled = false" in toml
        assert "sandbox_enabled = false" in toml
        assert "[users.stefan]" in toml
        assert "port = 8766" in toml
        assert 'kind = "claude_code"' in toml

    def test_config_native_brain_block(self, tmp_path):
        a = Answers(
            workspace=tmp_path / "ws", user_id="stefan", brain_kind="native",
            native_base_url="https://api.example.com/v1", native_model="my-model",
        )
        toml = render_config_toml(a)
        assert "[brain.native]" in toml
        assert 'base_url = "https://api.example.com/v1"' in toml
        assert 'model = "my-model"' in toml
        # The API key never lands in TOML.
        assert "api_key" not in toml

    def test_config_parses_back(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="stefan", web_port=9000)
        p = tmp_path / "config.toml"
        p.write_text(render_config_toml(a))
        from istota.config import load_config
        cfg = load_config(p)
        assert cfg.web.auth == "none"
        assert cfg.web.port == 9000
        assert cfg.talk.enabled is False
        assert cfg.security.sandbox_enabled is False
        assert "stefan" in cfg.users

    def test_config_disables_emissaries(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="stefan")
        p = tmp_path / "config.toml"
        p.write_text(render_config_toml(a))
        from istota.config import load_config
        cfg = load_config(p)
        assert cfg.emissaries_enabled is False

    def test_env_file_keys(self, tmp_path):
        a = Answers(
            workspace=tmp_path / "ws", brain_kind="native",
            native_api_key="sk-test", session_secret="deadbeef",
        )
        env = render_env_file(a)
        assert "ISTOTA_WEB_INSECURE_COOKIES=1" in env
        assert "ISTOTA_WEB_SESSION_SECRET_KEY=deadbeef" in env
        assert "ISTOTA_BRAIN_NATIVE_API_KEY=sk-test" in env

    def test_env_file_no_native_key_when_claude(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", brain_kind="claude_code", session_secret="x")
        env = render_env_file(a)
        assert "ISTOTA_BRAIN_NATIVE_API_KEY" not in env

    def test_money_on_by_default_no_disabled_modules(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="stefan")
        toml = render_config_toml(a)
        assert "disabled_modules" not in toml

    def test_money_off_writes_disabled_modules(self, tmp_path):
        a = Answers(workspace=tmp_path / "ws", user_id="stefan", money_enabled=False)
        toml = render_config_toml(a)
        assert 'disabled_modules = ["money"]' in toml


# ---------------------------------------------------------------------------
# Timezone resolution (a "PDT" abbreviation must never be stored)
# ---------------------------------------------------------------------------


class TestTimezone:
    def test_is_valid_timezone(self):
        assert setup_wizard._is_valid_timezone("America/Los_Angeles")
        assert setup_wizard._is_valid_timezone("UTC")
        # Abbreviations are NOT valid IANA names.
        assert not setup_wizard._is_valid_timezone("PDT")
        assert not setup_wizard._is_valid_timezone("PST")
        assert not setup_wizard._is_valid_timezone("")

    def test_default_timezone_from_tz_env(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/New_York")
        assert setup_wizard._default_timezone() == "America/New_York"

    def test_default_timezone_ignores_abbreviation_tz_env(self, monkeypatch):
        # A bogus TZ shouldn't win; fall through to /etc/localtime or UTC.
        monkeypatch.setenv("TZ", "PDT")
        assert setup_wizard._default_timezone() != "PDT"

    def test_default_timezone_is_always_a_valid_zone(self):
        # Whatever the host, the derived default must be ZoneInfo-loadable.
        assert setup_wizard._is_valid_timezone(setup_wizard._default_timezone())

    def test_collect_rejects_abbreviation_flag(self):
        # --timezone PDT must not be stored verbatim.
        args = _args(yes=True, timezone="PDT", user="stefan")
        out_lines: list[str] = []
        a = setup_wizard.collect_answers(
            args, input_fn=lambda p: "", which_fn=lambda n: "/usr/bin/claude",
            out=out_lines.append, getpass_fn=lambda p: "",
        )
        assert a.timezone != "PDT"
        assert setup_wizard._is_valid_timezone(a.timezone)
        assert any("not a valid IANA timezone" in line for line in out_lines)

    def test_collect_accepts_valid_flag(self):
        args = _args(yes=True, timezone="Europe/Berlin", user="stefan")
        a = setup_wizard.collect_answers(
            args, input_fn=lambda p: "", which_fn=lambda n: "/usr/bin/claude",
            out=lambda s: None, getpass_fn=lambda p: "",
        )
        assert a.timezone == "Europe/Berlin"


# ---------------------------------------------------------------------------
# Wizard branches
# ---------------------------------------------------------------------------


def _run(args, tmp_path, which_result, inputs=None):
    """Run setup with config dir under tmp_path and mocked which/input."""
    config_path = tmp_path / "cfg" / "config.toml"
    args.config = str(config_path)
    inputs = list(inputs or [])
    it = iter(inputs)

    def fake_input(prompt):
        try:
            return next(it)
        except StopIteration:
            return ""

    def fake_which(name):
        return which_result

    out_lines: list[str] = []
    rc = setup_wizard.run_setup(
        args, input_fn=fake_input, which_fn=fake_which, out=out_lines.append,
        # The API key is read via getpass; share the same input iterator so the
        # flat `inputs` list keeps working in order.
        getpass_fn=fake_input,
    )
    return rc, config_path, out_lines


class TestWizardBranches:
    def test_claude_detected_and_accepted(self, tmp_path):
        # --yes with claude present → claude_code, defaults.
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        toml = config_path.read_text()
        assert 'kind = "claude_code"' in toml
        assert (config_path.parent / "istota.env").exists()

    def test_claude_declined_falls_to_native(self, tmp_path):
        # Interactive: decline claude, then supply native details.
        args = _args(workspace=str(tmp_path / "ws"), user="stefan")
        inputs = [
            "n",                    # decline claude
            "https://api.x/v1",     # base url
            "my-model",             # model
            "sk-abc",               # api key
            "stefan",               # display name
            "UTC",                  # timezone
            "8766",                 # port
            "n",                    # location
            "y",                    # money
            "n",                    # email
        ]
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude", inputs=inputs)
        assert rc == 0
        toml = config_path.read_text()
        assert 'kind = "native"' in toml
        assert 'model = "my-model"' in toml
        env = (config_path.parent / "istota.env").read_text()
        assert "ISTOTA_BRAIN_NATIVE_API_KEY=sk-abc" in env

    def test_no_claude_native_noninteractive(self, tmp_path):
        args = _args(
            yes=True, workspace=str(tmp_path / "ws"), user="stefan",
            brain="native", native_model="m", native_api_key="k",
            native_base_url="https://api.y/v1",
        )
        rc, config_path, _ = _run(args, tmp_path, which_result=None)
        assert rc == 0
        toml = config_path.read_text()
        assert 'kind = "native"' in toml

    def test_native_without_key_errors(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan", brain="native", native_model="m")
        with pytest.raises(setup_wizard.SetupError, match="API key"):
            _run(args, tmp_path, which_result=None)

    def test_native_empty_key_reprompts(self, tmp_path):
        # A stray blank line before the key (paste artifact) must not silently
        # leave it empty — the secret reader re-prompts until a real value.
        args = _args(workspace=str(tmp_path / "ws"), user="stefan")
        inputs = [
            "n",                    # decline claude
            "https://api.x/v1",     # base url
            "my-model",             # model
            "",                     # API key: stray empty line (re-prompts)
            "sk-real",              # API key: real value
            "stefan",               # display name
            "UTC",                  # timezone
            "8766",                 # port
            "n",                    # location
            "y",                    # money
            "n",                    # email
        ]
        rc, config_path, out = _run(
            args, tmp_path, which_result="/usr/bin/claude", inputs=inputs,
        )
        assert rc == 0
        env = (config_path.parent / "istota.env").read_text()
        assert "ISTOTA_BRAIN_NATIVE_API_KEY=sk-real" in env
        assert any("API key is required" in line for line in out)

    def test_native_interactive_key_not_echoed_via_input(self, tmp_path):
        # The key must come from getpass_fn, not input_fn — assert input_fn is
        # never handed the raw key value.
        args = _args(workspace=str(tmp_path / "ws"), user="stefan")
        seen_input_prompts: list[str] = []

        config_path = tmp_path / "cfg" / "config.toml"
        args.config = str(config_path)
        answers = iter([
            "n", "https://api.x/v1", "my-model",  # brain
            "SECRET-KEY",                           # getpass reads this
            "stefan", "UTC", "8766", "n", "y", "n",
        ])

        def fake_input(prompt):
            seen_input_prompts.append(prompt)
            return next(answers)

        def fake_getpass(prompt):
            assert "API key" in prompt
            return next(answers)

        rc = setup_wizard.run_setup(
            args, input_fn=fake_input, which_fn=lambda _n: "/usr/bin/claude",
            out=lambda _l: None, getpass_fn=fake_getpass,
        )
        assert rc == 0
        assert not any("API key" in p for p in seen_input_prompts)

    def test_bootstrap_inits_db_and_workspace(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        # DB created under the workspace.
        db_path = tmp_path / "ws" / "istota.db"
        assert db_path.exists()
        # Workspace dirs seeded.
        assert (tmp_path / "ws" / "Users" / "stefan").is_dir()
        # User profile row exists.
        from istota import user_profiles
        assert user_profiles.get_profile(db_path, "stefan") is not None

    def test_no_money_disables_module_end_to_end(self, tmp_path):
        args = _args(
            yes=True, workspace=str(tmp_path / "ws"), user="stefan", no_money=True,
        )
        rc, config_path, _ = _run(args, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        from istota import user_profiles
        from istota.config import load_config
        db_path = tmp_path / "ws" / "istota.db"
        prof = user_profiles.get_profile(db_path, "stefan")
        assert prof is not None and "money" in prof.disabled_modules
        cfg = load_config(config_path)
        assert cfg.is_module_enabled("stefan", "money") is False
        assert cfg.is_module_enabled("stefan", "feeds") is True

    def test_env_file_is_chmod_600(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        env_path = tmp_path / "cfg" / "istota.env"
        import stat
        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600


class TestClobberGuard:
    def test_refuses_without_force_noninteractive(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        _run(args, tmp_path, which_result="/usr/bin/claude")  # first run
        # Second run without --force must refuse.
        args2 = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        with pytest.raises(setup_wizard.SetupError, match="already exists"):
            _run(args2, tmp_path, which_result="/usr/bin/claude")

    def test_force_overwrites(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        args2 = _args(yes=True, force=True, workspace=str(tmp_path / "ws"), user="bob")
        rc, config_path, _ = _run(args2, tmp_path, which_result="/usr/bin/claude")
        assert rc == 0
        assert "[users.bob]" in config_path.read_text()

    def test_interactive_decline_update_aborts(self, tmp_path):
        args = _args(yes=True, workspace=str(tmp_path / "ws"), user="stefan")
        _run(args, tmp_path, which_result="/usr/bin/claude")
        # Interactive re-run, answer "n" to "update in place?".
        args2 = _args(workspace=str(tmp_path / "ws"), user="stefan")
        rc, _, out = _run(args2, tmp_path, which_result="/usr/bin/claude", inputs=["n"])
        assert rc == 1
        assert any("aborted" in line.lower() for line in out)
