"""The argv boundary in front of `gws`, which is the one CLI nothing walks.

`google_workspace.main` is `os.execvp("gws", ["gws"] + sys.argv[1:])`. The
program it execs is not in this tree and is not installed on the development
machine, so its arguments cannot be enumerated the way every other skill's are
— and one of them is documented as taking a host path: `drive +upload
/path/to/file.pdf` reads a file here and puts it in Google Drive.

So the boundary is on argv rather than on a declaration, and this file is what
stands in for the coverage walk. Two properties matter and each has cases
written to kill the plausible wrong implementation:

**A token that is not path-shaped is passed through untouched.** Google
Workspace addresses its own objects by opaque id — `--fileId`, `--parents
FOLDER_ID`, spreadsheet ids — and a scan that refused those would break every
verb in the skill while protecting nothing. A scan that passed *everything*
through would be equally green against a table of only-refusal cases, which is
why every refusal case here has a passthrough sibling.

**A rewritten token is the resolved path, not the argument.** The mount is
reached through a symlink, so the two strings differ; an assertion comparing
them cannot tell a resolving implementation from one that merely approves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from istota.skills.google_workspace import main, scan_argv
from tests.support.skill_cli import run_skill_main


@pytest.fixture(autouse=True)
def _cwd_guard():
    """`main` chdirs into the workspace, and the cwd is process-global.

    Restored here rather than by each test's `monkeypatch.chdir`, because the
    tests that drive `main` do not all set one and a leaked cwd is the kind of
    cross-test poison that turns a *different* file red.
    """
    before = os.getcwd()
    try:
        yield
    finally:
        os.chdir(before)


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """The real mount's shape, behind a symlink, with alice as the caller."""
    real = tmp_path / "srv" / "shared"
    for sub in ("Users/alice", "Users/bob", "Talk"):
        (real / sub).mkdir(parents=True)
    link = tmp_path / "mount"
    link.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(link))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)

    # An empty directory, established rather than inherited: `_is_path_shaped`
    # ends in `Path(token).exists()` against the process cwd, so every
    # passthrough case below is an assertion about wherever pytest happens to
    # be standing until this is pinned. Run from a directory holding a file
    # called `list` or `drive`, they would go red for a reason that is not
    # about the scan.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return link


@pytest.fixture
def report(mount):
    path = mount / "Users" / "alice" / "report.pdf"
    path.write_bytes(b"%PDF-")
    return path


class TestWhatIsPassedThrough:
    """The half a refusal-only table cannot establish."""

    @pytest.mark.parametrize("token", [
        "drive",
        "+upload",
        "files",
        "list",
        "--fileId",
        "1a2B3c4D5e6F7g8H9i",
        "--parents",
        "FOLDER_ID",
        "--query",
        "name contains 'a/b'",
        "--page-all",
        "--dry-run",
        "--spreadsheetId",
        "Sheet1!A1:D10",
        "is:unread",
        '{"title": "Budget"}',
    ])
    def test_a_token_that_is_not_path_shaped_is_untouched(self, token, mount):
        assert scan_argv([token]) == ([token], None)

    def test_a_query_containing_a_slash_is_not_a_path(self, mount):
        argv = ["drive", "files", "list", "--query", "name contains 'a/b'"]
        assert scan_argv(argv) == (argv, None)

    def test_a_flag_with_an_opaque_value_after_an_equals_is_untouched(self, mount):
        argv = ["--fileId=1a2B3c4D5e6F7g8H9i"]
        assert scan_argv(argv) == (argv, None)


