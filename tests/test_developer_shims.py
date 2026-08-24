"""The shims: what `setup_env` writes when a deployment routes builds into a container.

Fifteen small files on the model's `PATH`, each execing a client that speaks the
exec protocol to that user's devbox and exits with the command's real status.
Nothing here contacts anything — the whole point of Design 10 is that this half
of the feature is inert I/O-free file writing, so the tests are file assertions.

**The load-bearing one is `test_shims_are_written_for_a_task_that_selected_nothing`.**
An earlier draft of this design gated the whole container branch on the
`developer` skill being *selected*, and `developer` is a menu skill with no
`always_include` and no `source_types` — it reaches `selected_skills` only via
sticky skills, which is to say on the **second** turn of a conversation. On a
fresh "work on repo X" the shims would have been absent, `npm ci` would have run
host-side, and it would have 403'd at the CONNECT proxy: a feature that silently
did not route on the normal path, failing in a way that reads as flakiness. That
test is the assertion that would have caught it.

The gate that *is* a security decision lives in the executor and is a different
predicate (`"developer" in authorized_skills`); `tests/test_executor_exec_bind.py`
holds that one.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from istota import config as istota_config
from istota import db
from istota.config import (
    Config,
    ContainerConfig,
    DeveloperConfig,
    SecurityConfig,
)
from istota.skills.developer import setup_env

SHIM_DIR_NAME = "exec-shims"


class _Ctx:
    """The shape `dispatch_setup_env_hooks` hands a hook.

    A real `EnvContext` carries more; the hook reads `config`, `task` and
    `user_temp_dir` and nothing else, and building the real one drags in the
    skill index.
    """

    def __init__(self, config, user_temp_dir: Path, user_id: str = "alice"):
        self.config = config
        self.user_temp_dir = str(user_temp_dir)
        self.task = db.Task(
            id=7, prompt="work on repo x", user_id=user_id,
            source_type="talk", status="running", conversation_token="room-1",
        )
        self.is_admin = True
        self.user_resources = []
        self.user_config = None
        self.discovered_calendars = []


def _make_config(tmp_path: Path, *, backend: str = "devbox", **container) -> Config:
    repos = tmp_path / "repos"
    repos.mkdir(exist_ok=True)
    config = Config()
    config.developer = DeveloperConfig(
        enabled=True,
        repos_dir=str(repos),
        container=ContainerConfig(backend=backend, **container),
    )
    config.security = SecurityConfig(skill_proxy_enabled=True)
    return config


def _run_hook(config: Config, tmp_path: Path, user_id: str = "alice") -> tuple[dict, Path]:
    user_temp = tmp_path / "temp" / user_id
    user_temp.mkdir(parents=True, exist_ok=True)
    env = setup_env(_Ctx(config, user_temp, user_id))
    return env, user_temp


def _shim_dir(user_temp: Path) -> Path:
    return user_temp / ".developer" / SHIM_DIR_NAME


class TestTheCorrectedGate:
    """Design 10, and the finding the whole stage turns on."""

    def test_shims_are_written_for_a_task_that_selected_nothing(self, tmp_path):
        """The first turn of a fresh conversation.

        `setup_env` is dispatched for every skill in the index whatever the task
        selected, and this hook self-gates on configuration alone. Nothing in
        this test mentions a selected skill, because the gate must not consult
        one.
        """
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        assert (_shim_dir(user_temp) / "npm").is_file()

    def test_nothing_is_written_with_the_backend_off(self, tmp_path):
        """`backend = none` is every deployment that has not opted in, and it
        must be byte-identical to what it was."""
        config = _make_config(tmp_path, backend="none")

        _, user_temp = _run_hook(config, tmp_path)

        assert not _shim_dir(user_temp).exists()
        assert not (user_temp / ".developer" / "devbox-exec").exists()

    def test_a_shim_left_behind_by_a_previous_task_is_removed(self, tmp_path):
        """`user_temp_dir` persists across tasks. A command taken out of
        `shim_commands` — or a whole deployment flipped back to `none` — must
        not leave a file on the model's PATH that still execs the client, since
        the shell resolves by name and nothing else would notice."""
        config = _make_config(tmp_path)
        _, user_temp = _run_hook(config, tmp_path)
        assert (_shim_dir(user_temp) / "cargo").is_file()

        config.developer.container.backend = "none"
        _run_hook(config, tmp_path)

        assert not (_shim_dir(user_temp) / "cargo").exists()

    def test_narrowing_the_list_removes_the_shim_it_dropped(self, tmp_path):
        config = _make_config(tmp_path)
        _, user_temp = _run_hook(config, tmp_path)
        assert (_shim_dir(user_temp) / "yarn").is_file()

        config.developer.container.shim_commands = ["npm"]
        _run_hook(config, tmp_path)

        assert not (_shim_dir(user_temp) / "yarn").exists()
        assert (_shim_dir(user_temp) / "npm").is_file()


class TestWhatIsOnPath:
    def test_one_shim_per_configured_command_and_each_is_executable(self, tmp_path):
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        for command in config.developer.container.shim_commands:
            path = _shim_dir(user_temp) / command
            assert path.is_file(), f"no shim for {command}"
            assert path.stat().st_mode & stat.S_IXUSR, f"{command} is not executable"

    @pytest.mark.parametrize("absent", ["python3", "python", "make"])
    def test_the_deliberate_absences(self, absent, tmp_path):
        """`python3` because the sandbox starts its own network bridge with it,
        several `developer/skill.md` recipes parse forge output with `python3
        -c`, and the exec client is itself a Python script. `make` because
        shimming a driver inverts routing for everything beneath it: a Makefile
        calling `git`, `gh` or `python3` would get the container's copies."""
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        assert not (_shim_dir(user_temp) / absent).exists()

    def test_an_operator_cannot_shim_the_interpreter(self, tmp_path):
        """`make` is configurable; `python3` is not, because a shim for it would
        route `build_bwrap_cmd`'s own network bridge into the container and
        break egress for every developer-enabled task."""
        config = _make_config(tmp_path)
        config.developer.container.shim_commands = list(
            istota_config._parse_container_block(
                {"shim_commands": ["npm", "python3", "make"]}
            )["shim_commands"]
        )

        _, user_temp = _run_hook(config, tmp_path)

        assert (_shim_dir(user_temp) / "make").is_file()
        assert not (_shim_dir(user_temp) / "python3").exists()

    def test_the_shim_directory_is_on_the_returned_path(self, tmp_path):
        config = _make_config(tmp_path)

        env, user_temp = _run_hook(config, tmp_path)

        entries = env["ISTOTA_PATH_PREPEND"].split(os.pathsep)
        assert str(_shim_dir(user_temp)) in entries

    def test_the_forge_wrappers_keep_their_place_ahead_of_it(self, tmp_path):
        """`.developer` first, so a shim can never shadow `gh` or `glab`."""
        config = _make_config(tmp_path)
        config.developer.gitlab_token = "t" * 20
        config.developer.gitlab_url = "https://gitlab.example.com"

        env, user_temp = _run_hook(config, tmp_path)

        entries = env["ISTOTA_PATH_PREPEND"].split(os.pathsep)
        assert entries == [str(user_temp / ".developer"), str(_shim_dir(user_temp))]
        assert (user_temp / ".developer" / "gh").is_file()
        assert (user_temp / ".developer" / "glab").is_file()

    def test_the_path_entry_arrives_with_no_forge_token_configured(self, tmp_path):
        """A deployment routing builds into the devbox does so whether or not it
        has a forge. Before this the reserved key was set only inside the
        forge-wrapper branch."""
        config = _make_config(tmp_path)

        env, user_temp = _run_hook(config, tmp_path)

        assert env["ISTOTA_PATH_PREPEND"] == str(_shim_dir(user_temp))


