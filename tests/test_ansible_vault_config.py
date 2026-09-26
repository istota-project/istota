"""The credential vault's config key, from inventory through to `load_config`.

`vault_path` is TOML-only by design: it is not in `user_profiles`
(`tests/test_secrets_vault.py::TestTheProfileTableGuard` holds that absence), so
there is no `istota user ensure` route and no web route. A `[users.<id>]` block
in `config.toml` is the only way to set it — and until ISSUE-505 neither config
generator wrote one, so on the two shipped deployment shapes an operator could
not name a vault file at all. The Ansible template rendered no `[users.<id>]`
block whatsoever, and `docker/istota/render-config.sh` wrote one that carried
every per-user key except this. Hand-editing the result does not survive either
shape: the template task rewrites `config.toml` on every converge, and the
entrypoint rewrites it on every boot.

It is the operator's *override* rather than the ordinary route. A user's own
vault is a `.kdbx` in their `istota/vault/` folder plus a filename chosen from
the settings page, and neither of those is a config key — which is why this file
is much smaller than it was: `vault_services` and everything about eligibility
went with the service mapping.

This file is the Ansible half. The Docker half lives in
`tests/test_render_config.py::TestTheCredentialVault`, beside the rest of that
generator's coverage.

**What the template owes, and what it does not.** Its job is fidelity: emit what
the operator wrote, escaped so the file parses. Judging the path is
`storage.resolve_user_vault_path`'s, at the point of use, which is why an
absolute path is asserted to survive verbatim rather than to be normalised here.

**What this cannot see**, inherited from `test_ansible_config_template.py`:
Ansible is not in the dependency set, so the template is rendered with plain
jinja2 plus shims for the Ansible-provided filters. No inventory, no host facts,
no Ansible Vault.
"""

from __future__ import annotations

import tomllib

import pytest

from tests.test_ansible_config_template import (
    ANSIBLE,
    _custom_filters,
    load_config_from,
    render,
)


def vault_filter():
    return _custom_filters()["istota_vault_users_toml"]


def rendered_users(**overrides) -> dict:
    """The `users` table of a rendered config, parsed."""
    return tomllib.loads(render(**overrides)).get("users", {})


ALICE = {
    "display_name": "Alice",
    "vault_path": "istota/vault/credentials.kdbx",
}


class TestTheUserWithNoVault:
    """Everybody, by default. The case that has to stay byte-unchanged."""

    def test_the_default_config_renders_no_users_table(self):
        assert rendered_users() == {}

    def test_a_user_without_the_keys_renders_no_block(self):
        """The invariant, and the reason it is one rather than tidiness.

        `istota_users` is populated on every real deployment and a vault on
        almost none. This shape rendered no `[users.<id>]` block at all before
        ISSUE-505, so `config.users` on it was built only by the
        `user_profiles` overlay — and a block emitted per user would hand the
        scheduler's startup `import_from_user_configs` a TOML-built
        `UserConfig` carrying nothing but defaults. `merge_into_user_config`
        then reads an empty DB list as "the user emptied it" rather than "not
        populated yet", which latches `email_addresses` and `routing` empty.

        Bounded in practice — the role's `istota user ensure` loop runs after
        the template task and before the restart handler, and the insert is
        `ON CONFLICT DO NOTHING` — so the exposure is a converge that aborted
        between the two. The emptiness of this render is what keeps it at that.
        """
        assert rendered_users(istota_users={"alice": {"display_name": "Alice"}}) == {}

    def test_an_empty_vault_path_renders_no_block(self):
        # Removing the line and blanking it are the same instruction.
        assert rendered_users(istota_users={"alice": {"vault_path": ""}}) == {}

    def test_a_stale_vault_services_line_renders_no_block_either(self):
        # An inventory left over from before the key was removed. The renderer
        # no longer reads it, so a user who declared only that has no vault and
        # gets no block — rather than an empty `[users.<id>]` table, which is
        # the shape the class above explains the cost of.
        assert rendered_users(
            istota_users={"alice": {"vault_services": ["karakeep"]}}
        ) == {}


