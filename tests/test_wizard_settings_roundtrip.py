"""What `deploy/wizard.sh` asks about has to survive all the way to `config.toml`.

Four files have to agree for a wizard answer to reach the daemon, and each
disagreement is silent in the same way — the operator answers a question, the
install succeeds, and the setting is not there:

  * `wizard.sh` writes a key into `settings.toml`;
  * `settings_to_vars.convert` maps it to an `istota_*` extra-var;
  * `defaults/main.yml` defines that variable (an extra-var naming nothing is
    accepted by Ansible and read by no template); and
  * `config.toml.j2` renders it.

The existing coverage checks the last two against each other. Nothing checked
the first two, which is how `wizard.sh` came to ask no question at all about
`[developer]`, `[talk.signaling]`, `[brain] room_selectable`, `[brain] fallback`
or `[web.map]` while every one of them had a variable and a template line
waiting for it.

So these tests run the real chain rather than asserting name-by-name: a
settings dict through the real `convert()`, into the real template, and then
look for the value in the parsed TOML. A name that is right in three files and
wrong in the fourth fails here.
"""

import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.test_ansible_config_template import render

REPO = Path(__file__).resolve().parent.parent
WIZARD = REPO / "deploy" / "wizard.sh"
DEFAULTS_FILE = REPO / "deploy" / "ansible" / "defaults" / "main.yml"


