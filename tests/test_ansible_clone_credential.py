"""ISSUE-258: the repo clone token must not reach git through a URL.

The role used to build ``https://oauth2:<token>@host/...`` and hand it to the
``git:`` module as ``repo:``. git persists whatever URL it cloned from as
``remote.origin.url``, so the token landed in the checkout's ``.git/config``
and was then expanded into the argument vector of ``git-remote-https`` on
every fetch — the Ansible tag fetch, an operator's ``git pull``, and the
auto-update cron's fetch, which runs every two minutes.

The token now sits in a ``0600`` root-only file that a purpose-built helper
reads. That is still plaintext at rest, which is the honest trade; what it
buys is that nothing carrying the token is written into the checkout or into a
command line, and that rotating it is a file write.

The helper is deliberately *not* git's built-in ``store``. ``store``
implements ``erase``, and git calls ``erase`` on any 401 — so one revoked or
freshly-rotated token would empty the credential file and leave the two-minute
auto-update fetch failing silently, with ``MAILTO=""`` and output redirected
to a log. ``TestHelperScript`` is what holds that property down: it executes
the rendered helper and checks that every verb but ``get`` is a no-op.

These tests use the seam ``test_ansible_user_provisioning.py`` established and
``test_ansible_memory_limits.py`` follows: parse ``tasks/main.yml`` as YAML,
render templates through a bare Jinja environment, and assert against the
result. What only a real host can answer — whether git consults the helper at
all — is not asserted here; that was verified by hand against
``git credential fill`` and belongs to a staging deploy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"
TEMPLATES = ANSIBLE / "templates"

RESOLVE_TASK = "Resolve the repository host and credential applicability"
APPLIES_TASK = "Decide whether a clone credential applies"
HELPER_FILE_TASK = "Install the clone credential helper"
TOKEN_TASK = "Install the repository clone token"
REGISTER_TASK = "Register the clone credential helper"
REMOVE_TASK = "Remove the clone token when none is configured"
UNREGISTER_TASK = "Unregister the clone credential helper when no token is configured"
CHECK_TASK = "Check the existing repository remote for a persisted credential"
STRIP_TASK = "Strip a persisted credential from the repository remote"
CLONE_TASK = "Clone or update istota repository (branch checkout)"
READBACK_TASK = "Read back the repository remote URL"
ASSERT_TASK = "Assert the repository remote carries no embedded credential"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tasks() -> list:
    return yaml.safe_load(TASKS_FILE.read_text())


def find_task(name: str) -> dict:
    for task in tasks():
        if isinstance(task, dict) and task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


def task_index(name: str) -> int:
    for i, task in enumerate(tasks()):
        if isinstance(task, dict) and task.get("name") == name:
            return i
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


def when_clauses(task: dict) -> list[str]:
    """``when:`` as a list of strings, however the task spelled it."""
    when = task.get("when")
    if when is None:
        return []
    if isinstance(when, str):
        return [when]
    return [str(clause) for clause in when]


def _jinja() -> Environment:
    """A Jinja env carrying the one Ansible filter these expressions use.

    ``ansible.builtin.urlsplit`` is a thin wrapper over ``urllib.parse``'s
    ``urlsplit`` — the same stdlib call registered here — so this renders what
    Ansible renders. The expressions are deliberately free of ``regex_replace``
    and of the ``match`` test so that stays true; live behaviour was checked
    against a real ``ansible-playbook`` run.
    """
    env = Environment()
    env.filters["urlsplit"] = lambda value, part: getattr(urlsplit(value), part)
    return env


def resolve_host(repo_url: str) -> str:
    """Run the role's own host expression, rather than a copy of it."""
    expr = find_task(RESOLVE_TASK)["set_fact"]["_istota_repo_host"]
    return _jinja().from_string(expr).render(istota_repo_url=repo_url)


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------