class TestTheUserWithAVault:
    def test_the_path_reaches_the_rendered_config(self):
        users = rendered_users(istota_users={"alice": ALICE})
        assert users["alice"]["vault_path"] == "istota/vault/credentials.kdbx"

    def test_the_loader_accepts_it(self):
        # The end-to-end claim: inventory in, `UserConfig.vault_path` out.
        # `load_config` is what `storage.resolve_user_vault_path` reads.
        config = load_config_from(render(istota_users={"alice": ALICE}))
        assert config.users["alice"].vault_path == "istota/vault/credentials.kdbx"

    def test_an_absolute_path_survives_verbatim(self):
        # The form that keeps the file out of every sandbox-writable tree. The
        # template must not normalise it — `storage._sandbox_writable_roots` is
        # what judges it, and it judges the string the operator wrote.
        users = rendered_users(
            istota_users={"alice": {"vault_path": "/srv/secure/alice.kdbx"}}
        )
        assert users["alice"]["vault_path"] == "/srv/secure/alice.kdbx"

    def test_the_block_carries_the_path_alone(self):
        # The whole of what this renderer writes. A second key here would be a
        # per-user setting reaching `config.toml` by a route nothing else in the
        # role uses, so the assertion is on the key set rather than on absences.
        users = rendered_users(istota_users={"alice": ALICE})
        assert set(users["alice"]) == {"vault_path"}


class TestTheEscaping:
    """Every value here is operator YAML, interpolated into a TOML basic string."""

    def test_a_quote_in_the_path_does_not_break_the_file(self):
        users = rendered_users(
            istota_users={"alice": {"vault_path": 'we"ird/vault.kdbx'}}
        )
        assert users["alice"]["vault_path"] == 'we"ird/vault.kdbx'

    def test_a_backslash_in_the_path_survives_the_round_trip(self):
        users = rendered_users(
            istota_users={"alice": {"vault_path": "back\\slash.kdbx"}}
        )
        assert users["alice"]["vault_path"] == "back\\slash.kdbx"

    def test_a_dotted_user_id_stays_one_user(self):
        # A Nextcloud username containing a dot is ordinary. Unquoted, the table
        # path splits and the vault is filed under a user nobody has — the same
        # trap `istota_briefing_blocks_toml` quotes its own key for.
        users = rendered_users(
            istota_users={"first.last": {"vault_path": "v.kdbx"}}
        )
        assert "first.last" in users
        assert users["first.last"]["vault_path"] == "v.kdbx"


class TestTheVaultBlockCoexistsWithBriefingBlocks:
    """Both renderers write under `users.<uid>`, and both must survive.

    `istota_briefing_blocks_toml` emits `[[users.<uid>.briefings]]`. TOML allows
    a super-table to be defined after a sub-table, so either order parses — but
    only one order reads as a config somebody wrote by hand, and a regression
    that dropped one of the two sections would be invisible to a test that
    rendered only its own.
    """

    USER = {
        **ALICE,
        "briefings": [
            {
                "name": "world",
                "cron": "0 7 * * *",
                "blocks": [{"title": "World News", "sources": []}],
            }
        ],
    }

    def test_both_sections_reach_the_parsed_config(self):
        users = rendered_users(istota_users={"alice": self.USER})
        assert users["alice"]["vault_path"] == "istota/vault/credentials.kdbx"
        assert [b["name"] for b in users["alice"]["briefings"]] == ["world"]

    def test_the_scalar_keys_come_before_the_subtable(self):
        # Comments stripped first: the template's own prose names
        # `[[users.X.briefings]]`, and an index over the whole text finds that
        # mention rather than the rendered table — which is a test that passes
        # or fails on how the comment above it is worded.
        body = [
            line
            for line in render(istota_users={"alice": self.USER}).splitlines()
            if not line.lstrip().startswith("#")
        ]
        scalars = next(i for i, line in enumerate(body) if line.startswith("vault_path ="))
        subtable = next(i for i, line in enumerate(body) if line.startswith("[[users."))
        assert scalars < subtable


