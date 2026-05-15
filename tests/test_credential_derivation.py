"""Phase 3: tests for the manifest-derived credential / authorization helpers.

Covers ``derive_credential_set``, ``derive_authorized_skills``,
``derive_skill_credential_map``, and ``derive_lookup_allowlist`` —
the four helpers that replace the deleted ``_PROXY_CREDENTIAL_VARS``,
``_CREDENTIAL_SKILL_MAP``, ``_allowed_credentials_for_skills``, and
``_build_skill_credential_map`` constants/functions.
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.executor import (
    _PROXY_LOOKUP_BLOCKED,
    derive_authorized_skills,
    derive_credential_set,
    derive_lookup_allowlist,
    derive_skill_credential_map,
)
from istota.skills._env import EnvContext, build_skill_env
from istota.skills._loader import load_skill_index
from istota.skills._types import EnvSpec, SkillMeta


def _bundled_index():
    """Real bundled skill manifests."""
    return load_skill_index(Path("config/skills"), bundled_dir=None)


def _ctx(**config_attrs):
    """Build an EnvContext with arbitrary nested config attrs.

    ``_ctx(**{"developer.gitlab_token": "x"})`` builds a config object
    where ``config.developer.gitlab_token == "x"``.
    """
    class _N:  # noqa: N801
        pass

    cfg = _N()
    for key, val in config_attrs.items():
        head, _, rest = key.partition(".")
        if not rest:
            setattr(cfg, head, val)
            continue
        sub = getattr(cfg, head, None)
        if sub is None:
            sub = _N()
            setattr(cfg, head, sub)
        setattr(sub, rest, val)

    class _T:  # noqa: N801
        user_id = "alice"

    return EnvContext(
        config=cfg, task=_T(), user_resources=[],
        user_config=None, user_temp_dir=Path("/tmp"), is_admin=False,
    )


# ---------------------------------------------------------------------------
# derive_credential_set
# ---------------------------------------------------------------------------


class TestDeriveCredentialSet:
    def test_collects_sensitive_vars_only(self):
        idx = {
            "a": SkillMeta(
                name="a", description="",
                env_specs=[
                    EnvSpec(var="A_TOKEN", source="config", config_path="a.tok",
                            sensitive=True),
                    EnvSpec(var="A_URL", source="config", config_path="a.url"),
                ],
            ),
            "b": SkillMeta(
                name="b", description="",
                env_specs=[
                    EnvSpec(var="B_KEY", source="secret", service="b", key="k",
                            sensitive=True),
                ],
            ),
        }
        assert derive_credential_set(idx) == frozenset({"A_TOKEN", "B_KEY"})

    def test_empty_index_returns_empty(self):
        assert derive_credential_set({}) == frozenset()

    def test_setup_env_sensitive_var_included(self):
        """Vars whose source is ``setup_env`` still appear in the
        credential set when marked sensitive — the manifest declares the
        var name; the hook owns the value."""
        idx = {
            "gw": SkillMeta(
                name="gw", description="",
                env_specs=[EnvSpec(
                    var="GOOGLE_WORKSPACE_CLI_TOKEN", source="setup_env",
                    sensitive=True,
                )],
            ),
        }
        assert "GOOGLE_WORKSPACE_CLI_TOKEN" in derive_credential_set(idx)

    def test_bundled_manifests_cover_known_credentials(self):
        """Golden test: the derived set against bundled manifests covers
        every credential the deleted ``_PROXY_CREDENTIAL_VARS`` enumerated."""
        creds = derive_credential_set(_bundled_index())
        for var in (
            "CALDAV_PASSWORD", "NC_PASS", "SMTP_PASSWORD", "IMAP_PASSWORD",
            "KARAKEEP_API_KEY", "GITLAB_TOKEN", "GITHUB_TOKEN",
            "GOOGLE_WORKSPACE_CLI_TOKEN", "NTFY_TOKEN", "NTFY_PASSWORD",
            "MONARCH_SESSION_ID", "MONARCH_CSRFTOKEN",
            "TUMBLR_API_KEY",
        ):
            assert var in creds, f"{var} missing from derived credential set"

    def test_bundled_manifests_exclude_master_key(self):
        creds = derive_credential_set(_bundled_index())
        assert "ISTOTA_SECRET_KEY" not in creds


# ---------------------------------------------------------------------------
# derive_authorized_skills
# ---------------------------------------------------------------------------


class TestDeriveAuthorizedSkillsCore:
    def _idx(self):
        return {
            "bookmarks": SkillMeta(
                name="bookmarks", description="", cli=True,
                env_specs=[EnvSpec(
                    var="KARAKEEP_API_KEY", source="config",
                    config_path="karakeep.api_key", when="karakeep.api_key",
                    sensitive=True,
                )],
            ),
            "developer": SkillMeta(
                name="developer", description="", cli=False,
                env_specs=[
                    EnvSpec(var="GITLAB_TOKEN", source="config",
                            config_path="developer.gitlab_token",
                            when="developer.gitlab_token", sensitive=True),
                    EnvSpec(var="GITHUB_TOKEN", source="config",
                            config_path="developer.github_token",
                            when="developer.github_token", sensitive=True),
                ],
            ),
            "browse": SkillMeta(name="browse", description=""),
        }

    def test_selected_always_authorized(self):
        ctx = _ctx()
        assert derive_authorized_skills(["browse"], self._idx(), ctx) == ["browse"]

    def test_auto_authorize_via_resolved_sensitive(self):
        ctx = _ctx(**{"karakeep.api_key": "x"})
        assert derive_authorized_skills([], self._idx(), ctx) == ["bookmarks"]

    def test_no_creds_only_selected(self):
        ctx = _ctx()
        assert derive_authorized_skills([], self._idx(), ctx) == []

    def test_any_not_all_for_multiprovider(self):
        """One of two configured providers triggers auto-auth."""
        ctx = _ctx(**{"developer.gitlab_token": "x"})
        assert derive_authorized_skills([], self._idx(), ctx) == ["developer"]

    def test_doc_only_skill_auto_authorizes(self):
        """``cli=False`` is irrelevant — developer auto-authorizes."""
        ctx = _ctx(**{"developer.github_token": "y"})
        assert derive_authorized_skills([], self._idx(), ctx) == ["developer"]

    def test_returns_sorted(self):
        ctx = _ctx(
            **{"karakeep.api_key": "x", "developer.gitlab_token": "y"},
        )
        assert derive_authorized_skills([], self._idx(), ctx) == [
            "bookmarks", "developer",
        ]

    def test_selected_unioned_with_auto_authorized(self):
        ctx = _ctx(**{"karakeep.api_key": "x"})
        assert derive_authorized_skills(["browse"], self._idx(), ctx) == [
            "bookmarks", "browse",
        ]


class TestDeriveAuthorizedSkillsFallbackVar:
    """``fallback_var`` must NOT contribute to authorization — operator
    EnvironmentFile fallbacks are instance-wide signals."""

    def _idx(self):
        return {
            "feeds": SkillMeta(
                name="feeds", description="", cli=True,
                env_specs=[EnvSpec(
                    var="TUMBLR_API_KEY", source="secret",
                    service="feeds", key="tumblr_api_key",
                    fallback_var="TUMBLR_API_KEY", sensitive=True,
                )],
            ),
        }

    def test_fallback_alone_does_not_auto_authorize(self, monkeypatch):
        """If the secret is missing but TUMBLR_API_KEY is set in os.environ,
        derive_authorized_skills must NOT see it as configured."""
        monkeypatch.setenv("TUMBLR_API_KEY", "operator-wide-key")
        # No db_path on ctx.config → secret resolution returns None.
        ctx = _ctx()
        assert derive_authorized_skills([], self._idx(), ctx) == []

    def test_value_path_still_honors_fallback(self, monkeypatch):
        """The same context that doesn't auto-auth still gets the fallback
        value in build_skill_env (value path uses default fallbacks_disabled=False)."""
        monkeypatch.setenv("TUMBLR_API_KEY", "operator-wide-key")
        ctx = _ctx()
        env = build_skill_env(["feeds"], self._idx(), ctx)
        assert env["TUMBLR_API_KEY"] == "operator-wide-key"


# ---------------------------------------------------------------------------
# derive_skill_credential_map
# ---------------------------------------------------------------------------


class TestDeriveSkillCredentialMapHelpers:
    def test_returns_only_sensitive(self):
        idx = {
            "x": SkillMeta(
                name="x", description="",
                env_specs=[
                    EnvSpec(var="X_TOKEN", source="config", config_path="x.tok",
                            sensitive=True),
                    EnvSpec(var="X_URL", source="config", config_path="x.url"),
                ],
            ),
        }
        assert derive_skill_credential_map(["x"], idx) == {"x": {"X_TOKEN"}}

    def test_unknown_skill_skipped(self):
        idx = {"x": SkillMeta(name="x", description="")}
        assert derive_skill_credential_map(["unknown"], idx) == {}


# ---------------------------------------------------------------------------
# derive_lookup_allowlist
# ---------------------------------------------------------------------------


class TestDeriveLookupAllowlistHelpers:
    def test_subtracts_block_list(self):
        """A manifest declaring ISTOTA_SECRET_KEY sensitive (e.g. a buggy
        setup_env hook) cannot make it through the lookup endpoint."""
        idx = {
            "evil": SkillMeta(
                name="evil", description="",
                env_specs=[
                    EnvSpec(var="ISTOTA_SECRET_KEY", source="setup_env",
                            sensitive=True),
                    EnvSpec(var="OTHER_TOKEN", source="config",
                            config_path="x.t", sensitive=True),
                ],
            ),
        }
        assert derive_lookup_allowlist(["evil"], idx) == {"OTHER_TOKEN"}

    def test_empty_authorized_returns_empty(self):
        assert derive_lookup_allowlist([], _bundled_index()) == set()

    def test_master_key_in_block_list(self):
        assert "ISTOTA_SECRET_KEY" in _PROXY_LOOKUP_BLOCKED


# ---------------------------------------------------------------------------
# Performance: derive_authorized_skills under realistic skill_index size
# ---------------------------------------------------------------------------


class TestDerivationPerf:
    """Cold derivation against the bundled skill index must stay well
    under 50ms — the spec's perf budget."""

    def test_cold_derive_under_50ms(self):
        idx = _bundled_index()
        ctx = _ctx()
        t0 = time.perf_counter()
        derive_authorized_skills([], idx, ctx)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"cold derive took {elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# build_skill_env: conflict semantics for two skills declaring the same var