class TestNoCredentialInTheCloneURL:
    """The regression guard. Each of these fails against the pre-fix role."""

    def test_clone_task_uses_the_bare_repo_url(self):
        repo = find_task(CLONE_TASK)["git"]["repo"]
        assert repo == "{{ istota_repo_url }}", (
            f"the clone task passes {repo!r}; anything but the bare "
            "istota_repo_url risks a credential being persisted as "
            "remote.origin.url (ISSUE-258)"
        )

    def test_no_task_interpolates_the_token_into_a_url(self):
        # The pre-fix role built this with a regex_replace into the scheme.
        # Catch any spelling of it, not just the one that was there.
        raw = TASKS_FILE.read_text()
        assert "istota_repo_url_auth" not in raw, (
            "istota_repo_url_auth is back; the clone token must not be "
            "interpolated into a URL (ISSUE-258)"
        )

    def test_the_token_appears_only_in_the_token_file_task(self):
        # The token file is the one place the token is allowed to be written,
        # because that file is what the helper reads. Anywhere else is either
        # a URL or a command line, and both are the defect.
        offenders = []
        for task in tasks():
            if not isinstance(task, dict):
                continue
            name = task.get("name")
            if name in (TOKEN_TASK, None):
                continue
            # `when:` guards and the applicability fact read the variable's
            # length; neither ever renders it.
            rendered_keys = {
                k: v for k, v in task.items() if k not in ("when", "set_fact")
            }
            if "istota_repo_clone_token" in yaml.safe_dump(rendered_keys):
                offenders.append(name)
        assert not offenders, (
            f"tasks render the clone token outside the token file: "
            f"{offenders} (ISSUE-258)"
        )

    @pytest.mark.parametrize("task_name", [CHECK_TASK, READBACK_TASK])
    def test_remote_readbacks_are_never_logged(self, task_name):
        # On a host not yet remediated, this task's stdout *is* the token.
        # Dropping no_log here would print it into the play recap, which is
        # the exposure the issue is about.
        assert find_task(task_name).get("no_log") is True, (
            f"{task_name!r} reads remote.origin.url, which on an "
            "unremediated host contains the token; it must not be logged"
        )


# ---------------------------------------------------------------------------
# The helper script — why it is not git's `store`
# ---------------------------------------------------------------------------


class TestHelperScript:
    """Execute the rendered helper. These are the reason it exists."""

    @staticmethod
    def _install(tmp_path: Path, token: str | None) -> Path:
        rendered = Environment().from_string(
            (TEMPLATES / "git-credential-istota.sh.j2").read_text()
        ).render(istota_namespace="istota")

        token_file = tmp_path / "clone-token"
        # The template hardcodes the real /etc path; point it at the fixture
        # so the verb dispatch can be exercised without root.
        rendered = rendered.replace("/etc/istota/clone-token", str(token_file))
        if token is not None:
            token_file.write_text(token)

        script = tmp_path / "istota-git-credential"
        script.write_text(rendered)
        script.chmod(0o755)
        return script

    @staticmethod
    def _run(script: Path, verb: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(script), verb],
            input="protocol=https\nhost=git.example.com\n\n",
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_get_answers_with_the_token(self, tmp_path):
        script = self._install(tmp_path, "glpat-EXAMPLE-TOKEN")
        result = self._run(script, "get")
        assert result.returncode == 0
        assert "username=oauth2" in result.stdout
        assert "password=glpat-EXAMPLE-TOKEN" in result.stdout

    @pytest.mark.parametrize("verb", ["erase", "store"])
    def test_erase_and_store_are_no_ops(self, tmp_path, verb):
        # This is the whole reason the role does not use `store --file=`.
        # git calls `erase` on any 401, and git-credential-store answers it by
        # truncating its file — turning one expired token into a permanently
        # broken auto-update that reports nothing.
        script = self._install(tmp_path, "glpat-EXAMPLE-TOKEN")
        token_file = tmp_path / "clone-token"
        before = token_file.read_text()

        result = self._run(script, verb)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", (
            f"the helper answered {verb!r}; it must only answer `get`"
        )
        assert token_file.read_text() == before, (
            f"{verb!r} modified the token file — the failure mode this helper "
            "exists to avoid"
        )

    def test_missing_token_file_falls_through_quietly(self, tmp_path):
        # A caller that cannot read the token (anyone but root) must look like
        # "no credential configured", not like an empty password — an empty
        # password is an auth attempt that fails.
        script = self._install(tmp_path, None)
        result = self._run(script, "get")
        assert result.returncode == 0
        assert "password=" not in result.stdout

    def test_empty_token_file_falls_through_quietly(self, tmp_path):
        script = self._install(tmp_path, "")
        result = self._run(script, "get")
        assert result.returncode == 0
        assert "password=" not in result.stdout

    def test_token_is_passed_through_verbatim(self, tmp_path):
        # Deliberately not a URL: git's credential-store format percent-decodes
        # the userinfo, which mangles a token containing '%', '@', ':' or '/'.
        awkward = "tok%40:with/@chars%"
        script = self._install(tmp_path, awkward)
        result = self._run(script, "get")
        assert f"password={awkward}" in result.stdout


