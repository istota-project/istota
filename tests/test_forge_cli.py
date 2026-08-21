"""The gh/glab wrapper — argv normalisation, policy, and child environment.

Everything here exercises the pure functions. The exec path and the socket
round-trip live in test_forge_cli_exec.py, which drives real subprocesses.
"""

import ast
import json
from pathlib import Path

import pytest

from istota.forge_cli import (
    EXIT_CREDENTIAL,
    EXIT_DENIED,
    EXIT_NO_PROXY,
    EXIT_USAGE,
    FORGE_GITHUB,
    FORGE_GITLAB,
    RETIRED,
    baseline_policy,
    build_invocation,
    build_policy,
    denied_reason,
    forge_from_argv0,
    is_meta_invocation,
    load_policy,
    normalize_args,
    unmatched_permits,
)

SENTINEL = "ghp_sentineltokenvalue0000000000000000"


# --------------------------------------------------------------------------- #
# argv[0]
# --------------------------------------------------------------------------- #


class TestForgeFromArgv0:
    @pytest.mark.parametrize("argv0,expected", [
        ("gh", FORGE_GITHUB),
        ("glab", FORGE_GITLAB),
        ("/usr/local/bin/gh", FORGE_GITHUB),
        ("/tmp/x/.developer/glab", FORGE_GITLAB),
        ("github-api", RETIRED),
        ("gitlab-api", RETIRED),
        ("/usr/local/bin/github-api", RETIRED),
        ("hub", None),
        ("", None),
    ])
    def test_dispatch(self, argv0, expected):
        assert forge_from_argv0(argv0) == expected


class TestMetaInvocation:
    @pytest.mark.parametrize("args", [
        [], ["--version"], ["-v"], ["--help"], ["-h"], ["help"], ["completion"],
    ])
    def test_meta(self, args):
        assert is_meta_invocation(args) is True

    @pytest.mark.parametrize("args", [
        ["pr", "--help"],       # already picked a subcommand
        ["pr", "list"],
        ["api", "/user"],
        ["repo", "view", "--help"],
    ])
    def test_not_meta(self, args):
        assert is_meta_invocation(args) is False


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


