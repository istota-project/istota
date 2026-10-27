"""The tmux brain and the CLI's credential (the model-catalogue spec, Stage 3).

`_tmux` used to start its server with the daemon's whole environment, which
became the global environment of every pane, and put the OAuth token on tmux's
argv with `-e`. Now every tmux call goes through one argv prefix on a private
socket with an emptied server environment, the pane gets the task env minus
every model credential, the CLI reads the subscription token from the bound
`.credentials.json`, and under "api_key" from an `apiKeyHelper` whose key file
lives in a per-attempt directory only the CLAUDE wrap binds.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from istota.brain._types import BrainRequest
from istota.brain import tmux_claude
from istota.brain.tmux_claude import TmuxClaudeBrain
from tests.support.drift import source_of

TOKEN = "OAUTH-SENTINEL"
KEY = "APIKEY-SENTINEL"
AMBIENT = "AMBIENT-SENTINEL"


def _req(tmp_path, **kw) -> BrainRequest:
    base = dict(prompt="hi", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60)
    base.update(kw)
    return BrainRequest(**base)


class TestOneWayToCallTmux:
    def test_every_tmux_argv_is_built_by_the_prefix(self):
        """`_load_buffer` once called `tmux` directly and would have loaded the
        prompt into the default server while the session ran on the private
        one."""
        import re

        text = source_of(tmux_claude)
        # An argv opens with the binary's name inside a list or tuple literal;
        # `shutil.which("tmux")` is the one other mention and builds none.
        argvs = re.findall(r"[\[(]\s*['\"]tmux['\"]\s*,", text)
        assert len(argvs) == 1, "a tmux argv is built outside _tmux_argv"
        assert 'def _tmux_argv' in text

    def test_the_prefix_names_the_private_socket(self):
        argv = tmux_claude._tmux_argv("list-sessions")
        assert argv[:4] == ["tmux", "-u", "-L", tmux_claude.TMUX_SOCKET]
        assert argv[-1] == "list-sessions"


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    """A `tmux` on PATH recording its argv and environment, one JSON line each."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "tmux.log"
    script = bindir / "tmux"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
        "if 'load-buffer' in sys.argv: sys.stdin.read()\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AMBIENT_TOKEN", AMBIENT)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


class TestTheServerAndThePane:
    def test_the_server_starts_with_an_emptied_environment(self, fake_tmux):
        brain = TmuxClaudeBrain()
        brain._new_session("s1", {"PATH": "/usr/bin", "ISTOTA_TASK_ID": "7"})
        brain._load_buffer("b1", "the prompt")
        for call in fake_tmux():
            assert call["argv"][:3] == ["-u", "-L", tmux_claude.TMUX_SOCKET]
            assert AMBIENT not in call["env"].values()
            assert TOKEN not in call["env"].values()
            assert set(call["env"]) <= {
                "PATH", "HOME", "SHELL", "PWD", "SHLVL", "_", "LC_CTYPE", "__CF_USER_TEXT_ENCODING",
            }

    def test_the_pane_shell_is_named_not_taken_from_the_account(self, monkeypatch):
        """The image's uid 10001 has /usr/sbin/nologin as its shell and the
        daemon has no SHELL, so a server started without one ran every pane
        through nologin: the session closed at once and the brain timed out
        waiting for a REPL that was never launched (the vm-tier probe)."""
        monkeypatch.setenv("SHELL", "/usr/sbin/nologin")
        assert tmux_claude._tmux_server_env()["SHELL"] == "/bin/sh"
        monkeypatch.delenv("SHELL")
        assert tmux_claude._tmux_server_env()["SHELL"] == "/bin/sh"

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    def test_no_credential_is_on_tmuxs_argv(self, fake_tmux, mode):
        env = {
            "PATH": "/usr/bin", "ISTOTA_CLI_AUTH": mode,
            "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": KEY,
            "ANTHROPIC_BASE_URL": "https://gateway.example",
            "ISTOTA_PROVIDER_ANTHROPIC_API_KEY": KEY,
        }
        TmuxClaudeBrain()._new_session("s1", tmux_claude.pane_env(env))
        argv = " ".join(" ".join(c["argv"]) for c in fake_tmux())
        for value in (TOKEN, KEY, "https://gateway.example"):
            assert value not in argv
        assert "ISTOTA_CLI_AUTH=" + mode in argv

    def test_the_pane_env_drops_every_model_credential(self):
        env = tmux_claude.pane_env({
            "PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": KEY,
            "ANTHROPIC_BASE_URL": "x", "ISTOTA_PROVIDER_OPENROUTER_API_KEY": KEY,
        })
        assert env == {"PATH": "/bin"}