class TestHelperInstallation:
    def test_script_is_root_owned_and_executable(self):
        spec = find_task(HELPER_FILE_TASK)["template"]
        assert spec["src"] == "git-credential-istota.sh.j2"
        assert spec["mode"] == "0755"
        assert spec["owner"] == "root"
        assert spec["group"] == "root"

    def test_token_file_is_root_only(self):
        spec = find_task(TOKEN_TASK)["copy"]
        assert spec["mode"] == "0600", (
            "the token file is read by a root-run helper and by nothing else, "
            "so nothing but root should be able to read it"
        )
        assert spec["owner"] == "root"
        assert spec["group"] == "root"

    def test_token_file_holds_the_bare_token(self):
        content = find_task(TOKEN_TASK)["copy"]["content"]
        assert content.strip() == "{{ istota_repo_clone_token }}", content
        assert "oauth2" not in content, (
            "a URL-shaped entry would be percent-decoded by git's own parser"
        )

    def test_token_file_is_never_logged(self):
        assert find_task(TOKEN_TASK).get("no_log") is True

    def test_helper_registered_against_the_repository_host_only(self):
        name = find_task(REGISTER_TASK)["community.general.git_config"]["name"]
        assert name.startswith("credential."), name
        assert name.endswith(".helper"), name
        assert "_istota_repo_host" in name, (
            "an unscoped credential.helper would be consulted for every host "
            "root talks to over git"
        )

    def test_helper_registered_at_system_scope(self):
        # /etc/gitconfig is read whichever user and whatever HOME the fetch
        # runs under, and the auto-update fetch runs from cron rather than
        # from the play.
        cfg = find_task(REGISTER_TASK)["community.general.git_config"]
        assert cfg["scope"] == "system", cfg["scope"]
        assert cfg["value"].startswith("/usr/local/bin/"), cfg["value"]

    @pytest.mark.parametrize(
        "task_name", [HELPER_FILE_TASK, TOKEN_TASK, REGISTER_TASK]
    )
    def test_installed_before_anything_fetches(self, task_name):
        # The property the first deploy onto a provisioned host turns on: the
        # credential must exist before the clone's own fetch needs it.
        assert task_index(task_name) < task_index(CLONE_TASK), (
            f"{task_name!r} runs after the clone, so the clone has no "
            "credential to authenticate with"
        )

    @pytest.mark.parametrize(
        "task_name", [HELPER_FILE_TASK, TOKEN_TASK, REGISTER_TASK]
    )
    def test_gated_on_the_credential_applying(self, task_name):
        guards = " ".join(when_clauses(find_task(task_name)))
        assert "_istota_clone_credential" in guards, (
            f"{task_name!r} is not gated; it must be skipped for a public "
            "clone and for a non-https remote"
        )


class TestCredentialRemoval:
    """Clearing the token must actually retire it, not orphan it on disk."""

    def test_token_and_helper_are_removed(self):
        task = find_task(REMOVE_TASK)
        assert task["file"]["state"] == "absent"
        targets = " ".join(task["loop"])
        assert "clone-token" in targets
        assert "git-credential" in targets

    def test_helper_registration_is_unset(self):
        cfg = find_task(UNREGISTER_TASK)["community.general.git_config"]
        assert cfg["state"] == "absent"
        assert cfg["scope"] == "system"

    @pytest.mark.parametrize("task_name", [REMOVE_TASK, UNREGISTER_TASK])
    def test_runs_only_when_no_credential_applies(self, task_name):
        guards = " ".join(when_clauses(find_task(task_name)))
        assert "not (_istota_clone_credential" in guards, guards


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------


class TestRepositoryHostResolution:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://git.example.com/g/r.git", "git.example.com"),
            ("https://git.example.com:8443/g/r.git", "git.example.com:8443"),
            ("https://oauth2@git.example.com/g/r.git", "git.example.com"),
            ("https://git.example.com/", "git.example.com"),
        ],
    )
    def test_extracts_host_and_port(self, url, expected):
        assert resolve_host(url) == expected

    def test_preserves_case(self):
        # git presents the host to a helper exactly as the URL spells it and
        # matches config subsections by exact string. Lowercasing here — which
        # is what urlsplit('hostname') does — would write a key git can never
        # match, and the symptom is an auth failure against config that looks
        # correct.
        assert resolve_host("https://GIT.Example.com/g/r.git") == "GIT.Example.com"

    def test_non_https_urls_are_excluded_rather_than_mangled(self):
        # An scp-style ssh URL has no host in the https sense; the role must
        # skip the credential entirely rather than derive junk from it.
        expr = find_task(RESOLVE_TASK)["set_fact"]["_istota_repo_is_https"]
        env = _jinja()
        for url in ("git@git.example.com:g/r.git", "ssh://git@git.example.com/g/r.git"):
            assert env.from_string(expr).render(istota_repo_url=url) == "False", url
        assert (
            env.from_string(expr).render(istota_repo_url="https://git.example.com/x")
            == "True"
        )

    def test_applicability_requires_both_a_token_and_https(self):
        expr = find_task(APPLIES_TASK)["set_fact"]["_istota_clone_credential"]
        assert "istota_repo_clone_token" in expr
        assert "_istota_repo_is_https" in expr