class TestNormalizeArgs:
    def test_plain_subcommand_path(self):
        path, flags = normalize_args(["pr", "create"])
        assert path == ["pr", "create"]
        assert flags == {}

    def test_long_flag_with_separate_value(self):
        path, flags = normalize_args(["api", "--method", "DELETE", "/x"])
        assert path == ["api"]
        assert flags["method"] == ["DELETE"]

    def test_long_flag_with_equals_value(self):
        _, flags = normalize_args(["api", "--method=DELETE", "/x"])
        assert flags["method"] == ["DELETE"]

    def test_short_flag_with_separate_value(self):
        _, flags = normalize_args(["api", "-X", "DELETE", "/x"])
        assert flags["X"] == ["DELETE"]

    def test_short_flag_with_attached_value(self):
        _, flags = normalize_args(["api", "-XDELETE", "/x"])
        assert "DELETE" in flags["X"]

    def test_clustered_short_flags(self):
        _, flags = normalize_args(["pr", "list", "-abc"])
        # Both readings recorded: the attached-value one and the cluster one.
        assert "bc" in flags["a"]
        assert "" in flags["b"]
        assert "" in flags["c"]

    def test_valueless_long_flag(self):
        _, flags = normalize_args(["pr", "create", "--draft"])
        assert flags["draft"] == [""]

    def test_double_dash_terminates(self):
        path, flags = normalize_args(["api", "--", "--method", "DELETE"])
        assert path == ["api"]
        assert flags == {}

    def test_operand_after_flag_does_not_join_path(self):
        path, _ = normalize_args(["issue", "create", "--label", "config"])
        assert path == ["issue", "create"]

    def test_flag_before_subcommand_stops_the_path(self):
        # A flag first means nothing after it is a subcommand as far as we
        # can tell, so the path stays empty rather than guessing.
        path, _ = normalize_args(["--repo", "x", "repo", "delete"])
        assert path == []


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class TestDeniedReason:
    def _policy(self, forge=FORGE_GITHUB):
        return baseline_policy(forge)

    @pytest.mark.parametrize("args", [
        ["repo", "delete", "someorg/somerepo"],
        ["repo", "archive"],
        ["repo", "rename", "new"],
        ["repo", "edit", "--visibility", "public"],
        ["auth", "token"],
        ["auth", "status"],
        ["auth", "status", "--show-token"],
        ["auth", "login"],
        ["secret", "set", "NAME"],
        ["variable", "delete", "NAME"],
        ["ssh-key", "add", "k.pub"],
        ["gpg-key", "add"],
        ["release", "delete", "v1"],
        ["release", "delete-asset", "v1", "a"],
        ["run", "delete", "1"],
        ["cache", "delete"],
        ["alias", "set", "x", "repo delete"],
        ["extension", "install", "owner/ext"],
        ["ext", "install", "owner/ext"],
        ["config", "set", "k", "v"],
        ["api", "graphql", "-f", "query=mutation{}"],
    ])
    def test_baseline_gh_denied(self, args):
        assert denied_reason(FORGE_GITHUB, args, self._policy()) is not None

    @pytest.mark.parametrize("args", [
        ["mr", "create", "--title", "t"],
        ["mr", "list"],
        ["ci", "status"],
        ["ci", "trace"],
        ["repo", "view"],
        ["issue", "list"],
    ])
    def test_baseline_glab_allowed(self, args):
        policy = self._policy(FORGE_GITLAB)
        assert denied_reason(FORGE_GITLAB, args, policy) is None

    @pytest.mark.parametrize("args", [
        ["pr", "create", "--title", "t", "--head", "b"],
        ["pr", "checks"],
        ["pr", "diff"],
        ["pr", "list", "--state", "open"],
        ["pr", "review", "--comment", "-b", "x"],
        ["issue", "create", "--title", "t"],
        ["repo", "view"],
        ["run", "rerun", "1"],          # starts work, destroys nothing
        ["workflow", "run", "ci.yml"],
        ["search", "code", "foo"],
    ])
    def test_baseline_gh_allowed(self, args):
        assert denied_reason(FORGE_GITHUB, args, self._policy()) is None

    @pytest.mark.parametrize("args", [
        ["api", "-X", "DELETE", "/repos/o/r"],
        ["api", "-X", "delete", "/repos/o/r"],
        ["api", "-XDELETE", "/repos/o/r"],
        ["api", "--method", "DELETE", "/repos/o/r"],
        ["api", "--method=DELETE", "/repos/o/r"],
        ["api", "--method=delete", "/repos/o/r"],
        ["api", "--method", "PATCH", "/repos/o/r"],
        ["api", "-X", "POST", "/repos/o/r/pulls"],
    ])
    def test_api_write_methods_denied(self, args):
        """Every spelling, including the two that evaded the first design."""
        assert denied_reason(FORGE_GITHUB, args, self._policy()) is not None

    @pytest.mark.parametrize("args", [
        ["api", "/repos/o/r"],
        ["api", "--method", "GET", "/repos/o/r"],
        ["api", "-X", "GET", "/user"],
        ["api", "/repos/o/r/pulls", "--paginate"],
    ])
    def test_api_read_methods_allowed(self, args):
        assert denied_reason(FORGE_GITHUB, args, self._policy()) is None

    def test_denied_word_in_flag_value_is_allowed(self):
        """`config` is a path rule; it must not fire on a label value."""
        for args in (
            ["issue", "create", "--label", "config"],
            ["issue", "create", "--label", "extension"],
            ["pr", "edit", "12", "--add-label", "config"],
            ["api", "/repos/o/r/contents/config"],
        ):
            assert denied_reason(FORGE_GITHUB, args, self._policy()) is None

    def test_denied_reason_names_the_rule(self):
        reason = denied_reason(FORGE_GITHUB, ["repo", "delete", "x"], self._policy())
        assert reason == "repo delete"


