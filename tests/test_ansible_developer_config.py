"""The rendered `[developer]` block and `DeveloperConfig` must agree.

Ansible rewrites `config.toml` on every run, so the template is the only
`[developer]` block a production host ever has. Two ways that drifts from the
code, both of which have happened:

  * a key the loader stopped reading is still rendered, so a retired setting
    persists on every host until someone notices the template; and
  * a field the loader gained is never rendered, so the operator cannot set it
    and the host silently runs the code default — which is how `gh_bin_path`
    came to point at `/usr/local/bin/gh` on a host where `apt` had installed
    `/usr/bin/gh`, warning at every start and failing every forge command.

Both are invisible from the Python side alone, which is why this parses the
template rather than testing the loader again.
"""

import re
from dataclasses import fields
from pathlib import Path

import pytest

from istota.config import DeveloperConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "deploy" / "ansible" / "templates" / "config.toml.j2"
DEFAULTS = REPO_ROOT / "deploy" / "ansible" / "defaults" / "main.yml"
SETTINGS_TO_VARS = REPO_ROOT / "deploy" / "settings_to_vars.py"

# `key = ...` at the start of a line, ignoring Jinja control lines and the
# indented list items inside a `{% for %}`.
_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*)\s*=", re.MULTILINE)
_VAR_RE = re.compile(r"\b(istota_[a-z0-9_]+)\b")


def _developer_block() -> str:
    """The template text from `[developer]` to the `{% endif %}` closing it.

    Counted rather than searched for: the block has `{% if %}` blocks nested
    inside it (`author_credit`, the two tokens under `istota_use_environment_
    file`), so the first `{% endif %}` after `[developer]` closes one of those,
    not the block.
    """
    text = TEMPLATE.read_text()
    lines = text.split("\n")
    start = lines.index("[developer]")
    depth = 1  # already inside `{% if istota_developer_enabled %}`
    for offset, line in enumerate(lines[start:], start=start):
        stripped = line.strip()
        if stripped.startswith("{% if "):
            depth += 1
        elif stripped.startswith("{% endif %}"):
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start:offset])
    raise AssertionError("unterminated [developer] block in config.toml.j2")


@pytest.fixture(scope="module")
def block() -> str:
    text = _developer_block()
    # The scanner counts `{% if %}` / `{% endif %}` pairs, so a whitespace-
    # control tag or an inline conditional could skew the count and return a
    # short block — which would turn every `assert key not in block` below into
    # a pass for the wrong reason. Fail loudly instead: this key is the last one
    # in the block.
    assert "devbox_proxy_audit_log" in text, (
        "the [developer] block scanner truncated early; the retirement "
        "assertions below would pass vacuously"
    )
    return text


def test_every_rendered_key_is_a_developer_config_field(block):
    field_names = {f.name for f in fields(DeveloperConfig)}
    rendered = set(_KEY_RE.findall(block))
    unknown = sorted(rendered - field_names)
    assert not unknown, (
        f"config.toml.j2 renders {unknown} into [developer], but DeveloperConfig "
        "has no such field. The loader ignores unknown keys, so this is silent: "
        "the setting reaches every host and does nothing."
    )


@pytest.mark.parametrize(
    "key",
    ["gitlab_api_allowlist", "github_api_allowlist", "api_timeout_seconds"],
)
def test_retired_keys_are_no_longer_rendered(block, key):
    """These three were made inert in the loader before the template dropped
    them, so for a while every Ansible run wrote back a key nothing read."""
    assert key not in block


def test_forge_policy_and_binary_paths_are_rendered(block):
    """The wrapper's policy knobs and the path to the real binary are only
    settable through the rendered file. Unrendered, an operator's config.toml
    entry is overwritten on the next deploy."""
    rendered = set(_KEY_RE.findall(block))
    for key in (
        "forge_cli_extra_denied",
        "forge_cli_permit",
        "gh_bin_path",
        "glab_bin_path",
    ):
        assert key in rendered, f"config.toml.j2 does not render [developer] {key}"


def test_every_referenced_var_has_an_ansible_default(block):
    defaults = DEFAULTS.read_text()
    referenced = set(_VAR_RE.findall(block))
    missing = sorted(
        var for var in referenced if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"config.toml.j2 references {missing}, which defaults/main.yml does not "
        "define. With Ansible's default ANSIBLE_ERROR_ON_UNDEFINED_VARS the "
        "template task fails at render time, so this is a broken play rather "
        "than a quiet misconfiguration — but it fails on the deploy, not here, "
        "which is the point of checking it here."
    )


def test_binary_paths_default_to_where_the_role_installs_them():
    """The role installs `gh` and `glab` from the Debian archive, which puts
    both in /usr/bin. The code default is /usr/local/bin, for the install
    shapes that render no config.toml — so the Ansible default has to differ,
    and a change to either one here should be a deliberate one."""
    defaults = DEFAULTS.read_text()
    assert 'istota_developer_gh_bin_path: "/usr/bin/gh"' in defaults
    assert 'istota_developer_glab_bin_path: "/usr/bin/glab"' in defaults
    assert DeveloperConfig().gh_bin_path == "/usr/local/bin/gh"
    assert DeveloperConfig().glab_bin_path == "/usr/local/bin/glab"


def test_settings_to_vars_maps_no_retired_developer_key():
    """`settings_to_vars.py` turns a settings dict into Ansible extra-vars. A
    mapping for a retired key resurrects it as an extra-var, which outranks the
    default and would reintroduce the key the template just dropped."""
    text = SETTINGS_TO_VARS.read_text()
    for key in ("gitlab_api_allowlist", "github_api_allowlist", "api_timeout_seconds"):
        assert f'"{key}"' not in text, f"settings_to_vars.py still maps {key}"
    for key in ("forge_cli_extra_denied", "forge_cli_permit", "gh_bin_path", "glab_bin_path"):
        assert f'"{key}"' in text, f"settings_to_vars.py does not map {key}"


def test_settings_to_vars_targets_real_ansible_vars():
    """The mapping's *values* are the names `config.toml.j2` consumes, and a
    typo in one is silent: `convert()` emits an extra-var nothing reads, and
    the template falls back to the default the operator meant to override."""
    text = SETTINGS_TO_VARS.read_text()
    defaults = DEFAULTS.read_text()
    start = text.index("_DEVELOPER_KEYS = {")
    end = text.index("}", start)
    targets = set(re.findall(r'"(istota_developer_[a-z0-9_]+)"', text[start:end]))
    assert targets, "no developer var targets found; the mapping block moved"
    missing = sorted(
        var for var in targets if not re.search(rf"^{var}:", defaults, re.MULTILINE)
    )
    assert not missing, (
        f"settings_to_vars.py maps to {missing}, which defaults/main.yml does "
        "not define — the override would silently do nothing."
    )