# ---------------------------------------------------------------------------
# Existing hosts, and the guard that keeps them fixed
# ---------------------------------------------------------------------------


class TestRemediationOfExistingCheckouts:
    def test_strips_the_credential_before_the_clone_runs(self):
        # Every host provisioned before this fix already has the token in
        # .git/config. Correcting the clone task alone leaves it there.
        assert task_index(STRIP_TASK) < task_index(CLONE_TASK), (
            "the remote is rewritten after the clone, so the clone's own "
            "fetch still expands the old credential into argv one more time "
            "on every deploy"
        )

    def test_rewrites_to_the_bare_url(self):
        cmd = find_task(STRIP_TASK)["command"]
        assert "remote set-url origin {{ istota_repo_url }}" in cmd, cmd
        assert "oauth2" not in cmd

    def test_never_strips_without_a_replacement_credential(self):
        # A run with the vault unloaded would otherwise remove the host's only
        # working credential and put nothing in its place.
        guards = " ".join(when_clauses(find_task(STRIP_TASK)))
        assert "_istota_clone_credential" in guards, guards

    def test_matches_a_password_not_a_bare_at_sign(self):
        # `git@host:path` and `https://oauth2@host/…` both contain '@' and
        # neither carries a secret. Matching '@' would rewrite them on every
        # run and, for the assert below, fail the play forever.
        guards = " ".join(when_clauses(find_task(STRIP_TASK)))
        assert "urlsplit('password')" in guards, guards

    def test_survives_check_mode(self):
        # `command` does not support check mode, so without this the register
        # yields a skip dict with no rc and the `when` raises on a missing
        # attribute — failing the play for the wrong reason.
        check = find_task(CHECK_TASK)
        assert check.get("check_mode") is False
        assert check.get("failed_when") is False
        guards = " ".join(when_clauses(find_task(STRIP_TASK)))
        assert "default(1)" in guards, guards


class TestNoInteractivePrompts:
    """A credential problem must fail, never block on a prompt.

    With the token out of the URL, git responds to a missing or rejected
    credential by asking for a username. The ``git`` module does not disable
    that (its own docs tell the caller to), and in the cron script a prompt
    would hang the run while it holds the flock — after which every later run
    exits silently at the lock and updates stop with nothing reported.
    """

    @pytest.mark.parametrize("task_name", [CLONE_TASK, "Resolve and checkout tag"])
    def test_git_tasks_disable_terminal_prompts(self, task_name):
        env = find_task(task_name).get("environment", {})
        assert env.get("GIT_TERMINAL_PROMPT") == "0", (
            f"{task_name!r} can hang the play on a credential prompt"
        )

    def test_update_script_disables_terminal_prompts_before_fetching(self):
        script = (TEMPLATES / "istota-update.sh.j2").read_text()
        export_at = script.find("GIT_TERMINAL_PROMPT=0")
        fetch_at = script.find("git fetch")
        assert export_at != -1, "the auto-update script can hang on a prompt"
        assert export_at < fetch_at, (
            "GIT_TERMINAL_PROMPT is set after the fetch it needs to protect"
        )


class TestPostCloneAssertion:
    def test_asserts_after_the_clone(self):
        assert task_index(ASSERT_TASK) > task_index(CLONE_TASK)

    def test_fails_the_play_on_a_credential_in_the_remote(self):
        that = find_task(ASSERT_TASK)["assert"]["that"]
        joined = " ".join(that) if isinstance(that, list) else that
        assert "urlsplit('password')" in joined, joined
        assert "length == 0" in joined, joined

    def test_readback_cannot_abort_the_play_opaquely(self):
        # This task carries no_log, so an unguarded failure gives the operator
        # "output has been hidden" and no rc, no stderr, no clue.
        task = find_task(READBACK_TASK)
        assert task.get("failed_when") is False
        assert task.get("check_mode") is False
        that = find_task(ASSERT_TASK)["assert"]["that"]
        joined = " ".join(that) if isinstance(that, list) else that
        assert "default('')" in joined, joined

    def test_failure_message_does_not_print_the_url(self):
        # The whole point is that the value stays out of logs. An assert that
        # fails by echoing remote.origin.url would leak it into the recap.
        fail_msg = find_task(ASSERT_TASK)["assert"].get("fail_msg", "")
        assert "stdout" not in fail_msg, (
            "fail_msg interpolates the registered URL, which is the token"
        )
        assert "ISSUE-258" in fail_msg, (
            "a future reader hitting this needs the entry that explains it"
        )