class _Recorder:
    """Run one session with the tmux boundary stubbed, recording what
    `_new_session` was given and what the key directory held at launch."""

    def __init__(self, monkeypatch, brain):
        self.new_session_env = None
        self.at_launch = {}
        self.settings_path = None
        monkeypatch.setattr(tmux_claude.shutil, "which", lambda _: "/usr/bin/tmux")
        monkeypatch.setattr(brain, "_new_session", self._new_session)
        monkeypatch.setattr(brain, "_launch_claude", self._launch)
        monkeypatch.setattr(brain, "_pane_pid", lambda *a: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a: False)
        monkeypatch.setattr(brain, "_kill", lambda *a: None)

    def _new_session(self, name, env):
        self.new_session_env = dict(env)

    def _launch(self, name, req, cwd):
        self.settings_path = req.cli_settings_path
        if req.cli_key_dir is not None and Path(req.cli_key_dir).is_dir():
            for entry in Path(req.cli_key_dir).iterdir():
                self.at_launch[entry.name] = (
                    entry.read_text(), stat.S_IMODE(entry.lstat().st_mode),
                )


class TestSubscription:
    def test_the_pane_gets_no_token_and_the_config_dir_points_at_the_file(
        self, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        creds = home / ".claude" / ".credentials.json"
        creds.write_text("{}")
        monkeypatch.setenv("HOME", str(home))
        brain = TmuxClaudeBrain()
        rec = _Recorder(monkeypatch, brain)
        links = []
        orig = tmux_claude.TmuxClaudeBrain._link_credentials

        def spy(config_dir, home_dir):
            orig(config_dir, home_dir)
            link = Path(config_dir) / ".credentials.json"
            links.append((link.is_symlink(), os.readlink(link) if link.is_symlink() else None))

        monkeypatch.setattr(tmux_claude.TmuxClaudeBrain, "_link_credentials", staticmethod(spy))
        brain.execute(_req(tmp_path, env={
            "ISTOTA_CLI_AUTH": "subscription", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
            "HOME": str(home),
        }))
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in rec.new_session_env
        assert TOKEN not in rec.new_session_env.values()
        # The CLI reads `$CLAUDE_CONFIG_DIR/.credentials.json` (the Stage 3
        # probe), so the session's config dir points at the bound file.
        assert links == [(True, str(creds))]


class TestApiKey:
    def test_the_helper_reads_a_key_file_in_the_attempts_own_directory(
        self, tmp_path, monkeypatch,
    ):
        keys = tmp_path / "temp" / ".cli-keys" / "alice" / "task_1"
        brain = TmuxClaudeBrain()
        rec = _Recorder(monkeypatch, brain)
        brain.execute(_req(tmp_path, cli_key_dir=keys, env={
            "ISTOTA_CLI_AUTH": "api_key", "ANTHROPIC_API_KEY": KEY,
            "ANTHROPIC_BASE_URL": "https://gateway.example",
        }))
        key_text, key_mode = rec.at_launch["key"]
        assert key_text == KEY and key_mode == 0o600
        settings_text, _mode = rec.at_launch["settings.json"]
        settings = json.loads(settings_text)
        assert KEY not in settings_text
        assert str(keys / "key") in settings["apiKeyHelper"]
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.example"
        # The CLI is pointed at that document, which still carries ours.
        assert rec.settings_path == keys / "settings.json"
        assert settings["attribution"] == {"commit": "", "pr": ""}
        # The pane holds neither the key nor the endpoint.
        assert "ANTHROPIC_API_KEY" not in rec.new_session_env
        assert "ANTHROPIC_BASE_URL" not in rec.new_session_env
        # Removed when the attempt ends, before any reroute.
        assert not keys.exists()

    def test_without_a_key_directory_the_attempt_fails(self, tmp_path, monkeypatch):
        brain = TmuxClaudeBrain()
        _Recorder(monkeypatch, brain)
        res = brain.execute(_req(tmp_path, env={
            "ISTOTA_CLI_AUTH": "api_key", "ANTHROPIC_API_KEY": KEY,
        }))
        assert res.success is False
        assert res.stop_reason == "error"
        assert KEY not in res.result_text


class TestTheKeyDirectory:
    def _config(self, tmp_path, mode):
        from istota.config import Config
        from istota.llm.providers import resolve_process_auth

        config = Config(temp_dir=tmp_path / "temp")
        config.brain.claude_code.auth = mode
        resolve_process_auth(config, {})
        return config

    def test_it_is_a_sibling_of_the_per_user_temp_dirs(self, tmp_path, make_task):
        from istota.executor import cli_key_dir_for_task

        path = cli_key_dir_for_task(self._config(tmp_path, "api_key"), make_task(id=7, user_id="alice"))
        assert path == tmp_path / "temp" / ".cli-keys" / "alice" / "task_7"

    def test_none_outside_api_key(self, tmp_path, make_task):
        from istota.executor import cli_key_dir_for_task

        assert cli_key_dir_for_task(self._config(tmp_path, "subscription"), make_task(id=7, user_id="alice")) is None

    @pytest.mark.parametrize("user_id", ["..", "", ".cli-keys", "a/b"])
    def test_an_unscopable_user_gets_none(self, tmp_path, make_task, user_id):
        from istota.executor import cli_key_dir_for_task

        task = make_task(id=7, user_id="alice")
        task.user_id = user_id
        assert cli_key_dir_for_task(self._config(tmp_path, "api_key"), task) is None


def test_the_fake_tmux_records(fake_tmux):
    """Positive control for the recorder the server tests read."""
    subprocess.run(["tmux", "-V"], check=True)
    assert fake_tmux()[0]["argv"] == ["-V"]