class TestBuildPolicy:
    def test_extra_denied_adds_scoped(self):
        policy = build_policy(FORGE_GITHUB, extra_denied=["gh repo view"])
        assert denied_reason(FORGE_GITHUB, ["repo", "view"], policy) is not None
        other = build_policy(FORGE_GITLAB, extra_denied=["gh repo view"])
        assert denied_reason(FORGE_GITLAB, ["repo", "view"], other) is None

    def test_extra_denied_unscoped_applies_to_both(self):
        for forge in (FORGE_GITHUB, FORGE_GITLAB):
            policy = build_policy(forge, extra_denied=["repo view"])
            assert denied_reason(forge, ["repo", "view"], policy) is not None

    def test_permit_removes_baseline_entry(self):
        policy = build_policy(FORGE_GITHUB, permit=["gh repo delete"])
        assert denied_reason(FORGE_GITHUB, ["repo", "delete", "x"], policy) is None
        # and leaves its neighbours alone
        assert denied_reason(FORGE_GITHUB, ["repo", "archive"], policy) is not None

    def test_baseline_untouched_by_build(self):
        build_policy(FORGE_GITHUB, extra_denied=["gh pr create"])
        assert denied_reason(FORGE_GITHUB, ["pr", "create"], baseline_policy(FORGE_GITHUB)) is None

    def test_unmatched_permit_reported(self):
        assert unmatched_permits(
            [FORGE_GITHUB, FORGE_GITLAB], ["gh repo delete"],
        ) == []
        assert unmatched_permits(
            [FORGE_GITHUB, FORGE_GITLAB], ["gh repo delete-repo"],
        ) == ["gh repo delete-repo"]


class TestLoadPolicy:
    def test_reads_a_written_policy(self, tmp_path):
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({
            FORGE_GITHUB: build_policy(FORGE_GITHUB, extra_denied=["gh pr merge"]),
            FORGE_GITLAB: build_policy(FORGE_GITLAB),
        }))
        policy = load_policy(str(path), FORGE_GITHUB)
        assert denied_reason(FORGE_GITHUB, ["pr", "merge", "1"], policy) is not None

    @pytest.mark.parametrize("content", ["", "not json", "{}", '{"github": []}', "null"])
    def test_unusable_file_falls_back_to_baseline_not_empty(self, tmp_path, content):
        path = tmp_path / "policy.json"
        path.write_text(content)
        policy = load_policy(str(path), FORGE_GITHUB)
        assert denied_reason(FORGE_GITHUB, ["repo", "delete", "x"], policy) is not None

    def test_missing_file_falls_back_to_baseline(self, tmp_path):
        policy = load_policy(str(tmp_path / "absent.json"), FORGE_GITHUB)
        assert denied_reason(FORGE_GITHUB, ["repo", "delete", "x"], policy) is not None

    def test_unset_path_falls_back_to_baseline(self):
        policy = load_policy(None, FORGE_GITHUB)
        assert policy == baseline_policy(FORGE_GITHUB)


# --------------------------------------------------------------------------- #
# Child environment
# --------------------------------------------------------------------------- #