class TestWhatIsScoped:
    def test_an_absolute_in_root_path_is_rewritten_to_its_resolution(
        self, mount, report,
    ):
        scanned, error = scan_argv(["drive", "+upload", str(report)])

        assert error is None
        assert scanned == ["drive", "+upload", str(report.resolve())]
        assert scanned[-1] != str(report), (
            "the token was approved rather than resolved; the mount is a "
            "symlink, so the two strings differ"
        )

    def test_an_absolute_path_outside_the_roots_is_refused(self, mount):
        scanned, error = scan_argv(["drive", "+upload", "/etc/passwd"])

        assert error is not None
        assert scanned == []
        assert "/etc/passwd" in error

    def test_another_users_workspace_is_refused(self, mount):
        theirs = mount / "Users" / "bob" / "theirs.pdf"
        theirs.write_bytes(b"%PDF-")

        _, error = scan_argv(["drive", "+upload", str(theirs)])

        assert error is not None

    def test_a_tilde_token_is_path_shaped_and_refused(self, mount):
        """`~/.config/gws/token` never reaches the exec.

        No expansion happens here: a `~` token resolves against the cwd like
        any other relative one and is refused for being outside the roots or
        for not existing. Either way it is refused, which is the answer that
        matters — expanding it would only change which message says so.
        """
        _, error = scan_argv(["--credentials", "~/.config/gws/token.json"])

        assert error is not None

    def test_a_dotdot_component_is_path_shaped_even_when_nothing_is_there(
        self, mount,
    ):
        """The case an existence test alone misses.

        A relative token with a `..` in it names something above the cwd and
        must be scanned whether or not it resolves to a file today.
        """
        _, error = scan_argv(["../../etc/passwd"])

        assert error is not None

    def test_a_flag_is_split_on_its_first_equals(self, mount):
        """`--file=/srv/app/istota/data/istota.db` is one token.

        It starts with `-`, holds no `..` component, and names nothing
        relative to the cwd, so a per-token rule passes it straight through
        and `gws` splits it itself. Splitting here is what closes that.
        """
        _, error = scan_argv(["--file=/etc/passwd"])

        assert error is not None

    def test_an_in_root_value_after_an_equals_is_rewritten_in_place(
        self, mount, report,
    ):
        scanned, error = scan_argv([f"--file={report}"])

        assert error is None
        assert scanned == [f"--file={report.resolve()}"]

    def test_only_the_first_equals_splits(self, mount, report):
        """A value that itself contains `=` keeps it."""
        target = mount / "Users" / "alice" / "a=b.pdf"
        target.write_bytes(b"%PDF-")

        scanned, error = scan_argv([f"--file={target}"])

        assert error is None
        assert scanned == [f"--file={target.resolve()}"]

    def test_an_existing_relative_path_is_resolved_against_the_cwd(
        self, mount, report, monkeypatch,
    ):
        monkeypatch.chdir(mount / "Users" / "alice")

        scanned, error = scan_argv(["+upload", "report.pdf"])

        assert error is None
        assert scanned == ["+upload", str(report.resolve())]

    def test_an_existing_relative_path_outside_the_roots_is_refused(
        self, mount, tmp_path, monkeypatch,
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.pdf").write_bytes(b"%PDF-")
        monkeypatch.chdir(outside)

        _, error = scan_argv(["+upload", "secret.pdf"])

        assert error is not None


class TestTheDoubleDash:
    """Honoured, and everything after it is still scanned.

    gws takes no positional passthrough today, so treating what follows as
    opaque would be a bypass by one character.
    """

    def test_the_separator_itself_survives(self, mount):
        argv = ["drive", "files", "list", "--", "FOLDER_ID"]
        assert scan_argv(argv) == (argv, None)

    def test_a_path_after_it_is_still_refused(self, mount):
        _, error = scan_argv(["drive", "+upload", "--", "/etc/passwd"])

        assert error is not None

    def test_a_path_after_it_is_still_resolved(self, mount, report):
        scanned, error = scan_argv(["drive", "+upload", "--", str(report)])

        assert error is None
        assert scanned == ["drive", "+upload", "--", str(report.resolve())]

    def test_a_token_after_it_is_still_split_on_its_equals(self, mount):
        """The bypass a `--` arm produced, and the reason there is no arm.

        An earlier version read `--` as a switch to whole-token testing, on
        the reasoning that after a separator nothing is a flag so the whole
        token is the value. That is *narrower*, not wider: `--file=/etc/passwd`
        as a whole token is relative, holds no `..` and names nothing, so it
        went through untouched — the one-character bypass the rule about
        scanning past `--` exists to close, reintroduced by the code meant to
        implement it.
        """
        _, error = scan_argv(["--", "--file=/etc/passwd"])

        assert error is not None


class TestAValueAttachedToAFlag:
    """A path does not have to be the token to reach gws.

    Three spellings put one inside a token, and a scan that reads only whole
    tokens passes all three. The `=` form is the one the spec names; the other
    two are the same defect in another parser's spelling, and whether gws
    accepts them is unverified — it is not in this tree, which is the reason
    to scan them rather than the reason not to.
    """

    @pytest.mark.parametrize("token", [
        "--file=/etc/passwd",
        "-f/etc/passwd",
        "@/etc/passwd",
        "key=/etc/passwd",
    ])
    def test_an_attached_out_of_root_path_is_refused(self, token, mount):
        _, error = scan_argv([token])

        assert error is not None, f"{token} reached gws unscanned"

    @pytest.mark.parametrize("prefix", ["-f", "@"])
    def test_an_attached_in_root_path_is_rewritten_behind_its_prefix(
        self, prefix, mount, report,
    ):
        scanned, error = scan_argv([f"{prefix}{report}"])

        assert error is None
        assert scanned == [f"{prefix}{report.resolve()}"]

    @pytest.mark.parametrize("token", ["-v", "--", "-", "@", "@me", "me@example.com"])
    def test_a_token_with_no_path_in_it_is_untouched(self, token, mount):
        assert scan_argv([token]) == ([token], None)


class TestTheWorkspaceIsTheCwd:
    """The consequence of scanning after the chdir, recorded rather than fixed.

    Every relative token is existence-tested inside the user's own workspace,
    which is a directory the model writes to — so a file named after a gws
    verb makes that verb path-shaped and the token is rewritten to an absolute
    path. The command then fails in gws rather than doing something
    unintended, which is why the existence signal stays: dropping it for
    separator-less tokens would let a bare `secret.pdf` beside a cwd that is
    *not* the workspace through unscanned, trading a loud failure for a quiet
    read.
    """

    def test_a_workspace_file_named_like_a_verb_rewrites_the_verb(
        self, mount, monkeypatch,
    ):
        workspace = mount / "Users" / "alice"
        (workspace / "drive").write_bytes(b"")
        monkeypatch.chdir(workspace)

        scanned, error = scan_argv(["drive", "files", "list"])

        assert error is None
        assert scanned == [str((workspace / "drive").resolve()), "files", "list"]

    def test_a_bare_workspace_filename_is_still_admitted(
        self, mount, report, monkeypatch,
    ):
        """The case the existence signal is there for: `+upload report.pdf`."""
        monkeypatch.chdir(mount / "Users" / "alice")

        scanned, error = scan_argv(["+upload", "report.pdf"])

        assert error is None
        assert scanned == ["+upload", str(report.resolve())]


class TestNoRootsResolvable:
    def test_every_path_shaped_token_is_refused(self, monkeypatch):
        """An empty allowlist refuses everything; it never widens."""
        for var in (
            "NEXTCLOUD_MOUNT_PATH", "ISTOTA_USER_ID",
            "ISTOTA_DEFERRED_DIR", "ISTOTA_CONVERSATION_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        _, error = scan_argv(["+upload", "/tmp/anything.pdf"])

        assert error is not None

    def test_an_opaque_token_still_passes(self, monkeypatch):
        for var in (
            "NEXTCLOUD_MOUNT_PATH", "ISTOTA_USER_ID",
            "ISTOTA_DEFERRED_DIR", "ISTOTA_CONVERSATION_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        assert scan_argv(["--parents", "FOLDER_ID"]) == (
            ["--parents", "FOLDER_ID"], None,
        )


class TestMain:
    """The exec itself: what argv reaches it, and where it is standing."""

    @pytest.fixture
    def execs(self, monkeypatch):
        calls: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(
            os, "execvp", lambda file, args: calls.append((file, list(args))),
        )
        return calls

    def test_a_refused_path_never_reaches_the_exec(self, mount, execs):
        run = run_skill_main(main, ["drive", "+upload", "/etc/passwd"])

        assert execs == []
        assert run.exit_code == 1, run.stdout
        assert run.envelope.get("status") == "error", run.stdout
        assert run.envelope.get("reason") == "host_path_refused", run.envelope

    def test_an_accepted_path_reaches_the_exec_resolved(
        self, mount, report, execs, monkeypatch,
    ):
        monkeypatch.chdir(mount)

        run_skill_main(main, ["drive", "+upload", str(report)])

        assert execs == [("gws", ["gws", "drive", "+upload", str(report.resolve())])]

    def test_the_cwd_is_the_users_workspace(self, mount, execs, monkeypatch):
        """What carries the residual the scan cannot classify.

        A relative token naming something that does not exist yet is not
        path-shaped, so it passes through unscoped — and `gws` then resolves
        it against *this* process's cwd. Standing in the user's own workspace
        is what puts it in-roots by construction rather than beside the
        daemon's working directory.
        """
        monkeypatch.chdir(mount)

        run_skill_main(main, ["drive", "files", "list"])

        assert execs, "the exec never happened"
        assert Path.cwd().resolve() == (mount / "Users" / "alice").resolve()

    def test_a_workspace_that_cannot_be_entered_refuses_the_invocation(
        self, mount, execs, monkeypatch, tmp_path,
    ):
        """A resolved workspace that is not there is not the "no workspace" case.

        `user_workspace_root` checks nothing on disk, so a deployment whose
        workspace has not been created yet hands back a path and `chdir`
        raises. Carrying on would leave the residual the chdir exists to carry
        — a relative token naming something that does not exist — resolving
        against the daemon's working directory, with a log line as the only
        record. The roots are non-empty in that state, so nothing else refuses
        it.
        """
        import shutil

        shutil.rmtree(mount / "Users" / "alice")
        monkeypatch.chdir(tmp_path)

        run = run_skill_main(main, ["drive", "files", "list"])

        assert execs == []
        assert run.exit_code == 1, run.stdout
        assert run.envelope.get("reason") == "host_path_refused", run.envelope
        assert Path.cwd().resolve() == tmp_path.resolve()

    def test_it_execs_from_wherever_it_is_when_no_workspace_resolves(
        self, tmp_path, execs, monkeypatch,
    ):
        """A missing workspace is not a refusal on its own.

        There is nothing to scope a non-path token against, and refusing the
        whole invocation would break every opaque-id verb on a deployment
        with no mount. The path-shaped tokens are still refused — by the
        empty allowlist, above.
        """
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        monkeypatch.chdir(tmp_path)

        run_skill_main(main, ["drive", "files", "list"])

        assert execs == [("gws", ["gws", "drive", "files", "list"])]
        assert Path.cwd().resolve() == tmp_path.resolve()