# ---------------------------------------------------------------------------


class TestBuildSkillEnvConflict:
    def _idx_same_value(self):
        return {
            "a": SkillMeta(
                name="a", description="",
                env_specs=[EnvSpec(
                    var="NC_URL", source="config", config_path="nextcloud.url",
                )],
            ),
            "b": SkillMeta(
                name="b", description="",
                env_specs=[EnvSpec(
                    var="NC_URL", source="config", config_path="nextcloud.url",
                )],
            ),
        }

    def test_same_value_no_warning(self, caplog):
        """Common case: ``NC_URL`` co-declared on ``nextcloud`` and
        ``files`` resolves to the same value; no warning."""
        import logging
        ctx = _ctx(**{"nextcloud.url": "https://nc.example.com"})
        with caplog.at_level(logging.WARNING, logger="istota.skills_env"):
            env = build_skill_env(["a", "b"], self._idx_same_value(), ctx)
        assert env["NC_URL"] == "https://nc.example.com"
        conflicts = [r for r in caplog.records if "env_conflict" in r.message]
        assert conflicts == []

    def test_different_values_warns(self, caplog):
        """Two skills declaring the same var with different non-None
        values triggers a warning."""
        import logging
        idx = {
            "a": SkillMeta(
                name="a", description="",
                env_specs=[EnvSpec(var="X", source="config",
                                   config_path="a.x")],
            ),
            "b": SkillMeta(
                name="b", description="",
                env_specs=[EnvSpec(var="X", source="config",
                                   config_path="b.x")],
            ),
        }
        ctx = _ctx(**{"a.x": "alpha", "b.x": "beta"})
        with caplog.at_level(logging.WARNING, logger="istota.skills_env"):
            env = build_skill_env(["a", "b"], idx, ctx)
        # last-write-wins by iteration order
        assert env["X"] == "beta"
        conflicts = [r for r in caplog.records if "env_conflict" in r.message]
        assert len(conflicts) == 1
        assert "var=X" in conflicts[0].message

    def test_none_does_not_overwrite(self):
        """One skill resolves, another returns None — resolved value
        survives regardless of order."""
        idx = {
            "resolves": SkillMeta(
                name="resolves", description="",
                env_specs=[EnvSpec(var="X", source="config",
                                   config_path="r.x")],
            ),
            "skips": SkillMeta(
                name="skips", description="",
                env_specs=[EnvSpec(
                    var="X", source="config", config_path="missing.path",
                    when="missing.path",
                )],
            ),
        }
        ctx = _ctx(**{"r.x": "value"})
        # skips (None) declared after resolves: existing value retained.
        env = build_skill_env(["resolves", "skips"], idx, ctx)
        assert env["X"] == "value"
        # skips first: resolves's value still wins on second pass.
        env2 = build_skill_env(["skips", "resolves"], idx, ctx)
        assert env2["X"] == "value"