class TestTheFilterIsTotal:
    """It renders during `load_config`'s own template pass, so it cannot raise.

    Each of these is operator YAML that type-checks as something else, and the
    render is a deploy step: a raise here fails the play with a jinja2
    traceback rather than a config the loader then reports on. The loader's own
    `_vault_path_value` is the layer that warns.
    """

    @pytest.mark.parametrize(
        "users",
        [
            None,
            [],
            "alice",
            {"alice": None},
            {"alice": "vault.kdbx"},
            {"alice": {"vault_path": 7}},
            {"alice": {"vault_path": ["v.kdbx"]}},
            {"alice": {"vault_services": "karakeep"}},
        ],
    )
    def test_it_returns_a_string_for_anything(self, users):
        assert isinstance(vault_filter()(users), str)

    def test_a_non_string_path_renders_no_block_rather_than_stringifying_it(self):
        # `7` is not a path under any reading, and rendering `"7"` would
        # configure a vault at that name. Dropped rather than raised, because
        # the render is a deploy step.
        assert vault_filter()({"alice": {"vault_path": 7}}) == ""


class TestTheSyncInterval:
    def test_it_renders_from_inventory(self):
        config = load_config_from(render(istota_scheduler_vault_sync_interval=900))
        assert config.scheduler.vault_sync_interval == 900

    def test_the_default_matches_the_dataclass(self):
        # The `config_mapper` defect class: a role default that disagrees with
        # the dataclass is a setting whose value depends on which shape you
        # deployed.
        from istota.config import SchedulerConfig

        config = load_config_from(render())
        assert (
            config.scheduler.vault_sync_interval
            == SchedulerConfig().vault_sync_interval
        )

    def test_zero_turns_the_gate_off(self):
        config = load_config_from(render(istota_scheduler_vault_sync_interval=0))
        assert config.scheduler.vault_sync_interval == 0


def test_the_filter_is_registered():
    """A filter the plugin defines and does not export is unreachable from the
    template, and StrictUndefined reports that as an undefined *variable*."""
    assert "istota_vault_users_toml" in _custom_filters()


def test_the_docs_document_the_key():
    """`istota_users` is documented by an example, not a schema.

    The role defaults point at `docs/deployment/ansible.md` for the per-user
    keys, so a key that page does not show is a key no operator finds.
    """
    text = (ANSIBLE.parent.parent / "docs" / "deployment" / "ansible.md").read_text()
    assert "vault_path:" in text
    assert "docs/deployment/ansible.md" in (ANSIBLE / "defaults" / "main.yml").read_text()


class TestTheInstallerPathReachesBothSettings:
    """`settings.toml` -> `settings_to_vars.py` -> vars -> the template.

    `deploy/install.sh --headless` is the supported bare-metal entry point and
    it never sees an Ansible vars file: an operator writes `settings.toml` and
    the converter produces the vars. So a variable the template reads but the
    converter cannot produce is unreachable from the installer, which is
    ISSUE-505's own defect one layer up.

    Both routes here are generic rather than named — `[users.X]` passes through
    as-is and `[scheduler]` is prefix-mapped — so neither needed a change. That
    is exactly why they need a test: nothing in the converter mentions either
    setting, so nothing would go red if the generic route were narrowed.
    """

    def _convert(self, settings: dict) -> dict:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "settings_to_vars", ANSIBLE.parent / "settings_to_vars.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.convert(settings)

    def test_the_user_key_survives_the_converter(self):
        result = self._convert({"users": {"alice": ALICE}})
        assert result["istota_users"]["alice"]["vault_path"] == ALICE["vault_path"]

    def test_the_sync_interval_survives_the_converter(self):
        # And this is why the variable is named `istota_scheduler_*` rather than
        # `istota_vault_*`: `[scheduler]` is a prefix-mapped section, so the
        # prefix is what the converter can produce. A variable named outside it
        # would render from a hand-written vars file and be unreachable from
        # `settings.toml`, silently.
        result = self._convert({"scheduler": {"vault_sync_interval": 900}})
        assert result["istota_scheduler_vault_sync_interval"] == 900

    def test_the_converted_vars_render_the_block(self):
        """The whole chain, rather than each half separately."""
        variables = self._convert(
            {"users": {"alice": ALICE}, "scheduler": {"vault_sync_interval": 900}}
        )
        config = load_config_from(render(**variables))
        assert config.users["alice"].vault_path == ALICE["vault_path"]
        assert config.scheduler.vault_sync_interval == 900
