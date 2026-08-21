"""The gh/glab wrapper — argv normalisation, policy, and child environment.

Everything here exercises the pure functions. The exec path and the socket
round-trip live in test_forge_cli_exec.py, which drives real subprocesses.
"""

import ast
import json
import os
from pathlib import Path

import pytest

from istota.forge_cli import (
    _BASELINE_PATH_RULES,
    _CARRY_EXACT,
    _CARRY_GIT,
    _CARRY_GIT_PREFIX,
    _CARRY_PREFIX,
    _SCRUB,
    EXIT_CREDENTIAL,
    EXIT_DENIED,
    EXIT_EXEC,
    EXIT_MISCONFIGURED,
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


def _paths(args):
    """Both candidate readings, as a list of lists."""
    return normalize_args(args)[0]


def _flags(args):
    return normalize_args(args)[1]


class TestNormalizeArgs:
    def test_plain_subcommand_path(self):
        paths, flags = normalize_args(["pr", "create"])
        assert paths == [["pr", "create"], ["pr", "create"]]
        assert flags == {}

    def test_long_flag_with_separate_value(self):
        paths, flags = normalize_args(["api", "--method", "DELETE", "/x"])
        # Swallowed reading drops the value; unswallowed keeps it as a path
        # candidate, because --method's arity is not knowable here.
        assert paths == [["api", "/x"], ["api", "DELETE", "/x"]]
        assert flags["method"] == ["DELETE"]

    def test_long_flag_with_equals_value(self):
        flags = _flags(["api", "--method=DELETE", "/x"])
        assert flags["method"] == ["DELETE"]

    def test_short_flag_with_separate_value(self):
        flags = _flags(["api", "-X", "DELETE", "/x"])
        assert flags["X"] == ["DELETE"]

    def test_short_flag_with_attached_value(self):
        flags = _flags(["api", "-XDELETE", "/x"])
        assert "DELETE" in flags["X"]

    def test_clustered_short_flags(self):
        flags = _flags(["pr", "list", "-abc"])
        # Both readings recorded: the attached-value one and the cluster one.
        assert "bc" in flags["a"]
        assert "" in flags["b"]
        assert "" in flags["c"]

    def test_valueless_long_flag(self):
        flags = _flags(["pr", "create", "--draft"])
        assert flags["draft"] == [""]

    def test_double_dash_terminates(self):
        paths, flags = normalize_args(["api", "--", "--method", "DELETE"])
        assert paths == [["api"], ["api"]]
        assert flags == {}

    def test_flag_value_stays_out_of_the_swallowed_reading(self):
        assert _paths(["issue", "create", "--label", "config"]) == [
            ["issue", "create"], ["issue", "create", "config"],
        ]

    def test_flag_before_subcommand_keeps_the_subcommand(self):
        """`gh -R o/r repo delete` must still resolve to `repo delete`.

        The swallowed reading is the one that gets there; the unswallowed one
        carries the repo slug at index 0 and matches nothing."""
        assert _paths(["-R", "o/r", "repo", "delete"]) == [
            ["repo", "delete"], ["o/r", "repo", "delete"],
        ]

    def test_empty_argv_entries_dropped(self):
        """cobra's stripFlags drops them; left in, one shifts every rule index."""
        assert _paths(["", "repo", "delete"]) == [
            ["repo", "delete"], ["repo", "delete"],
        ]


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

    @pytest.mark.parametrize("forge", [FORGE_GITHUB, FORGE_GITLAB])
    def test_every_baseline_rule_denies_its_own_verb(self, forge):
        """Derived from the table rather than transcribed from it, so a new
        entry cannot be added without coverage and a typo cannot hide."""
        policy = self._policy(forge)
        for rule in _BASELINE_PATH_RULES[forge]:
            args = [*rule, "some-operand"]
            assert denied_reason(forge, args, policy) == " ".join(rule), rule

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

    @pytest.mark.parametrize("content", [
        "", "not json", "{}", '{"github": []}', "null",
        # An empty section reads as "deny nothing" and is the shape a
        # half-written generator produces.
        '{"github": {}}',
        '{"github": {"path_rules": []}}',
        # The natural hand-written spelling: strings instead of word lists.
        # list("repo delete") makes this parse and then match nothing.
        '{"github": {"path_rules": ["repo delete"]}}',
        '{"github": {"path_rules": [["repo", "delete"], "auth"]}}',
        '{"github": {"path_rules": [[]]}}',
        '{"github": {"path_rules": [["repo", 3]]}}',
        # A flag rule whose path is a string never matches: _path_matches
        # compares a list slice against it.
        '{"github": {"path_rules": [["repo", "delete"]],'
        ' "flag_value_rules": [{"path": "api", "flag": "X", "in": ["delete"]}]}}',
        '{"github": {"path_rules": [["repo", "delete"]],'
        ' "body_flag_rules": [{"path": "api", "flags": ["f"]}]}}',
    ])
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

    def test_devbox_copy_is_a_real_file(self):
        """A symlink would pass a byte comparison and fail `docker build`,
        which is the exact failure the copy exists to avoid."""
        _, vendored = _module_paths()
        assert not vendored.is_symlink()

    def test_wrapper_has_a_shebang_and_is_executable(self):
        """Both copies are exec'd directly by the kernel under the names they
        are installed as. Without a shebang that is ENOEXEC, and the shell's
        retry under /bin/sh exits 2 — colliding with EXIT_USAGE."""
        for path in _module_paths():
            first = path.read_bytes().split(b"\n", 1)[0]
            assert first == b"#!/usr/bin/env python3", f"{path}: {first!r}"
            assert os.access(path, os.X_OK), f"{path} is not executable"


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
        codes = [
            EXIT_USAGE, EXIT_DENIED, EXIT_NO_PROXY,
            EXIT_CREDENTIAL, EXIT_EXEC, EXIT_MISCONFIGURED,
        ]
        assert len(set(codes)) == len(codes)
        assert all(c > 1 for c in codes)


class TestScrubIsRedundantByConstruction:
    """_SCRUB cannot fire today, and that is the invariant, not an accident.

    build_invocation builds the child env by allowlist, so nothing in _SCRUB
    reaches it anyway — which is exactly why the three "scrubbed" tests below
    would pass with the scrub deleted. Pin the relationship instead: if a later
    change widens the carry set over a scrub entry, this fails and the scrub
    becomes load-bearing rather than silently letting the entry through.
    """

    def test_no_scrub_entry_is_in_the_carry_set(self):
        carried = [
            key for key in _SCRUB
            if key in _CARRY_EXACT
            or key in _CARRY_GIT
            or key.startswith(_CARRY_PREFIX)
            or key.startswith(_CARRY_GIT_PREFIX)
        ]
        assert carried == []

    def test_git_is_carried_by_name_not_by_prefix(self):
        """A blanket GIT_ prefix would admit GIT_TRACE_CURL, which prints
        Authorization headers into the task log, and GIT_SSH_COMMAND /
        GIT_ASKPASS, which run a chosen command inside a token-holding
        process."""
        assert "GIT_" not in _CARRY_PREFIX
        for dangerous in ("GIT_TRACE_CURL", "GIT_SSH_COMMAND", "GIT_ASKPASS",
                          "GIT_EXTERNAL_DIFF", "GIT_PAGER"):
            assert dangerous not in _CARRY_GIT
            assert not dangerous.startswith(_CARRY_GIT_PREFIX)


class TestHostname:
    @pytest.mark.parametrize("url,expected", [
        ("https://github.com", "github.com"),
        ("https://github.com/", "github.com"),
        ("https://github.com.", "github.com"),          # trailing dot
        ("https://ghe.example.com", "ghe.example.com"),
        ("https://git.example.com:8443/gitlab", "git.example.com"),
        ("https://[::1]:8080/", "::1"),                 # IPv6 literal
        ("https://a/b@ghe.example.com", "a"),           # path, not userinfo
        ("https://user:pw@ghe.example.com", "ghe.example.com"),
        ("github.com", "github.com"),                   # no scheme
        ("https://", ""),
        ("", ""),
    ])
    def test_hostname(self, url, expected):
        from istota.forge_cli import _hostname
        assert _hostname(url) == expected

    def test_empty_host_falls_back_to_github_not_enterprise(self):
        """A parse failure must not silently route to GH_ENTERPRISE_TOKEN,
        which authenticates nothing and reads like a scope problem."""
        _, _, env = build_invocation(
            FORGE_GITHUB, ["pr", "list"], {"PATH": "/usr/bin"}, SENTINEL,
            "/usr/local/bin/gh", "/tmp/cfg", "https://",
        )
        assert env["GH_HOST"] == "github.com"
        assert env["GH_TOKEN"] == SENTINEL
        assert "GH_ENTERPRISE_TOKEN" not in env