def _settings_to_vars():
    """`deploy/` is not a package and not on the path; load the module by path."""
    path = REPO / "deploy" / "settings_to_vars.py"
    spec = importlib.util.spec_from_file_location("istota_settings_to_vars", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stv():
    return _settings_to_vars()


def _render_from_settings(stv, settings: dict) -> dict:
    """The whole chain, as the installer runs it: settings -> vars -> config."""
    return tomllib.loads(render(**stv.convert(settings)))


# ---------------------------------------------------------------------------
# The chain, one case per section the stage added.
# ---------------------------------------------------------------------------


class TestASettingsAnswerReachesTheRenderedConfig:
    def test_developer_credentials_and_repos_dir(self, stv):
        """The chicken-and-egg case. `tasks/main.yml` asserts a forge token when
        the skill is on, so a wizard that could not set either left the operator
        editing vars by hand after a run that never asked."""
        config = _render_from_settings(stv, {
            "developer": {
                "enabled": True,
                "repos_dir": "/srv/app/istota/repos",
                "gitlab_url": "https://forge.example.com",
                "gitlab_username": "bot-account",
                "gitlab_token": "glpat-placeholder",
                "github_username": "bot-account",
                "github_token": "ghp-placeholder",
            },
            # The template renders the tokens into config.toml only when the
            # role is not using an environment file. Ask for the shape that
            # puts them in the file, so the assertion can see them.
            "use_environment_file": False,
        })
        assert config["developer"]["enabled"] is True
        assert config["developer"]["repos_dir"] == "/srv/app/istota/repos"
        assert config["developer"]["gitlab_url"] == "https://forge.example.com"
        assert config["developer"]["gitlab_token"] == "glpat-placeholder"
        assert config["developer"]["github_token"] == "ghp-placeholder"

    def test_the_developer_block_is_absent_when_the_wizard_leaves_it_off(self, stv):
        """The other half of the same answer, and the one the assert depends on:
        `enabled = false` has to reach the role, or a settings file written with
        no token still trips the play."""
        config = _render_from_settings(stv, {"developer": {"enabled": False}})
        assert "developer" not in config

    def test_talk_signaling(self, stv):
        config = _render_from_settings(stv, {
            "talk": {"signaling": {"enabled": True, "url": "https://signal.example.com"}},
        })
        assert config["talk"]["signaling"]["enabled"] is True
        assert config["talk"]["signaling"]["url"] == "https://signal.example.com"

    def test_talk_signaling_travels_without_its_parent_section(self, stv):
        """The wizard writes `[talk.signaling]` and no `[talk]` header, because
        writing `[talk]`'s own keys empty would override the role's defaults for
        them. A converter reaching the child only through a populated parent
        would drop the whole answer."""
        settings = {"talk": {"signaling": {"enabled": True}}}
        result = stv.convert(settings)
        assert result["istota_talk_signaling_enabled"] is True
        assert "istota_talk_enabled" not in result
        assert "istota_talk_bot_username" not in result

    def test_brain_room_selectable(self, stv):
        config = _render_from_settings(stv, {
            "brain": {"kind": "claude_code", "room_selectable": ["native", "tmux_claude"]},
        })
        assert config["brain"]["room_selectable"] == ["native", "tmux_claude"]

    def test_an_empty_allowlist_renders_no_key_at_all(self, stv):
        """Empty is the default and means no room or job may pin anything. The
        template guards the key on truthiness, so the absence is the setting."""
        config = _render_from_settings(stv, {"brain": {"room_selectable": []}})
        assert "room_selectable" not in config["brain"]

    def test_brain_fallback(self, stv):
        config = _render_from_settings(stv, {
            "brain": {"kind": "native", "fallback": "claude_code"},
        })
        assert config["brain"]["fallback"] == "claude_code"

    def test_web_map_provider_and_key(self, stv):
        config = _render_from_settings(stv, {
            "web": {"map": {"provider": "carto", "api_key": "placeholder-key"}},
        })
        assert config["web"]["map"]["provider"] == "carto"
        assert config["web"]["map"]["api_key"] == "placeholder-key"

    def test_web_map_custom_styles(self, stv):
        config = _render_from_settings(stv, {
            "web": {"map": {
                "provider": "custom",
                "dark_style": "https://tiles.example.com/dark.json",
                "light_style": "https://tiles.example.com/light.json",
                "attribution": "&copy; Example",
            }},
        })
        assert config["web"]["map"]["provider"] == "custom"
        assert config["web"]["map"]["dark_style"] == "https://tiles.example.com/dark.json"
        assert config["web"]["map"]["light_style"] == "https://tiles.example.com/light.json"
        assert config["web"]["map"]["attribution"] == "&copy; Example"


class TestTheChainWouldNoticeABrokenLink:
    """A rendering test passes for two reasons — the chain works, or the value
    was going to be there anyway. These separate them."""

    @pytest.mark.parametrize("provider", ["osm", "openfreemap"])
    def test_the_provider_is_not_simply_the_default(self, stv, provider):
        assert _render_from_settings(
            stv, {"web": {"map": {"provider": provider}}}
        )["web"]["map"]["provider"] == provider

    def test_an_unmapped_settings_key_does_not_reach_the_config(self, stv):
        """The control for every assertion above: a key the converter does not
        map renders nothing, so the passes are not free."""
        config = _render_from_settings(stv, {"web": {"map": {"nonesuch": "x"}}})
        assert "nonesuch" not in config["web"]["map"]


# ---------------------------------------------------------------------------
# The derivation `fallback` has and the other four do not.
# ---------------------------------------------------------------------------


class TestTheFallbackDerivationSurvivesAnUnansweredPrompt:
    """`istota_brain_fallback`'s default is an expression, not a literal: it
    works out `claude_code` for a tmux_claude deployment and "" for the rest.
    Any extra-var replaces it, so "the operator did not answer" and "the
    operator answered none" must not produce the same settings file."""

    def test_the_default_is_still_an_expression(self):
        """If this stops being derived, the omit-when-absent rule below is
        pointless ceremony and should go with it."""
        line = next(
            text for text in DEFAULTS_FILE.read_text().splitlines()
            if text.startswith("istota_brain_fallback:")
        )
        assert "{{" in line, (
            "istota_brain_fallback is no longer derived; revisit whether the "
            "wizard still needs its 'derive' answer."
        )

    def test_settings_without_the_key_emit_no_variable(self, stv):
        assert "istota_brain_fallback" not in stv.convert({"brain": {"kind": "native"}})

    def test_a_tmux_deployment_keeps_its_derived_failover(self, stv):
        """The case the omission protects, end to end."""
        config = _render_from_settings(stv, {"brain": {"kind": "tmux_claude"}})
        assert config["brain"]["fallback"] == "claude_code"

    def test_an_explicit_empty_answer_does_override_it(self, stv):
        """And the operator can still say no — it just has to be said."""
        config = _render_from_settings(
            stv, {"brain": {"kind": "tmux_claude", "fallback": ""}}
        )
        assert "fallback" not in config["brain"]

    def test_the_wizard_writes_the_key_only_on_an_explicit_answer(self):
        """The shell half of the same rule. `derive` is the wizard's default
        answer and must reach the settings file as no key at all."""
        text = WIZARD.read_text()
        assert '_WIZ_BRAIN_FALLBACK="derive"' in text, "the 'derive' sentinel is gone"
        assert '[ "$_WIZ_BRAIN_FALLBACK" != "derive" ]' in text, (
            "wizard.sh no longer guards the fallback key on an explicit answer, "
            "so an unanswered prompt now overrides the role's derivation."
        )


# ---------------------------------------------------------------------------
# The wizard's own output, run rather than read.
# ---------------------------------------------------------------------------


# Extract `wiz_write_settings` and run it against a fixed set of answers. The
# name-level tests below cannot see the one failure that matters most here: a
# key emitted with malformed TOML on the right-hand side. `room_selectable` is
# the live example — the wizard assembles a TOML array in shell, so an
# unquoted element would be a settings file no installer can read, and every
# static check would still pass.
_HARNESS = r"""
set -euo pipefail
_BOLD=""; _BLUE=""; _GREEN=""; _YELLOW=""; _RED=""; _DIM=""; _RESET=""
eval "$(grep -E '^(info|ok|warn|error|die|section|dim)\(\)' "$WIZ")"
SETTINGS_FILE="$OUT"
ISTOTA_HOME="/srv/app/istota"
ISTOTA_NAMESPACE="istota"
REPO_URL="https://example.invalid/istota.git"
REPO_BRANCH="main"
eval "$(grep -E '^_WIZ_[A-Z0-9_]+=' "$WIZ")"
_WIZ_USER_IDS=()
eval "$(awk '/^prompt_value\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^wiz_brain_policy\(\) \{/,/^\}/' "$WIZ")"
eval "$(awk '/^wiz_write_settings\(\) \{/,/^\}/' "$WIZ")"
eval "${EXTRA:-}"
if [ -n "${ANSWERS:-}" ]; then
    wiz_brain_policy >/dev/null <<< "$ANSWERS"
fi
wiz_write_settings >/dev/null
"""

# Every answer the stage added, set to something a real operator might give.
# No real host, namespace, path or account: this file is committed.
_ALL_ANSWERED = """
_WIZ_DEVELOPER_ENABLED=true
_WIZ_DEVELOPER_REPOS_DIR="/srv/app/istota/repos"
_WIZ_DEVELOPER_GITLAB_URL="https://forge.example.com"
_WIZ_DEVELOPER_GITLAB_USERNAME="bot-account"
_WIZ_DEVELOPER_GITLAB_TOKEN="glpat-placeholder"
_WIZ_DEVELOPER_GITHUB_USERNAME="bot-account"
_WIZ_DEVELOPER_GITHUB_TOKEN="ghp-placeholder"
_WIZ_TALK_SIGNALING_ENABLED=true
_WIZ_TALK_SIGNALING_URL="https://signal.example.com"
_WIZ_WEB_MAP_PROVIDER="carto"
_WIZ_WEB_MAP_API_KEY="placeholder-key"
"""

# The two `[brain]` answers are typed rather than assigned, because the wizard
# builds the allowlist into a TOML array element by element in shell and that
# assembly is the part worth testing. Setting `_WIZ_BRAIN_ROOM_SELECTABLE` to a
# pre-quoted string instead skips it: verified by mutation, an unquoted element
# left every test in this file green.
_BRAIN_ANSWERS = "native, claude_code\nclaude_code\n"


def _run_wizard_write(tmp_path: Path, extra: str = "", answers: str = "") -> dict:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    out = tmp_path / "settings.toml"
    proc = subprocess.run(
        ["bash", "-c", _HARNESS],
        env={
            "WIZ": str(WIZARD),
            "OUT": str(out),
            "EXTRA": extra,
            "ANSWERS": answers,
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"wiz_write_settings failed: {proc.stderr}"
    return tomllib.loads(out.read_text())


class TestTheWizardWritesAFileTheInstallerCanRead:
    def test_the_default_answers_produce_valid_toml(self, tmp_path):
        settings = _run_wizard_write(tmp_path)
        assert settings["developer"]["enabled"] is False
        assert settings["talk"]["signaling"]["enabled"] is False
        assert settings["web"]["map"]["provider"] == "openfreemap"
        assert settings["brain"]["room_selectable"] == []

    def test_the_default_run_writes_no_fallback_key(self, tmp_path):
        """The 'derive' answer, as it reaches disk. A `fallback` of any value
        here would replace the role's derivation for every wizard install."""
        assert "fallback" not in _run_wizard_write(tmp_path)["brain"]

    def test_every_answer_survives_to_the_rendered_config(self, tmp_path, stv):
        """The whole chain in one assertion, starting from the wizard rather
        than from a settings dict written by hand to match it."""
        settings = _run_wizard_write(tmp_path, _ALL_ANSWERED, _BRAIN_ANSWERS)
        settings["use_environment_file"] = False
        config = tomllib.loads(render(**stv.convert(settings)))

        assert config["developer"]["repos_dir"] == "/srv/app/istota/repos"
        assert config["developer"]["gitlab_url"] == "https://forge.example.com"
        assert config["developer"]["gitlab_token"] == "glpat-placeholder"
        assert config["developer"]["github_token"] == "ghp-placeholder"
        assert config["talk"]["signaling"]["enabled"] is True
        assert config["talk"]["signaling"]["url"] == "https://signal.example.com"
        assert config["brain"]["room_selectable"] == ["native", "claude_code"]
        assert config["brain"]["fallback"] == "claude_code"
        assert config["web"]["map"]["provider"] == "carto"
        assert config["web"]["map"]["api_key"] == "placeholder-key"

    def test_the_allowlist_is_written_as_a_toml_array(self, tmp_path):
        """Assembled element by element in shell, which is the one value here
        that can be emitted as something TOML rejects."""
        settings = _run_wizard_write(tmp_path, _ALL_ANSWERED, _BRAIN_ANSWERS)
        assert settings["brain"]["room_selectable"] == ["native", "claude_code"]


# ---------------------------------------------------------------------------
# The wizard and the converter, held together by name.
# ---------------------------------------------------------------------------


def _wizard_keys(section: str) -> set[str]:
    """The `key = ` names wizard.sh writes under a `[section]` header.

    Reads both places the wizard emits TOML — the heredoc and the shell
    variables it builds blocks in — since a section can be written either way
    and a scanner that knew only one would report an empty set, which every
    test below would pass on. Each caller asserts the set is non-empty for that
    reason.

    The header has to be alone on its line: `wizard.sh` names several of these
    sections in prose too, and matching a comment mentioning `[brain]` returns
    the keys of whatever block follows it.

    Two things this deliberately does not do, both because it found nothing
    when it did. It does not stop at the `"` closing the shell string, since
    the keys a section writes conditionally are appended *after* that quote —
    stopping there saw one key of `[developer]`'s seven and missed `[brain]`'s
    `fallback` entirely, while every test still passed. And it strips a
    `varname+="` prefix, since that is how those appended lines start. Only a
    TOML header or the heredoc terminator ends a block. The cost is that a
    stray `key = value` in the shell between two headers is picked up as
    written; that direction fails loudly, asking for a mapping that is not
    needed, rather than quietly checking nothing.
    """
    lines = WIZARD.read_text().splitlines()
    try:
        start = lines.index(f"[{section}]")
    except ValueError as exc:
        raise AssertionError(f"no `[{section}]` header on a line of its own") from exc

    keys = set()
    for line in lines[start + 1:]:
        stripped = line.strip()
        # A TOML header, the heredoc terminator, or the heredoc opener — that
        # last one because the block written just above it is a shell string,
        # and without it the scan runs on into the heredoc's own top-level keys.
        if stripped.startswith("[") or stripped == "TOML" or stripped.startswith("cat >"):
            break
        stripped = re.sub(r'^[a-z_]+\+?="', "", stripped)
        match = re.match(r'^([a-z_][a-z0-9_]*) = ', stripped)
        if match:
            keys.add(match.group(1))
    return keys


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        # The two sections whose keys the scanner cannot see without its
        # shell-append handling. Both passed while finding almost nothing: the
        # `[developer]` scan saw `enabled` alone, so the six credential keys
        # this stage added were checked against the converter by nothing, and
        # the `[brain]` scan missed `fallback`. `written <= mapped` is true of
        # the empty set, so the subset test cannot catch its own blindness —
        # this names what has to be in there.
        ("developer", {"enabled", "repos_dir", "gitlab_token", "github_token"}),
        ("brain", {"kind", "room_selectable", "fallback"}),
    ],
)
def test_the_scanner_reaches_the_conditionally_written_keys(section, expected):
    found = _wizard_keys(section)
    assert expected <= found, (
        f"the [{section}] scan found {sorted(found)}, missing "
        f"{sorted(expected - found)}. Either wizard.sh stopped writing them or "
        "the scanner stopped seeing them; the second reads as a pass."
    )


@pytest.mark.parametrize(
    ("section", "mapping_name"),
    [
        ("developer", "_DEVELOPER_KEYS"),
        ("talk.signaling", "_TALK_SIGNALING_KEYS"),
        ("web.map", "_WEB_MAP_KEYS"),
    ],
)
def test_every_key_the_wizard_writes_is_mapped_by_the_converter(stv, section, mapping_name):
    """A key `wizard.sh` writes and `settings_to_vars.py` does not map is an
    answer the operator gives and the deployment never sees."""
    written = _wizard_keys(section)
    assert written, f"found no keys under [{section}] in wizard.sh; the scanner broke"
    mapped = set(getattr(stv, mapping_name))
    assert written <= mapped, (
        f"wizard.sh writes {sorted(written - mapped)} under [{section}], which "
        f"{mapping_name} does not map — the answer would be silently dropped."
    )


def test_the_brain_keys_the_wizard_writes_are_mapped(stv):
    """`[brain]` is checked apart from the three above because its block also
    carries `kind`, which `convert` handles on its own rather than through a
    map, and the nested `[brain.native]` keys, which have their own."""
    written = _wizard_keys("brain")
    assert written, "found no keys under [brain] in wizard.sh; the scanner broke"
    mapped = set(stv._BRAIN_FLAT_KEYS) | {"kind"}
    assert written <= mapped, (
        f"wizard.sh writes {sorted(written - mapped)} under [brain], which "
        "convert() does not map."
    )


@pytest.mark.parametrize(
    "mapping_name", ["_TALK_SIGNALING_KEYS", "_WEB_MAP_KEYS", "_BRAIN_FLAT_KEYS"]
)
def test_the_new_mappings_target_real_ansible_vars(stv, mapping_name):
    """Same check `test_ansible_developer_config` makes of the developer map: a
    typo in a target is silent, because Ansible accepts an extra-var nothing
    reads and the template falls back to the default."""
    defaults = DEFAULTS_FILE.read_text()
    missing = sorted(
        var for var in getattr(stv, mapping_name).values()
        if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"{mapping_name} maps to {missing}, which defaults/main.yml does not "
        "define — the override would silently do nothing."
    )