class TestBuildInvocation:
    def _call(self, forge=FORGE_GITHUB, parent=None, token=SENTINEL, url=""):
        parent = {"PATH": "/usr/local/bin:/usr/bin", "HOME": "/home/bot"} if parent is None else parent
        return build_invocation(
            forge, ["pr", "list"], parent, token,
            "/usr/local/bin/gh" if forge == FORGE_GITHUB else "/usr/local/bin/glab",
            "/tmp/cfg", url,
        )

    def test_path_is_unchanged(self):
        parent = {"PATH": "/a:/b:/c"}
        _, _, env = self._call(parent=parent)
        assert env["PATH"] == "/a:/b:/c"

    def test_credential_sockets_survive(self):
        parent = {
            "PATH": "/usr/bin",
            "ISTOTA_SKILL_PROXY_SOCK": "/tmp/p.sock",
            "ISTOTA_CRED_SOCK": "/run/c.sock",
        }
        _, _, env = self._call(parent=parent)
        # git's credential helper reads these out of its own environment.
        assert env["ISTOTA_SKILL_PROXY_SOCK"] == "/tmp/p.sock"
        assert env["ISTOTA_CRED_SOCK"] == "/run/c.sock"

    def test_debug_vars_scrubbed(self):
        parent = {"PATH": "/usr/bin", "GH_DEBUG": "api", "GLAB_DEBUG": "1"}
        _, _, env = self._call(parent=parent)
        assert "GH_DEBUG" not in env
        assert "GLAB_DEBUG" not in env

    def test_inherited_tokens_scrubbed(self):
        parent = {"PATH": "/usr/bin", "GITHUB_TOKEN": "inherited", "GH_ENTERPRISE_TOKEN": "x"}
        _, _, env = self._call(parent=parent)
        assert env.get("GH_TOKEN") == SENTINEL
        assert "GITHUB_TOKEN" not in env
        assert "GH_ENTERPRISE_TOKEN" not in env

    def test_policy_path_not_passed_onward(self):
        parent = {"PATH": "/usr/bin", "ISTOTA_FORGE_POLICY": "/tmp/policy.json"}
        _, _, env = self._call(parent=parent)
        assert "ISTOTA_FORGE_POLICY" not in env

    def test_unrelated_vars_dropped(self):
        parent = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-x", "NC_PASS": "y"}
        _, _, env = self._call(parent=parent)
        assert "ANTHROPIC_API_KEY" not in env
        assert "NC_PASS" not in env

    def test_proxy_and_locale_carried(self):
        parent = {
            "PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:8080",
            "LC_ALL": "C", "GIT_AUTHOR_NAME": "bot", "TZ": "UTC",
        }
        _, _, env = self._call(parent=parent)
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
        assert env["LC_ALL"] == "C"
        assert env["GIT_AUTHOR_NAME"] == "bot"
        assert env["TZ"] == "UTC"

    def test_token_appears_under_exactly_one_key(self):
        _, _, env = self._call()
        holders = [k for k, v in env.items() if SENTINEL in v]
        assert holders == ["GH_TOKEN"]

    def test_github_com_uses_gh_token(self):
        _, _, env = self._call(url="https://github.com")
        assert env["GH_HOST"] == "github.com"
        assert env["GH_TOKEN"] == SENTINEL
        assert "GH_ENTERPRISE_TOKEN" not in env

    def test_enterprise_host_uses_enterprise_token(self):
        """gh resolves auth per host; GH_TOKEN on a GHE host authenticates nothing."""
        _, _, env = self._call(url="https://ghe.example.com")
        assert env["GH_HOST"] == "ghe.example.com"
        assert env["GH_ENTERPRISE_TOKEN"] == SENTINEL
        assert "GH_TOKEN" not in env

    def test_gitlab_host_keeps_port_and_subpath(self):
        _, _, env = self._call(
            forge=FORGE_GITLAB, url="https://git.example.com:8443/gitlab",
        )
        assert env["GITLAB_HOST"] == "https://git.example.com:8443/gitlab"
        assert env["GITLAB_TOKEN"] == SENTINEL

    def test_config_dir_and_quiet_flags(self):
        _, _, env = self._call()
        assert env["GH_CONFIG_DIR"] == "/tmp/cfg"
        assert env["GH_NO_UPDATE_CHECKER"] == "1"
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert env["GH_PAGER"] == "cat"
        assert env["NO_COLOR"] == "1"

    def test_glab_gets_its_own_config_dir(self):
        _, _, env = self._call(forge=FORGE_GITLAB)
        assert env["GLAB_CONFIG_DIR"] == "/tmp/cfg"
        assert "GH_CONFIG_DIR" not in env

    def test_returns_absolute_real_bin_and_preserved_args(self):
        path, argv, _ = self._call()
        assert path == "/usr/local/bin/gh"
        assert argv == ["gh", "pr", "list"]

    def test_meta_invocation_carries_no_token(self):
        _, _, env = self._call(token=None)
        assert "GH_TOKEN" not in env
        assert "GH_HOST" in env  # host still set; only the credential is absent


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def _module_paths():
    root = Path(__file__).resolve().parent.parent
    return root / "src/istota/forge_cli.py", root / "docker/devbox/lib/istota_forge_cli.py"


class TestVendoredCopy:
    def test_devbox_copy_is_byte_identical(self):
        canonical, vendored = _module_paths()
        assert vendored.exists(), f"{vendored} missing — run scripts/sync-devbox-lib.sh"
        assert canonical.read_bytes() == vendored.read_bytes(), (
            "src/istota/forge_cli.py and docker/devbox/lib/istota_forge_cli.py "
            "have drifted — run scripts/sync-devbox-lib.sh"
        )


class TestImportHygiene:
    """The container copy runs under a bare python3 with no istota package.

    A new guard rather than an existing convention: the other stdlib-only
    leaves are stdlib-only by discipline, not by assertion.
    """

    def test_no_istota_imports(self):
        canonical, _ = _module_paths()
        tree = ast.parse(canonical.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("istota"), alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "relative import in a copied leaf"
                assert not (node.module or "").startswith("istota"), node.module


class TestExitCodes:
    def test_codes_are_distinct_and_above_one(self):
        codes = [EXIT_USAGE, EXIT_DENIED, EXIT_NO_PROXY, EXIT_CREDENTIAL]
        assert len(set(codes)) == len(codes)
        assert all(c > 1 for c in codes)