class TestBothPathsAreBakedIn:
    """Design 5. A shim runs as a child of the model's own shell, so an
    env-supplied path is a path the model chooses — and an earlier draft baked
    the client path on exactly that reasoning and then introduced the socket as
    an environment variable one section later, which would have let
    `ISTOTA_DEVBOX_EXEC_SOCKET=/tmp/mine npm ci` return a fabricated exit 0 from
    a socket the model wrote.
    """

    def test_the_client_path_and_the_socket_path_are_both_literals(self, tmp_path):
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        body = (_shim_dir(user_temp) / "npm").read_text()
        client = user_temp / ".developer" / "devbox-exec"
        socket = istota_config.exec_socket_path(config, "alice")
        assert str(client) in body
        assert str(socket) in body

    def test_no_environment_variable_carries_either(self, tmp_path):
        """Not "the well-known name is absent" — *no* expansion of anything.

        A shim that read either path from a variable would be defeated by one
        assignment in front of the command, and a check for a particular
        variable name would pass the moment somebody picked a different name.
        """
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        body = (_shim_dir(user_temp) / "npm").read_text()
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        # `"$@"` is the command's own arguments and is the one expansion a shim
        # legitimately performs.
        assert code.count("$") == code.count('"$@"'), (
            f"a shim expands something other than its arguments:\n{code}"
        )

    def test_no_shim_names_a_devbox_environment_variable(self, tmp_path):
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        for path in _shim_dir(user_temp).iterdir():
            assert "ISTOTA_DEVBOX_EXEC_SOCKET" not in path.read_text()

    def test_the_socket_is_this_users_and_not_the_parent(self, tmp_path):
        """The parent holds every user's socket; mounting or naming it would be
        arbitrary command execution against another user's repositories."""
        config = _make_config(tmp_path)

        _, alice_temp = _run_hook(config, tmp_path, user_id="alice")
        _, bob_temp = _run_hook(config, tmp_path, user_id="bob")

        alice = (_shim_dir(alice_temp) / "npm").read_text()
        bob = (_shim_dir(bob_temp) / "npm").read_text()
        assert "/alice/exec.sock" in alice
        assert "/bob/exec.sock" in bob
        assert "/bob/" not in alice

    def test_a_path_with_a_space_survives_quoting(self, tmp_path):
        """The paths are interpolated as shell literals, and a task temp
        directory is operator-configured."""
        spaced = tmp_path / "a dir with spaces"
        spaced.mkdir()
        config = _make_config(tmp_path)
        config.developer.container.exec_socket_dir = str(spaced / "exec")

        _, user_temp = _run_hook(config, tmp_path)

        body = (_shim_dir(user_temp) / "npm").read_text()
        assert "'" in body, "an unquoted path with a space would word-split"


class TestStdinAndArguments:
    def test_a_pipe_passes_stdin_and_a_terminal_does_not(self, tmp_path):
        """`[ -t 0 ]` tests exactly the condition the server's refusal was
        written for — keeping a child off the operator's tty under `istota
        serve`. Never setting it silently breaks every pipeline into a shimmed
        command and, under the native Bash tool's pipefail, colours the result
        141."""
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        body = (_shim_dir(user_temp) / "npm").read_text()
        assert "[ -t 0 ]" in body
        lines = [line.strip() for line in body.splitlines() if line.strip().startswith("exec ")]
        assert len(lines) == 2
        terminal, piped = lines
        assert "--stdin" not in terminal
        assert "--stdin" in piped

    def test_a_shim_actually_forwards_the_flag_when_run(self, tmp_path):
        """Executed rather than read. A shim is a shell script and the branch is
        the whole of its logic; asserting on the text alone would pass on a
        script whose `if` was inverted."""
        import subprocess

        config = _make_config(tmp_path)
        _, user_temp = _run_hook(config, tmp_path)
        shim = _shim_dir(user_temp) / "npm"

        # Replace the client with something that reports its own argv, so the
        # shim's branch is what is under test rather than the transport.
        client = user_temp / ".developer" / "devbox-exec"
        client.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        client.chmod(0o755)

        piped = subprocess.run(
            [str(shim), "ci", "--foo bar", "*.js"],
            capture_output=True, text=True, stdin=subprocess.PIPE, check=False,
        )

        argv = piped.stdout.splitlines()
        assert "--stdin" in argv, "a shim reading from a pipe did not forward stdin"
        # An argument carrying a space and one carrying a glob travel verbatim:
        # the shim uses "$@" and the client is exec'd, so nothing re-splits or
        # re-globs them. The tail is asserted whole rather than by membership —
        # word splitting would leave "--foo" and "bar" as two entries, and a
        # membership check on "*.js" would pass on a shell that globbed it into
        # nothing.
        assert argv[-4:] == ["npm", "ci", "--foo bar", "*.js"]


class TestTheCopiedFiles:
    def test_the_client_and_the_protocol_module_are_byte_identical(self, tmp_path):
        """Two files, not one. A single standalone script would put the wire
        format in three places — module, vendored container copy, client — with
        `scripts/sync-devbox-lib.sh` covering only one of them."""
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        source_dir = Path(istota_config.__file__).resolve().parent
        for name, source in (
            ("devbox-exec", "devbox_exec_client.py"),
            ("devbox_exec_protocol.py", "devbox_exec_protocol.py"),
        ):
            copied = (user_temp / ".developer" / name).read_text()
            assert copied == (source_dir / source).read_text(), f"{name} drifted"

    def test_the_protocol_module_keeps_its_import_name(self, tmp_path):
        """The client resolves it as the file beside itself
        (`import devbox_exec_protocol`), so the filename is load-bearing rather
        than cosmetic."""
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        assert (user_temp / ".developer" / "devbox_exec_protocol.py").is_file()

    def test_the_client_is_executable(self, tmp_path):
        config = _make_config(tmp_path)

        _, user_temp = _run_hook(config, tmp_path)

        mode = (user_temp / ".developer" / "devbox-exec").stat().st_mode
        assert mode & stat.S_IXUSR


class TestItContactsNothing:
    def test_no_socket_is_opened(self, tmp_path, monkeypatch):
        """`setup_env` runs for every skill on every task, so a round trip here
        would sit in front of every Talk reply, every briefing, every cron row
        and every heartbeat tick — and a devbox outage would become a failed
        briefing. That is the ISSUE-288 shape, and it is why Design 10 deleted
        the setup-time ping in favour of a `doctor` check."""
        import socket as socket_module

        opened: list = []
        real_socket = socket_module.socket

        class _Watched(real_socket):  # type: ignore[misc, valid-type]
            def connect(self, address):  # noqa: ANN001
                opened.append(address)
                raise AssertionError(f"setup_env opened a socket to {address}")

        monkeypatch.setattr(socket_module, "socket", _Watched)
        config = _make_config(tmp_path)

        _run_hook(config, tmp_path)

        assert not opened

    def test_a_task_with_no_user_writes_no_shims(self, tmp_path):
        """The heartbeat builds a task with no user id. There is no per-user
        socket to name, so there is nothing honest to write."""
        config = _make_config(tmp_path)
        user_temp = tmp_path / "temp" / "none"
        user_temp.mkdir(parents=True)
        ctx = _Ctx(config, user_temp, user_id="")

        setup_env(ctx)

        assert not _shim_dir(user_temp).exists()
